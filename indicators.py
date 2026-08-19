"""
Indicator detection logic for the distress early-warning tool, phase 1.

Each detector takes raw Companies House API responses (as returned by
CompaniesHouseClient) for a single company and returns a list of alert
dicts. Every alert carries an evidence link back to the underlying
Companies House record and a confidence flag, consistent with the
"flag, don't conclude" design principle in the architecture document.

Coverage in this build: filing defaults, company status changes,
director/officer resignation clustering, new registered charges,
possible double-pledged charges, PSC structure changes, and open
insolvency cases. Auditor resignation and going-concern wording
require parsing the text of filed accounts documents, not just filing
metadata, and are intentionally left for a later phase (see README).

Every detector stays company-level only, consistent with the "no
individual data" design principle: PSC and charge detectors below
deliberately read only structural fields (a PSC's `kind`, a charge's
asset description) and never a person's name, DOB, or address, even
though the underlying Companies House response includes those fields.

Alerts also carry a `tier` field (tier1 = act now, tier2 = worth a
look, tier3 = pattern-building/watch), a rough severity ordering for
the dashboard, not a validated risk score.
"""

from datetime import datetime, timedelta, timezone

WEB_BASE = "https://find-and-update.company-information.service.gov.uk/company"

WATCH_STATUSES = {
    "administration",
    "liquidation",
    "receivership",
    "voluntary-arrangement",
    "insolvency-proceedings",
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _company_url(company_number):
    return f"{WEB_BASE}/{company_number}"


def detect_filing_defaults(company_number, profile):
    """Flags overdue accounts or an overdue confirmation statement."""
    alerts = []
    if not profile:
        return alerts

    accounts = profile.get("accounts", {})
    if accounts.get("overdue"):
        alerts.append({
            "company_number": company_number,
            "indicator": "accounts_overdue",
            "tier": "tier3",
            "detail": "Company accounts are shown as overdue on the company profile.",
            "evidence_url": _company_url(company_number) + "/filing-history",
            "confidence": "high",
            "detected_at": _now(),
        })

    confirmation_statement = profile.get("confirmation_statement", {})
    if confirmation_statement.get("overdue"):
        alerts.append({
            "company_number": company_number,
            "indicator": "confirmation_statement_overdue",
            "tier": "tier3",
            "detail": "Confirmation statement is shown as overdue.",
            "evidence_url": _company_url(company_number) + "/filing-history",
            "confidence": "high",
            "detected_at": _now(),
        })

    return alerts


def detect_company_status_flags(company_number, profile):
    """Flags a non-active company status (administration, liquidation, etc)."""
    alerts = []
    if not profile:
        return alerts

    status = profile.get("company_status")
    if status in WATCH_STATUSES:
        alerts.append({
            "company_number": company_number,
            "indicator": "company_status_change",
            "tier": "tier1",
            "detail": f"Company status is reported as '{status}'.",
            "evidence_url": _company_url(company_number),
            "confidence": "high",
            "detected_at": _now(),
        })
    return alerts


def detect_officer_resignation_cluster(
    company_number, officers_response, window_days=90, cluster_size=2
):
    """
    Flags a cluster of officer resignations within a rolling window.

    A single resignation is not flagged on its own; clustering is the
    signal, consistent with the architecture document's guidance that
    a lone departure is weaker evidence than several in a short window.
    """
    alerts = []
    if not officers_response:
        return alerts

    resignations = []
    for officer in officers_response.get("items", []):
        resigned_on = officer.get("resigned_on")
        if resigned_on:
            try:
                resigned_date = datetime.strptime(resigned_on, "%Y-%m-%d")
            except ValueError:
                continue
            # Deliberately not capturing officer name here. This indicator
            # is published to a public dashboard, so the output must stay
            # at the company level, not the individual level. See README.
            resignations.append(resigned_date)

    resignations.sort()
    for i in range(len(resignations)):
        window_start = resignations[i]
        window_end = window_start + timedelta(days=window_days)
        cluster = [r for r in resignations if window_start <= r <= window_end]
        if len(cluster) >= cluster_size:
            alerts.append({
                "company_number": company_number,
                "indicator": "officer_resignation_cluster",
                "tier": "tier2",
                "detail": (
                    f"{len(cluster)} officer resignation(s) recorded within "
                    f"a {window_days}-day window (earliest {cluster[0].date()}, "
                    f"latest {cluster[-1].date()})."
                ),
                "evidence_url": _company_url(company_number) + "/officers",
                "confidence": "medium",
                "detected_at": _now(),
            })
            break  # report the first qualifying cluster only, avoid overlapping duplicates

    return alerts


def detect_new_charges(company_number, charges_response, lookback_days=90):
    """Flags newly created registered charges within a lookback window."""
    alerts = []
    if not charges_response:
        return alerts

    cutoff = datetime.now() - timedelta(days=lookback_days)
    for charge in charges_response.get("items", []):
        created_on = charge.get("created_on")
        if not created_on:
            continue
        try:
            created_date = datetime.strptime(created_on, "%Y-%m-%d")
        except ValueError:
            continue
        if created_date >= cutoff:
            alerts.append({
                "company_number": company_number,
                "indicator": "new_registered_charge",
                "tier": "tier3",
                "detail": (
                    f"New charge created on {created_on}, "
                    f"status '{charge.get('status', 'unknown')}'."
                ),
                "evidence_url": _company_url(company_number) + "/charges",
                "confidence": "medium",
                "detected_at": _now(),
            })

    return alerts


# Boilerplate "all monies" / "all assets" debentures are extremely common
# and largely interchangeable in wording; matching on those would flag
# most companies with more than one lender and add no signal. Only
# compare descriptions long/specific enough to plausibly identify one
# particular asset (a named freehold, a specific plant line, etc).
MIN_PARTICULARS_LENGTH_FOR_COMPARISON = 40


def _normalized_particulars(charge):
    """
    Returns a normalized version of the charge's secured-asset description
    for comparison against other charges, or None if there isn't a long
    enough description to compare safely (see threshold above).

    Deliberately ignores `persons_entitled` (the chargee/lender). That
    field can hold a natural person's name, e.g. a director lending
    personally rather than a bank, and this pipeline stays company-level
    only, consistent with the rest of this module.
    """
    particulars = charge.get("particulars") or {}
    description = particulars.get("description")
    if not description or not isinstance(description, str):
        return None
    normalized = " ".join(description.lower().split())
    if len(normalized) < MIN_PARTICULARS_LENGTH_FOR_COMPARISON:
        return None
    return normalized


def detect_double_pledged_charges(company_number, charges_response):
    """
    Flags when the same (or near-identical, exact-match only in this
    version) secured-asset description appears across two or more live
    charges. A specific asset pledged more than once to different
    lenders, without one charge being satisfied first, is the
    double-pledging pattern.

    Known limitation: this does exact string matching on normalized
    text, not fuzzy matching, so wording variation between two charges
    describing the same real-world asset will be missed. Worth revisiting
    with a fuzzy-match library (e.g. difflib.SequenceMatcher) once you've
    seen real charge description text and can judge the false-negative
    rate. Exact matching was chosen deliberately over fuzzy matching for
    this first version to avoid the opposite failure, generic boilerplate
    descriptions fuzzy-matching each other and flooding the dashboard.
    """
    alerts = []
    if not charges_response:
        return alerts

    live_statuses = {"outstanding", "part-satisfied"}
    by_description = {}
    for charge in charges_response.get("items", []):
        if charge.get("status") not in live_statuses:
            continue
        normalized = _normalized_particulars(charge)
        if not normalized:
            continue
        by_description.setdefault(normalized, []).append(charge.get("charge_number", "?"))

    for charge_numbers in by_description.values():
        if len(charge_numbers) >= 2:
            alerts.append({
                "company_number": company_number,
                "indicator": "possible_double_pledged_asset",
                "tier": "tier1",
                "detail": (
                    f"{len(charge_numbers)} live charges (numbers "
                    f"{', '.join(str(c) for c in charge_numbers)}) describe the "
                    "same secured asset. Could be a legitimate, correctly "
                    "disclosed second-ranking charge, or undisclosed "
                    "double-pledging, manual review of the charge documents "
                    "needed to tell which."
                ),
                "evidence_url": _company_url(company_number) + "/charges",
                "confidence": "medium",
                "detected_at": _now(),
            })

    return alerts


INDIVIDUAL_PSC_KINDS = {"individual-person-with-significant-control"}
NON_INDIVIDUAL_PSC_KINDS = {
    "corporate-entity-person-with-significant-control",
    "legal-person-person-with-significant-control",
}


def _psc_kind_counts(psc_response):
    """
    Returns {kind: active_count}. Reads only the `kind` field of each PSC
    entry, never `name`, `date_of_birth`, `address`, or any other
    individual-identifying field on the record, even though the
    Companies House response includes them. This pipeline's PSC detector
    is intentionally structural-only.
    """
    counts = {}
    if not psc_response:
        return counts
    for item in psc_response.get("items", []):
        if item.get("ceased_on"):
            continue  # only count currently active PSCs
        kind = item.get("kind", "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def detect_psc_structure_change(company_number, psc_response, previous_snapshot):
    """
    Flags a shift in the TYPE of controlling parties on record, most
    notably an individual PSC being replaced by a corporate or
    legal-person PSC, a pattern consistent with ownership moving behind
    a holding entity.

    Stateful, unlike the other detectors here: a single snapshot can't
    show a change on its own, so this needs the previous run's kind-counts
    for this company. Callers persist the returned snapshot the same way
    main.py persists known_alerts.json, and pass it back in next run.

    Returns (alerts, current_snapshot). current_snapshot is returned even
    when there's no previous_snapshot to compare against (first time this
    company is checked), so the caller has something to persist for next
    time.
    """
    alerts = []
    current_counts = _psc_kind_counts(psc_response)

    if previous_snapshot:
        prev_individual = sum(previous_snapshot.get(k, 0) for k in INDIVIDUAL_PSC_KINDS)
        curr_individual = sum(current_counts.get(k, 0) for k in INDIVIDUAL_PSC_KINDS)
        prev_non_individual = sum(previous_snapshot.get(k, 0) for k in NON_INDIVIDUAL_PSC_KINDS)
        curr_non_individual = sum(current_counts.get(k, 0) for k in NON_INDIVIDUAL_PSC_KINDS)

        if curr_individual < prev_individual and curr_non_individual > prev_non_individual:
            alerts.append({
                "company_number": company_number,
                "indicator": "psc_structure_change",
                "tier": "tier2",
                "detail": (
                    "An individual person with significant control appears to "
                    "have been replaced by a corporate or legal-person PSC "
                    "since the last check. Worth confirming whether this "
                    "reflects a legitimate restructuring or a change that "
                    "obscures ultimate ownership."
                ),
                "evidence_url": _company_url(company_number) + "/persons-with-significant-control",
                "confidence": "medium",
                "detected_at": _now(),
            })

    return alerts, current_counts


def detect_insolvency_case(company_number, insolvency_response):
    """Flags any open insolvency case returned by the insolvency endpoint."""
    alerts = []
    if not insolvency_response:
        return alerts

    for case in insolvency_response.get("cases", []):
        alerts.append({
            "company_number": company_number,
            "indicator": "insolvency_case",
            "tier": "tier1",
            "detail": f"Insolvency case recorded, type '{case.get('type', 'unknown')}'.",
            "evidence_url": _company_url(company_number),
            "confidence": "high",
            "detected_at": _now(),
        })

    return alerts


def run_all_detectors(
    company_number, profile, officers_response, charges_response,
    insolvency_response, psc_response=None, previous_psc_snapshot=None,
):
    """
    Runs every detector for one company. psc_response and
    previous_psc_snapshot are optional and default to None so existing
    callers that don't yet fetch PSC data keep working unchanged; pass
    both to also get PSC structure-change detection.

    Returns (alerts, psc_snapshot). psc_snapshot is previous_psc_snapshot
    unchanged when psc_response wasn't supplied, or the freshly computed
    snapshot when it was, callers should persist it either way so the
    comparison has something to work from next run.
    """
    alerts = []
    alerts += detect_filing_defaults(company_number, profile)
    alerts += detect_company_status_flags(company_number, profile)
    alerts += detect_officer_resignation_cluster(company_number, officers_response)
    alerts += detect_new_charges(company_number, charges_response)
    alerts += detect_double_pledged_charges(company_number, charges_response)
    alerts += detect_insolvency_case(company_number, insolvency_response)

    psc_snapshot = previous_psc_snapshot
    if psc_response is not None:
        psc_alerts, psc_snapshot = detect_psc_structure_change(
            company_number, psc_response, previous_psc_snapshot
        )
        alerts += psc_alerts

    return alerts, psc_snapshot
