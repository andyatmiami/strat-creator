#!/usr/bin/env python3
"""Research script: validate the RFE -> STRAT -> Refinement Doc -> Epics pipeline.

Correlates data across Jira (RHAIRFE, RHAISTRAT, RHOAIENG projects),
local refinement doc artifacts, and issue link metadata to determine
whether a sequential pipeline is practiced today.

Usage:
    # Analyze specific STRATs (e.g. the 15 with refinement docs)
    python3 scripts/research_pipeline.py --strat-keys RHAISTRAT-122 RHAISTRAT-149

    # Analyze all closed STRATs
    python3 scripts/research_pipeline.py --all-closed

    # Auto-detect from existing refinement doc files
    python3 scripts/research_pipeline.py

    # Skip Epic detail lookups (faster, less API calls)
    python3 scripts/research_pipeline.py --all-closed --skip-epic-details

Output:
    artifacts/pipeline-research.json       Structured data
    artifacts/pipeline-research-summary.md Human-readable report

Environment variables:
    JIRA_SERVER  Jira server URL
    JIRA_USER    Jira username/email
    JIRA_TOKEN   Jira API token

Exit codes:
    0  Success
    1  Error
    2  Missing credentials
"""

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

from jira_utils import (
    get_issue,
    require_env,
    search_issues,
)

# Boilerplate RHOAIENG keys from the refinement doc template's
# "ODH/RHOAI build process Onboarding" section.
DEFAULT_BOILERPLATE_KEYS = {"RHOAIENG-31244", "RHOAIENG-31290", "RHOAIENG-31303"}

_RHOAIENG_RE = re.compile(r"RHOAIENG-\d+")


# ─── Data Collection ────────────────────────────────────────────────────────

def collect_strat_data(server, user, token, strat_key):
    """Fetch STRAT data from Jira: metadata, links, and dates.

    Returns a dict with strat metadata, or None on error.
    """
    try:
        data = get_issue(server, user, token, strat_key,
                         fields=["summary", "status", "created", "updated",
                                 "resolutiondate", "issuelinks", "issuetype"])
    except Exception as e:
        print(f"  ERROR fetching {strat_key}: {e}", file=sys.stderr)
        return None

    fields = data.get("fields", {})

    # Parse issue links
    cloners_rfe = None
    epic_links = []
    other_links = []
    for link in fields.get("issuelinks", []):
        type_name = link.get("type", {}).get("name", "")
        if "outwardIssue" in link:
            linked = link["outwardIssue"]
            direction = "outward"
        elif "inwardIssue" in link:
            linked = link["inwardIssue"]
            direction = "inward"
        else:
            continue

        linked_key = linked.get("key", "")
        linked_info = {
            "key": linked_key,
            "type_name": type_name,
            "direction": direction,
            "summary": linked.get("fields", {}).get("summary", ""),
            "status": linked.get("fields", {}).get("status", {}).get("name", ""),
        }

        if type_name == "Cloners" and linked_key.startswith("RHAIRFE-"):
            cloners_rfe = linked_info
        elif linked_key.startswith("RHOAIENG-"):
            epic_links.append(linked_info)
        else:
            other_links.append(linked_info)

    return {
        "strat_key": strat_key,
        "summary": fields.get("summary", ""),
        "status": fields.get("status", {}).get("name", ""),
        "issue_type": fields.get("issuetype", {}).get("name", ""),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "resolved": fields.get("resolutiondate"),
        "cloners_rfe": cloners_rfe,
        "epic_links": epic_links,
        "other_links": other_links,
    }


def collect_rfe_dates(server, user, token, rfe_key):
    """Fetch RFE creation date from Jira."""
    try:
        data = get_issue(server, user, token, rfe_key,
                         fields=["created", "updated", "resolutiondate"])
        fields = data.get("fields", {})
        return {
            "key": rfe_key,
            "created": fields.get("created"),
            "resolved": fields.get("resolutiondate"),
        }
    except Exception as e:
        print(f"  ERROR fetching RFE {rfe_key}: {e}", file=sys.stderr)
        return {"key": rfe_key, "created": None, "resolved": None}


