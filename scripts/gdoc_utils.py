"""Shared Google Docs API utilities for fetching and exporting documents.

Uses only stdlib (urllib) — no external dependencies required. Authenticates
via OAuth 2.0 refresh token flow using environment variables or Application
Default Credentials (ADC) as fallback.

Environment variables (Option A — explicit OAuth):
    GOOGLE_CLIENT_ID      OAuth 2.0 client ID
    GOOGLE_CLIENT_SECRET  OAuth 2.0 client secret
    GOOGLE_REFRESH_TOKEN  OAuth 2.0 refresh token

Environment variables (Option B — ADC fallback):
    Uses ~/.config/gcloud/application_default_credentials.json
    from `gcloud auth application-default login`
"""

import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request


# ─── Authentication ──────────────────────────────────────────────────────────

def require_google_env():
    """Read Google OAuth credentials from environment variables.

    Returns (client_id, client_secret, refresh_token) tuple.
    Returns (None, None, None) if any are missing.
    """
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")
    if all([client_id, client_secret, refresh_token]):
        return client_id, client_secret, refresh_token
    return None, None, None


def load_adc():
    """Read Google OAuth credentials from Application Default Credentials.

    Checks GOOGLE_APPLICATION_CREDENTIALS env var first, then falls back to
    the default gcloud ADC path.

    Returns (client_id, client_secret, refresh_token) tuple.
    Returns (None, None, None) if file missing or invalid.
    """
    adc_path = os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS",
        os.path.expanduser(
            "~/.config/gcloud/application_default_credentials.json"
        ),
    )
    try:
        with open(adc_path, "r", encoding="utf-8") as f:
            creds = json.load(f)
        client_id = creds.get("client_id")
        client_secret = creds.get("client_secret")
        refresh_token = creds.get("refresh_token")
        if all([client_id, client_secret, refresh_token]):
            return client_id, client_secret, refresh_token
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return None, None, None


def resolve_credentials():
    """Resolve Google credentials from env vars or ADC.

    Returns (client_id, client_secret, refresh_token) tuple.
    Returns (None, None, None) if no credentials found.
    """
    client_id, client_secret, refresh_token = require_google_env()
    if all([client_id, client_secret, refresh_token]):
        return client_id, client_secret, refresh_token
    return load_adc()


def get_access_token(client_id, client_secret, refresh_token):
    """Exchange a refresh token for a short-lived access token.

    POSTs to https://oauth2.googleapis.com/token.
    Returns the access token string.
    Raises RuntimeError on failure.
    """
    url = "https://oauth2.googleapis.com/token"
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }).encode()

    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data["access_token"]
    except urllib.error.HTTPError as e:
        error_body = e.read().decode(errors="replace")
        raise RuntimeError(
            f"Token refresh failed (HTTP {e.code}): {error_body}"
        ) from e
    except KeyError:
        raise RuntimeError("Token response missing 'access_token' field")


# ─── Document ID Extraction ──────────────────────────────────────────────────

_DOC_ID_FROM_URL = re.compile(r"/d/([-\w]{25,})")
_BARE_DOC_ID = re.compile(r"^[-\w]{25,}$")


def extract_doc_id(url_or_id):
    """Extract a Google Doc ID from a URL or validate a bare ID.

    Accepts:
        https://docs.google.com/document/d/DOC_ID/edit
        https://docs.google.com/document/d/DOC_ID/edit?tab=t.0
        https://docs.google.com/document/d/DOC_ID/edit#heading=h.xyz
        DOC_ID (bare, 25+ alphanumeric/dash/underscore chars)

    Returns the document ID string.
    Raises ValueError if the input is not a valid doc URL or ID.
    """
    match = _DOC_ID_FROM_URL.search(url_or_id)
    if match:
        return match.group(1)
    if _BARE_DOC_ID.match(url_or_id):
        return url_or_id
    raise ValueError(
        f"Cannot extract Google Doc ID from: {url_or_id!r}\n"
        "Expected a Google Docs URL or a bare document ID (25+ characters)."
    )


# ─── Google Drive API ────────────────────────────────────────────────────────

MIME_TYPES = {
    "markdown": "text/markdown",
    "plain": "text/plain",
    "html": "text/html",
}


