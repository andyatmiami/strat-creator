#!/usr/bin/env python3
"""GitHub CLI wrapper utilities for the strat-creator pipeline.

Wraps ``gh`` CLI calls for creating branches, committing files, and
managing draft PRs in a configurable GitHub repository.  Used by
strategy.review (to create refinement PRs) and strategy.refine
(to poll PR readiness and fetch content).

Environment variables:
    GH_REFINEMENT_REPO  Target repository in owner/name format
                        (e.g. ``ederign/strat-refinements``).
                        Required for all write operations.
"""

import json
import os
import subprocess
import sys


# ─── Environment ────────────────────────────────────────────────────────────

def require_gh_env():
    """Read and validate GH_REFINEMENT_REPO.  Returns the repo string.

    Raises ``SystemExit`` if the variable is not set.
    """
    repo = os.environ.get("GH_REFINEMENT_REPO")
    if not repo:
        print("ERROR: GH_REFINEMENT_REPO env var is not set. "
              "Set it to owner/repo (e.g. ederign/strat-refinements).",
              file=sys.stderr)
        sys.exit(1)
    return repo


def _run_gh(args, check=True):
    """Run a ``gh`` CLI command and return parsed JSON stdout.

    Returns the parsed JSON object, or raw stdout string when the
    output is not valid JSON.
    """
    result = subprocess.run(
        ["gh"] + args,
        capture_output=True, text=True, check=check,
    )
    if result.returncode != 0 and not check:
        return None
    stdout = result.stdout.strip()
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return stdout


# ─── Branch Operations ──────────────────────────────────────────────────────

def branch_name_for_strat(strat_id):
    """Return the canonical branch name for a strategy refinement PR."""
    return f"strat-refinement/{strat_id.lower()}"


def file_path_for_strat(strat_id):
    """Return the path within the repo where the refinement doc lives."""
    folder = strat_id.lower()
    return f"{folder}/{folder}.md"


def create_branch(repo, branch, base="main", dry_run=False):
    """Create a new branch from *base* in *repo*.

    Uses the GitHub API via ``gh api`` to create a git ref.
    Returns True on success, False if the branch already exists.
    """
    if dry_run:
        print(f"[DRY RUN] Would create branch {branch} from {base} "
              f"in {repo}", file=sys.stderr)
        return True

    # Get the SHA of the base branch
    ref_data = _run_gh([
        "api", f"repos/{repo}/git/ref/heads/{base}",
        "--jq", ".object.sha",
    ])
    if not ref_data:
        print(f"ERROR: Could not resolve base branch {base} in {repo}",
              file=sys.stderr)
        return False

    base_sha = ref_data.strip() if isinstance(ref_data, str) else str(ref_data)

    # Create the new ref
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/git/refs",
         "-f", f"ref=refs/heads/{branch}",
         "-f", f"sha={base_sha}"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        if "Reference already exists" in result.stderr:
            return False  # branch already exists — not an error
        print(f"ERROR creating branch: {result.stderr}", file=sys.stderr)
        return False
    return True


# ─── File Operations ────────────────────────────────────────────────────────

def create_or_update_file(repo, branch, path, content, message,
                          dry_run=False):
    """Create or update a file on *branch* via the GitHub Contents API.

    Returns True on success.
    """
    if dry_run:
        print(f"[DRY RUN] Would write {path} on {branch} in {repo}",
              file=sys.stderr)
        return True

    import base64
    encoded = base64.b64encode(content.encode()).decode()

    # Check if file already exists (need its SHA for updates)
    existing = subprocess.run(
        ["gh", "api", f"repos/{repo}/contents/{path}",
         "--jq", ".sha", "-H", "Accept: application/vnd.github.v3+json",
         "--method", "GET",
         "-f", f"ref={branch}"],
        capture_output=True, text=True, check=False,
    )

    args = [
        "gh", "api", f"repos/{repo}/contents/{path}",
        "--method", "PUT",
        "-f", f"message={message}",
        "-f", f"content={encoded}",
        "-f", f"branch={branch}",
    ]

    if existing.returncode == 0 and existing.stdout.strip():
        file_sha = existing.stdout.strip().strip('"')
        args.extend(["-f", f"sha={file_sha}"])

    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(f"ERROR writing file: {result.stderr}", file=sys.stderr)
        return False
    return True


# ─── PR Operations ──────────────────────────────────────────────────────────