def parse_refinement_doc(artifacts_dir, strat_key, boilerplate_keys):
    """Parse a refinement doc artifact for RHOAIENG references.

    Returns dict with exists, epic_refs, boilerplate_refs, fetched_at.
    """
    path = os.path.join(artifacts_dir, "strat-tasks",
                        f"{strat_key}-refinement-doc.md")
    if not os.path.exists(path):
        return {"exists": False, "epic_refs": [], "boilerplate_refs": [],
                "fetched_at": None}

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # Extract fetch timestamp from HTML comment
    fetched_match = re.search(r"<!--\s*Fetched:\s*(\S+)\s*-->", content)
    fetched_at = fetched_match.group(1) if fetched_match else None

    # Find all RHOAIENG references
    all_refs = set(_RHOAIENG_RE.findall(content))
    boilerplate = all_refs & boilerplate_keys
    epic_refs = sorted(all_refs - boilerplate_keys)
    boilerplate_refs = sorted(boilerplate)

    return {
        "exists": True,
        "epic_refs": epic_refs,
        "boilerplate_refs": boilerplate_refs,
        "fetched_at": fetched_at,
    }


def collect_child_issues(server, user, token, strat_key):
    """Fetch child work items of a STRAT via JQL parent = key.

    Returns list of dicts with key, summary, type, status, created.
    """
    try:
        issues = search_issues(
            server, user, token,
            f"parent = {strat_key}",
            fields=["summary", "status", "created", "issuetype"],
            max_results=100,
        )
        return [
            {
                "key": i.get("key", ""),
                "summary": i.get("fields", {}).get("summary", ""),
                "type": i.get("fields", {}).get("issuetype", {}).get("name", ""),
                "status": i.get("fields", {}).get("status", {}).get("name", ""),
                "created": i.get("fields", {}).get("created"),
            }
            for i in issues
        ]
    except Exception as e:
        print(f"  ERROR fetching children of {strat_key}: {e}",
              file=sys.stderr)
        return []


def collect_epic_details(server, user, token, epic_keys):
    """Batch-fetch Epic details via JQL key in (...).

    Returns dict mapping epic_key -> {key, created, type, status}.
    """
    if not epic_keys:
        return {}

    # JQL key in (...) supports ~100 keys per query
    results = {}
    batch_size = 80
    keys_list = sorted(epic_keys)

    for i in range(0, len(keys_list), batch_size):
        batch = keys_list[i:i + batch_size]
        key_list = ", ".join(batch)
        jql = f"key in ({key_list})"
        try:
            issues = search_issues(
                server, user, token, jql,
                fields=["summary", "status", "created", "issuetype"],
                max_results=100,
            )
            for issue in issues:
                key = issue.get("key", "")
                fields = issue.get("fields", {})
                results[key] = {
                    "key": key,
                    "summary": fields.get("summary", ""),
                    "created": fields.get("created"),
                    "type": fields.get("issuetype", {}).get("name", ""),
                    "status": fields.get("status", {}).get("name", ""),
                }
        except Exception as e:
            print(f"  ERROR batch-fetching epics: {e}", file=sys.stderr)

    return results


# ─── Analysis ────────────────────────────────────────────────────────────────

def _parse_date(iso_str):
    """Parse ISO date string to datetime, or None."""
    if not iso_str:
        return None
    try:
        # Handle Jira's format: 2025-01-15T10:30:00.000+0000
        cleaned = re.sub(r"(\d{2})(\d{2})$", r"\1:\2", iso_str)
        return datetime.fromisoformat(cleaned)
    except (ValueError, TypeError):
        return None


def compute_pipeline_signals(record):
    """Compute pipeline presence/ordering signals for a STRAT record."""
    has_rfe = record.get("source_rfe", {}).get("created") is not None
    has_doc = record.get("refinement_doc", {}).get("exists", False)

    epics_children = set(record.get("epics_from_children", []))
    epics_jira = set(record.get("epics_from_jira", []))
    epics_doc = set(record.get("epics_from_doc", []))
    all_epics = epics_children | epics_jira | epics_doc
    has_epics = bool(all_epics)

    # Temporal ordering
    rfe_created = _parse_date(
        record.get("source_rfe", {}).get("created"))
    strat_created = _parse_date(record.get("strat_created"))

    rfe_before_strat = None
    if rfe_created and strat_created:
        rfe_before_strat = rfe_created <= strat_created

    # Find earliest epic creation
    earliest_epic = None
    for detail in record.get("epic_details", []):
        epic_dt = _parse_date(detail.get("created"))
        if epic_dt and (earliest_epic is None or epic_dt < earliest_epic):
            earliest_epic = epic_dt

    strat_before_epics = None
    if strat_created and earliest_epic:
        strat_before_epics = strat_created <= earliest_epic

    full_pipeline = (has_rfe and has_doc and has_epics
                     and rfe_before_strat is True
                     and strat_before_epics is True)

    return {
        "has_source_rfe": has_rfe,
        "has_refinement_doc": has_doc,
        "has_epics": has_epics,
        "epic_count_children": len(epics_children),
        "epic_count_jira": len(epics_jira),
        "epic_count_doc": len(epics_doc),
        "epic_count_total": len(all_epics),
        "rfe_before_strat": rfe_before_strat,
        "strat_before_epics": strat_before_epics,
        "full_pipeline": full_pipeline,
    }