def _drive_request(access_token, path, accept="application/json"):
    """Make a GET request to the Google Drive API v3.

    Returns the response body as bytes.
    Raises urllib.error.HTTPError on failure.
    """
    url = f"https://www.googleapis.com/drive/v3{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {access_token}",
        "Accept": accept,
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _get_doc_metadata_via_docs_api(access_token, doc_id):
    """Fetch document title via the Google Docs API.

    Fallback when the Drive API files.get endpoint is blocked by
    Workspace policy. Returns {"name": "..."}.
    Raises urllib.error.HTTPError on failure.
    """
    url = (f"https://docs.googleapis.com/v1/documents/"
           f"{urllib.parse.quote(doc_id)}?fields=title")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {access_token}",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
        return {"name": data.get("title", "")}


def get_doc_metadata(access_token, doc_id):
    """Fetch document metadata (title, last modified time).

    Tries the Drive API first, falls back to the Google Docs API if
    the Drive files.get endpoint returns 403/404 (common when Workspace
    admin policies block third-party Drive API access).

    Returns {"name": "...", ...}.
    Raises on HTTP error.
    """
    path = (f"/files/{urllib.parse.quote(doc_id)}"
            "?fields=name,modifiedTime")
    try:
        data = _drive_request(access_token, path)
        return json.loads(data)
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            try:
                return _get_doc_metadata_via_docs_api(access_token, doc_id)
            except urllib.error.HTTPError:
                pass
        _handle_drive_error(e, doc_id, "fetch metadata")
        raise


def export_doc(access_token, doc_id, mime_type="text/markdown"):
    """Export a Google Doc in the specified format.

    Args:
        access_token: OAuth access token
        doc_id: Google Doc document ID
        mime_type: Export MIME type (default: text/markdown)

    Returns the exported content as a string.
    Raises on HTTP error.
    """
    path = (f"/files/{urllib.parse.quote(doc_id)}/export"
            f"?mimeType={urllib.parse.quote(mime_type)}")
    try:
        data = _drive_request(access_token, path, accept=mime_type)
        return data.decode("utf-8")
    except urllib.error.HTTPError as e:
        _handle_drive_error(e, doc_id, "export")
        raise


def export_doc_via_docx(access_token, doc_id):
    """Export a Google Doc by downloading as .docx and converting to markdown.

    This approach exports all tabs (not just the first), unlike the direct
    Drive markdown export. Requires the `markitdown` package.

    Args:
        access_token: OAuth access token
        doc_id: Google Doc document ID

    Returns the exported content as a markdown string.
    Raises on HTTP error or if markitdown is not installed.
    """
    try:
        from markitdown import MarkItDown
    except ImportError:
        raise RuntimeError(
            "markitdown package is required for --via-docx export. "
            "Install it with: pip install markitdown"
        )

    docx_mime = ("application/vnd.openxmlformats-officedocument"
                 ".wordprocessingml.document")
    path = (f"/files/{urllib.parse.quote(doc_id)}/export"
            f"?mimeType={urllib.parse.quote(docx_mime)}")
    try:
        data = _drive_request(access_token, path, accept=docx_mime)
    except urllib.error.HTTPError as e:
        _handle_drive_error(e, doc_id, "export as docx")
        raise

    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:
        md = MarkItDown()
        result = md.convert(tmp_path)
        return result.text_content
    finally:
        os.unlink(tmp_path)


def _handle_drive_error(error, doc_id, operation):
    """Print a human-readable error message for Drive API failures."""
    code = error.code
    if code == 404:
        print(f"Error: Document not found: {doc_id}", file=sys.stderr)
    elif code == 403:
        body = error.read().decode(errors="replace")
        if "exportSizeLimitExceeded" in body:
            print(
                f"Error: Document too large for export (>10MB): {doc_id}",
                file=sys.stderr,
            )
        else:
            print(
                f"Error: Access denied for document {doc_id}. "
                "Ensure the document is shared with the authenticated account.",
                file=sys.stderr,
            )
    elif code == 401:
        print(
            f"Error: Authentication failed during {operation}. "
            "Access token may be expired or invalid.",
            file=sys.stderr,
        )
    else:
        print(
            f"Error: Drive API {operation} failed (HTTP {code}) "
            f"for document {doc_id}",
            file=sys.stderr,
        )
