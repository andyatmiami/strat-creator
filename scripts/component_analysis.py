#!/usr/bin/env python3
"""Analyze which RHOAI components are involved per STRAT.

Uses Jira `components` field on RFEs, STRATs, and child epics to determine
component involvement. Cross-references all three sources to find gaps and
measure accuracy of component tagging across the pipeline.

Usage:
    python3 scripts/component_analysis.py \
        --pipeline-json artifacts/pipeline-research-full-pipeline.json \
        --output artifacts/component-analysis.json
"""
import argparse
import json
import os
import sys
import urllib.parse
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from jira_utils import get_issue, search_issues, api_call_with_retry


def fetch_issue_components(server, user, token, issue_key):
    """Fetch the components field from a Jira issue."""
    try:
        data = get_issue(server, user, token, issue_key,
                         fields=["components"])
        fields = data.get("fields", {})
        return [c["name"] for c in fields.get("components", [])]
    except Exception as e:
        print(f"  Warning: could not fetch components for {issue_key}: {e}",
              file=sys.stderr)
        return []


def batch_fetch_components(server, user, token, issue_keys):
    """Batch-fetch components for a list of issue keys using JQL.

    Returns dict of {key: [component_names]}.
    """
    if not issue_keys:
        return {}

    result = {}
    # JQL has URL length limits, batch in groups of 50
    batch_size = 50
    for i in range(0, len(issue_keys), batch_size):
        batch = issue_keys[i:i + batch_size]
        keys_str = ", ".join(batch)
        jql = f"key in ({keys_str})"
        try:
            issues = search_issues(server, user, token, jql,
                                   fields=["components"],
                                   max_results=100)
            for issue in issues:
                key = issue.get("key", "")
                fields = issue.get("fields", {})
                comps = [c["name"] for c in fields.get("components", [])]
                result[key] = comps
        except Exception as e:
            print(f"  Warning: batch fetch failed for {len(batch)} issues: {e}",
                  file=sys.stderr)

    return result


def analyze_strat(server, user, token, strat_record, epic_components_cache):
    """Analyze a single STRAT for component involvement."""
    key = strat_record["strat_key"]

    # 1. Fetch RFE components
    source_rfe = strat_record.get("source_rfe", {})
    rfe_key = ""
    rfe_components = []
    if isinstance(source_rfe, dict):
        rfe_key = source_rfe.get("key", "")
    if rfe_key:
        rfe_components = fetch_issue_components(server, user, token, rfe_key)

    # 2. Fetch STRAT's own components from Jira
    strat_components = fetch_issue_components(server, user, token, key)

    # 3. Collect epic components from cache
    epic_keys = set()
    for e in strat_record.get("epic_details", []):
        epic_keys.add(e["key"])

    epic_comp_map = {}  # epic_key -> [components]
    all_epic_components = set()
    for ek in epic_keys:
        comps = epic_components_cache.get(ek, [])
        if comps:
            epic_comp_map[ek] = comps
            all_epic_components.update(comps)

    # 4. Compute sets
    rfe_set = set(rfe_components)
    strat_set = set(strat_components)
    epic_set = all_epic_components
    combined = rfe_set | strat_set | epic_set

    # Cross-source analysis
    on_rfe_only = rfe_set - strat_set - epic_set
    on_strat_only = strat_set - rfe_set - epic_set
    on_epics_only = epic_set - rfe_set - strat_set
    on_all_three = rfe_set & strat_set & epic_set
    on_rfe_and_strat = (rfe_set & strat_set) - epic_set
    on_rfe_and_epics = (rfe_set & epic_set) - strat_set
    on_strat_and_epics = (strat_set & epic_set) - rfe_set

    # RFE -> STRAT carry-through: how many RFE components made it to the STRAT?
    rfe_carried_to_strat = rfe_set & strat_set if rfe_set else set()
    rfe_dropped_by_strat = rfe_set - strat_set if rfe_set else set()
    strat_added_vs_rfe = strat_set - rfe_set if rfe_set else set()

    return {
        "strat_key": key,
        "strat_summary": strat_record.get("strat_summary", ""),
        "rfe_key": rfe_key,
        "rfe_components": sorted(rfe_set),
        "strat_components": sorted(strat_set),
        "epic_components": sorted(epic_set),
        "combined_components": sorted(combined),
        "rfe_count": len(rfe_set),
        "strat_count": len(strat_set),
        "epic_count": len(epic_set),
        "combined_count": len(combined),
        # Cross-source breakdown
        "on_rfe_only": sorted(on_rfe_only),
        "on_strat_only": sorted(on_strat_only),
        "on_epics_only": sorted(on_epics_only),
        "on_all_three": sorted(on_all_three),
        "on_rfe_and_strat": sorted(on_rfe_and_strat),
        "on_rfe_and_epics": sorted(on_rfe_and_epics),
        "on_strat_and_epics": sorted(on_strat_and_epics),
        # RFE -> STRAT carry-through
        "rfe_carried_to_strat": sorted(rfe_carried_to_strat),
        "rfe_dropped_by_strat": sorted(rfe_dropped_by_strat),
        "strat_added_vs_rfe": sorted(strat_added_vs_rfe),
        "epic_detail_count": len(epic_keys),
        "epics_with_components": len(epic_comp_map),
    }


