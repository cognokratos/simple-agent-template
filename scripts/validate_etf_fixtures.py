#!/usr/bin/env python3
"""Structural integrity check for the shipped ETF fixtures.

This validates *shape and provenance*, deliberately not policy. It does not
implement the scoring rules and never will: the moment a second implementation of
the policy exists, the published numbers describe whichever one someone happened
to run. Decision correctness is asserted against the shipped Rust engine:

    make rules-test     # cargo test, and it regenerates the baseline artifact

What this does assert is everything a reader needs in order to trust the data:
identifiers are unique and well formed, vocabularies match what the database and
the engine accept, numeric ranges are sane, every fund cites a source and a
snapshot date, the investor profile and rules specification are structurally
complete, and every labelled test case points at a real fund.

It stays Python and dependency-free on purpose, so the fixtures can be checked
without Docker, Rust or Node.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

DECISIONS = {"reject", "research", "shortlist"}
ASSET_CLASSES = {"equity", "bond", "commodity", "multi_asset", "money_market"}
DISTRIBUTION_POLICIES = {"accumulating", "distributing", "none"}
REPLICATIONS = {"physical", "sampled", "synthetic"}
RISK_TOLERANCES = {"low", "medium", "high"}

REQUIRED_ETF_FIELDS = {
    "etf_id",
    "ticker",
    "isin",
    "name",
    "provider",
    "exchange",
    "asset_class",
    "region",
    "index_name",
    "domicile",
    "ucits",
    "distribution_policy",
    "replication",
    "ter",
    "aum_usd",
    "fund_age_years",
    "holdings_count",
    "top_10_concentration",
    "tracking_difference_3y",
    "volatility_3y",
    "return_3y_annualized",
    "description",
    "data_as_of",
    "sources",
}

#: Nullable on purpose. Real reference data has gaps, and the engine has an
#: explicit policy for them; requiring a value here would push someone to invent
#: one, which is the failure this project is most concerned with.
NULLABLE_METRICS = {
    "ter",
    "aum_usd",
    "fund_age_years",
    "holdings_count",
    "top_10_concentration",
    "tracking_difference_3y",
    "volatility_3y",
    "return_3y_annualized",
}

ETF_ID_PATTERN = re.compile(r"^[A-Z0-9]{2,6}-[A-Z0-9]{2,10}$")
ISIN_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

REQUIRED_SOURCE_FIELDS = {"name", "url", "source_type", "locator", "retrieved_at"}

#: How directly a source addresses the record it is attached to, weakest first.
#: Kept in step with SOURCE_TYPES in mcp-server/src/domain.rs.
SOURCE_TYPES = (
    "issuer_homepage",
    "data_vendor_profile",
    "issuer_product_page",
    "issuer_factsheet",
    "issuer_kid",
)

#: Types that name a specific document or product record rather than a site to
#: search in. A bare host cannot substantiate one of these: `https://issuer.com`
#: and `https://issuer.com/products/9679/factsheet.pdf` are both `http...` and are
#: not the same evidence, so grading them on the scheme alone treats the weakest
#: possible citation as the strongest.
DEEP_LINK_SOURCE_TYPES = ("issuer_product_page", "issuer_factsheet", "issuer_kid")

URL_PATTERN = re.compile(r"^https://[a-z0-9.-]+\.[a-z]{2,}(/\S*)?$")


class Failure(Exception):
    """A fixture problem worth stopping for."""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)


def load(name: str) -> Any:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def validate_source(etf_id: str, source: dict[str, Any], data_as_of: date) -> str:
    """Check one cited source, and grade how strong the citation actually is.

    The point of the grading is honesty rather than gatekeeping. A homepage plus an
    ISIN is a legitimate, checkable citation — a reader can find the fund from it —
    and it is *not* a document-level reference. What must never happen is a record
    claiming `issuer_factsheet` while linking a landing page, because then the
    provenance field asserts evidence the link does not carry.
    """
    check(
        set(source) == REQUIRED_SOURCE_FIELDS,
        f"{etf_id} has a source with unexpected or missing fields: {sorted(source)}",
    )
    check(bool(source["name"].strip()), f"{etf_id} has a source with no name")
    check(
        bool(URL_PATTERN.match(source["url"])),
        f"{etf_id} has source URL {source['url']!r}, which is not an https URL",
    )
    source_type = source["source_type"]
    check(
        source_type in SOURCE_TYPES,
        f"{etf_id} has source_type {source_type!r}; expected one of {', '.join(SOURCE_TYPES)}",
    )
    # A locator is what makes a site-level citation auditable at all: without it
    # the record cites a company rather than a fund.
    check(
        bool(source["locator"].strip()),
        f"{etf_id} has a source with no locator; a citation must say how to find this fund in it",
    )
    check(
        source["locator"].strip() != source["url"],
        f"{etf_id} repeats the URL as its locator, which locates nothing",
    )
    has_path = "/" in source["url"].removeprefix("https://").rstrip("/")
    if source_type in DEEP_LINK_SOURCE_TYPES:
        check(
            has_path,
            f"{etf_id} claims source_type {source_type!r} but links the bare host "
            f"{source['url']!r}. A document-level claim needs a document-level URL; use "
            f"'issuer_homepage' if that is what this really is.",
        )
    check(
        bool(DATE_PATTERN.match(source["retrieved_at"])),
        f"{etf_id} has a source with a malformed retrieved_at",
    )
    retrieved = date.fromisoformat(source["retrieved_at"])
    check(retrieved <= date.today(), f"{etf_id} cites a source retrieved in the future")
    # A value dated after the document it came from cannot have come from it.
    check(
        retrieved >= data_as_of,
        f"{etf_id} carries data as of {data_as_of} from a source read earlier, on {retrieved}",
    )
    return source_type


def validate_etfs(etfs: list[dict[str, Any]]) -> list[str]:
    checks: list[str] = []
    source_types: dict[str, int] = {}
    check(isinstance(etfs, list) and etfs, "etfs.json must be a non-empty list")
    check(len(etfs) >= 20, f"expected at least 20 ETFs, got {len(etfs)}")

    ids: set[str] = set()
    for etf in etfs:
        etf_id = etf.get("etf_id", "<missing>")
        check(set(etf) == REQUIRED_ETF_FIELDS, f"{etf_id} has unexpected or missing fields")
        check(etf_id not in ids, f"duplicate etf_id {etf_id}")
        ids.add(etf_id)
        check(bool(ETF_ID_PATTERN.match(etf_id)), f"{etf_id} is not a TICKER-VENUE identifier")
        check(bool(ISIN_PATTERN.match(etf["isin"])), f"{etf_id} has a malformed ISIN")
        check(etf["asset_class"] in ASSET_CLASSES, f"{etf_id} has asset_class {etf['asset_class']!r}")
        check(
            etf["distribution_policy"] in DISTRIBUTION_POLICIES,
            f"{etf_id} has distribution_policy {etf['distribution_policy']!r}",
        )
        check(etf["replication"] in REPLICATIONS, f"{etf_id} has replication {etf['replication']!r}")
        check(isinstance(etf["ucits"], bool), f"{etf_id} has a non-boolean ucits flag")

        # Canonical casing, so the engine's case-insensitive matching is never the
        # only thing standing between a feed and a mismatched comparison.
        for field in ("asset_class", "region", "distribution_policy", "replication"):
            check(etf[field] == etf[field].lower(), f"{etf_id} stores a non-canonical {field}")
        check(etf["ticker"] == etf["ticker"].upper(), f"{etf_id} stores a non-canonical ticker")

        for field in NULLABLE_METRICS:
            value = etf[field]
            check(
                value is None or isinstance(value, (int, float)),
                f"{etf_id}.{field} must be a number or null, not {type(value).__name__}",
            )
        for field, low, high in (
            ("ter", 0.0, 0.05),
            ("top_10_concentration", 0.0, 1.0),
            ("fund_age_years", 0.0, 100.0),
            ("volatility_3y", 0.0, 5.0),
        ):
            value = etf[field]
            if value is not None:
                check(low <= value <= high, f"{etf_id}.{field}={value} is outside {low}..{high}")
        for field in ("aum_usd", "holdings_count"):
            value = etf[field]
            if value is not None:
                check(value >= 0, f"{etf_id}.{field} is negative")

        check(bool(etf["description"].strip()), f"{etf_id} has an empty description")
        check(bool(DATE_PATTERN.match(etf["data_as_of"])), f"{etf_id} has a malformed data_as_of")
        check(
            date.fromisoformat(etf["data_as_of"]) <= date.today(),
            f"{etf_id} carries a data_as_of in the future",
        )
        check(
            isinstance(etf["sources"], list) and etf["sources"],
            f"{etf_id} cites no source; every metric must be attributable",
        )
        for source in etf["sources"]:
            source_type = validate_source(etf_id, source, date.fromisoformat(etf["data_as_of"]))
            source_types[source_type] = source_types.get(source_type, 0) + 1

    checks.append(f"{len(etfs)} ETFs, unique IDs, valid ISINs, canonical vocabularies")
    checks.append(
        "every source carries a type, a locator and a retrieved_at date: "
        + ", ".join(f"{count}x {name}" for name, count in sorted(source_types.items()))
    )
    if not set(source_types) & set(DEEP_LINK_SOURCE_TYPES):
        # Reported, not failed. The snapshot is honest about citing issuer sites
        # rather than documents, and saying so on every run is better than a silent
        # pass that reads as document-level provenance.
        checks.append(
            "no record claims a document-level citation; provenance is issuer-site plus ISIN "
            "locator, which is checkable by hand and weaker than a factsheet URL"
        )

    # A ticker is convenient and not unique. The snapshot keeps a cross-listed
    # fund on purpose, so anything keyed on ticker alone fails a test rather than
    # passing by luck.
    tickers = [etf["ticker"] for etf in etfs]
    check(
        len(set(tickers)) < len(tickers),
        "the snapshot must retain at least one cross-listed ticker so ticker-keyed lookups "
        "cannot pass by coincidence",
    )
    checks.append("at least one ticker is shared by two listings, as intended")

    # etf_id identifies a *listing*; the ISIN identifies the economic fund. Two
    # listings of one fund must agree on every scored characteristic, or the engine
    # would return two different answers for one investment candidate and grouping
    # them would hide the discrepancy instead of exposing it.
    by_isin: dict[str, list[dict[str, Any]]] = {}
    for etf in etfs:
        by_isin.setdefault(etf["isin"], []).append(etf)
    economic_fields = (
        "name",
        "provider",
        "asset_class",
        "region",
        "index_name",
        "domicile",
        "ucits",
        "distribution_policy",
        "replication",
        "ter",
        "aum_usd",
        "fund_age_years",
        "holdings_count",
        "top_10_concentration",
    )
    cross_listed = {isin: rows for isin, rows in by_isin.items() if len(rows) > 1}
    check(bool(cross_listed), "the snapshot must retain at least one cross-listed fund")
    for isin, rows in cross_listed.items():
        for field in economic_fields:
            values = {row[field] for row in rows}
            check(
                len(values) == 1,
                f"listings of {isin} disagree on {field}: {values}. Two listings of one share "
                f"class must score identically.",
            )
        venues = {row["exchange"] for row in rows}
        check(
            len(venues) == len(rows),
            f"listings of {isin} do not each name a distinct exchange: {venues}",
        )
    checks.append(
        f"{len(by_isin)} distinct funds across {len(etfs)} listings; "
        f"{len(cross_listed)} cross-listed fund(s) agree on every scored field"
    )

    complete = sum(
        1
        for etf in etfs
        if all(etf[field] is not None for field in ("ter", "aum_usd", "holdings_count"))
    )
    checks.append(f"{complete}/{len(etfs)} ETFs carry all three headline metrics")
    return checks


def validate_profile(profile: dict[str, Any]) -> list[str]:
    for field in (
        "profile_id",
        "version",
        "description",
        "base_currency",
        "investment_horizon_years",
        "strategy",
        "risk_tolerance",
        "hard_constraints",
        "preferences",
    ):
        check(field in profile, f"investor_profile.json is missing {field}")
    check(
        profile["risk_tolerance"] in RISK_TOLERANCES,
        f"unknown risk_tolerance {profile['risk_tolerance']!r}",
    )
    check(profile["investment_horizon_years"] > 0, "investment_horizon_years must be positive")
    check(isinstance(profile["hard_constraints"], dict), "hard_constraints must be an object")
    check(isinstance(profile["preferences"], dict), "preferences must be an object")
    for name, value in profile["preferences"].items():
        check(isinstance(value, bool), f"preference {name!r} must be a boolean")
    return [
        f"investor profile {profile['profile_id']} v{profile['version']}: "
        f"{len(profile['hard_constraints'])} hard constraint(s), "
        f"{len(profile['preferences'])} preference(s)"
    ]


def validate_rules(rules: dict[str, Any], profile: dict[str, Any]) -> list[str]:
    check(set(rules["decision_vocabulary"]) == DECISIONS, "decision_vocabulary must be the three decisions")
    check(rules["decision_order"] == ["reject", "research", "shortlist"], "decision_order is wrong")

    bands = sorted(rules["decision_thresholds"], key=lambda b: b["min_score"])
    check(len(bands) == 3, "expected exactly one threshold band per decision")
    expected_min = 0
    for band in bands:
        check(band["decision"] in DECISIONS, f"unknown decision {band['decision']!r}")
        check(band["min_score"] == expected_min, f"threshold gap or overlap at {expected_min}")
        expected_min = band["max_score"] + 1
    check(expected_min == 101, "decision_thresholds must cover 0 through 100")

    total = 0
    for component in rules["score_components"]:
        metric_total = sum(metric["weight"] for metric in component["metrics"])
        check(
            metric_total == component["weight"],
            f"component {component['key']!r} weight {component['weight']} != metrics {metric_total}",
        )
        total += component["weight"]
    check(total == 100, f"score component weights sum to {total}, not 100")

    check(bool(rules["hard_constraints"]), "hard_constraints must not be empty")
    for constraint in rules["hard_constraints"]:
        check(constraint["decision"] in DECISIONS, f"{constraint['code']} names an unknown decision")
        check(
            constraint["profile_key"] in profile["hard_constraints"],
            f"{constraint['code']} keys on {constraint['profile_key']!r}, which the profile does not define",
        )
    check(bool(rules["decision_caps"]), "decision_caps must not be empty")
    for cap in rules["decision_caps"]:
        check(cap["max_decision"] in DECISIONS, f"{cap['code']} names an unknown decision")

    policy = rules["missing_data_policy"]
    check(bool(policy["critical_fields"]), "missing_data_policy must name critical fields")
    check(bool(policy["completeness_fields"]), "missing_data_policy must name completeness fields")
    check(
        set(policy["critical_fields"]).issubset(set(policy["completeness_fields"])),
        "every critical field must also count towards completeness",
    )

    # Every preference the profile expresses must have a documented effect,
    # otherwise switching one on silently does nothing.
    realisation = rules["preference_realisation"]
    for preference in profile["preferences"]:
        check(
            preference in realisation,
            f"profile preference {preference!r} has no entry in preference_realisation",
        )
    return [
        f"rules spec v{rules['version']}: {len(rules['score_components'])} components summing to 100, "
        f"{len(rules['hard_constraints'])} hard constraint(s), {len(rules['decision_caps'])} cap(s)",
        f"every profile preference has a documented effect ({len(realisation)} entries)",
    ]


def validate_test_cases(cases: list[dict[str, Any]], etf_ids: set[str]) -> list[str]:
    check(isinstance(cases, list) and cases, "test_cases.json must be a non-empty list")
    check(len(cases) >= 12, f"expected at least 12 labelled cases, got {len(cases)}")
    seen: set[str] = set()
    covered: set[str] = set()
    for case in cases:
        case_id = case.get("case_id", "<missing>")
        check(case_id not in seen, f"duplicate case_id {case_id}")
        seen.add(case_id)
        check(case["etf_id"] in etf_ids, f"{case_id} names {case['etf_id']}, which is not in etfs.json")
        check(
            case["expected_decision"] in DECISIONS,
            f"{case_id} expects an unknown decision {case['expected_decision']!r}",
        )
        covered.add(case["expected_decision"])
        low, high = case["expected_score_range"]
        check(0 <= low <= high <= 100, f"{case_id} has an impossible score range {low}..{high}")
        check(bool(case["rationale"].strip()), f"{case_id} has no rationale")
    check(
        covered == DECISIONS,
        f"the labelled set must exercise every decision; it covers {sorted(covered)}",
    )
    return [f"{len(cases)} labelled cases covering {', '.join(sorted(covered))}"]


def main() -> int:
    etfs = load("etfs.json")
    profile = load("investor_profile.json")
    rules = load("rules_spec.json")
    cases = load("test_cases.json")

    checks: list[str] = []
    try:
        checks += validate_etfs(etfs)
        checks += validate_profile(profile)
        checks += validate_rules(rules, profile)
        checks += validate_test_cases(cases, {etf["etf_id"] for etf in etfs})
    except Failure as failure:
        print(f"ETF fixture integrity: FAILED\n  {failure}")
        return 1

    print("ETF fixture integrity:")
    for line in checks:
        print(f"  ok  {line}")
    print("\nDecision correctness is asserted against the shipped Rust engine:")
    print("  make rules-test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