# ─── Main Pipeline ──────────────────────────────────────────────────────────

def analyze_strat(server, user, token, strat_key, artifacts_dir,
                  boilerplate_keys, skip_epic_details=False):
    """Full analysis for a single STRAT. Returns a record dict."""
    print(f"Analyzing {strat_key}...", file=sys.stderr)

    # 1. STRAT data from Jira
    strat_data = collect_strat_data(server, user, token, strat_key)
    if strat_data is None:
        return {"strat_key": strat_key, "error": "Failed to fetch from Jira"}

    # 2. Refinement doc
    doc_data = parse_refinement_doc(artifacts_dir, strat_key, boilerplate_keys)

    # 3. Source RFE dates
    rfe_data = {"key": None, "created": None, "resolved": None}
    if strat_data["cloners_rfe"]:
        rfe_key = strat_data["cloners_rfe"]["key"]
        rfe_data = collect_rfe_dates(server, user, token, rfe_key)

    # 4. Child work items (parent-child hierarchy)
    children = collect_child_issues(server, user, token, strat_key)
    epics_from_children = [c["key"] for c in children]

    # 5. Collect all RHOAIENG keys from all three sources
    epics_from_jira = [l["key"] for l in strat_data["epic_links"]]
    epics_from_doc = doc_data.get("epic_refs", [])
    all_epic_keys = (set(epics_from_jira) | set(epics_from_doc)
                     | set(epics_from_children))

    # 6. Epic details (optional)
    epic_details = []
    if not skip_epic_details and all_epic_keys:
        # Children already have details; only batch-fetch the rest
        children_map = {c["key"]: c for c in children}
        remaining = all_epic_keys - set(children_map.keys())
        details_map = collect_epic_details(
            server, user, token, remaining)
        details_map.update(children_map)
        for key in sorted(all_epic_keys):
            detail = details_map.get(key, {"key": key, "created": None,
                                           "type": "unknown", "status": "unknown"})
            source = []
            if key in set(epics_from_children):
                source.append("child")
            if key in set(epics_from_jira):
                source.append("link")
            if key in set(epics_from_doc):
                source.append("doc")
            detail["source"] = "+".join(source) if source else "unknown"
            epic_details.append(detail)

    record = {
        "strat_key": strat_key,
        "strat_summary": strat_data["summary"],
        "strat_status": strat_data["status"],
        "strat_created": strat_data["created"],
        "strat_resolved": strat_data["resolved"],
        "source_rfe": rfe_data,
        "refinement_doc": doc_data,
        "epics_from_children": epics_from_children,
        "epics_from_jira": epics_from_jira,
        "epics_from_doc": epics_from_doc,
        "epic_details": epic_details,
        "other_links": strat_data["other_links"],
    }
    record["pipeline_signals"] = compute_pipeline_signals(record)
    return record


def compute_summary(records):
    """Compute aggregate statistics from all STRAT records."""
    total = len(records)
    errored = sum(1 for r in records if "error" in r)
    valid = [r for r in records if "error" not in r]
    n = len(valid)

    with_rfe = sum(1 for r in valid
                   if r["pipeline_signals"]["has_source_rfe"])
    with_doc = sum(1 for r in valid
                   if r["pipeline_signals"]["has_refinement_doc"])
    with_epics = sum(1 for r in valid
                     if r["pipeline_signals"]["has_epics"])
    with_child_epics = sum(1 for r in valid
                           if r["pipeline_signals"]["epic_count_children"] > 0)
    with_jira_epics = sum(1 for r in valid
                          if r["pipeline_signals"]["epic_count_jira"] > 0)
    with_doc_epics = sum(1 for r in valid
                         if r["pipeline_signals"]["epic_count_doc"] > 0)

    rfe_ordered = sum(1 for r in valid
                      if r["pipeline_signals"]["rfe_before_strat"] is True)
    epic_ordered = sum(1 for r in valid
                       if r["pipeline_signals"]["strat_before_epics"] is True)
    full_pipeline = sum(1 for r in valid
                        if r["pipeline_signals"]["full_pipeline"])

    def pct(count, denom):
        return round(count / denom * 100, 1) if denom > 0 else 0

    return {
        "total_analyzed": total,
        "errors": errored,
        "valid": n,
        "with_source_rfe": with_rfe,
        "with_source_rfe_pct": pct(with_rfe, n),
        "with_refinement_doc": with_doc,
        "with_refinement_doc_pct": pct(with_doc, n),
        "with_any_epics": with_epics,
        "with_any_epics_pct": pct(with_epics, n),
        "with_child_epics": with_child_epics,
        "with_jira_epic_links": with_jira_epics,
        "with_doc_epic_refs": with_doc_epics,
        "rfe_before_strat": rfe_ordered,
        "rfe_before_strat_pct": pct(rfe_ordered, n),
        "strat_before_epics": epic_ordered,
        "strat_before_epics_pct": pct(epic_ordered, n),
        "full_pipeline": full_pipeline,
        "full_pipeline_pct": pct(full_pipeline, n),
    }


