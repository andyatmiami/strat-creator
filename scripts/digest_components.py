#!/usr/bin/env python3
"""Pre-process raw component data into a condensed digest for agent analysis.

Reads artifacts/component-drift/raw-components.json and produces
artifacts/component-drift/component-digest.md with:

1. Exact-match groups (deterministic, no agent judgment needed)
2. Ungrouped single-project components (need semantic analysis)
3. Non-exact co-occurrence pairs (count >= 2)
4. Substitution patterns
5. Shared-lead map (same person leads differently-named components)

Usage:
    python3 scripts/digest_components.py
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(__file__)
INPUT_FILE = os.path.join(SCRIPT_DIR, "..", "artifacts", "component-drift",
                          "raw-components.json")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "..", "artifacts", "component-drift",
                           "component-digest.md")

PROJECTS = ["RHAIRFE", "RHAISTRAT", "RHOAIENG"]


def load_data():
    with open(INPUT_FILE) as f:
        return json.load(f)


def build_component_sets(data):
    """Build normalized name -> project presence and lead maps."""
    # name -> {project: lead}
    comp_info = defaultdict(dict)
    for proj in PROJECTS:
        for c in data["projects"][proj]["components"]:
            name = c["name"].strip()
            comp_info[name][proj] = c.get("lead")
    return comp_info


def classify_groups(comp_info):
    """Split components into exact-match groups and ungrouped."""
    all_three = []
    two_project = []
    one_project = []

    for name, proj_map in sorted(comp_info.items(), key=lambda x: x[0].lower()):
        projects_present = [p for p in PROJECTS if p in proj_map]
        leads = {proj_map[p] for p in projects_present if proj_map[p]}

        entry = {
            "name": name,
            "projects": projects_present,
            "leads": sorted(leads) if leads else [],
        }

        if len(projects_present) == 3:
            all_three.append(entry)
        elif len(projects_present) == 2:
            two_project.append(entry)
        else:
            entry["project"] = projects_present[0]
            one_project.append(entry)

    return all_three, two_project, one_project


def filter_co_occurrence(data):
    """Filter co-occurrence to non-exact-name pairs with count >= 2."""
    co_occ = data.get("co_occurrence")
    if not co_occ:
        return {}

    filtered = {}
    for direction, entries in co_occ.items():
        kept = []
        for e in entries:
            a = e["a"].strip()
            b = e["b"].strip()
            if a.lower() == b.lower():
                continue
            if e["count"] < 2:
                continue
            kept.append({"a": a, "b": b, "count": e["count"]})
        if kept:
            filtered[direction] = kept
    return filtered


def filter_substitutions(data):
    """Return substitution patterns sorted by count."""
    subs = data.get("substitutions")
    if not subs:
        return []
    return [
        {
            "dropped": s["dropped"].strip(),
            "added": s["added"].strip(),
            "direction": s["direction"],
            "count": s["count"],
        }
        for s in subs
        if s["count"] >= 1
    ]


def build_shared_lead_map(comp_info):
    """Find components with the same lead across projects but different names.

    Returns list of {"lead": name, "components": [{"project": p, "name": n}]}.
    """
    # lead -> [(project, component_name)]
    lead_comps = defaultdict(list)
    for name, proj_map in comp_info.items():
        for proj, lead in proj_map.items():
            if lead:
                lead_comps[lead].append((proj, name))

    results = []
    for lead, entries in sorted(lead_comps.items()):
        # Only interesting if the same lead has different component names
        names = set(n for _, n in entries)
        if len(names) <= 1:
            continue
        # Only interesting if spans multiple projects
        projects = set(p for p, _ in entries)
        if len(projects) <= 1:
            continue
        results.append({
            "lead": lead,
            "components": [{"project": p, "name": n}
                           for p, n in sorted(entries)],
        })
    return results


def render_markdown(all_three, two_project, one_project, co_occurrence,
                    substitutions, shared_leads, data):
    """Render the digest as markdown."""
    lines = []
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines.append("# Component Digest for Drift Analysis")
    lines.append("")
    lines.append(f"Generated: {ts}")
    lines.append(f"Source: `artifacts/component-drift/raw-components.json`")
    lines.append("")

    # Counts
    counts = {p: data["projects"][p]["count"] for p in PROJECTS}
    lines.append("## Overview")
    lines.append("")
    lines.append(f"| Project | Components |")
    lines.append(f"| --- | --- |")
    for p in PROJECTS:
        lines.append(f"| {p} | {counts[p]} |")
    total_unique = len(all_three) + len(two_project) + len(one_project)
    lines.append("")
    lines.append(f"- **{total_unique}** unique component names (after "
                 f"whitespace normalization)")
    lines.append(f"- **{len(all_three)}** exact match across all 3 projects")
    lines.append(f"- **{len(two_project)}** exact match across 2 projects")
    lines.append(f"- **{len(one_project)}** in only 1 project "
                 f"(need semantic analysis)")
    lines.append("")

    # --- Section 1: Exact matches (all three) ---
    lines.append("## Pre-Grouped: Exact Name Match — All Three Projects "
                 f"({len(all_three)})")
    lines.append("")
    lines.append("These are deterministic matches. No agent judgment needed.")
    lines.append("")
    lines.append("| RHAIRFE | RHAISTRAT | RHOAIENG | Lead(s) |")
    lines.append("| --- | --- | --- | --- |")
    for e in all_three:
        leads_str = ", ".join(e["leads"]) if e["leads"] else "—"
        lines.append(f"| {e['name']} | {e['name']} | {e['name']} "
                     f"| {leads_str} |")
    lines.append("")

    # --- Section 2: Exact matches (two projects) ---
    lines.append("## Pre-Grouped: Exact Name Match — Two Projects "
                 f"({len(two_project)})")
    lines.append("")
    lines.append("The missing project cell is a candidate for semantic "
                 "matching from the ungrouped list below.")
    lines.append("")
    lines.append("| RHAIRFE | RHAISTRAT | RHOAIENG | Lead(s) |")
    lines.append("| --- | --- | --- | --- |")
    for e in two_project:
        cells = {}
        for p in PROJECTS:
            cells[p] = e["name"] if p in e["projects"] else "—"
        leads_str = ", ".join(e["leads"]) if e["leads"] else "—"
        lines.append(f"| {cells['RHAIRFE']} | {cells['RHAISTRAT']} "
                     f"| {cells['RHOAIENG']} | {leads_str} |")
    lines.append("")

    # --- Section 3: Ungrouped (single project) ---
    lines.append(f"## Ungrouped: Single-Project Components ({len(one_project)})")
    lines.append("")
    lines.append("These need semantic analysis. Evaluate whether each should "
                 "merge into an existing group or remain ungrouped.")
    lines.append("")
    lines.append("| Project | Component | Lead |")
    lines.append("| --- | --- | --- |")
    for e in one_project:
        lead = e["leads"][0] if e["leads"] else "—"
        lines.append(f"| {e['project']} | {e['name']} | {lead} |")
    lines.append("")

    # --- Section 4: Co-occurrence signals ---
    lines.append("## Cross-Project Signals: Co-occurrence "
                 "(non-exact names, count >= 2)")
    lines.append("")
    lines.append("Components that appear together on the same STRAT work "
                 "items. Higher count = stronger evidence of relatedness. "
                 "**Caution**: ubiquitous components (Documentation, AI Core "
                 "Dashboard) co-occur with everything — high counts between "
                 "two ubiquitous components are noise, not signal.")
    lines.append("")
    for direction, entries in co_occurrence.items():
        proj_a, proj_b = direction.split("->")
        lines.append(f"### {proj_a} / {proj_b}")
        lines.append("")
        lines.append(f"| {proj_a} | {proj_b} | Count |")
        lines.append("| --- | --- | --- |")
        for e in entries:
            lines.append(f"| {e['a']} | {e['b']} | {e['count']} |")
        lines.append("")

    # --- Section 5: Substitution patterns ---
    lines.append("## Cross-Project Signals: Substitution Patterns")
    lines.append("")
    lines.append("When a component is dropped from the source project and a "
                 "different component is added on the target project for the "
                 "same work item. This is direct evidence of vocabulary "
                 "renaming. **Caution**: not all substitutions are true "
                 "equivalences — a drop/add on the same STRAT may reflect "
                 "scope change, not renaming. Use judgment.")
    lines.append("")
    if substitutions:
        lines.append("| Dropped | Added | Direction | Count |")
        lines.append("| --- | --- | --- | --- |")
        for s in substitutions:
            lines.append(f"| {s['dropped']} | {s['added']} "
                         f"| {s['direction']} | {s['count']} |")
    else:
        lines.append("*(no substitution data available)*")
    lines.append("")

    # --- Section 6: Shared lead map ---
    lines.append("## Cross-Project Signals: Shared Leads")
    lines.append("")
    lines.append("Same person leads differently-named components across "
                 "projects. Organizational evidence that these components "
                 "cover the same domain.")
    lines.append("")
    if shared_leads:
        for entry in shared_leads:
            comps = ", ".join(f"{c['name']} ({c['project']})"
                              for c in entry["components"])
            lines.append(f"- **{entry['lead']}**: {comps}")
        lines.append("")
    else:
        lines.append("*(no shared-lead signals found)*")
        lines.append("")

    return "\n".join(lines)


def main():
    if not os.path.exists(INPUT_FILE):
        print(f"Error: {INPUT_FILE} not found. Run fetch_components.py first.",
              file=sys.stderr)
        sys.exit(1)

    data = load_data()
    comp_info = build_component_sets(data)
    all_three, two_project, one_project = classify_groups(comp_info)

    co_occurrence = filter_co_occurrence(data)
    substitutions = filter_substitutions(data)
    shared_leads = build_shared_lead_map(comp_info)

    md = render_markdown(all_three, two_project, one_project, co_occurrence,
                         substitutions, shared_leads, data)

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        f.write(md)

    print(f"Wrote {OUTPUT_FILE}", file=sys.stderr)
    print(f"\n=== Digest Summary ===", file=sys.stderr)
    print(f"  Exact match (all 3):  {len(all_three)}", file=sys.stderr)
    print(f"  Exact match (2):      {len(two_project)}", file=sys.stderr)
    print(f"  Ungrouped (1):        {len(one_project)}", file=sys.stderr)
    print(f"  Co-occurrence pairs:  "
          f"{sum(len(v) for v in co_occurrence.values())}", file=sys.stderr)
    print(f"  Substitution pairs:   {len(substitutions)}", file=sys.stderr)
    print(f"  Shared-lead signals:  {len(shared_leads)}", file=sys.stderr)


if __name__ == "__main__":
    main()
