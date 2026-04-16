# analyze_process.py

Multi-phase pipeline analysis of the RHAISTRAT Jira process. Discovers closed STRATs, scans for refinement docs, runs pipeline correlation and component analysis, and produces an executive report.

## Prerequisites

### Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The script uses only stdlib (`urllib`, `json`, `re`, etc.) for Jira API calls. Google Docs fetching (Phase 3) requires no extra packages either — `gdoc_utils.py` is also stdlib-only.

### Jira credentials

Set these environment variables (required for phases 1, 2, 4, 5):

```bash
export JIRA_SERVER="https://issues.redhat.com"   # your Jira instance
export JIRA_USER="you@redhat.com"
export JIRA_TOKEN="your-api-token"
```

Generate a token at: `https://<your-jira>/secure/ViewProfile.jspa` > Personal Access Tokens (Jira Server), or `https://id.atlassian.com/manage-profile/security/api-tokens` (Jira Cloud).

### Google Docs credentials (Phase 3 only)

Phase 3 fetches refinement documents linked from STRATs. If credentials are missing, the phase skips gracefully — everything else still works.

**Option A — OAuth refresh token:**

```bash
export GOOGLE_CLIENT_ID="your-client-id"
export GOOGLE_CLIENT_SECRET="your-client-secret"
export GOOGLE_REFRESH_TOKEN="your-refresh-token"
```

**Option B — Application Default Credentials (ADC):**

```bash
gcloud auth application-default login \
    --scopes=https://www.googleapis.com/auth/drive.readonly
```

This writes credentials to `~/.config/gcloud/application_default_credentials.json` which the script picks up automatically.

## Usage

```bash
# Run all phases (skips already-cached phases)
python3 scripts/analyze_process.py

# Run a single phase
python3 scripts/analyze_process.py --phase 1

# Force re-run of a phase (also invalidates downstream phases)
python3 scripts/analyze_process.py --refresh 3

# Force re-run of everything
python3 scripts/analyze_process.py --refresh-all

# Regenerate the report from cached data (no API calls)
python3 scripts/analyze_process.py --phase 6

# Custom cache directory
python3 scripts/analyze_process.py --cache-dir /tmp/my-cache
```

## Phases

| Phase | Description | API calls | Cache file |
|-------|-------------|-----------|------------|
| 1 | **Discovery** — find all closed STRATs, partition RFE-clones vs non-clones | ~7 JQL pages + ~187 RFE date fetches | `phase1-discovery.json` |
| 2 | **Google Doc scan** — scan STRAT descriptions + remote links for doc URLs | ~374 (2 per STRAT) | `phase2-gdoc-links.json` |
| 3 | **Refinement doc fetch** — bulk download Google Docs as markdown | ~1 per doc (skips cached) | `phase3-doc-fetch.json` |
| 4 | **Lightweight pipeline** — pipeline correlation on all RFE-clones (no epic details) | ~560 (3 per STRAT) | `phase4-pipeline-lightweight.json` |
| 5 | **Deep analysis** — full pipeline + component analysis on subset with all 3 artifacts | ~200 (subset only) | `phase5-deep-analysis.json` |
| 6 | **Executive report** — synthesize all phases into markdown | 0 (local only) | `process-analysis-report.md` |

**Total API calls for a full run**: ~1,200 (first run only; cached runs make 0 calls).

## Caching

Phase outputs are cached in `artifacts/process-analysis/` (configurable via `--cache-dir`). Each phase checks for its cache file before running:

- If cached, the phase is skipped with a message
- `--refresh N` deletes phase N's cache **and all downstream phases** (N+1 through 5)
- `--refresh-all` deletes all cached phases
- Phase 6 (report) always regenerates since it's local-only

Refinement doc artifacts are stored in `artifacts/strat-tasks/` (shared with other tools), not in the cache directory.

## Output

The primary output is `artifacts/process-analysis/process-analysis-report.md`, which includes:

1. **Executive Summary** — does the sequential pipeline exist as a practiced workflow?
2. **Population Overview** — STRAT universe, artifact availability, Google Doc discovery
3. **Pipeline Sequence Analysis** — temporal ordering, epic timing gaps
4. **Component Analysis** — cross-source accuracy, component distribution
5. **Pipeline Health Assessment** — what works, failure points, observations
6. **Appendices** — full pipeline STRAT details, methodology

## Dependencies (sibling scripts)

The orchestrator imports directly from these scripts in the same directory:

| Script | Functions used |
|--------|---------------|
| `jira_utils.py` | `require_env`, `get_issue`, `search_issues`, `get_remote_links`, `adf_to_markdown`, `get_issue_dates` |
| `research_pipeline.py` | `analyze_strat`, `compute_summary`, `DEFAULT_BOILERPLATE_KEYS`, `_parse_date` |
| `component_analysis.py` | `analyze_strat`, `batch_fetch_components`, `render_markdown` |
| `gdoc_utils.py` | `resolve_credentials`, `get_access_token`, `get_doc_metadata`, `export_doc` |

---

# Design Reference

Everything below is context for developers extending the script. Skip this section if you just want to run it.

## Domain Model

The script analyzes a Jira-based product planning process across three projects:

- **RHAIRFE** — RFE (Request for Enhancement) project. Product management creates feature requests here. Issue type: `Feature Request`.
- **RHAISTRAT** — Strategy project. Each STRAT is cloned from an RFE via a `Cloners` link type (outward: "clones", inward: "is cloned by"). STRATs add technical approach, component breakdown, and scope. Issue type: `Feature`.
- **RHOAIENG** — Engineering project. Epics represent implementation work. Linked to STRATs via parent-child hierarchy, Jira issue links, or text references in refinement docs.

Each project has its own `components` field vocabulary (85 in RHAISTRAT, 82 in RHAIRFE, 59 in RHOAIENG). The vocabularies overlap but aren't identical — e.g., "Monitoring" on an RFE may appear as "Observability" on the STRAT.

**Intended sequential pipeline**: RFE created → STRAT cloned from RFE → Refinement Doc written → Epics created under STRAT.

**Refinement documents** are Google Docs linked from STRATs via the Jira description field or the remote links API (`/rest/api/3/issue/{key}/remotelink`). They contain detailed technical breakdown, architecture decisions, and often reference specific RHOAIENG epic keys.

## Baseline Findings (April 2025)

These numbers establish what "normal" looks like for the dataset. Changes to the script or the underlying process should be evaluated against these baselines.

| Metric | Value | Notes |
|--------|-------|-------|
| Total closed STRATs | ~689 | Full RHAISTRAT project |
| RFE-cloned STRATs | ~187 (27%) | Remaining 73% have no Cloners link — out of scope |
| STRATs with refinement doc | ~57 (30% of clones) | Found via description regex + remote links |
| STRATs with child epics | ~121 (65% of clones) | Via `parent=KEY` JQL |
| STRATs with all 3 artifacts | ~43 (23% of clones) | RFE + refinement doc + epics |
| Full pipeline rate | 12/43 (28%) | RFE created before STRAT, STRAT created before earliest epic |
| Primary failure point | STRAT→Epic ordering | 28/31 failures because epics pre-date STRATs |
| Median epic timing gap | 40 days | Positive = epics created before STRAT |
| RFE→STRAT component carry-through | 70% | Average % of RFE components that appear on STRAT |
| STRATs missing epic-identified components | 33/43 (77%) | Components on epics but absent from STRAT |
| Combined components per STRAT (mean) | 5.6 | Union of RFE + STRAT + epic components |

**Key insight**: STRATs formalize work already in progress rather than initiating it. The sequential pipeline is an aspirational model, not the practiced workflow.

## Phase Output Schemas

### Phase 1 — `phase1-discovery.json`

```json
{
  "all_closed_count": 689,
  "rfe_clone_count": 187,
  "non_clone_count": 502,
  "rfe_clone_strats": [
    {
      "strat_key": "RHAISTRAT-122",
      "summary": "MLFlow integration in RHOAI [DP]",
      "created": "2024-01-15T...",
      "resolved": "2025-02-10T...",
      "rfe_key": "RHAIRFE-745",
      "rfe_created": "2023-11-01T..."
    }
  ],
  "_metadata": { "generated_at": "...", "phase": 1 }
}
```

### Phase 2 — `phase2-gdoc-links.json`

```json
{
  "total_scanned": 187,
  "strats_with_docs": 57,
  "total_unique_docs": 62,
  "results": {
    "RHAISTRAT-122": {
      "from_description": ["1abc...docid"],
      "from_remote_links": ["1abc...docid", "2def...docid"],
      "all_doc_ids": ["1abc...docid", "2def...docid"]
    }
  }
}
```

### Phase 3 — `phase3-doc-fetch.json`