def create_draft_pr(repo, branch, title, body, dry_run=False):
    """Create a draft PR in *repo* from *branch*.

    Returns the PR URL on success, or a mock URL in dry-run mode.
    """
    if dry_run:
        print(f"[DRY RUN] Would create draft PR in {repo}: {title}",
              file=sys.stderr)
        return f"https://github.com/{repo}/pull/DRAFT"

    result = subprocess.run(
        ["gh", "pr", "create",
         "--repo", repo,
         "--head", branch,
         "--title", title,
         "--body", body,
         "--draft"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        print(f"ERROR creating PR: {result.stderr}", file=sys.stderr)
        return None

    pr_url = result.stdout.strip()
    return pr_url


def find_pr_by_branch(repo, branch):
    """Find an open PR by head branch name.

    Returns a dict with ``number``, ``isDraft``, ``state``, ``url``
    or None if no open PR exists for that branch.
    """
    data = _run_gh([
        "pr", "list",
        "--repo", repo,
        "--head", branch,
        "--json", "number,isDraft,state,url",
        "--limit", "1",
    ])
    if isinstance(data, list) and data:
        return data[0]
    return None


def get_pr_status(repo, pr_number):
    """Check whether a PR is still a draft or ready for review.

    Returns a dict with ``number``, ``isDraft``, ``state``, ``url``,
    ``headRefName`` or None on error.
    """
    data = _run_gh([
        "pr", "view", str(pr_number),
        "--repo", repo,
        "--json", "number,isDraft,state,url,headRefName",
    ])
    return data if isinstance(data, dict) else None


def get_pr_file_content(repo, pr_number, path):
    """Fetch the content of a file from the PR's head branch.

    Returns the file content as a string, or None on error.
    """
    # First get the head branch name
    pr_data = get_pr_status(repo, pr_number)
    if not pr_data:
        return None
    head_branch = pr_data.get("headRefName")
    if not head_branch:
        return None

    # Fetch file content from that branch
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/contents/{path}",
         "-H", "Accept: application/vnd.github.v3.raw",
         "-f", f"ref={head_branch}"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        print(f"ERROR fetching {path} from {head_branch}: {result.stderr}",
              file=sys.stderr)
        return None
    return result.stdout


def create_pr_review(repo, pr_number, body, dry_run=False):
    """Submit a PR review with event COMMENT.

    Posts a review-level comment (visible in the PR timeline with
    distinct styling, auto-collapsed by GitHub on subsequent reviews).
    Returns True on success.
    """
    if dry_run:
        print(f"[DRY RUN] Would post PR review on {repo}#{pr_number}",
              file=sys.stderr)
        return True

    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/pulls/{pr_number}/reviews",
         "--method", "POST",
         "-f", f"body={body}",
         "-f", "event=COMMENT"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        print(f"ERROR creating PR review: {result.stderr}", file=sys.stderr)
        return False
    return True


def convert_pr_to_draft(repo, pr_number, dry_run=False):
    """Convert a PR back to draft using the GraphQL API.

    Returns True on success.
    """
    if dry_run:
        print(f"[DRY RUN] Would convert {repo}#{pr_number} to draft",
              file=sys.stderr)
        return True

    # Get the PR's node ID
    pr_data = _run_gh([
        "pr", "view", str(pr_number),
        "--repo", repo,
        "--json", "id",
    ])
    if not isinstance(pr_data, dict) or "id" not in pr_data:
        print(f"ERROR: Could not get node ID for PR #{pr_number}",
              file=sys.stderr)
        return False

    node_id = pr_data["id"]
    query = (
        'mutation { convertPullRequestToDraft'
        f'(input: {{pullRequestId: "{node_id}"}}) '
        '{ pullRequest { isDraft } } }'
    )
    result = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        print(f"ERROR converting PR to draft: {result.stderr}",
              file=sys.stderr)
        return False
    return True


def merge_pr(repo, pr_number, dry_run=False):
    """Merge a PR using the merge commit strategy.

    Returns True on success.
    """
    if dry_run:
        print(f"[DRY RUN] Would merge {repo}#{pr_number}",
              file=sys.stderr)
        return True

    result = subprocess.run(
        ["gh", "pr", "merge", str(pr_number),
         "--repo", repo, "--merge"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        print(f"ERROR merging PR: {result.stderr}", file=sys.stderr)
        return False
    return True


def pr_number_from_url(url):
    """Extract the PR number from a GitHub PR URL.

    Handles URLs like ``https://github.com/owner/repo/pull/123``.
    Returns an int, or None if the URL is not a valid PR URL.
    """
    if not url:
        return None
    parts = url.rstrip("/").split("/")
    if len(parts) >= 2 and parts[-2] == "pull":
        try:
            return int(parts[-1])
        except ValueError:
            pass
    return None
