---
name: component-drift.group
description: Analyze component drift across RHAIRFE, RHAISTRAT, and RHOAIENG — produce a 3-column grouping table mapping related components.
user-invocable: true
allowed-tools: Read, Write, Bash, Glob
model: opus
---

You are a component taxonomy analyst. Your job is to produce a unified grouping table that maps semantically related Jira components across three projects: **RHAIRFE**, **RHAISTRAT**, and **RHOAIENG**.

Each project maintains its own component vocabulary. Many components are the same concept under different names (e.g., "Monitoring" vs "Observability", "Training Hub" vs "Fine Tuning"). Your task is to group them into rows where each row represents one logical capability, with columns for each project.

## Step 1 — Generate the Digest

Run the pre-processing script to produce a condensed digest from the raw component data:

```bash
python3 scripts/digest_components.py
```

This creates `artifacts/component-drift/component-digest.md` with:
- **Pre-grouped exact matches** (deterministic — ~44 all-three + ~32 two-project)
- **Ungrouped single-project components** (~30 that need your analysis)
- **Co-occurrence pairs** (non-exact names, count >= 2)
- **Substitution patterns** (drop/add evidence from same work items)
- **Shared-lead map** (same person leads differently-named components)

## Step 2 — Read the Digest

Read `artifacts/component-drift/component-digest.md`. The exact-match groups are already done. Your focus is the **30 ungrouped components** — evaluate whether each should merge into an existing group or remain ungrouped.

## Step 3 — Semantic Grouping

For each ungrouped component, evaluate whether it belongs in an existing group (from the exact-match sections) or should form a new group with other ungrouped components. Use these signals, in order of strength:

### Signal 1: Substitution patterns (strongest for non-exact matches)
If the data shows component A was dropped and component B was added on the same work item, this is direct evidence they represent the same capability. Treat as near-certain equivalence when count >= 2. Single-occurrence substitutions are candidates but require corroborating evidence.

### Signal 2: High co-occurrence with different names (strong)
If component A in project X consistently appears alongside component B in project Y on the same STRATs (count >= 5), they likely represent the same capability. **Important caveat**: ubiquitous components like "Documentation" and "AI Core Dashboard" co-occur with everything. High counts between two ubiquitous components are noise, not signal. Co-occurrence is only meaningful when at least one component is relatively rare.

### Signal 3: Shared lead (moderate)
Same person leads differently-named components across projects. This is organizational evidence they cover the same domain. Supportive but not sufficient alone — one person can lead genuinely different components. Particularly weak for RHAISTRAT where only 26% of components have leads assigned.

### Signal 4: Semantic name similarity (LLM judgment)
Use your domain knowledge to identify name variants:
- Typo/formatting variants: "Notebook Server" vs "Notebooks Server"
- Subset relationships: "Workbenches/IDE" contains "IDE"
- Synonym/rebranding: "TrustyAI" vs "AI Safety" vs "AI Evaluations"
- Abbreviation: "Pipelines" may be shorthand for "AI Pipelines"

### Signal 5: No signal
If a component has no co-occurrence, no substitution evidence, no shared lead, and no obvious semantic match, it stays ungrouped. Do not force matches.

### Rules

- **Group = same capability.** Only group components that represent the *same Jira component concept* if the vocabularies were aligned. Related but distinct components (e.g., "Notebooks Extensions" vs "Notebooks Images") should NOT be merged — they are separate capabilities that happen to be in the same domain.
- **One group per component.** Every component appears in exactly one group or in the ungrouped list. Never duplicate.
- **Evidence required.** Every non-exact-match grouping must cite which signal(s) justified it.
- **Co-occurrence noise.** "Documentation" appears on 74% of STRATs. It co-occurs with everything. Do not use co-occurrence with ubiquitous components as grouping evidence.
- **Substitution caution.** Not all substitutions are true equivalences — a drop/add on the same STRAT may reflect scope change rather than renaming. Apply judgment. "Notebook Server -> Documentation" is clearly not an equivalence.
- **The data is a sample.** Co-occurrence reflects 43 STRATs, not the full universe. Absence of co-occurrence does not prove components are unrelated.

## Step 4 — Write the Output

Write the final table to `artifacts/component-drift/component-groups.md` in this format:

```markdown
# Component Groups: RHAIRFE / RHAISTRAT / RHOAIENG

Generated: <timestamp>

## Summary
- **N** total groups
- **X** exact matches across all 3 projects
- **Y** exact matches across 2 projects (no semantic fill found for 3rd)
- **Z** exact matches across 2 projects + 1 semantic match
- **W** groups formed by semantic/substitution evidence only
- **V** ungrouped components (no cross-project match found)

## Grouping Table

| # | RHAIRFE | RHAISTRAT | RHOAIENG | Confidence | Evidence |
|---|---------|-----------|----------|------------|----------|
| 1 | AI Core Dashboard | AI Core Dashboard | AI Core Dashboard | Exact | all 3 projects |
...
| N | — | Dashboard | — | Ungrouped | no match found |

## Ungrouped Components

| Project | Component | Notes |
|---------|-----------|-------|
| RHAISTRAT | guide-llm | project-specific, no equivalent |
...

## Verification

### Coverage Check
- RHAIRFE: all 82 components accounted for: YES/NO
- RHAISTRAT: all 85 components accounted for: YES/NO
- RHOAIENG: all 59 components accounted for: YES/NO

### Grouping Breakdown
- Exact match (all 3): N
- Exact match (2) + semantic (1): N
- Exact match (2) only: N
- Semantic/substitution groups: N
- Ungrouped: N
- Total component slots: should equal 82 + 85 + 59 = 226
```

### Confidence values

Use these confidence tiers in the table:

- **Exact**: identical name across all projects in the row
- **Exact+Inferred**: 2 exact + 1 inferred via signals
- **Strong**: substitution pattern (count >= 2) or high co-occurrence + semantic match
- **Probable**: single substitution + corroborating signal, or shared lead + semantic match
- **Speculative**: semantic name similarity only, no empirical signal

### Evidence column

Cite the specific signal(s). Examples:
- `exact: all 3 projects`
- `exact: RHAIRFE+RHAISTRAT; inferred: shared lead (Adriel Paredes) + name variant`
- `substitution: "AgentDev -> Agentic" 2x`
- `shared lead: Dominik Dahlem leads Model Eval (RHAIRFE) and AI Evaluations (RHOAIENG)`
- `semantic: "Pipelines" is shorthand for "AI Pipelines"`

$ARGUMENTS
