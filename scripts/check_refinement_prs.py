#!/usr/bin/env python3
"""Check GitHub refinement PRs for staff engineer feedback.

Scans strategy task files for open refinement PRs, checks their status,
fetches ready documents, and prints a structured summary.

Usage:
    python3 scripts/check_refinement_prs.py [--artifacts-dir artifacts]

Output (JSON to stdout):
    {
        "ready": [{"strat_id": "...", "title": "...", "pr_url": "...", "jira_key": "..."}],
        "waiting": [...],
        "closed": [...]
    }

Side effects:
    - Writes fetched refinement documents to artifacts/strat-refinements/
    - Clears refinement_pr_url on closed PRs
    - Prints human-readable status lines to stderr
"""

import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from artifact_utils import read_frontmatter_validated, update_frontmatter
from gh_utils import (
    require_gh_env,
    get_pr_status,
    get_pr_file_content,
    pr_number_from_url,
    file_path_for_strat,
)


def _find_strategies_with_prs(artifacts_dir):
    """Find strategies that have refinement_pr_url set and needs_attention=true."""
    candidates = []
    task_dir = os.path.join(artifacts_dir, "strat-tasks")
    review_dir = os.path.join(artifacts_dir, "strat-reviews")

    for pattern in ["STRAT-*.md", "RHAISTRAT-*.md"]:
        for path in sorted(glob.glob(os.path.join(task_dir, pattern))):
            task_fm, _ = read_frontmatter_validated(path, "strat-task")
            pr_url = task_fm.get("refinement_pr_url")
            if not pr_url:
                continue

            strat_id = task_fm["strat_id"]
            review_path = os.path.join(review_dir, f"{strat_id}-review.md")
            if not os.path.exists(review_path):
                continue

            review_fm, _ = read_frontmatter_validated(review_path, "strat-review")
            if not review_fm.get("needs_attention"):
                continue

            candidates.append({
                "strat_id": strat_id,
                "title": task_fm.get("title", ""),
                "pr_url": pr_url,
                "jira_key": task_fm.get("jira_key"),
                "task_path": path,
            })

    return candidates


def _check_and_fetch(repo, candidates, artifacts_dir):
    """Check PR status and fetch ready documents."""
    refinements_dir = os.path.join(artifacts_dir, "strat-refinements")
    os.makedirs(refinements_dir, exist_ok=True)

    results = {"ready": [], "waiting": [], "closed": []}

    for candidate in candidates:
        strat_id = candidate["strat_id"]
        pr_url = candidate["pr_url"]
        pr_num = pr_number_from_url(pr_url)

        if not pr_num:
            print(
                f"[ERROR] {strat_id}: Invalid PR URL — {pr_url}",
                file=sys.stderr,
            )
            continue

        status = get_pr_status(repo, pr_num)
        if not status:
            print(
                f"[ERROR] {strat_id}: Could not fetch PR status — {pr_url}",
                file=sys.stderr,
            )
            continue

        entry = {
            "strat_id": strat_id,
            "title": candidate["title"],
            "pr_url": pr_url,
            "jira_key": candidate["jira_key"],
        }

        state = status.get("state", "")
        is_draft = status.get("isDraft", False)

        if state == "CLOSED" and state != "MERGED":
            print(
                f"[CLOSED] {strat_id}: PR was closed without merge — {pr_url}",
                file=sys.stderr,
            )
            update_frontmatter(
                candidate["task_path"],
                {"refinement_pr_url": None},
                "strat-task",
            )
            stale_path = os.path.join(
                refinements_dir, f"{strat_id}-refinement.md"
            )
            if os.path.exists(stale_path):
                os.remove(stale_path)
                print(
                    f"[CLEANUP] {strat_id}: Removed stale refinement doc "
                    f"(PR closed)",
                    file=sys.stderr,
                )
            results["closed"].append(entry)
            continue

        if is_draft:
            print(
                f"[WAITING] {strat_id}: PR still in draft — {pr_url}",
                file=sys.stderr,
            )
            stale_path = os.path.join(
                refinements_dir, f"{strat_id}-refinement.md"
            )
            if os.path.exists(stale_path):
                os.remove(stale_path)
                print(
                    f"[CLEANUP] {strat_id}: Removed stale refinement doc "
                    f"(PR still in draft)",
                    file=sys.stderr,
                )
            results["waiting"].append(entry)
            continue

        # Ready for review or merged
        file_path = file_path_for_strat(strat_id)
        content = get_pr_file_content(repo, pr_num, file_path)
        if not content:
            print(
                f"[ERROR] {strat_id}: Could not fetch refinement document "
                f"from {file_path}",
                file=sys.stderr,
            )
            continue

        out_path = os.path.join(
            refinements_dir, f"{strat_id}-refinement.md"
        )
        with open(out_path, "w") as f:
            f.write(content)

        print(
            f"[FETCHED] {strat_id}: Refinement document saved to {out_path}",
            file=sys.stderr,
        )
        results["ready"].append(entry)

    return results


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Check refinement PRs for staff engineer feedback.",
    )
    parser.add_argument(
        "--artifacts-dir",
        default="artifacts",
        help="Artifacts directory (default: artifacts)",
    )
    args = parser.parse_args()

    repo = require_gh_env()
    candidates = _find_strategies_with_prs(args.artifacts_dir)

    if not candidates:
        print("No strategies with open refinement PRs found.", file=sys.stderr)
        json.dump({"ready": [], "waiting": [], "closed": []}, sys.stdout)
        print()
        sys.exit(0)

    results = _check_and_fetch(repo, candidates, args.artifacts_dir)

    # Print summary to stderr
    print("\n=== Refinement PR Check Summary ===", file=sys.stderr)
    if results["ready"]:
        print("  Ready for re-refinement:", file=sys.stderr)
        for e in results["ready"]:
            print(f"    - {e['strat_id']}: {e['title']} (PR: {e['pr_url']})",
                  file=sys.stderr)
    if results["waiting"]:
        print("  Still waiting:", file=sys.stderr)
        for e in results["waiting"]:
            print(f"    - {e['strat_id']}: {e['title']} (PR: {e['pr_url']})",
                  file=sys.stderr)
    if results["closed"]:
        print("  Closed (no action):", file=sys.stderr)
        for e in results["closed"]:
            print(f"    - {e['strat_id']}: {e['title']} (PR: {e['pr_url']})",
                  file=sys.stderr)

    # Structured output to stdout
    json.dump(results, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
