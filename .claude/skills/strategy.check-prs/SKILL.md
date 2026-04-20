---
name: strategy.check-prs
description: Check GitHub refinement PRs for staff engineer feedback. Fetch ready PR content for re-refinement and remove needs-attention labels.
context: fork
user-invocable: true
allowed-tools: Read, Write, Edit, Glob, Grep, Bash
---

You are a pipeline automation step that checks whether staff engineers have completed their feedback on strategy refinement PRs. When a PR has been moved from Draft to Ready for Review, you fetch the refinement document and prepare it for the next `/strategy.refine` run.

## Dry Run Mode

If `--dry-run` is in `$ARGUMENTS`, skip ALL external writes:
- Do NOT remove labels from Jira
- DO still check PR status on GitHub (reads are safe)
- DO still fetch and write refinement documents locally

## Prerequisites

This skill requires `GH_REFINEMENT_REPO` to be set. If it is not set, print an error and stop:

```
ERROR: GH_REFINEMENT_REPO is not set. This skill requires a configured GitHub repository.
Set it to owner/repo (e.g. ederign/strat-refinements).
```

## Step 1: Find Strategies with Open Refinement PRs

Scan `artifacts/strat-tasks/` for strategy files that have a `refinement_pr_url` set in their frontmatter:

```bash
python3 scripts/frontmatter.py read artifacts/strat-tasks/<filename>.md
```

For each file, check if `refinement_pr_url` is non-null. If it is, also read the corresponding review file in `artifacts/strat-reviews/` to confirm `needs_attention=true`. Only proceed with strategies that have both a PR URL and `needs_attention=true`.

If no strategies have open refinement PRs, print `No strategies with open refinement PRs found.` and stop.

## Step 2: Check PR Status

For each strategy with an open refinement PR, extract the PR number from the URL and check its status:

```bash
python3 -c "
import sys, json; sys.path.insert(0, 'scripts')
from gh_utils import require_gh_env, get_pr_status, pr_number_from_url
repo = require_gh_env()
pr_num = pr_number_from_url(sys.argv[1])
if pr_num:
    status = get_pr_status(repo, pr_num)
    print(json.dumps(status))
else:
    print('null')
" "{refinement_pr_url}"
```

Categorize each PR:

- **Still draft** (`isDraft=true`): Skip. Print `[WAITING] {strat_id}: PR still in draft — {pr_url}`
- **Ready for review** (`isDraft=false`, `state=OPEN`): Proceed to Step 3.
- **Merged** (`state=MERGED`): Proceed to Step 3 (treat same as ready).
- **Closed without merge** (`state=CLOSED`): Skip. Print `[CLOSED] {strat_id}: PR was closed without merge — {pr_url}`. Clear `refinement_pr_url` from frontmatter.

## Step 3: Fetch Refinement Document Content

For each ready PR, fetch the refinement document:

```bash
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from gh_utils import require_gh_env, get_pr_file_content, pr_number_from_url, file_path_for_strat
repo = require_gh_env()
pr_num = pr_number_from_url(sys.argv[1])
path = file_path_for_strat(sys.argv[2])
content = get_pr_file_content(repo, pr_num, path)
if content:
    print(content)
else:
    print('ERROR: Could not fetch refinement document', file=sys.stderr)
    sys.exit(1)
" "{refinement_pr_url}" "{strat_id}"
```

Write the fetched content to `artifacts/strat-refinements/{strat_id}-refinement.md`. Create the `artifacts/strat-refinements/` directory if it does not exist.

Print `[FETCHED] {strat_id}: Refinement document saved to artifacts/strat-refinements/{strat_id}-refinement.md`

## Step 4: Remove needs-attention Label from Jira

For each strategy where the refinement document was successfully fetched, remove the `strat-creator-needs-attention` label from the Jira issue (if `jira_key` is set and not in dry-run mode):

```bash
python3 -c "
import sys, os; sys.path.insert(0, 'scripts')
from jira_utils import remove_labels
server, user, token = os.environ['JIRA_SERVER'], os.environ['JIRA_USER'], os.environ['JIRA_TOKEN']
remove_labels(server, user, token, sys.argv[1], ['strat-creator-needs-attention'])
" "{jira_key}"
```

- **Dry-run mode**: Print `[DRY RUN] Would remove strat-creator-needs-attention from {jira_key}` instead.
- **No jira_key**: Skip label removal, print `[SKIP] No jira_key for {strat_id} — skipping Jira label removal`.
- **Jira credentials unavailable**: Skip label removal and notify the user.

## Step 5: Summary

Print a summary of all strategies checked:

```
=== Refinement PR Check Summary ===
  Ready for re-refinement:
    - {strat_id}: {title} (PR: {pr_url})
  Still waiting:
    - {strat_id}: {title} (PR: {pr_url})
  Closed (no action):
    - {strat_id}: {title} (PR: {pr_url})

Next step: Run /strategy.refine to regenerate strategies using the fetched refinement documents.
```

If strategies are ready for re-refinement, advise the user to run `/strategy.refine` followed by `/strategy.review`.

$ARGUMENTS
