---
name: strategy.review
description: Adversarial review of refined strategies. Scores against rubric, then runs independent forked reviewers for detailed prose.
user-invocable: true
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, Skill, Agent
---

You are a strategy review orchestrator. Your job is to score and review the strategies in `artifacts/strat-tasks/`, producing per-strategy review files with numeric scores and detailed prose.

## Dry Run Mode

If `--dry-run` is in `$ARGUMENTS`, skip ALL external writes:
- Do NOT write or update any Jira issues
- Do NOT post review comments to Jira — save them to `artifacts/strat-reviews/{id}-review-comment.md` instead
- DO still read from Jira and local artifacts (reads are safe)
- DO still create local review files in `artifacts/strat-reviews/`

## Step 1: Verify Artifacts Exist

Read files in `artifacts/strat-tasks/`. If no strategy artifacts exist or they haven't been refined yet (no "Strategy" section), tell the user to run `/strategy.refine` first and stop.

Check if prior reviews exist in `artifacts/strat-reviews/`. If any exist for the strategies being reviewed, read them — this is a re-review after revisions.

## Step 2: Fetch Architecture Context

```bash
bash scripts/fetch-architecture-context.sh
```

## Step 3: Bootstrap assess-strat

```bash
bash scripts/bootstrap-assess-strat.sh
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
python3 scripts/apply_scores.py /tmp/strat-assess/review/scores.csv \
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

```
Skill(skill="strategy-feasibility-review")
Skill(skill="strategy-testability-review")
Skill(skill="strategy-scope-review")
Skill(skill="strategy-architecture-review")
```

Do NOT use the Agent tool for reviews. Use the Skill tool — the reviewer skills are defined in `.claude/skills/` and contain specific review instructions.

- **`strategy-feasibility-review`**: Can we build this with the proposed approach? Are effort estimates credible?
- **`strategy-testability-review`**: Are acceptance criteria testable? What edge cases are missing?
- **`strategy-scope-review`**: Is each strategy right-sized? Does the effort match the scope?
- **`strategy-architecture-review`** (if architecture context available): Are dependencies correctly identified? Are integration patterns correct?

Each reviewer receives:
- The strategy artifacts (`artifacts/strat-tasks/`)
- The source RFEs (`artifacts/rfes.md`, `artifacts/rfe-tasks/`)
- Prior review files from `artifacts/strat-reviews/` (if this is a re-review)

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
python3 scripts/frontmatter.py set artifacts/strat-reviews/<id>-review.md \
    reviewers.feasibility=<prose_verdict> \
    reviewers.testability=<prose_verdict> \
    reviewers.scope=<prose_verdict> \
    reviewers.architecture=<prose_verdict>
```

**Important:** The `recommendation` field is NEVER changed by prose reviewers. It comes from the numeric scores only. Prose reviewers set their own `reviewers.*` verdicts for informational purposes — these do NOT affect the gate decision.

**Preserve disagreements.** If the feasibility reviewer says "this is fine" but the scope reviewer says "this is too big," report both views. Do not average or harmonize.

## Step 7a: Post Review Summary to Jira

For each reviewed strategy, compose a review summary comment and post it to the RHAISTRAT issue (or save to file in dry-run mode).

Read the review file frontmatter to get scores and recommendation:

```bash
python3 scripts/frontmatter.py read artifacts/strat-reviews/{id}-review.md
```

Compose the comment in markdown using this format:

```markdown
*[Strat Creator]* Strategy Review — {VERDICT} (Score: {total}/8)

| Criterion | Score | Status |
|-----------|-------|--------|
| Feasibility | {F}/2 | {✓ if 2, ⚠ if 1, ✗ if 0} |
| Testability | {T}/2 | {✓ if 2, ⚠ if 1, ✗ if 0} |
| Scope | {S}/2 | {✓ if 2, ⚠ if 1, ✗ if 0} |
| Architecture | {A}/2 | {✓ if 2, ⚠ if 1, ✗ if 0} |

{For each dimension scored < 2, one sentence summarizing the issue from the prose review.}

**Action:** {verdict-specific guidance}
```

Action text by verdict:
- **APPROVE**: "No action needed — strategy passed quality review."
- **REVISE**: "Edit the strategy to address flagged issues, then remove the `needs-attention` label. The pipeline will re-evaluate automatically."
- **REJECT**: "This strategy has fundamental problems. Consider revisiting the source RFE or re-running `/strategy.refine` with different constraints."

**Posting:**

Save the composed markdown to a temp file, then post:

```bash
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from jira_utils import add_comment, markdown_to_adf
import os
comment_md = open(sys.argv[1]).read()
add_comment(os.environ['JIRA_SERVER'], os.environ['JIRA_USER'],
            os.environ['JIRA_TOKEN'], sys.argv[2], markdown_to_adf(comment_md))
" /tmp/strat-review-comment-{KEY}.md RHAISTRAT-NNNN
```

