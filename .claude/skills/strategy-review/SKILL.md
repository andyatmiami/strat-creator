---
name: strategy-review
description: Adversarial review of refined strategies. Scores against rubric, then runs independent forked reviewers for detailed prose.
user-invocable: true
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Skill, Agent
---

You are a strategy review orchestrator. Your job is to score and review the strategies in `artifacts/strat-tasks/`, producing per-strategy review files with numeric scores and detailed prose.

## Dry Run Modes

**`--dry-run`** (full dry run): Skip ALL external writes — no Jira, no GitHub.
- Do NOT write or update any Jira issues
- Do NOT post review comments to Jira — save them to `artifacts/strat-reviews/{id}-review-comment.md` instead
- Do NOT create GitHub PRs — save refinement docs to `artifacts/strat-refinements/` instead
- DO still read from Jira and local artifacts (reads are safe)
- DO still create local review files in `artifacts/strat-reviews/`

## Local Mode

Check if strategy files exist in `local/strat-tasks/`. If they do, this is a local human review session pulled via `/strategy-pull`.

In local mode:
- Read strategy files from `local/strat-tasks/` instead of `artifacts/strat-tasks/`
- Read review files from `local/strat-reviews/` instead of `artifacts/strat-reviews/`
- Write review files to `local/strat-reviews/`
- **Skip ALL Jira writes** — no labels, no comments, no attachments posted to Jira
- **Skip the Pipeline Label Gate** (Step 1a) — the strategy was already processed by CI
- DO still run full scoring and prose reviews locally

Local mode is also active if any strategy file's frontmatter contains `workflow: local`.

If both `local/strat-tasks/` and `artifacts/strat-tasks/` have files, prefer `local/strat-tasks/`.

**`--dry-run-jira`** (Jira-only dry run): Skip Jira writes but DO perform GitHub operations.
- Do NOT write or update any Jira issues
- Do NOT post review comments to Jira — save them to `artifacts/strat-reviews/{id}-review-comment.md` instead
- Do NOT add external links to Jira issues
- DO create GitHub branches, commit files, and open draft PRs (Step 7d)
- DO set `refinement_pr_url` in strategy task frontmatter
- DO still read from Jira and local artifacts (reads are safe)
- DO still create local review files in `artifacts/strat-reviews/`

Use `--dry-run-jira` when you want to test the GitHub PR workflow without modifying Jira tickets.

## Step 1: Verify Artifacts Exist

Read files in `artifacts/strat-tasks/`. If no strategy artifacts exist or they haven't been refined yet (no "Strategy" section), tell the user to run `/strategy-refine` first and stop.

Check if prior reviews exist in `artifacts/strat-reviews/`. If any exist for the strategies being reviewed, read them — this is a re-review after revisions.

## Step 1a: Pipeline Label Gate