# ─── Output Rendering ───────────────────────────────────────────────────────

def _short_date(iso_str):
    """Format ISO date to YYYY-MM-DD, or '-'."""
    if not iso_str:
        return "-"
    return iso_str[:10]


def render_markdown_summary(records, summary, output_path):
    """Write a human-readable markdown report."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    valid = [r for r in records if "error" not in r]

    lines = [
        "# Pipeline Research: RFE -> STRAT -> Refinement Doc -> Epics",
        "",
        f"Generated: {ts}",
        "",
        "## Executive Summary",
        "",
        f"- **{summary['valid']}** STRATs analyzed"
        f" ({summary['errors']} errors skipped)",
        f"- **{summary['with_source_rfe']}** ({summary['with_source_rfe_pct']}%)"
        f" have a source RFE linked via Cloners",
        f"- **{summary['with_refinement_doc']}** ({summary['with_refinement_doc_pct']}%)"
        f" have a refinement doc on disk",
        f"- **{summary['with_any_epics']}** ({summary['with_any_epics_pct']}%)"
        f" have Epic references (children, Jira links, and/or refinement doc)",
        f"  - {summary['with_child_epics']} via parent-child hierarchy,"
        f" {summary['with_jira_epic_links']} via Jira issue links,"
        f" {summary['with_doc_epic_refs']} via refinement doc text",
        f"- **{summary['full_pipeline']}** ({summary['full_pipeline_pct']}%)"
        f" follow the full pipeline with valid temporal ordering",
        "",
        "## Pipeline Coverage",
        "",
        "| STRAT | Status | Source RFE | Ref Doc | Children | Links | Doc Refs"
        " | Full Pipeline |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    for r in sorted(valid, key=lambda x: x["strat_key"]):
        sig = r["pipeline_signals"]
        rfe = r["source_rfe"]["key"] or "-"
        doc = "Yes" if sig["has_refinement_doc"] else "No"
        ec = str(sig["epic_count_children"])
        ej = str(sig["epic_count_jira"])
        ed = str(sig["epic_count_doc"])
        full = "Yes" if sig["full_pipeline"] else "No"
        lines.append(
            f"| {r['strat_key']} | {r['strat_status']} | {rfe} | {doc}"
            f" | {ec} | {ej} | {ed} | {full} |"
        )

    lines += [
        "",
        "## Temporal Sequence",
        "",
        "| STRAT | RFE Created | STRAT Created | Earliest Epic | RFE<STRAT"
        " | STRAT<Epics |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    for r in sorted(valid, key=lambda x: x["strat_key"]):
        sig = r["pipeline_signals"]
        rfe_dt = _short_date(r["source_rfe"].get("created"))
        strat_dt = _short_date(r.get("strat_created"))

        earliest = "-"
        for d in r.get("epic_details", []):
            if d.get("created"):
                if earliest == "-" or d["created"][:10] < earliest:
                    earliest = d["created"][:10]

        rfe_ord = {True: "Yes", False: "NO", None: "-"}[sig["rfe_before_strat"]]
        epic_ord = {True: "Yes", False: "NO", None: "-"}[sig["strat_before_epics"]]
        lines.append(
            f"| {r['strat_key']} | {rfe_dt} | {strat_dt} | {earliest}"
            f" | {rfe_ord} | {epic_ord} |"
        )

    # Epic provenance detail
    has_details = any(r.get("epic_details") for r in valid)
    if has_details:
        lines += [
            "",
            "## Epic Details",
            "",
            "| STRAT | Epic Key | Type | Status | Created | Source |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for r in sorted(valid, key=lambda x: x["strat_key"]):
            for d in r.get("epic_details", []):
                lines.append(
                    f"| {r['strat_key']} | {d['key']} | {d.get('type', '-')}"
                    f" | {d.get('status', '-')} | {_short_date(d.get('created'))}"
                    f" | {d.get('source', '-')} |"
                )

    lines.append("")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Wrote {output_path}", file=sys.stderr)


# ─── STRAT Key Discovery ────────────────────────────────────────────────────

def discover_strat_keys_from_artifacts(artifacts_dir):
    """Find STRAT keys from existing refinement doc files."""
    pattern = os.path.join(artifacts_dir, "strat-tasks",
                           "RHAISTRAT-*-refinement-doc.md")
    keys = []
    for path in sorted(glob.glob(pattern)):
        basename = os.path.basename(path)
        match = re.match(r"(RHAISTRAT-\d+)-refinement-doc\.md", basename)
        if match:
            keys.append(match.group(1))
    return keys


def discover_all_closed(server, user, token):
    """Find all closed RHAISTRAT keys via JQL."""
    jql = "project = RHAISTRAT AND status = Closed ORDER BY key ASC"
    print("Querying all closed STRATs...", file=sys.stderr)
    issues = search_issues(
        server, user, token, jql,
        fields=["summary"],
        max_results=50,
    )
    keys = [i["key"] for i in issues]
    print(f"  Found {len(keys)} closed STRATs", file=sys.stderr)
    return keys


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--strat-keys", nargs="+", metavar="KEY",
        help="Specific RHAISTRAT keys to analyze",
    )
    parser.add_argument(
        "--all-closed", action="store_true",
        help="Query Jira for all closed STRATs and analyze each",
    )
    parser.add_argument(
        "--artifacts-dir", default="artifacts",
        help="Directory containing strat-tasks/ artifacts (default: artifacts)",
    )
    parser.add_argument(
        "--output", default="artifacts/pipeline-research.json",
        help="Path for JSON output (default: artifacts/pipeline-research.json)",
    )
    parser.add_argument(
        "--skip-epic-details", action="store_true",
        help="Skip batch-fetching Epic metadata (faster, fewer API calls)",
    )
    parser.add_argument(
        "--boilerplate-keys", nargs="*",
        default=list(DEFAULT_BOILERPLATE_KEYS),
        help="RHOAIENG keys to filter as boilerplate template refs",
    )
    args = parser.parse_args()

    server, user, token = require_env()
    if not all([server, user, token]):
        print(
            "Error: Jira credentials not found.\n"
            "Set JIRA_SERVER, JIRA_USER, JIRA_TOKEN environment variables.",
            file=sys.stderr,
        )
        sys.exit(2)

    boilerplate = set(args.boilerplate_keys)

    # Determine which STRATs to analyze
    if args.strat_keys:
        strat_keys = args.strat_keys
    elif args.all_closed:
        strat_keys = discover_all_closed(server, user, token)
    else:
        strat_keys = discover_strat_keys_from_artifacts(args.artifacts_dir)
        if not strat_keys:
            print("No STRAT keys found. Use --strat-keys or --all-closed.",
                  file=sys.stderr)
            sys.exit(1)

    print(f"Analyzing {len(strat_keys)} STRATs...", file=sys.stderr)

    # Analyze each STRAT
    records = []
    for i, key in enumerate(strat_keys):
        print(f"[{i + 1}/{len(strat_keys)}] ", end="", file=sys.stderr)
        record = analyze_strat(
            server, user, token, key, args.artifacts_dir,
            boilerplate, skip_epic_details=args.skip_epic_details,
        )
        records.append(record)

    # Compute summary
    summary = compute_summary(records)

    # Write JSON output
    output = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "strat_count": len(strat_keys),
            "skip_epic_details": args.skip_epic_details,
            "boilerplate_keys": sorted(boilerplate),
        },
        "summary": summary,
        "strats": records,
    }

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print(f"\nWrote {args.output}", file=sys.stderr)

    # Write markdown summary
    md_path = args.output.replace(".json", "-summary.md")
    render_markdown_summary(records, summary, md_path)

    # Print quick summary to stdout
    print(f"\n=== Pipeline Research Summary ===")
    print(f"STRATs analyzed:     {summary['valid']}"
          f" ({summary['errors']} errors)")
    print(f"With source RFE:     {summary['with_source_rfe']}"
          f" ({summary['with_source_rfe_pct']}%)")
    print(f"With refinement doc: {summary['with_refinement_doc']}"
          f" ({summary['with_refinement_doc_pct']}%)")
    print(f"With any Epics:      {summary['with_any_epics']}"
          f" ({summary['with_any_epics_pct']}%)")
    print(f"Full pipeline:       {summary['full_pipeline']}"
          f" ({summary['full_pipeline_pct']}%)")


if __name__ == "__main__":
    main()
