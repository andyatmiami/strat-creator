#!/usr/bin/env python3
"""Gather HOW context sources for strategy refinement.

Checks for available input sources and outputs a structured JSON
summary of what's available for each strategy being refined.

Sources checked:
    1. PR refinement document (highest priority)
    2. Removed implementation context from RFE comments

Usage:
    python3 scripts/gather_refine_inputs.py RHAISTRAT-1182 \\
        --source-rfe RHAIRFE-1115 \\
        [--artifacts-dir artifacts]

Output (JSON to stdout):
    {
        "strat_id": "RHAISTRAT-1182",
        "source_rfe": "RHAIRFE-1115",
        "pr_refinement": {
            "available": true,
            "path": "artifacts/strat-refinements/RHAISTRAT-1182-refinement.md"
        },
        "removed_context": {
            "available": true,
            "path": "artifacts/strat-originals/RHAIRFE-1115-removed-context.md",
            "comments_path": "artifacts/strat-originals/RHAIRFE-1115-comments.md"
        }
    }
"""

import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))


_RFE_CREATOR_MARKER = (
    "[RFE Creator]"
)
_REMOVED_CONTEXT_MARKER = (
    "The following technical implementation details were removed "
    "from the RFE description during review."
)


def _check_pr_refinement(strat_id, artifacts_dir):
    """Check for a fetched PR refinement document."""
    path = os.path.join(
        artifacts_dir, "strat-refinements", f"{strat_id}-refinement.md"
    )
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return {"available": True, "path": path}
    return {"available": False, "path": path}


def _fetch_and_parse_comments(source_rfe, artifacts_dir):
    """Fetch RFE comments from Jira and extract removed context."""
    originals_dir = os.path.join(artifacts_dir, "strat-originals")
    os.makedirs(originals_dir, exist_ok=True)
    comments_path = os.path.join(originals_dir, f"{source_rfe}-comments.md")
    removed_path = os.path.join(originals_dir, f"{source_rfe}-removed-context.md")

    # Try to fetch fresh comments from Jira
    jira_available = all(
        os.environ.get(v) for v in ("JIRA_SERVER", "JIRA_USER", "JIRA_TOKEN")
    )

    raw_comments = None
    if jira_available:
        result = subprocess.run(
            ["python3", "scripts/fetch_issue.py", source_rfe,
             "--fields", "comment", "--markdown"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            try:
                data = json.loads(result.stdout)
                raw_comments = data.get("comment", [])
            except json.JSONDecodeError:
                pass

    if raw_comments is not None:
        # Write comments file
        lines = [f"# Comments: {source_rfe}\n"]
        for comment in raw_comments:
            author = comment.get("author", "Unknown")
            date = comment.get("created", "")[:10]
            body = comment.get("body", "")
            lines.append(f"\n## {author} — {date}\n")
            lines.append(f"\n{body}\n")
        with open(comments_path, "w") as f:
            f.write("\n".join(lines))
    elif not os.path.exists(comments_path):
        return {
            "available": False,
            "path": removed_path,
            "comments_path": comments_path,
        }

    # Scan for removed context marker
    try:
        comments_text = open(comments_path).read()
    except FileNotFoundError:
        return {
            "available": False,
            "path": removed_path,
            "comments_path": comments_path,
        }

    marker_idx = comments_text.find(_RFE_CREATOR_MARKER)
    if marker_idx == -1:
        return {
            "available": False,
            "path": removed_path,
            "comments_path": comments_path,
        }

    detail_idx = comments_text.find(_REMOVED_CONTEXT_MARKER, marker_idx)
    if detail_idx == -1:
        return {
            "available": False,
            "path": removed_path,
            "comments_path": comments_path,
        }

    # Extract everything after the marker line
    after_marker = comments_text[detail_idx + len(_REMOVED_CONTEXT_MARKER):]
    # Find the content — skip past any blank lines after the marker sentence
    content = after_marker.lstrip("\n").strip()

    if content:
        with open(removed_path, "w") as f:
            f.write(content + "\n")
        return {
            "available": True,
            "path": removed_path,
            "comments_path": comments_path,
        }

    return {
        "available": False,
        "path": removed_path,
        "comments_path": comments_path,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Gather HOW context sources for strategy refinement.",
    )
    parser.add_argument("strat_id", help="Strategy ID (e.g. RHAISTRAT-1182)")
    parser.add_argument(
        "--source-rfe", required=True,
        help="Source RFE key (e.g. RHAIRFE-1115)",
    )
    parser.add_argument(
        "--artifacts-dir", default="artifacts",
        help="Artifacts directory (default: artifacts)",
    )
    args = parser.parse_args()

    result = {
        "strat_id": args.strat_id,
        "source_rfe": args.source_rfe,
        "pr_refinement": _check_pr_refinement(
            args.strat_id, args.artifacts_dir
        ),
        "removed_context": _fetch_and_parse_comments(
            args.source_rfe, args.artifacts_dir
        ),
    }

    # Log to stderr
    pr = result["pr_refinement"]
    rc = result["removed_context"]
    if pr["available"]:
        print(f"[INFO] Using PR refinement document for {args.strat_id}",
              file=sys.stderr)
    if rc["available"]:
        print(f"[INFO] Found removed implementation context for {args.source_rfe}",
              file=sys.stderr)
    if not pr["available"] and not rc["available"]:
        print(f"[INFO] No HOW context sources found for {args.strat_id}",
              file=sys.stderr)

    json.dump(result, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