For each strategy found in `artifacts/strat-tasks/`, read its frontmatter to get the `jira_key`. If `jira_key` is not null, fetch the STRAT's labels from Jira:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/fetch_issue.py RHAISTRAT-NNNN --fields labels --markdown
```

If the STRAT has either `strat-creator-rubric-pass` or `strat-creator-needs-attention` in its labels, **skip this strategy** — it has already been processed by the pipeline:
- Do NOT review it
- Print `[SKIP] RHAISTRAT-NNNN — already has <label>, skipping review`
- Continue to the next strategy

If all strategies are skipped, print a summary and stop.

## Step 2: Fetch Architecture Context

If `--architecture-context <path>` is in `$ARGUMENTS`, use the local path:

```bash
bash ${CLAUDE_SKILL_DIR}/scripts/fetch-architecture-context.sh <path>
```

Otherwise, fetch from remote:

```bash
bash ${CLAUDE_SKILL_DIR}/scripts/fetch-architecture-context.sh
```

## Step 3: Bootstrap assess-strat

```bash
bash ${CLAUDE_SKILL_DIR}/scripts/bootstrap-assess-strat.sh
```

This clones the assess-strat plugin into `.context/assess-strat/`, copies skills and agent definitions, and exports the rubric to `artifacts/strat-rubric.md`.

## Step 4: Score Strategies

For each strategy in `artifacts/strat-tasks/`, launch a strat-scorer agent to produce numeric scores. The assess-strat plugin provides the rubric and agent definition.

Resolve the plugin root: the bootstrap script clones it to `.context/assess-strat/`. Use this path as `{PLUGIN_ROOT}`.

Create the run directory:

```bash
mkdir -p /tmp/strat-assess/review
```

For each strategy file, spawn one agent (model: opus, run_in_background: true) with this prompt:

```
You are a strategy quality assessor. Your task:
1. Read `{PROMPT_PATH}` for the full scoring rubric.
2. Follow its instructions exactly, substituting {KEY} for the strategy key and {RUN_DIR} for the run directory. Read the strategy from {DATA_FILE} (not the path in the rubric's step 1).
3. If architecture context is available at `.context/architecture-context/`, use Glob and Grep to validate architecture claims against real component docs.
Strategy key: {KEY}
Data file: {DATA_FILE}
Run directory: {RUN_DIR}
```

Substitute all placeholders:
- `{PROMPT_PATH}` → absolute path of `{PLUGIN_ROOT}/scripts/agent_prompt.md`
- `{DATA_FILE}` → the strategy file path (e.g., `artifacts/strat-tasks/RHAISTRAT-1469.md`)
- `{KEY}` → the strategy key (e.g., `RHAISTRAT-1469`)
- `{RUN_DIR}` → `/tmp/strat-assess/review`

Wait for all scorer agents to complete.

## Step 5: Parse Scores and Apply Verdicts (AUTOMATED — no LLM judgment)

After all scorer agents have completed, run the scoring scripts to deterministically compute verdicts and apply them to review files:

```bash
# Parse .result.md files → scores.csv with deterministic verdicts
python3 .context/assess-strat/scripts/parse_results.py /tmp/strat-assess/review/

# Apply scores and verdicts to review file frontmatter
python3 ${CLAUDE_SKILL_DIR}/scripts/apply_scores.py /tmp/strat-assess/review/scores.csv \
    --review-dir artifacts/strat-reviews \
    --result-dir /tmp/strat-assess/review

# Print summary statistics
python3 .context/assess-strat/scripts/summarize_run.py /tmp/strat-assess/review/
```

**Do NOT manually extract scores, compute verdicts, or set frontmatter.** The scripts handle this deterministically. The verdict rules are:
```
APPROVE:  total >= 6  AND  no zeros       → needs_attention=false
REVISE:   total >= 3  AND  ≤1 zero        → needs_attention=true
REJECT:   total < 3   OR   2+ zeros       → needs_attention=true
```

## Step 6: Run Prose Reviews

Use the **Skill tool** to invoke each of these reviewer skills in parallel. Call all four via the Skill tool simultaneously — each runs in its own isolated context and no reviewer sees another's output.

Pass the strategy key(s) being reviewed as arguments so each reviewer targets only the relevant strategies. If reviewing multiple strategies, pass all keys space-separated.

```
Skill(skill="strategy-feasibility-review", args="RHAISTRAT-NNNN")
Skill(skill="strategy-testability-review", args="RHAISTRAT-NNNN")
Skill(skill="strategy-scope-review", args="RHAISTRAT-NNNN")
Skill(skill="strategy-architecture-review", args="RHAISTRAT-NNNN")
```

Do NOT use the Agent tool for reviews. Use the Skill tool — the reviewer skills are defined in `.claude/skills/` and contain specific review instructions.

- **`strategy-feasibility-review`**: Can we build this with the proposed approach? Are effort estimates credible?
- **`strategy-testability-review`**: Are acceptance criteria testable? What edge cases are missing?
- **`strategy-scope-review`**: Is each strategy right-sized? Does the effort match the scope?
- **`strategy-architecture-review`** (if architecture context available): Are dependencies correctly identified? Are integration patterns correct?

Each reviewer auto-detects local mode (`local/strat-tasks/` vs `artifacts/strat-tasks/`) and reads the appropriate directories for strategies, RFE originals, and prior reviews.

## Step 7: Write Prose to Review Files

For each reviewed strategy, update the review file body in `artifacts/strat-reviews/{id}-review.md` with the prose from all four reviewers. The scores table was already written by `apply_scores.py` in Step 5 — add the prose sections after it.

The review file body should contain:

```markdown
## Scores
[already written by apply_scores.py — do not overwrite]

## Feasibility Review: {STRAT_ID} — {title}
<assessment from feasibility reviewer>

## Testability Review: {STRAT_ID} — {title}
<assessment from testability reviewer>

## Scope Review: {STRAT_ID} — {title}
<assessment from scope reviewer>

## Architecture Review: {STRAT_ID} — {title}
<assessment from architecture reviewer, or "skipped — no context">

## Agreements
<where reviewers aligned>

## Disagreements
<where reviewers diverged — preserve both views>
```

After writing prose, update the `reviewers.*` frontmatter fields with each prose reviewer's individual verdict:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/frontmatter.py set artifacts/strat-reviews/<id>-review.md \
    reviewers.feasibility=<prose_verdict> \
    reviewers.testability=<prose_verdict> \
    reviewers.scope=<prose_verdict> \
    reviewers.architecture=<prose_verdict>
```

**Important:** The `recommendation` field is NEVER changed by prose reviewers. It comes from the numeric scores only. Prose reviewers set their own `reviewers.*` verdicts for informational purposes — these do NOT affect the gate decision.

**Preserve disagreements.** If the feasibility reviewer says "this is fine" but the scope reviewer says "this is too big," report both views. Do not average or harmonize.

## Step 7a: Post Review Summary to Jira

For each reviewed strategy, compose and post a review summary comment to the RHAISTRAT issue (or save to file in dry-run mode).

1. **Compose the comment** using the deterministic script:

   ```bash
   python3 scripts/compose_review_comment.py \
       artifacts/strat-reviews/{id}-review.md \
       --output /tmp/strat-review-comment-{strat_id}.md
   ```

   This reads the review file frontmatter (scores, recommendation) and the Scores table notes to produce the formatted Jira comment with the scores table, per-dimension issue summaries, and verdict-specific action text.

2. **Post to Jira:**

   ```bash
   python3 -c "
   import sys; sys.path.insert(0, 'scripts')
   from jira_utils import add_comment, markdown_to_adf
   import os
   comment_md = open(sys.argv[1]).read()
   add_comment(os.environ['JIRA_SERVER'], os.environ['JIRA_USER'],
               os.environ['JIRA_TOKEN'], sys.argv[2], markdown_to_adf(comment_md))
   " /tmp/strat-review-comment-{strat_id}.md RHAISTRAT-NNNN
   ```

- **Dry-run mode** (`--dry-run` or `--dry-run-jira`): Run `compose_review_comment.py` with `--output artifacts/strat-reviews/{id}-review-comment.md` instead. Print `[DRY RUN] Review comment saved to artifacts/strat-reviews/{id}-review-comment.md`.
- **Jira credentials unavailable**: Save to file (same as dry-run) and notify the user.

## Step 7b: Attach Full Review File to Jira

If NOT in dry-run mode and `jira_key` is not null, attach the full review file to the RHAISTRAT issue as a Jira attachment. This gives reviewers access to the complete prose reviews alongside the summary comment.

```bash
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from jira_utils import add_attachment, require_env
s, u, t = require_env()
add_attachment(s, u, t, sys.argv[1], sys.argv[2])
" RHAISTRAT-NNNN artifacts/strat-reviews/{id}-review.md
```

Print `[ATTACHMENT] Review file attached to RHAISTRAT-NNNN`.

In dry-run mode, skip and print `[DRY RUN] Skipping attachment for RHAISTRAT-NNNN`.

## Step 7c: Apply Verdict Labels

If NOT in dry-run mode and `jira_key` is not null, add the appropriate label based on the verdict:

```bash
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from jira_utils import add_labels, require_env
s, u, t = require_env()
add_labels(s, u, t, sys.argv[1], sys.argv[2:])
" RHAISTRAT-NNNN <labels>
```

Labels by verdict:
- **APPROVE**: add `strat-creator-rubric-pass`
- **REVISE**: add `strat-creator-needs-attention`
- **REJECT**: add `strat-creator-needs-attention`

Print `[LABEL] <label> added to RHAISTRAT-NNNN`.

In dry-run mode, skip and print `[DRY RUN] Skipping labels for RHAISTRAT-NNNN`.

## Step 7d: Create Refinement PR (if needs_attention)

For each strategy with `needs_attention=true`, check whether `GH_REFINEMENT_REPO` is set. If it is not set, skip this step entirely and log `[SKIP] GH_REFINEMENT_REPO not set — skipping refinement PR creation`.

For each qualifying strategy:

1. **Prepare the PR artifacts** using the deterministic splitting script:

   ```bash
   python3 scripts/prepare_refinement_pr.py \
       artifacts/strat-tasks/{id}.md \
       artifacts/strat-reviews/{id}-review.md \
       --out-doc /tmp/strat-refinement-{strat_id}.md \
       --out-body /tmp/strat-refinement-{strat_id}-body.md \
       --out-gates /tmp/strat-refinement-{strat_id}-gates.md
   ```

   This script deterministically splits the strategy file into three outputs:
   - **`--out-doc`**: The strategy content (the "HOW") — `## Strategy` section only, with legacy disclaimer comments, `### Prerequisites & Process Gates`, and `## Staff Engineer Input` stripped out. This is what staff engineers edit directly in the PR.
   - **`--out-body`**: The PR description — frontmatter metadata, review summary with scores table, and full Business Need content (the "WHY").
   - **`--out-gates`**: The `### Prerequisites & Process Gates` table, posted as a PR review comment. File is only created if the section exists in the strategy.

2. **Create or update the refinement PR and post process gates:**

   ```bash
   python3 scripts/create_refinement_pr.py {strat_id} \
       --doc /tmp/strat-refinement-{strat_id}.md \
       --body /tmp/strat-refinement-{strat_id}-body.md \
       --gates /tmp/strat-refinement-{strat_id}-gates.md
   ```

   This script handles the full PR lifecycle: checks for an existing PR on the branch, creates a new branch + draft PR or updates the existing one, and posts the process gates as a PR review comment (event type COMMENT). It prints the PR URL to stdout.

   Omit `--gates` if `prepare_refinement_pr.py` did not produce a gates file.

3. Add the PR URL as an external link on the Jira issue (if `jira_key` is set and not in dry-run mode):

   ```bash
   python3 -c "
   import sys, os; sys.path.insert(0, 'scripts')
   from jira_utils import add_remote_link
   server, user, token = os.environ['JIRA_SERVER'], os.environ['JIRA_USER'], os.environ['JIRA_TOKEN']
   add_remote_link(server, user, token, sys.argv[1], sys.argv[2],
                   'Feature Refinement PR',
                   'https://github.githubassets.com/favicons/favicon.svg')
   " "RHAISTRAT-NNNN" "PR_URL"
   ```

4. Update the strategy task frontmatter with the PR URL:

   ```bash
   python3 scripts/frontmatter.py set artifacts/strat-tasks/{filename}.md \
       refinement_pr_url="{PR_URL}"
   ```

**`--dry-run` mode (full dry run):**
- Run `prepare_refinement_pr.py` with output paths under `artifacts/strat-refinements/` instead of `/tmp/` (`{strat_id}-refinement.md`, `{strat_id}-pr-body.md`, `{strat_id}-gates.md`). Do not create a GitHub PR.
- Skip `create_refinement_pr.py`, Jira remote link creation, and frontmatter update.
- Print `[DRY RUN] Refinement doc saved to artifacts/strat-refinements/{strat_id}-refinement.md`.

**`--dry-run-jira` mode (Jira-only dry run):**
- DO run both `prepare_refinement_pr.py` and `create_refinement_pr.py` — this is the whole point of this mode.
- DO set `refinement_pr_url` in strategy task frontmatter with the real PR URL.
- DO save the refinement doc to `artifacts/strat-refinements/{strat_id}-refinement.md` as well (local copy).
- Skip Jira remote link creation — print `[DRY RUN JIRA] Skipping Jira remote link for {jira_key}`.
- Print the PR URL so the user can open it in their browser.

## Step 7c: Merge Approved Refinement PRs

For each strategy with `needs_attention=false` that has `refinement_pr_url` set in its frontmatter, the strategy has passed review after a revision cycle — merge and clean up the PR.

1. **Merge the PR:**

   ```bash
   python3 -c "
   import sys, os; sys.path.insert(0, 'scripts')
   from gh_utils import merge_pr, pr_number_from_url
   pr_num = pr_number_from_url(sys.argv[1])
   repo = os.environ['GH_REFINEMENT_REPO']
   merge_pr(repo, pr_num)
   " "{refinement_pr_url}"
   ```

2. **Clear the PR URL from frontmatter:**

   ```bash
   python3 scripts/frontmatter.py set artifacts/strat-tasks/{filename}.md \
       refinement_pr_url=
   ```

- **Dry-run mode** (`--dry-run` or `--dry-run-jira`): Print `[DRY RUN] Would merge refinement PR for {strat_id}: {refinement_pr_url}`. Do not merge or clear frontmatter.
- **`GH_REFINEMENT_REPO` not set**: Skip this step.

## Step 8: Advise the User

Based on the results:
- **All approved** (`needs_attention=false`): Tell the user strategies are ready for `/strat.prioritize`.
- **Some need revision** (`needs_attention=true`, verdict=REVISE): List specific issues by dimension. If a refinement PR was created, tell the user to edit the PR and mark it Ready for Review. If no PR (`GH_REFINEMENT_REPO` not set), tell the user to edit the `## Staff Engineer Input` section, remove `needs-attention`, and re-run `/strategy-refine` then `/strategy-review`.
- **Fundamental problems** (`needs_attention=true`, verdict=REJECT): Recommend revisiting the source RFE or providing extensive guidance in the refinement PR (or `## Staff Engineer Input` if no PR). If a PR was created, note that it is available for the staff engineer to redirect the approach entirely.

$ARGUMENTS
