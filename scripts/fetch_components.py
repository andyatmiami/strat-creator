#!/usr/bin/env python3
"""Fetch Jira component vocabularies and derive drift-analysis context.

1. Fetches the full component list (with leads) from RHAIRFE, RHAISTRAT,
   and RHOAIENG via the Jira project components API.
2. Reads the existing component-analysis.json (from analyze_process.py phase 5)
   to derive:
   - Co-occurrence matrix: how often each (project_A_component,
     project_B_component) pair appears on the same STRAT.
   - Substitution pairs: when an RFE component is dropped and a STRAT
     component is added on the same work item.

All output is written to artifacts/component-drift/ for subsequent
agent-based similarity analysis.

Usage:
    python3 scripts/fetch_components.py

Output:
    artifacts/component-drift/raw-components.json

Requires JIRA_SERVER, JIRA_USER, JIRA_TOKEN environment variables.
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from jira_utils import require_env, api_call_with_retry

PROJECTS = ["RHAIRFE", "RHAISTRAT", "RHOAIENG"]
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "artifacts",
                          "component-drift")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "raw-components.json")
COMPONENT_ANALYSIS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "artifacts", "component-analysis.json")


def fetch_project_components(server, user, token, project_key):
    """GET /rest/api/3/project/{key}/components — returns all components."""
    path = f"/project/{project_key}/components"
    data = api_call_with_retry(server, path, user, token)
    if data is None:
        return []
    return data


def build_co_occurrence(analysis_results):
    """Build co-occurrence counts from per-STRAT component analysis.

    For each STRAT that has components from multiple projects, count how
    often each (source_project_component, target_project_component) pair
    appears together on the same work item.

    Returns dict keyed by "PROJ_A->PROJ_B" with list of
    {"a": comp_a, "b": comp_b, "count": N, "strat_keys": [...]}.
    """
    # Pair keys: RFE↔STRAT, RFE↔EPIC, STRAT↔EPIC
    pairs = {
        "RHAIRFE->RHAISTRAT": defaultdict(lambda: {"count": 0, "strats": []}),
        "RHAIRFE->RHOAIENG": defaultdict(lambda: {"count": 0, "strats": []}),
        "RHAISTRAT->RHOAIENG": defaultdict(lambda: {"count": 0, "strats": []}),
    }

    for r in analysis_results:
        strat_key = r["strat_key"]
        rfe_comps = set(r.get("rfe_components", []))
        strat_comps = set(r.get("strat_components", []))
        epic_comps = set(r.get("epic_components", []))

        for rc in rfe_comps:
            for sc in strat_comps:
                entry = pairs["RHAIRFE->RHAISTRAT"][(rc, sc)]
                entry["count"] += 1
                entry["strats"].append(strat_key)
        for rc in rfe_comps:
            for ec in epic_comps:
                entry = pairs["RHAIRFE->RHOAIENG"][(rc, ec)]
                entry["count"] += 1
                entry["strats"].append(strat_key)
        for sc in strat_comps:
            for ec in epic_comps:
                entry = pairs["RHAISTRAT->RHOAIENG"][(sc, ec)]
                entry["count"] += 1
                entry["strats"].append(strat_key)

    # Flatten to serializable lists, sorted by count descending
    output = {}
    for pair_key, mapping in pairs.items():
        entries = []
        for (a, b), data in mapping.items():
            entries.append({
                "a": a,
                "b": b,
                "count": data["count"],
                "strat_keys": data["strats"],
            })
        entries.sort(key=lambda e: -e["count"])
        output[pair_key] = entries
    return output


def extract_substitutions(analysis_results):
    """Extract component substitution pairs from drop/add patterns.

    When a component is on the RFE but dropped from the STRAT, and a
    different component is added on the STRAT (same work item), those
    form candidate equivalence pairs.  Similarly for STRAT→Epic gaps.

    Returns list of {"dropped": comp, "added": comp, "source": "RFE->STRAT"
    or "STRAT->EPIC", "strat_key": key}.
    """
    substitutions = []

    for r in analysis_results:
        strat_key = r["strat_key"]

        # RFE → STRAT substitutions
        dropped = r.get("rfe_dropped_by_strat", [])
        added = r.get("strat_added_vs_rfe", [])
        if dropped and added:
            for d in dropped:
                for a in added:
                    substitutions.append({
                        "dropped": d,
                        "added": a,
                        "direction": "RHAIRFE->RHAISTRAT",
                        "strat_key": strat_key,
                    })

        # STRAT → Epic: components on epics only (missing from STRAT)
        # paired with components on STRAT only (missing from epics)
        strat_only = r.get("on_strat_only", [])
        epic_only = r.get("on_epics_only", [])
        if strat_only and epic_only:
            for s in strat_only:
                for e in epic_only:
                    substitutions.append({
                        "dropped": s,
                        "added": e,
                        "direction": "RHAISTRAT->RHOAIENG",
                        "strat_key": strat_key,
                    })

    # Aggregate: count how often each (dropped, added, direction) triple
    # appears across STRATs
    agg = defaultdict(lambda: {"count": 0, "strat_keys": []})
    for s in substitutions:
        key = (s["dropped"], s["added"], s["direction"])
        agg[key]["count"] += 1
        agg[key]["strat_keys"].append(s["strat_key"])

    aggregated = []
    for (dropped, added, direction), data in agg.items():
        aggregated.append({
            "dropped": dropped,
            "added": added,
            "direction": direction,
            "count": data["count"],
            "strat_keys": data["strat_keys"],
        })
    aggregated.sort(key=lambda e: -e["count"])
    return aggregated


def main():
    server, user, token = require_env()
    missing = []
    if not server:
        missing.append("JIRA_SERVER")
    if not user:
        missing.append("JIRA_USER")
    if not token:
        missing.append("JIRA_TOKEN")
    if missing:
        print(f"Error: missing env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    result = {
        "_metadata": {
            "generated_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "projects": PROJECTS,
        },
        "projects": {},
    }

    # --- 1. Fetch component vocabularies with leads ---
    for project in PROJECTS:
        print(f"Fetching components for {project}...", file=sys.stderr)
        raw = fetch_project_components(server, user, token, project)

        components = []
        for c in raw:
            lead = c.get("lead")
            lead_name = None
            if isinstance(lead, dict):
                lead_name = (lead.get("displayName")
                             or lead.get("name")
                             or lead.get("emailAddress"))
            components.append({
                "id": c.get("id"),
                "name": c.get("name", ""),
                "description": c.get("description", ""),
                "lead": lead_name,
            })

        components.sort(key=lambda c: c["name"].lower())

        result["projects"][project] = {
            "count": len(components),
            "components": components,
        }
        print(f"  {project}: {len(components)} components", file=sys.stderr)

    # --- 2. Derive co-occurrence and substitutions from cached analysis ---
    if os.path.exists(COMPONENT_ANALYSIS_PATH):
        print(f"\nReading {COMPONENT_ANALYSIS_PATH} for co-occurrence "
              f"analysis...", file=sys.stderr)
        with open(COMPONENT_ANALYSIS_PATH) as f:
            analysis_data = json.load(f)
        analysis_results = analysis_data.get("results", [])

        co_occurrence = build_co_occurrence(analysis_results)
        substitutions = extract_substitutions(analysis_results)

        result["co_occurrence"] = co_occurrence
        result["substitutions"] = substitutions

        # Summarize
        for pair_key, entries in co_occurrence.items():
            print(f"  {pair_key}: {len(entries)} co-occurring pairs",
                  file=sys.stderr)
        print(f"  Substitution patterns: {len(substitutions)} unique pairs",
              file=sys.stderr)
    else:
        print(f"\nWarning: {COMPONENT_ANALYSIS_PATH} not found — skipping "
              f"co-occurrence and substitution analysis. Run "
              f"analyze_process.py first for richer context.",
              file=sys.stderr)
        result["co_occurrence"] = None
        result["substitutions"] = None

    # --- 3. Write output ---
    with open(OUTPUT_FILE, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nWrote {OUTPUT_FILE}", file=sys.stderr)

    # Print summary
    print("\n=== Component Vocabulary Summary ===", file=sys.stderr)
    for project in PROJECTS:
        data = result["projects"][project]
        names = [c["name"] for c in data["components"]]
        leads = set(c["lead"] for c in data["components"] if c["lead"])
        print(f"  {project}: {data['count']} components, "
              f"{len(leads)} unique leads", file=sys.stderr)
        preview = names[:5]
        if len(names) > 5:
            preview.append(f"... +{len(names) - 5} more")
        print(f"    {', '.join(preview)}", file=sys.stderr)

    if result["substitutions"]:
        print("\n=== Top Substitution Patterns ===", file=sys.stderr)
        for s in result["substitutions"][:10]:
            print(f"  {s['dropped']} -> {s['added']} "
                  f"({s['direction']}, {s['count']}x)", file=sys.stderr)


if __name__ == "__main__":
    main()
