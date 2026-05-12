#!/usr/bin/env python3
"""Create or update a refinement PR and post process gates review.

Handles the full PR lifecycle: check for existing PR, create branch
and draft PR (or update existing), and post process gates as a PR
review comment.

Usage:
    python3 scripts/create_refinement_pr.py RHAISTRAT-1182 \\
        --doc /tmp/strat-refinement-RHAISTRAT-1182.md \\
        --body /tmp/strat-refinement-RHAISTRAT-1182-body.md \\
        [--gates /tmp/strat-refinement-RHAISTRAT-1182-gates.md]

Output (stdout):
    The PR URL (new or existing).

Exit codes:
    0  Success
    1  Error
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from gh_utils import (
    require_gh_env,
    branch_name_for_strat,
    file_path_for_strat,
    create_branch,
    create_or_update_file,
    create_draft_pr,
    create_pr_review,
    convert_pr_to_draft,
    find_pr_by_branch,
    pr_number_from_url,
)


def main():
    parser = argparse.ArgumentParser(
        description="Create or update a refinement PR.",
    )
    parser.add_argument("strat_id", help="Strategy ID (e.g. RHAISTRAT-1182)")
    parser.add_argument(
        "--doc", required=True, help="Path to the strategy document to commit"
    )
    parser.add_argument(
        "--body", required=True, help="Path to the PR description body"
    )
    parser.add_argument(
        "--gates", default=None, help="Path to process gates content (optional)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without making changes",
    )
    args = parser.parse_args()

    repo = require_gh_env()
    strat_id = args.strat_id
    branch = branch_name_for_strat(strat_id)
    file_path = file_path_for_strat(strat_id)

    doc_content = open(args.doc).read()
    body_content = open(args.body).read()
    gates_content = open(args.gates).read() if args.gates and os.path.exists(args.gates) else None

    # Check for existing PR
    existing_pr = find_pr_by_branch(repo, branch)

    if existing_pr:
        pr_url = existing_pr["url"]
        pr_num = existing_pr["number"]
        print(
            f"[UPDATE] Updating existing PR #{pr_num} on branch {branch}",
            file=sys.stderr,
        )
        ok = create_or_update_file(
            repo, branch, file_path, doc_content,
            f"Update refinement doc for {strat_id}",
            dry_run=args.dry_run,
        )
        if not ok:
            print("ERROR: Failed to update file on branch", file=sys.stderr)
            sys.exit(1)

        if not existing_pr.get("isDraft"):
            print(
                f"[DRAFT] Converting PR #{pr_num} back to draft",
                file=sys.stderr,
            )
            convert_pr_to_draft(repo, pr_num, dry_run=args.dry_run)
    else:
        print(
            f"[CREATE] Creating new branch {branch} and draft PR",
            file=sys.stderr,
        )
        create_branch(repo, branch, dry_run=args.dry_run)
        ok = create_or_update_file(
            repo, branch, file_path, doc_content,
            f"Add refinement doc for {strat_id}",
            dry_run=args.dry_run,
        )
        if not ok:
            print("ERROR: Failed to create file on branch", file=sys.stderr)
            sys.exit(1)

        pr_url = create_draft_pr(
            repo, branch,
            f"[Refinement] {strat_id}: Strategy needs revision",
            body_content,
            dry_run=args.dry_run,
        )
        if not pr_url:
            print("ERROR: Failed to create draft PR", file=sys.stderr)
            sys.exit(1)

        pr_num = pr_number_from_url(pr_url)

    # Post process gates as PR review
    if gates_content and pr_num:
        print(
            f"[REVIEW] Posting process gates as PR review on #{pr_num}",
            file=sys.stderr,
        )
        create_pr_review(repo, pr_num, gates_content, dry_run=args.dry_run)

    # Output the PR URL
    print(pr_url)


if __name__ == "__main__":
    main()