```json
{
  "attempted": 12,
  "succeeded": 10,
  "cached": 45,
  "not_found": 1,
  "errors": 1,
  "results": {
    "RHAISTRAT-122": {
      "status": "ok|cached|404|error",
      "doc_id": "1abc...docid",
      "path": "artifacts/strat-tasks/RHAISTRAT-122-refinement-doc.md"
    }
  }
}
```

If Google credentials are unavailable, the phase writes a stub with `"skipped_reason": "no_credentials"` and downstream phases still work (they just see fewer refinement docs).

### Phase 4 — `phase4-pipeline-lightweight.json`

```json
{
  "metadata": {
    "generated_at": "...",
    "strat_count": 187,
    "skip_epic_details": true,
    "boilerplate_keys": ["RHOAIENG-1", "..."]
  },
  "summary": {
    "total_analyzed": 187,
    "with_source_rfe": 187,
    "with_source_rfe_pct": 100.0,
    "with_refinement_doc": 57,
    "with_refinement_doc_pct": 30.5,
    "with_epics": 121,
    "with_epics_pct": 64.7
  },
  "strats": [
    {
      "strat_key": "RHAISTRAT-122",
      "strat_summary": "...",
      "strat_created": "2024-01-15T...",
      "source_rfe": { "key": "RHAIRFE-745", "created": "2023-11-01T..." },
      "pipeline_signals": {
        "has_source_rfe": true,
        "has_refinement_doc": true,
        "has_epics": true,
        "epic_count_children": 5,
        "epic_count_jira": 2,
        "epic_count_doc": 1
      },
      "epic_details": []
    }
  ],
  "errors": []
}
```

`epic_details` is empty when `skip_epic_details` is true (Phase 4). Phase 5 re-runs on the subset with `skip_epic_details=false`, populating `epic_details` with `{"key", "summary", "created", "status"}` per epic.

### Phase 5 — `phase5-deep-analysis.json`

```json
{
  "candidate_count": 43,
  "pipeline": {
    "strats": [ /* same schema as phase 4 but with epic_details populated */ ],
    "summary": {
      "full_pipeline_count": 12,
      "rfe_before_strat_count": 40,
      "strat_before_epics_count": 12,
      "total_analyzed": 43
    }
  },
  "components": {
    "results": [
      {
        "strat_key": "RHAISTRAT-122",
        "rfe_key": "RHAIRFE-745",
        "rfe_components": ["Comp A", "Comp B"],
        "strat_components": ["Comp A", "Comp C"],
        "epic_components": ["Comp A", "Comp D"],
        "combined_components": ["Comp A", "Comp B", "Comp C", "Comp D"],
        "rfe_count": 2,
        "strat_count": 2,
        "epic_count": 2,
        "combined_count": 4,
        "on_rfe_only": ["Comp B"],
        "on_strat_only": ["Comp C"],
        "on_epics_only": ["Comp D"],
        "on_all_three": ["Comp A"],
        "rfe_carried_to_strat": ["Comp A"],
        "rfe_dropped_by_strat": ["Comp B"],
        "strat_added_vs_rfe": ["Comp C"]
      }
    ],
    "total_epics_checked": 215
  }
}
```

## Known Limitations

- **Non-clone STRATs are excluded** — 73% of closed STRATs have no Cloners link to an RFE. These may follow a different process but are out of scope.
- **Component vocabulary drift** — Components aren't normalized across projects. "Training Hub" (RFE) ≠ "Fine Tuning" (STRAT). Carry-through metrics undercount true overlap.
- **Google Docs access** — Many users won't have credentials. Phase 3 skips gracefully, but this reduces the Phase 5 candidate set (fewer STRATs with "all three artifacts").
- **Refinement doc detection is heuristic** — Only finds docs linked via description text or remote links. Docs shared in comments, Confluence, or Slack are invisible.
- **Epic discovery via parent-child only** — Phase 4 uses `parent=KEY` JQL. Epics linked via issue links or referenced in docs are counted in signals but not fetched for temporal analysis.
- **Static snapshot** — Results reflect point-in-time state. STRATs resolved after the analysis run, or epics added later, won't appear until `--refresh`.

## Relationship to strat-creator

This script is a diagnostic tool — it answers "how does the current STRAT process actually work?" The rest of `strat-creator` is a production pipeline for creating new STRATs (`strategy.create`, `strategy.refine`, `strategy.review`). Findings from this analysis inform how those skills should handle real-world patterns:

- Strategy creation should expect that engineering work may already be in progress
- Component lists on STRATs should be validated against child epics, not trusted as complete
- Refinement docs should be fetched and cross-referenced when available, but can't be assumed to exist
