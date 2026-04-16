#!/usr/bin/env python3
"""Fetch a Google Doc and export it as markdown.

Lightweight read utility for skills that need to fetch Google Docs
refinement documents linked from RHAISTRAT tickets. Outputs JSON to
stdout or writes a companion artifact file.

Usage:
    python3 scripts/fetch_gdoc.py <url_or_doc_id> [--format markdown|plain|html]

    # Fetch and write companion artifact file
    python3 scripts/fetch_gdoc.py <url_or_doc_id> --fetch-all artifacts --strat-key RHAISTRAT-400

    # Export all tabs (not just the first) via .docx conversion
    python3 scripts/fetch_gdoc.py <url_or_doc_id> --via-docx
    python3 scripts/fetch_gdoc.py <url_or_doc_id> --via-docx --fetch-all artifacts --strat-key RHAISTRAT-400

Environment variables (Option A — OAuth refresh token):
    GOOGLE_CLIENT_ID      OAuth 2.0 client ID
    GOOGLE_CLIENT_SECRET  OAuth 2.0 client secret
    GOOGLE_REFRESH_TOKEN  OAuth 2.0 refresh token

Environment variables (Option B — ADC fallback):
    Uses ~/.config/gcloud/application_default_credentials.json
    from `gcloud auth application-default login`

Exit codes:
    0  Success
    1  API/network/script error
    2  Missing Google credentials (caller should try alternative)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from gdoc_utils import (
    MIME_TYPES,
    export_doc,
    export_doc_via_docx,
    extract_doc_id,
    get_access_token,
    get_doc_metadata,
    resolve_credentials,
)


def _build_source_url(doc_id):
    """Build a canonical Google Docs URL from a document ID."""
    return f"https://docs.google.com/document/d/{doc_id}/edit"


def _export_content(access_token, doc_id, fmt, via_docx):
    """Export document content using the appropriate method.

    Returns the exported content as a string.
    """
    if via_docx:
        return export_doc_via_docx(access_token, doc_id)
    mime_type = MIME_TYPES.get(fmt, "text/markdown")
    return export_doc(access_token, doc_id, mime_type)


def _fetch_and_print(url_or_id, fmt, access_token, via_docx=False):
    """Default mode: export doc and print JSON to stdout.

    Returns 0 on success, 1 on error.
    """
    try:
        doc_id = extract_doc_id(url_or_id)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    try:
        metadata = get_doc_metadata(access_token, doc_id)
        content = _export_content(access_token, doc_id, fmt, via_docx)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    output = {
        "doc_id": doc_id,
        "title": metadata.get("name", ""),
        "content": content,
        "format": "markdown" if via_docx else fmt,
        "source_url": _build_source_url(doc_id),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "method": "docx+markitdown" if via_docx else "drive-export",
    }
    json.dump(output, sys.stdout, indent=2)
    print()
    return 0


def _fetch_all(url_or_id, artifacts_dir, strat_key, fmt, access_token,
               via_docx=False):
    """Write-to-disk mode: export doc and write companion artifact file.

    Writes to artifacts/strat-tasks/{strat_key}-refinement-doc.md.
    Returns 0 on success, 1 on error.
    """
    try:
        doc_id = extract_doc_id(url_or_id)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    try:
        metadata = get_doc_metadata(access_token, doc_id)
        content = _export_content(access_token, doc_id, fmt, via_docx)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    tasks_dir = os.path.join(artifacts_dir, "strat-tasks")
    os.makedirs(tasks_dir, exist_ok=True)

    filename = strat_key if strat_key else doc_id
    out_path = os.path.join(tasks_dir, f"{filename}-refinement-doc.md")
    source_url = _build_source_url(doc_id)
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    title = metadata.get("name", "")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"<!-- Refinement document fetched from Google Docs -->\n")
        f.write(f"<!-- Source: {source_url} -->\n")
        f.write(f"<!-- Fetched: {fetched_at} -->\n")
        f.write(f"<!-- Title: {title} -->\n\n")
        f.write(content)
        if not content.endswith("\n"):
            f.write("\n")

    print(f"OK: wrote {out_path}")
    return 0


def _get_authenticated_token():
    """Resolve credentials and obtain an access token.

    Returns the access token string, or calls sys.exit(2) if no
    credentials are available.
    """
    client_id, client_secret, refresh_token = resolve_credentials()
    if not all([client_id, client_secret, refresh_token]):
        print(
            "Error: Google credentials not found.\n"
            "\n"
            "Set these environment variables:\n"
            "  GOOGLE_CLIENT_ID\n"
            "  GOOGLE_CLIENT_SECRET\n"
            "  GOOGLE_REFRESH_TOKEN\n"
            "\n"
            "Or run: gcloud auth application-default login",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        return get_access_token(client_id, client_secret, refresh_token)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "doc_url_or_id",
        help="Google Doc URL or bare document ID",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "plain", "html"],
        default="markdown",
        dest="fmt",
        help="Export format (default: markdown)",
    )
    parser.add_argument(
        "--fetch-all",
        metavar="ARTIFACTS_DIR",
        help="Export document and write companion artifact file to "
             "the given artifacts directory.",
    )
    parser.add_argument(
        "--strat-key",
        metavar="RHAISTRAT-NNN",
        help="RHAISTRAT key to use as the output filename prefix. "
             "Required with --fetch-all. If omitted, uses the doc ID.",
    )
    parser.add_argument(
        "--via-docx",
        action="store_true",
        help="Export by downloading as .docx and converting to markdown "
             "with markitdown. Captures all tabs (not just the first). "
             "Requires: pip install markitdown",
    )
    args = parser.parse_args()

    access_token = _get_authenticated_token()

    if args.fetch_all:
        rc = _fetch_all(
            args.doc_url_or_id, args.fetch_all, args.strat_key,
            args.fmt, access_token, via_docx=args.via_docx,
        )
    else:
        rc = _fetch_and_print(
            args.doc_url_or_id, args.fmt, access_token,
            via_docx=args.via_docx,
        )

    sys.exit(rc)


if __name__ == "__main__":
    main()