- **Dry-run mode**: Write the comment markdown to `artifacts/strat-reviews/{id}-review-comment.md` instead. Print `[DRY RUN] Review comment saved to artifacts/strat-reviews/{id}-review-comment.md`.
- **Jira credentials unavailable**: Save to file (same as dry-run) and notify the user.

## Step 7b: Create Refinement PR (if needs_attention)

For each strategy with `needs_attention=true`, check whether `GH_REFINEMENT_REPO` is set. If it is not set, skip this step entirely and log `[SKIP] GH_REFINEMENT_REPO not set — skipping refinement PR creation`.

For each qualifying strategy:

1. Read the strategy task file (`artifacts/strat-tasks/{id}.md`) and the review file frontmatter (`recommendation`, `scores.*`).

2. Build the Feature Refinement Document from the **existing strategy file content**. The document uses the same structure as the strategy template (`strat-template.md`) so staff engineers work in the format they already know.

   Compose the document as follows:
   - Copy the **full strategy file content** (frontmatter + all three sections: Business Need, Strategy, Staff Engineer Input) from `artifacts/strat-tasks/{id}.md`
   - Prepend a review summary block at the top of the body (below frontmatter) with the score table and a condensed summary of issues from each low-scoring dimension
   - The `## Staff Engineer Input` section in the copied content is where the staff engineer will write their feedback — this is the same section they already know from the existing workflow

   The result is a self-contained document: the staff engineer sees the full strategy, the review findings, and the place to write feedback — all in one file.

3. Check if a PR already exists for branch `strat-refinement/{strat_id}`:

   ```bash
   python3 -c "
   import sys; sys.path.insert(0, 'scripts')
   from gh_utils import find_pr_by_branch, require_gh_env
   repo = require_gh_env()
   pr = find_pr_by_branch(repo, sys.argv[1])
   import json; print(json.dumps(pr))
   " "strat-refinement/{strat_id}"
   ```

   - **If PR exists**: Update the refinement document on the existing branch using `create_or_update_file`. Do not create a new PR.
   - **If no PR**: Create a branch, commit the document, and open a draft PR:

   ```bash
   python3 -c "
   import sys, os; sys.path.insert(0, 'scripts')
   from gh_utils import require_gh_env, create_branch, create_or_update_file, create_draft_pr
   repo = require_gh_env()
   strat_id = sys.argv[1]
   branch = f'strat-refinement/{strat_id}'
   create_branch(repo, branch)
   content = open(sys.argv[2]).read()
   create_or_update_file(repo, branch, f'refinements/{strat_id}.md', content,
                         f'Add refinement doc for {strat_id}')
   pr_url = create_draft_pr(repo, branch,
       f'[Refinement] {strat_id}: Strategy needs revision',
       f'This PR contains the Feature Refinement Document for {strat_id}.\n\n'
       f'**Edit the `## Staff Engineer Input` section** with corrections, '
       f'direction, and scope adjustments, then mark this PR as '
       f'Ready for Review to trigger pipeline re-evaluation.')
   print(pr_url)
   " "{strat_id}" /tmp/strat-refinement-{strat_id}.md
   ```

4. Add the PR URL as an external link on the Jira issue (if `jira_key` is set and not in dry-run mode):

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

5. Update the strategy task frontmatter with the PR URL:

   ```bash
   python3 scripts/frontmatter.py set artifacts/strat-tasks/{filename}.md \
       refinement_pr_url="{PR_URL}"
   ```

**Dry-run mode:**
- Write the generated refinement document to `artifacts/strat-refinements/{strat_id}-refinement.md` instead of creating a GitHub PR.
- Skip Jira remote link creation.
- Skip setting `refinement_pr_url` in frontmatter (since there is no real PR URL).
- Print `[DRY RUN] Refinement doc saved to artifacts/strat-refinements/{strat_id}-refinement.md`.

## Step 8: Advise the User

Based on the results:
- **All approved** (`needs_attention=false`): Tell the user strategies are ready for `/strat.prioritize`.
- **Some need revision** (`needs_attention=true`, verdict=REVISE): List specific issues by dimension. If a refinement PR was created, tell the user to edit the PR and mark it Ready for Review. If no PR (GH_REFINEMENT_REPO not set), tell the user to edit the `## Staff Engineer Input` section and re-run `/strategy.refine` then `/strategy.review`.
- **Fundamental problems** (`needs_attention=true`, verdict=REJECT): Recommend revisiting the source RFE or providing extensive guidance in the refinement PR (or `## Staff Engineer Input` if no PR). If a PR was created, note that it is available for the staff engineer to redirect the approach entirely.

$ARGUMENTS