def _stats(values):
    """Return mean, median, min, max for a list of numbers."""
    s = sorted(values)
    return {
        "mean": sum(s) / len(s),
        "median": s[len(s) // 2],
        "min": s[0],
        "max": s[-1],
    }


def render_markdown(results, output_path):
    """Render analysis results as markdown."""
    md_path = output_path.replace(".json", "-summary.md")

    lines = []
    lines.append("# Component Analysis: RFE → STRAT → Epics")
    lines.append("")
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines.append("")

    rfe_counts = [r["rfe_count"] for r in results]
    strat_counts = [r["strat_count"] for r in results]
    epic_counts = [r["epic_count"] for r in results]
    combined_counts = [r["combined_count"] for r in results]
    has_rfe = [r for r in results if r["rfe_key"]]

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- **{len(results)}** STRATs analyzed")
    lines.append(f"- **{len(has_rfe)}** have a linked RFE")
    lines.append("")

    lines.append("### Component counts by source")
    lines.append("")
    lines.append("| Source | Mean | Median | Range |")
    lines.append("| --- | --- | --- | --- |")
    for label, vals in [("RFE", rfe_counts), ("STRAT", strat_counts),
                         ("Epics", epic_counts), ("Combined", combined_counts)]:
        s = _stats(vals)
        lines.append(f"| {label} | {s['mean']:.1f} | {s['median']} | {s['min']}–{s['max']} |")
    lines.append("")

    lines.append("### Combined component distribution")
    lines.append("")
    for lo, hi, label in [(1, 1, "1 component"), (2, 3, "2–3 components"),
                           (4, 6, "4–6 components"), (7, 99, "7+ components")]:
        n = sum(1 for c in combined_counts if lo <= c <= hi)
        lines.append(f"- **{label}**: {n} ({n/len(combined_counts)*100:.0f}%)")
    lines.append("")

    # RFE → STRAT carry-through
    with_rfe = [r for r in results if r["rfe_key"]]
    if with_rfe:
        rfe_carry_rates = []
        for r in with_rfe:
            if r["rfe_count"] > 0:
                rate = len(r["rfe_carried_to_strat"]) / r["rfe_count"]
                rfe_carry_rates.append(rate)

        lines.append("### RFE → STRAT component carry-through")
        lines.append("")
        if rfe_carry_rates:
            avg_rate = sum(rfe_carry_rates) / len(rfe_carry_rates)
            perfect = sum(1 for r in rfe_carry_rates if r == 1.0)
            lines.append(f"- Average carry-through rate: **{avg_rate*100:.0f}%** of RFE components appear on STRAT")
            lines.append(f"- Perfect carry-through (100%): {perfect}/{len(rfe_carry_rates)} ({perfect/len(rfe_carry_rates)*100:.0f}%)")
            dropped = sum(1 for r in with_rfe if r["rfe_dropped_by_strat"])
            lines.append(f"- STRATs that dropped RFE components: {dropped}/{len(with_rfe)} ({dropped/len(with_rfe)*100:.0f}%)")
            added = sum(1 for r in with_rfe if r["strat_added_vs_rfe"])
            lines.append(f"- STRATs that added new components vs RFE: {added}/{len(with_rfe)} ({added/len(with_rfe)*100:.0f}%)")
        lines.append("")

    # STRAT accuracy vs epics
    missing_from_strat = sum(1 for r in results if r["on_epics_only"])
    lines.append("### STRAT component accuracy vs epics")
    lines.append(f"- STRATs missing components found on epics: {missing_from_strat}/{len(results)} ({missing_from_strat/len(results)*100:.0f}%)")
    lines.append("")

    # Component frequency (combined)
    freq = {}
    for r in results:
        for c in r["combined_components"]:
            freq[c] = freq.get(c, 0) + 1
    lines.append("## Component Frequency (Combined)")
    lines.append("")
    lines.append("| Component | STRATs | % |")
    lines.append("| --- | --- | --- |")
    for comp, count in sorted(freq.items(), key=lambda x: -x[1]):
        pct = count / len(results) * 100
        lines.append(f"| {comp} | {count} | {pct:.0f}% |")
    lines.append("")

    # Per-STRAT table
    lines.append("## Per-STRAT Component Breakdown")
    lines.append("")
    lines.append("| STRAT | RFE | RFE Comps | STRAT Comps | Epic-Only Comps | Combined | Summary |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for r in sorted(results, key=lambda x: -x["combined_count"]):
        rfe = r["rfe_key"] or "-"
        rfe_c = len(r["rfe_components"])
        strat_c = ", ".join(r["strat_components"]) or "*(none)*"
        epic_only = ", ".join(r["on_epics_only"]) or "-"
        summary_short = r["strat_summary"][:50].replace("|", "/")
        lines.append(f"| {r['strat_key']} | {rfe} | {rfe_c} | {strat_c} | {epic_only} | {r['combined_count']} | {summary_short} |")
    lines.append("")

    # RFE → STRAT gap analysis
    lines.append("## RFE → STRAT Gap: Components on RFE but Dropped by STRAT")
    lines.append("")
    lines.append("| STRAT | RFE | Dropped Components | Added by STRAT |")
    lines.append("| --- | --- | --- | --- |")
    for r in sorted(results, key=lambda x: -len(x["rfe_dropped_by_strat"])):
        if r["rfe_dropped_by_strat"] or r["strat_added_vs_rfe"]:
            dropped = ", ".join(r["rfe_dropped_by_strat"]) or "-"
            added = ", ".join(r["strat_added_vs_rfe"]) or "-"
            lines.append(f"| {r['strat_key']} | {r['rfe_key']} | {dropped} | {added} |")
    lines.append("")

    # STRAT → Epics gap analysis
    lines.append("## STRAT → Epics Gap: Components on Epics but Missing from STRAT")
    lines.append("")
    lines.append("| STRAT | Missing Components |")
    lines.append("| --- | --- |")
    for r in sorted(results, key=lambda x: -len(x["on_epics_only"])):
        if r["on_epics_only"]:
            lines.append(f"| {r['strat_key']} | {', '.join(r['on_epics_only'])} |")
    lines.append("")

    with open(md_path, "w") as f:
        f.write("\n".join(lines))
    print(f"Wrote {md_path}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Component analysis per STRAT")
    parser.add_argument("--pipeline-json", required=True,
                        help="Path to pipeline research JSON")
    parser.add_argument("--output", default="artifacts/component-analysis.json",
                        help="Output JSON path")
    args = parser.parse_args()

    server = os.environ["JIRA_SERVER"]
    user = os.environ["JIRA_USER"]
    token = os.environ["JIRA_TOKEN"]

    with open(args.pipeline_json) as f:
        data = json.load(f)

    strats = data["strats"]
    print(f"Analyzing {len(strats)} STRATs for component involvement...",
          file=sys.stderr)

    # Step 1: Collect all unique epic keys across all STRATs
    all_epic_keys = set()
    for s in strats:
        for e in s.get("epic_details", []):
            all_epic_keys.add(e["key"])
    print(f"Batch-fetching components for {len(all_epic_keys)} unique epics...",
          file=sys.stderr)

    # Step 2: Batch-fetch epic components
    epic_components_cache = batch_fetch_components(
        server, user, token, sorted(all_epic_keys))
    epics_with_comps = sum(1 for v in epic_components_cache.values() if v)
    print(f"  {epics_with_comps}/{len(all_epic_keys)} epics have components set",
          file=sys.stderr)

    # Step 3: Analyze each STRAT (includes fetching RFE + STRAT components)
    results = []
    for i, strat in enumerate(strats):
        key = strat["strat_key"]
        rfe_key = ""
        src = strat.get("source_rfe", {})
        if isinstance(src, dict):
            rfe_key = src.get("key", "")
        print(f"[{i+1}/{len(strats)}] {key} (RFE: {rfe_key or 'none'})...",
              file=sys.stderr)
        result = analyze_strat(server, user, token, strat, epic_components_cache)
        results.append(result)

    # Output JSON
    output = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pipeline_json": args.pipeline_json,
            "strat_count": len(results),
            "total_epics_checked": len(all_epic_keys),
            "epics_with_components": epics_with_comps,
        },
        "results": results,
    }
    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Wrote {args.output}", file=sys.stderr)

    # Render markdown
    render_markdown(results, args.output)

    # Print summary
    rfe_counts = [r["rfe_count"] for r in results]
    strat_counts = [r["strat_count"] for r in results]
    combined_counts = [r["combined_count"] for r in results]
    print(f"\n=== Component Analysis Summary ===", file=sys.stderr)
    print(f"STRATs analyzed:          {len(results)}", file=sys.stderr)
    print(f"RFE components (mean):    {sum(rfe_counts)/len(rfe_counts):.1f}", file=sys.stderr)
    print(f"STRAT components (mean):  {sum(strat_counts)/len(strat_counts):.1f}", file=sys.stderr)
    print(f"Combined (mean):          {sum(combined_counts)/len(combined_counts):.1f}", file=sys.stderr)
    print(f"Combined (median):        {sorted(combined_counts)[len(combined_counts)//2]}", file=sys.stderr)
    print(f"Combined (range):         {min(combined_counts)} – {max(combined_counts)}", file=sys.stderr)


if __name__ == "__main__":
    main()
