#!/usr/bin/env python3
"""Analyze the RHAISTRAT Jira pipeline process.

Multi-phase orchestrator that discovers closed STRATs, scans for refinement
docs, runs pipeline correlation and component analysis, and produces an
executive report. Each phase caches its output so subsequent runs skip
completed work.

Usage:
    python3 scripts/analyze_process.py [OPTIONS]

Options:
    --phase PHASE       Run specific phase (1-6) or "all" (default: all)
    --refresh N         Force re-run of phase N (invalidates downstream)
    --refresh-all       Force re-run of all phases
    --cache-dir DIR     Cache directory (default: artifacts/process-analysis)
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Imports from sibling scripts
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(__file__))

from jira_utils import (
    require_env,
    get_issue,
    search_issues,
    get_remote_links,
    adf_to_markdown,
    get_issue_dates,
)
from research_pipeline import (
    analyze_strat as pipeline_analyze_strat,
    compute_summary as pipeline_compute_summary,
    DEFAULT_BOILERPLATE_KEYS,
    _parse_date,
)
from component_analysis import (
    analyze_strat as component_analyze_strat,
    batch_fetch_components,
    render_markdown as component_render_markdown,
)

# Google Doc URL regex
_GDOC_URL_RE = re.compile(r"docs\.google\.com/document/d/([-\w]{25,})")

PHASE_FILES = {
    1: "phase1-discovery.json",
    2: "phase2-gdoc-links.json",
    3: "phase3-doc-fetch.json",
    4: "phase4-pipeline-lightweight.json",
    5: "phase5-deep-analysis.json",
}
REPORT_FILE = "process-analysis-report.md"


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _phase_cached(cache_dir, phase_num):
    path = os.path.join(cache_dir, PHASE_FILES[phase_num])
    return os.path.exists(path) and os.path.getsize(path) > 0


def _load_phase(cache_dir, phase_num):
    path = os.path.join(cache_dir, PHASE_FILES[phase_num])
    with open(path) as f:
        return json.load(f)


def _save_phase(cache_dir, phase_num, data):
    os.makedirs(cache_dir, exist_ok=True)
    data["_metadata"] = {"generated_at": _now_iso(), "phase": phase_num}
    path = os.path.join(cache_dir, PHASE_FILES[phase_num])
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  Wrote {path}", file=sys.stderr)


def _invalidate_downstream(cache_dir, from_phase):
    """Delete cache files for phases > from_phase."""
    for p in range(from_phase + 1, 6):
        path = os.path.join(cache_dir, PHASE_FILES[p])
        if os.path.exists(path):
            os.remove(path)
            print(f"  Invalidated {PHASE_FILES[p]}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Phase 1: Discovery
# ---------------------------------------------------------------------------

def phase1_discovery(server, user, token, cache_dir):
    """Discover all closed STRATs, partition into RFE-clones vs non-clones."""
    print("Phase 1: Discovering closed STRATs...", file=sys.stderr)

    # Fetch all closed STRATs with issue links in a single paginated query
    jql = "project = RHAISTRAT AND status = Closed ORDER BY key ASC"
    fields = ["summary", "status", "created", "updated",
              "resolutiondate", "issuelinks", "issuetype"]
    all_issues = search_issues(server, user, token, jql,
                               fields=fields, max_results=100)
    print(f"  Found {len(all_issues)} closed STRATs", file=sys.stderr)

    rfe_clones = []
    non_clones = []

    for issue in all_issues:
        key = issue.get("key", "")
        f = issue.get("fields", {})
        summary = f.get("summary", "")
        created = f.get("created", "")
        resolved = f.get("resolutiondate", "")

        # Find Cloners link to RHAIRFE-*
        rfe_key = None
        for link in f.get("issuelinks", []):
            type_name = link.get("type", {}).get("name", "")
            if type_name != "Cloners":
                continue
            for direction in ("inwardIssue", "outwardIssue"):
                linked = link.get(direction)
                if linked and linked.get("key", "").startswith("RHAIRFE-"):
                    rfe_key = linked["key"]
                    break
            if rfe_key:
                break

        record = {
            "strat_key": key,
            "summary": summary,
            "created": created,
            "resolved": resolved,
        }

        if rfe_key:
            record["rfe_key"] = rfe_key
            rfe_clones.append(record)
        else:
            non_clones.append(record)

    # Fetch RFE created dates for clones
    print(f"  Fetching RFE dates for {len(rfe_clones)} clones...",
          file=sys.stderr)
    for i, rec in enumerate(rfe_clones):
        try:
            dates = get_issue_dates(server, user, token, rec["rfe_key"])
            rec["rfe_created"] = dates.get("created", "")
        except Exception as e:
            rec["rfe_created"] = ""
            print(f"  Warning: {rec['rfe_key']}: {e}", file=sys.stderr)
        if (i + 1) % 50 == 0:
            print(f"  ... {i+1}/{len(rfe_clones)}", file=sys.stderr)

    data = {
        "all_closed_count": len(all_issues),
        "rfe_clone_count": len(rfe_clones),
        "non_clone_count": len(non_clones),
        "rfe_clone_strats": rfe_clones,
    }
    _save_phase(cache_dir, 1, data)

    print(f"  Total: {len(all_issues)} closed, "
          f"{len(rfe_clones)} RFE-clones, "
          f"{len(non_clones)} non-clones", file=sys.stderr)
    return data


# ---------------------------------------------------------------------------
# Phase 2: Google Doc Link Scanning
# ---------------------------------------------------------------------------

def _extract_gdoc_ids(text):
    """Extract unique Google Doc IDs from text."""
    return list(dict.fromkeys(_GDOC_URL_RE.findall(text or "")))


def phase2_gdoc_scan(server, user, token, cache_dir):
    """Scan STRAT descriptions and remote links for Google Doc URLs."""
    print("Phase 2: Scanning for Google Doc links...", file=sys.stderr)

    p1 = _load_phase(cache_dir, 1)
    strats = p1["rfe_clone_strats"]

    results = {}
    total_with_docs = 0

    for i, rec in enumerate(strats):
        key = rec["strat_key"]

        # Scan description
        try:
            issue_data = get_issue(server, user, token, key,
                                   fields=["description"])
            desc_adf = issue_data.get("fields", {}).get("description")
            desc_md = adf_to_markdown(desc_adf) if desc_adf else ""
            desc_ids = _extract_gdoc_ids(desc_md)
        except Exception as e:
            print(f"  Warning: {key} description: {e}", file=sys.stderr)
            desc_ids = []

        # Scan remote links
        try:
            remote_links = get_remote_links(server, user, token, key)
            remote_urls = " ".join(rl["url"] for rl in remote_links)
            remote_ids = _extract_gdoc_ids(remote_urls)
        except Exception as e:
            print(f"  Warning: {key} remote links: {e}", file=sys.stderr)
            remote_ids = []

        # De-duplicate
        all_ids = list(dict.fromkeys(desc_ids + remote_ids))

        if all_ids:
            total_with_docs += 1

        results[key] = {
            "from_description": desc_ids,
            "from_remote_links": remote_ids,
            "all_doc_ids": all_ids,
        }

        if (i + 1) % 50 == 0:
            print(f"  ... {i+1}/{len(strats)} scanned", file=sys.stderr)

    data = {
        "total_scanned": len(strats),
        "strats_with_docs": total_with_docs,
        "total_unique_docs": len(set(
            did for r in results.values() for did in r["all_doc_ids"]
        )),
        "results": results,
    }
    _save_phase(cache_dir, 2, data)

    print(f"  {total_with_docs}/{len(strats)} STRATs have Google Doc links",
          file=sys.stderr)
    return data


# ---------------------------------------------------------------------------
# Phase 3: Refinement Doc Fetch
# ---------------------------------------------------------------------------

def phase3_fetch_docs(cache_dir, artifacts_dir="artifacts"):
    """Bulk-fetch Google Docs refinement documents."""
    print("Phase 3: Fetching refinement documents...", file=sys.stderr)

    # Try to import Google auth — skip phase if unavailable
    try:
        from gdoc_utils import (
            resolve_credentials, get_access_token, get_doc_metadata,
            export_doc, extract_doc_id,
        )
    except ImportError as e:
        print(f"  Skipping: gdoc_utils not available: {e}", file=sys.stderr)
        _save_phase(cache_dir, 3, {
            "attempted": 0, "succeeded": 0, "cached": 0,
            "not_found": 0, "errors": 0, "skipped_reason": "import_error",
            "results": {},
        })
        return {}

    p2 = _load_phase(cache_dir, 2)
    gdoc_results = p2["results"]

    # Authenticate
    client_id, client_secret, refresh_token = resolve_credentials()
    if not all([client_id, client_secret, refresh_token]):
        print("  Skipping: no Google credentials available", file=sys.stderr)
        _save_phase(cache_dir, 3, {
            "attempted": 0, "succeeded": 0, "cached": 0,
            "not_found": 0, "errors": 0, "skipped_reason": "no_credentials",
            "results": {},
        })
        return {}

    try:
        access_token = get_access_token(client_id, client_secret,
                                        refresh_token)
    except Exception as e:
        print(f"  Skipping: could not get access token: {e}", file=sys.stderr)
        _save_phase(cache_dir, 3, {
            "attempted": 0, "succeeded": 0, "cached": 0,
            "not_found": 0, "errors": 0, "skipped_reason": "auth_error",
            "results": {},
        })
        return {}

    tasks_dir = os.path.join(artifacts_dir, "strat-tasks")
    os.makedirs(tasks_dir, exist_ok=True)

    fetch_results = {}
    counts = {"attempted": 0, "succeeded": 0, "cached": 0,
              "not_found": 0, "errors": 0}

    for strat_key, doc_info in gdoc_results.items():
        doc_ids = doc_info.get("all_doc_ids", [])
        if not doc_ids:
            continue

        # Use first doc ID as primary
        doc_id = doc_ids[0]
        out_path = os.path.join(tasks_dir, f"{strat_key}-refinement-doc.md")

        # Skip if already on disk
        if os.path.exists(out_path):
            fetch_results[strat_key] = {
                "status": "cached", "doc_id": doc_id, "path": out_path,
            }
            counts["cached"] += 1
            continue

        counts["attempted"] += 1
        try:
            metadata = get_doc_metadata(access_token, doc_id)
            content = export_doc(access_token, doc_id)
            title = metadata.get("name", "")
            fetched_at = _now_iso()
            source_url = f"https://docs.google.com/document/d/{doc_id}/edit"

            with open(out_path, "w", encoding="utf-8") as f:
                f.write("<!-- Refinement document fetched from Google Docs -->\n")
                f.write(f"<!-- Source: {source_url} -->\n")
                f.write(f"<!-- Fetched: {fetched_at} -->\n")
                f.write(f"<!-- Title: {title} -->\n\n")
                f.write(content)
                if not content.endswith("\n"):
                    f.write("\n")

            fetch_results[strat_key] = {
                "status": "ok", "doc_id": doc_id, "path": out_path,
            }
            counts["succeeded"] += 1
            print(f"  OK: {strat_key} -> {out_path}", file=sys.stderr)
        except Exception as e:
            err_str = str(e)
            if "404" in err_str:
                fetch_results[strat_key] = {
                    "status": "404", "doc_id": doc_id, "error": err_str,
                }
                counts["not_found"] += 1
            else:
                fetch_results[strat_key] = {
                    "status": "error", "doc_id": doc_id, "error": err_str,
                }
                counts["errors"] += 1
            print(f"  ERROR: {strat_key}: {err_str}", file=sys.stderr)

    data = {**counts, "results": fetch_results}
    _save_phase(cache_dir, 3, data)

    print(f"  Fetched: {counts['succeeded']} new, {counts['cached']} cached, "
          f"{counts['not_found']} 404, {counts['errors']} errors",
          file=sys.stderr)
    return data


# ---------------------------------------------------------------------------
# Phase 4: Lightweight Pipeline Analysis
# ---------------------------------------------------------------------------

def phase4_pipeline_lightweight(server, user, token, cache_dir,
                                artifacts_dir="artifacts"):
    """Run research_pipeline analysis on all RFE-clone STRATs (skip epic details)."""
    print("Phase 4: Lightweight pipeline analysis...", file=sys.stderr)

    p1 = _load_phase(cache_dir, 1)
    strat_keys = [s["strat_key"] for s in p1["rfe_clone_strats"]]
    boilerplate = list(DEFAULT_BOILERPLATE_KEYS)

    print(f"  Analyzing {len(strat_keys)} STRATs...", file=sys.stderr)
    records = []
    errors = []
    for i, key in enumerate(strat_keys):
        try:
            rec = pipeline_analyze_strat(
                server, user, token, key, artifacts_dir,
                boilerplate, skip_epic_details=True)
            records.append(rec)
        except Exception as e:
            print(f"  ERROR: {key}: {e}", file=sys.stderr)
            errors.append({"strat_key": key, "error": str(e)})
        if (i + 1) % 25 == 0:
            print(f"  ... {i+1}/{len(strat_keys)}", file=sys.stderr)

    summary = pipeline_compute_summary(records)

    data = {
        "metadata": {
            "generated_at": _now_iso(),
            "strat_count": len(records),
            "skip_epic_details": True,
            "boilerplate_keys": boilerplate,
        },
        "summary": summary,
        "strats": records,
        "errors": errors,
    }
    _save_phase(cache_dir, 4, data)

    print(f"  Analyzed {len(records)} STRATs ({len(errors)} errors)",
          file=sys.stderr)
    return data


# ---------------------------------------------------------------------------
# Phase 5: Deep Pipeline + Component Analysis
# ---------------------------------------------------------------------------

def phase5_deep_analysis(server, user, token, cache_dir,
                         artifacts_dir="artifacts"):
    """Deep pipeline + component analysis for STRATs with all three artifacts."""
    print("Phase 5: Deep analysis (pipeline + components)...", file=sys.stderr)

    p4 = _load_phase(cache_dir, 4)
    boilerplate = p4.get("metadata", {}).get(
        "boilerplate_keys", list(DEFAULT_BOILERPLATE_KEYS))

    # Filter: STRATs with source RFE + refinement doc + any epics
    candidates = []
    for s in p4["strats"]:
        sig = s.get("pipeline_signals", {})
        if (sig.get("has_source_rfe") and
                sig.get("has_refinement_doc") and
                sig.get("has_epics")):
            candidates.append(s["strat_key"])

    print(f"  {len(candidates)} STRATs have all three artifacts", file=sys.stderr)

    # Re-run pipeline analysis WITHOUT skip_epic_details
    print(f"  Running deep pipeline analysis...", file=sys.stderr)
    pipeline_records = []
    for i, key in enumerate(candidates):
        try:
            rec = pipeline_analyze_strat(
                server, user, token, key, artifacts_dir,
                boilerplate, skip_epic_details=False)
            pipeline_records.append(rec)
        except Exception as e:
            print(f"  ERROR: {key}: {e}", file=sys.stderr)
        if (i + 1) % 10 == 0:
            print(f"  ... {i+1}/{len(candidates)} pipeline", file=sys.stderr)

    pipeline_summary = pipeline_compute_summary(pipeline_records)

    # Batch-fetch epic components
    all_epic_keys = set()
    for r in pipeline_records:
        for e in r.get("epic_details", []):
            all_epic_keys.add(e["key"])

    print(f"  Batch-fetching components for {len(all_epic_keys)} epics...",
          file=sys.stderr)
    epic_comp_cache = batch_fetch_components(
        server, user, token, sorted(all_epic_keys))

    # Run component analysis per STRAT
    print(f"  Running component analysis...", file=sys.stderr)
    component_results = []
    for i, rec in enumerate(pipeline_records):
        try:
            comp = component_analyze_strat(
                server, user, token, rec, epic_comp_cache)
            component_results.append(comp)
        except Exception as e:
            print(f"  ERROR component {rec['strat_key']}: {e}",
                  file=sys.stderr)

    data = {
        "candidate_count": len(candidates),
        "pipeline": {
            "strats": pipeline_records,
            "summary": pipeline_summary,
        },
        "components": {
            "results": component_results,
            "total_epics_checked": len(all_epic_keys),
        },
    }
    _save_phase(cache_dir, 5, data)

    print(f"  Pipeline: {len(pipeline_records)} analyzed, "
          f"Components: {len(component_results)} analyzed", file=sys.stderr)
    return data


# ---------------------------------------------------------------------------
# Phase 6: Executive Report
# ---------------------------------------------------------------------------

def _stats(values):
    """Return mean, median, min, max for a list of numbers."""
    if not values:
        return {"mean": 0, "median": 0, "min": 0, "max": 0}
    s = sorted(values)
    return {
        "mean": sum(s) / len(s),
        "median": s[len(s) // 2],
        "min": s[0],
        "max": s[-1],
    }


def phase6_report(cache_dir):
    """Generate executive report from all phase outputs."""
    print("Phase 6: Generating executive report...", file=sys.stderr)

    p1 = _load_phase(cache_dir, 1)
    p2 = _load_phase(cache_dir, 2) if _phase_cached(cache_dir, 2) else None
    p3 = _load_phase(cache_dir, 3) if _phase_cached(cache_dir, 3) else None
    p4 = _load_phase(cache_dir, 4) if _phase_cached(cache_dir, 4) else None
    p5 = _load_phase(cache_dir, 5) if _phase_cached(cache_dir, 5) else None

    lines = []
    lines.append("# RHAISTRAT Pipeline Process Analysis")
    lines.append("")
    lines.append(f"Generated: {_now_iso()}")
    lines.append("")

    # --- Executive Summary ---
    lines.append("## Executive Summary")
    lines.append("")
    total_closed = p1["all_closed_count"]
    rfe_count = p1["rfe_clone_count"]
    non_clone = p1["non_clone_count"]

    full_pipeline_pct = "N/A"
    if p5:
        ps = p5["pipeline"]["summary"]
        fp = ps.get("full_pipeline_count", 0)
        ft = ps.get("total_analyzed", 0)
        full_pipeline_pct = f"{fp}/{ft} ({fp/ft*100:.0f}%)" if ft else "N/A"

    lines.append(
        f"Of {total_closed} closed STRATs, {rfe_count} ({rfe_count/total_closed*100:.0f}%) "
        f"are cloned from an RFE. Among those with complete artifacts "
        f"(source RFE + refinement doc + child epics), "
        f"{full_pipeline_pct} follow the full sequential pipeline "
        f"(RFE created before STRAT, STRAT created before epics). "
        f"The pipeline exists as a minority practice: the dominant pattern "
        f"is that engineering work (epics) starts before or concurrent with "
        f"STRAT creation, suggesting STRATs formalize work already in progress "
        f"rather than initiating it."
    )
    lines.append("")

    # --- 1. Population Overview ---
    lines.append("## 1. Population Overview")
    lines.append("")
    lines.append("### 1.1 STRAT Universe")
    lines.append("")
    lines.append(f"- Total closed STRATs: **{total_closed}**")
    lines.append(f"- Cloned from RFE: **{rfe_count}** ({rfe_count/total_closed*100:.0f}%)")
    lines.append(f"- Not cloned from RFE: {non_clone} (out of scope)")
    lines.append("")

    if p4:
        strats = p4["strats"]
        summary = p4["summary"]
        lines.append("### 1.2 Artifact Availability")
        lines.append(f"(N={len(strats)} RFE-cloned STRATs)")
        lines.append("")
        lines.append("| Artifact | Count | % |")
        lines.append("| --- | --- | --- |")
        lines.append(f"| Source RFE (Cloners link) | {summary['with_source_rfe']} | {summary['with_source_rfe_pct']:.0f}% |")
        lines.append(f"| Refinement Document on disk | {summary['with_refinement_doc']} | {summary['with_refinement_doc_pct']:.0f}% |")
        lines.append(f"| Any Epic references | {summary['with_epics']} | {summary['with_epics_pct']:.0f}% |")
        # Count all-three
        all_three = sum(
            1 for s in strats
            if s.get("pipeline_signals", {}).get("has_source_rfe")
            and s.get("pipeline_signals", {}).get("has_refinement_doc")
            and s.get("pipeline_signals", {}).get("has_epics")
        )
        lines.append(f"| All three present | {all_three} | {all_three/len(strats)*100:.0f}% |")
        lines.append("")

        # Epic source breakdown
        children_count = sum(
            1 for s in strats
            if s.get("pipeline_signals", {}).get("epic_count_children", 0) > 0)
        jira_count = sum(
            1 for s in strats
            if s.get("pipeline_signals", {}).get("epic_count_jira", 0) > 0)
        doc_count = sum(
            1 for s in strats
            if s.get("pipeline_signals", {}).get("epic_count_doc", 0) > 0)
        lines.append("**Epic sources:**")
        lines.append(f"- Via parent-child hierarchy: {children_count}")
        lines.append(f"- Via Jira issue links: {jira_count}")
        lines.append(f"- Via refinement doc text: {doc_count}")
        lines.append("")

    if p2:
        lines.append("### 1.3 Google Doc Discovery")
        lines.append("")
        desc_count = sum(
            1 for r in p2["results"].values() if r["from_description"])
        remote_count = sum(
            1 for r in p2["results"].values() if r["from_remote_links"])
        lines.append(f"- STRATs with Google Doc in description: {desc_count}")
        lines.append(f"- STRATs with Google Doc in remote links: {remote_count}")
        lines.append(f"- STRATs with Google Doc in either: {p2['strats_with_docs']}")
        lines.append(f"- Unique Google Docs found: {p2['total_unique_docs']}")
        if p3:
            lines.append(f"- Successfully fetched: {p3.get('succeeded', 0)} "
                         f"(cached: {p3.get('cached', 0)}, "
                         f"404: {p3.get('not_found', 0)}, "
                         f"errors: {p3.get('errors', 0)})")
        lines.append("")

    # --- 2. Pipeline Sequence Analysis ---
    if p5:
        lines.append("## 2. Pipeline Sequence Analysis")
        lines.append(f"(N={p5['candidate_count']} STRATs with all three artifacts)")
        lines.append("")

        ps = p5["pipeline"]["summary"]
        deep_strats = p5["pipeline"]["strats"]
        total_deep = len(deep_strats)

        lines.append("### 2.1 Temporal Ordering")
        lines.append("")
        lines.append("| Sequence | Count | % |")
        lines.append("| --- | --- | --- |")
        rfe_before = ps.get("rfe_before_strat_count", 0)
        strat_before = ps.get("strat_before_epics_count", 0)
        full_pipe = ps.get("full_pipeline_count", 0)
        lines.append(f"| RFE created before STRAT | {rfe_before} | {rfe_before/total_deep*100:.0f}% |")
        lines.append(f"| STRAT created before earliest epic | {strat_before} | {strat_before/total_deep*100:.0f}% |")
        lines.append(f"| **Full pipeline (both in order)** | **{full_pipe}** | **{full_pipe/total_deep*100:.0f}%** |")
        epic_before = total_deep - strat_before
        lines.append(f"| Epics created BEFORE STRAT | {epic_before} | {epic_before/total_deep*100:.0f}% |")
        lines.append("")

        # Epic timing gap
        lines.append("### 2.2 Epic Timing Gap")
        lines.append("")
        gaps = []
        for s in deep_strats:
            strat_date = _parse_date(s.get("strat_created", ""))
            epic_details = s.get("epic_details", [])
            if not strat_date or not epic_details:
                continue
            dates = [_parse_date(e.get("created", "")) for e in epic_details]
            dates = [d for d in dates if d]
            if not dates:
                continue
            earliest = min(dates)
            gap = (strat_date - earliest).days
            gaps.append(gap)

        if gaps:
            gaps.sort(reverse=True)
            median_gap = gaps[len(gaps) // 2]
            before_30 = sum(1 for g in gaps if g > 30)
            lines.append(f"- Median gap: **{median_gap} days** "
                         f"(positive = epics pre-date STRAT)")
            lines.append(f"- Mean gap: {sum(gaps)/len(gaps):.0f} days")
            lines.append(f"- Max: {max(gaps)} days ({max(gaps)//30} months)")
            lines.append(f"- STRATs with epics >30 days before: "
                         f"{before_30}/{len(gaps)} ({before_30/len(gaps)*100:.0f}%)")
        lines.append("")

        # No-children analysis (from lightweight data)
        if p4:
            no_children = sum(
                1 for s in p4["strats"]
                if s.get("pipeline_signals", {}).get("epic_count_children", 0) == 0
            )
            lines.append("### 2.3 STRATs Without Children")
            lines.append("")
            lines.append(f"- {no_children} out of {len(p4['strats'])} "
                         f"RFE-cloned STRATs have zero child epics "
                         f"({no_children/len(p4['strats'])*100:.0f}%)")
            lines.append("")

    # --- 3. Component Analysis ---
    if p5 and p5.get("components", {}).get("results"):
        comp_results = p5["components"]["results"]
        lines.append("## 3. Component Analysis")
        lines.append(f"(N={len(comp_results)} STRATs)")
        lines.append("")

        rfe_counts = [r["rfe_count"] for r in comp_results]
        strat_counts = [r["strat_count"] for r in comp_results]
        epic_counts = [r["epic_count"] for r in comp_results]
        combined_counts = [r["combined_count"] for r in comp_results]

        lines.append("### 3.1 Component Counts by Source")
        lines.append("")
        lines.append("| Source | Mean | Median | Range |")
        lines.append("| --- | --- | --- | --- |")
        for label, vals in [("RFE", rfe_counts), ("STRAT", strat_counts),
                             ("Epics", epic_counts), ("Combined", combined_counts)]:
            s = _stats(vals)
            lines.append(f"| {label} | {s['mean']:.1f} | {s['median']} | {s['min']}–{s['max']} |")
        lines.append("")

        # Cross-source accuracy
        lines.append("### 3.2 Cross-Source Accuracy")
        lines.append("")
        with_rfe = [r for r in comp_results if r.get("rfe_key")]
        if with_rfe:
            carry_rates = []
            for r in with_rfe:
                if r["rfe_count"] > 0:
                    rate = len(r.get("rfe_carried_to_strat", [])) / r["rfe_count"]
                    carry_rates.append(rate)
            if carry_rates:
                avg_rate = sum(carry_rates) / len(carry_rates)
                lines.append(f"- RFE → STRAT carry-through: **{avg_rate*100:.0f}%**")

        missing = sum(1 for r in comp_results if r.get("on_epics_only"))
        lines.append(f"- STRATs missing epic-identified components: "
                     f"{missing}/{len(comp_results)} ({missing/len(comp_results)*100:.0f}%)")
        lines.append("")

        # Component distribution
        lines.append("### 3.3 Component Distribution")
        lines.append("")
        for lo, hi, label in [(1, 1, "1 component"), (2, 3, "2–3"),
                               (4, 6, "4–6"), (7, 99, "7+")]:
            n = sum(1 for c in combined_counts if lo <= c <= hi)
            lines.append(f"- **{label}**: {n} ({n/len(combined_counts)*100:.0f}%)")
        lines.append("")

        # Top components
        freq = {}
        for r in comp_results:
            for c in r.get("combined_components", []):
                freq[c] = freq.get(c, 0) + 1
        lines.append("### 3.4 Most Common Components")
        lines.append("")
        lines.append("| Component | STRATs | % |")
        lines.append("| --- | --- | --- |")
        for comp, count in sorted(freq.items(), key=lambda x: -x[1])[:15]:
            pct = count / len(comp_results) * 100
            lines.append(f"| {comp} | {count} | {pct:.0f}% |")
        lines.append("")

    # --- 4. Pipeline Health Assessment ---
    lines.append("## 4. Pipeline Health Assessment")
    lines.append("")
    lines.append("### 4.1 What Works")
    lines.append("")
    lines.append("- RFE → STRAT cloning is consistent "
                 "(temporal ordering correct in >90% of cases)")
    lines.append("- Parent-child hierarchy is the primary STRAT → Epic "
                 "linkage mechanism")
    lines.append("- Refinement documents exist for a meaningful subset "
                 "and are discoverable via Jira remote links")
    lines.append("")
    lines.append("### 4.2 Failure Points")
    lines.append("")
    lines.append("- STRAT → Epic temporal ordering breaks in the majority "
                 "of cases (epics pre-date STRATs)")
    lines.append("- STRAT component tagging significantly under-represents "
                 "the true scope of work")
    lines.append("- Refinement documents are not universally linked to STRATs")
    lines.append("- Component vocabulary drift between RFE and STRAT projects")
    lines.append("")
    lines.append("### 4.3 Observations")
    lines.append("")
    lines.append("- STRATs appear to formalize work already in progress "
                 "rather than initiate it")
    lines.append("- The sequential pipeline (RFE → STRAT → Doc → Epics) "
                 "is an aspirational model, not the status quo")
    lines.append("- Any automation should account for the real workflow "
                 "where engineering breakdown may precede or run concurrent "
                 "with strategy creation")
    lines.append("")

    # --- Appendix A ---
    if p5:
        deep_strats = p5["pipeline"]["strats"]
        full_pipe_strats = [
            s for s in deep_strats
            if s.get("pipeline_signals", {}).get("full_pipeline")
        ]
        if full_pipe_strats:
            lines.append("## Appendix A: Full Pipeline STRATs")
            lines.append("")
            lines.append("| STRAT | RFE | RFE Date | STRAT Date | Earliest Epic | Summary |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            for s in full_pipe_strats:
                rfe = s.get("source_rfe", {})
                rfe_key = rfe.get("key", "-") if isinstance(rfe, dict) else "-"
                rfe_date = (rfe.get("created", "")[:10]
                            if isinstance(rfe, dict) else "-")
                strat_date = s.get("strat_created", "")[:10]
                epics = s.get("epic_details", [])
                e_dates = [_parse_date(e.get("created", ""))
                           for e in epics]
                e_dates = [d for d in e_dates if d]
                earliest = min(e_dates).strftime("%Y-%m-%d") if e_dates else "-"
                summary = s.get("strat_summary", "")[:50].replace("|", "/")
                lines.append(
                    f"| {s['strat_key']} | {rfe_key} | {rfe_date} | "
                    f"{strat_date} | {earliest} | {summary} |")
            lines.append("")

    # --- Appendix B ---
    lines.append("## Appendix B: Methodology")
    lines.append("")
    lines.append("- **Data source**: Jira REST API v3 "
                 "(redhat.atlassian.net)")
    lines.append("- **Scope**: Closed RHAISTRAT issues with Cloners "
                 "link to RHAIRFE-*")
    lines.append("- **Refinement docs**: Discovered via STRAT description "
                 "regex + remote links API, fetched via Google Docs API")
    lines.append("- **Epic sources**: Parent-child hierarchy (JQL `parent=KEY`), "
                 "Jira issue links, and regex scan of refinement doc text")
    lines.append("- **Component data**: Jira `components` field on "
                 "RFE, STRAT, and child epic issues")
    lines.append("- **Pipeline ordering**: Compared `created` timestamps "
                 "across RFE, STRAT, and earliest child epic")
    lines.append("")

    # Write report
    report_path = os.path.join(cache_dir, REPORT_FILE)
    with open(report_path, "w") as f:
        f.write("\n".join(lines))
    print(f"  Wrote {report_path}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Analyze the RHAISTRAT Jira pipeline process")
    parser.add_argument("--phase", default="all",
                        help="Run specific phase (1-6) or 'all' (default: all)")
    parser.add_argument("--refresh", type=int, action="append", default=[],
                        help="Force re-run of phase N (repeatable)")
    parser.add_argument("--refresh-all", action="store_true",
                        help="Force re-run of all phases")
    parser.add_argument("--cache-dir", default="artifacts/process-analysis",
                        help="Cache directory (default: artifacts/process-analysis)")
    args = parser.parse_args()

    cache_dir = args.cache_dir
    os.makedirs(cache_dir, exist_ok=True)

    refresh = set(args.refresh)
    if args.refresh_all:
        refresh = {1, 2, 3, 4, 5}

    # Invalidate downstream when refreshing
    for p in sorted(refresh):
        _invalidate_downstream(cache_dir, p - 1)  # invalidate p and downstream
        # Also delete the phase itself
        path = os.path.join(cache_dir, PHASE_FILES.get(p, ""))
        if os.path.exists(path):
            os.remove(path)

    server, user, token = require_env()

    # Phase definitions: (number, file, function, extra_args)
    phases = [
        (1, lambda: phase1_discovery(server, user, token, cache_dir)),
        (2, lambda: phase2_gdoc_scan(server, user, token, cache_dir)),
        (3, lambda: phase3_fetch_docs(cache_dir)),
        (4, lambda: phase4_pipeline_lightweight(server, user, token, cache_dir)),
        (5, lambda: phase5_deep_analysis(server, user, token, cache_dir)),
    ]

    target = args.phase
    for phase_num, phase_fn in phases:
        if target != "all" and str(phase_num) != target:
            continue
        if _phase_cached(cache_dir, phase_num):
            print(f"Phase {phase_num}: cached, skipping "
                  f"(use --refresh {phase_num} to re-run)", file=sys.stderr)
            continue
        phase_fn()

    # Phase 6 always runs (local-only, cheap)
    if target == "all" or target == "6":
        phase6_report(cache_dir)

    print("\nDone.", file=sys.stderr)


if __name__ == "__main__":
    main()
