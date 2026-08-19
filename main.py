"""
Runs the detailed per-company Companies House checks (profile, officers,
charges, insolvency) for two combined sources of companies:

  1. watchlist.csv - your manually curated list, always fully checked
     every run, no cap.
  2. data/flagged_companies.csv - a CANDIDATE list produced by
     bulk_scan.py's full-register pre-filter, ordered largest-company-
     first (using Accounts.AccountCategory as a size proxy, since
     turnover itself often isn't disclosed at all under UK small/micro
     company filing exemptions). Only company numbers and names, no
     alerts, see bulk_scan.py for why. This list can still be large, so
     it is not fully processed every run. Instead this script works
     through it in rotating daily batches, tracked by a cursor file,
     so the whole backlog gets covered over several runs, larger
     companies first within each pass through the list.

Because only a subset of companies get checked on any given run, this
script maintains a small persistent store of the latest CONFIRMED
alert for every company ever checked (data/known_alerts.json), keyed
by company number. Each run only updates the entries for companies it
actually checked this run; everything else keeps its last known result.
The dashboard is built from this cumulative store, not just this run's
batch, so a company's confirmed alert from three days ago doesn't
disappear from the dashboard just because today's batch covered
different companies. Only companies with at least one live, confirmed
alert are kept in this store, a "checked and currently clean" company
is dropped rather than persisted, to keep this file's size bounded to
roughly the number of companies genuinely worth a human's attention,
not the much larger candidate backlog.

A second, similarly small persistent store (data/psc_snapshots.json)
holds each checked company's last-seen PSC "kind" counts (individual
vs. corporate/legal-person), never names, so indicators.py can detect
a change in PSC structure between runs. Unlike known_alerts.json, this
one keeps an entry for every company ever checked, not just ones with
a live alert, since a "currently no alert" company still needs its
kind-counts remembered to detect next run's change.

Usage:
    export CH_API_KEY="your_key_here"
    python main.py

All output remains company-level only, no officer or individual names
are collected or written anywhere in this pipeline, see indicators.py.
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone

from ch_client import CompaniesHouseClient
from indicators import run_all_detectors


def load_company_csv(path):
    """Returns an ordered list of company numbers from a CSV with a
    company_number column. Returns an empty list if the file doesn't
    exist, callers treat that as optional input, not an error."""
    if not os.path.exists(path):
        return []
    companies = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            number = (row.get("company_number") or "").strip()
            if number:
                companies.append(number)
    return companies


def load_cursor(path):
    if not os.path.exists(path):
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("next_index", 0)
    except (json.JSONDecodeError, OSError):
        return 0


def save_cursor(path, next_index):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"next_index": next_index, "updated_at": datetime.now(timezone.utc).isoformat()}, f)


def select_batch(flagged_companies, cursor_path, batch_size):
    """Rotates through flagged_companies, batch_size at a time, wrapping
    around to the start once the end of the list is reached."""
    if not flagged_companies or batch_size <= 0:
        return [], 0

    start = load_cursor(cursor_path) % len(flagged_companies)
    end = start + batch_size
    if end <= len(flagged_companies):
        batch = flagged_companies[start:end]
        next_index = end % len(flagged_companies)
    else:
        batch = flagged_companies[start:] + flagged_companies[: end - len(flagged_companies)]
        next_index = end - len(flagged_companies)
    return batch, next_index


def load_json_store(path):
    """Returns the dict at `path`, or {} if it doesn't exist yet or is
    unreadable. Generic loader shared by known_alerts.json (company_number
    -> [alert, ...]) and psc_snapshots.json (company_number -> {kind: count})."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_json_store(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def process_company(client, company_number, previous_psc_snapshot=None):
    profile = client.company_profile(company_number)
    officers_response = client.officers(company_number)
    charges_response = client.charges(company_number)
    insolvency_response = client.insolvency(company_number)
    psc_response = client.persons_with_significant_control(company_number)
    alerts, psc_snapshot = run_all_detectors(
        company_number, profile, officers_response, charges_response,
        insolvency_response, psc_response, previous_psc_snapshot,
    )
    company_name = (profile or {}).get("company_name", "")
    for alert in alerts:
        alert["company_name"] = company_name
    return alerts, psc_snapshot


def main():
    parser = argparse.ArgumentParser(description="Companies House distress indicator scan")
    parser.add_argument("--watchlist", default="watchlist.csv")
    parser.add_argument("--flagged-csv", default="data/flagged_companies.csv")
    parser.add_argument("--known-alerts", default="data/known_alerts.json")
    parser.add_argument("--psc-snapshots", default="data/psc_snapshots.json")
    parser.add_argument("--cursor-file", default="data/detail_scan_cursor.json")
    parser.add_argument("--max-flagged-per-run", type=int, default=500,
                         help="Cap on how many bulk-flagged companies get the detailed "
                              "REST check per run. Watchlist companies are never capped.")
    parser.add_argument("--output", default="alerts.csv")
    parser.add_argument("--json-output", default="site/data/alerts.json")
    args = parser.parse_args()

    try:
        client = CompaniesHouseClient()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    watchlist_companies = load_company_csv(args.watchlist)
    flagged_companies = load_company_csv(args.flagged_csv)
    known_alerts = load_json_store(args.known_alerts)
    psc_snapshots = load_json_store(args.psc_snapshots)

    batch, next_cursor = select_batch(flagged_companies, args.cursor_file, args.max_flagged_per_run)

    seen = set()
    companies_to_check = []
    for number in watchlist_companies + batch:
        if number not in seen:
            seen.add(number)
            companies_to_check.append(number)

    if not companies_to_check:
        print("No companies to check (empty watchlist and no flagged backlog yet).", file=sys.stderr)

    checked_count = 0
    for company_number in companies_to_check:
        print(f"Checking {company_number}...")
        try:
            alerts, psc_snapshot = process_company(
                client, company_number, psc_snapshots.get(company_number)
            )
        except Exception as e:
            print(f"  Error retrieving data for {company_number}: {e}", file=sys.stderr)
            continue
        checked_count += 1
        if psc_snapshot:
            psc_snapshots[company_number] = psc_snapshot
        if alerts:
            print(f"  {len(alerts)} alert(s) found.")
            known_alerts[company_number] = alerts
        else:
            # Checked and currently clean, drop any stale prior alert.
            known_alerts.pop(company_number, None)

    if flagged_companies:
        save_cursor(args.cursor_file, next_cursor)
        print(
            f"\nBulk-flagged backlog: {len(flagged_companies):,} candidates total, "
            f"{len(batch):,} checked this run, next run resumes at index {next_cursor}."
        )

    save_json_store(args.known_alerts, known_alerts)
    save_json_store(args.psc_snapshots, psc_snapshots)

    all_alerts = [alert for alerts in known_alerts.values() for alert in alerts]

    fieldnames = [
        "company_number", "company_name", "indicator", "tier", "detail",
        "evidence_url", "confidence", "detected_at",
    ]
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_alerts)

    os.makedirs(os.path.dirname(args.json_output) or ".", exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "companies_scanned_this_run": checked_count,
        "flagged_backlog_size": len(flagged_companies),
        "companies_with_active_alerts": len(known_alerts),
        "alerts": all_alerts,
    }
    with open(args.json_output, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(
        f"\n{len(all_alerts)} total alert(s) across {len(known_alerts)} companies written to "
        f"{args.output} and {args.json_output} ({checked_count} companies checked this run)."
    )


if __name__ == "__main__":
    main()
