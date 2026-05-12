#!/usr/bin/env python3
"""Compose a Jira review comment from a strategy review file.

Reads the review file frontmatter and scores table, then outputs
a formatted markdown comment suitable for posting to Jira.

Usage:
    python3 scripts/compose_review_comment.py \\
        artifacts/strat-reviews/RHAISTRAT-1182-review.md \\
        --output /tmp/strat-review-comment-RHAISTRAT-1182.md

Output:
    Writes the composed markdown comment to the output file.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from artifact_utils import read_frontmatter_validated


_ACTION_TEXT = {
    "approve": (
        "No action needed — strategy passed quality review."
    ),
    "revise": (
        "Edit the strategy to address flagged issues, then remove the "
        "`needs-attention` label. The pipeline will re-evaluate automatically."
    ),
    "reject": (
        "This strategy has fundamental problems. Consider revisiting the "
        "source RFE or re-running `/strategy.refine` with different constraints."
    ),
}


def _score_status(score):
    if score >= 2:
        return "✓"
    if score == 1:
        return "⚠"
    return "✗"


def _parse_score_notes(review_body):
    """Extract per-dimension notes from the ``## Scores`` table."""
    notes = {}
    in_table = False
    for line in review_body.split("\n"):
        if line.strip().startswith("## Scores"):
            in_table = True
            continue
        if in_table and line.startswith("## "):
            break
        if not in_table or not line.startswith("|"):
            continue
        if line.startswith("|--") or line.startswith("| --"):
            continue
        cells = [c.strip() for c in line.split("|")]
        cells = [c for c in cells if c]
        if len(cells) < 3:
            continue
        label = cells[0].strip("*").strip()
        if label in ("Criterion", "Total"):
            continue
        notes[label.lower()] = cells[2]
    return notes


def compose(review_file, output):
    review_fm, review_body = read_frontmatter_validated(
        review_file, "strat-review"
    )

    strat_id = review_fm["strat_id"]
    recommendation = review_fm["recommendation"]
    scores = review_fm.get("scores", {})
    total = scores.get("total", 0)
    score_notes = _parse_score_notes(review_body)

    lines = [
        f"*[Strat Creator]* Strategy Review — "
        f"{recommendation.upper()} (Score: {total}/8)",
        "",
        "| Criterion | Score | Status |",
        "|-----------|-------|--------|",
    ]

    for dim in ("feasibility", "testability", "scope", "architecture"):
        s = scores.get(dim, 0)
        lines.append(f"| {dim.title()} | {s}/2 | {_score_status(s)} |")

    lines.append("")

    for dim in ("feasibility", "testability", "scope", "architecture"):
        s = scores.get(dim, 0)
        if s < 2 and dim in score_notes:
            lines.append(f"**{dim.title()}:** {score_notes[dim]}")
            lines.append("")

    action = _ACTION_TEXT.get(recommendation, "")
    lines.append(f"**Action:** {action}")

    comment_md = "\n".join(lines) + "\n"

    out_dir = os.path.dirname(output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(output, "w") as f:
        f.write(comment_md)

    print(f"OK: {output}")


def main():
    parser = argparse.ArgumentParser(
        description="Compose a Jira review comment from a review file.",
    )
    parser.add_argument("review_file", help="Path to the review file")
    parser.add_argument(
        "--output", required=True, help="Output path for the comment markdown"
    )
    args = parser.parse_args()
    compose(args.review_file, args.output)


if __name__ == "__main__":
    main()
