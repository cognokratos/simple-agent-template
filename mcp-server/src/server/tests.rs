//! Search semantics: what ranks, what filters, and against which generation.
//!
//! The search tool's contract is that its ordering and its `decision` /
//! `min_investment_score` filters describe the deterministic engine's *current*
//! result — not a stored column that is null until a human commits something.
//! These tests drive the same ranking, filtering and collapsing code the tool
//! uses, with rows constructed in memory, so the whole contract holds without a
//! database.

use std::collections::BTreeSet;

use chrono::TimeZone as _;
use chrono::Utc;

use super::*;
use crate::fixtures::{load_etfs, load_profile, load_rules};

/// A stored row from the shipped snapshot, in the state a fresh database is in:
/// unreviewed, with no committed decision and no committed score.
fn unreviewed(etf_id: &str) -> EtfRow {
    let seed = load_etfs()
        .into_iter()
        .find(|etf| etf.etf_id == etf_id)
        .unwrap_or_else(|| panic!("{etf_id} is not in etfs.json"));
    EtfRow {
        etf_id: seed.etf_id,
        ticker: seed.ticker,
        isin: seed.isin,
        name: seed.name,
        provider: seed.provider,
        exchange: seed.exchange,
        asset_class: seed.asset_class,
        region: seed.region,
        index_name: seed.index_name,
        domicile: seed.domicile,
        ucits: seed.ucits,
        distribution_policy: seed.distribution_policy,
        replication: seed.replication,
        ter: seed.ter,
        aum_usd: seed.aum_usd,
        fund_age_years: seed.fund_age_years,
        holdings_count: seed.holdings_count,
        top_10_concentration: seed.top_10_concentration,
        tracking_difference_3y: seed.tracking_difference_3y,
        volatility_3y: seed.volatility_3y,
        return_3y_annualized: seed.return_3y_annualized,
        description: seed.description,
        data_as_of: seed.data_as_of.parse().expect("fixture carries an ISO date"),
        sources: serde_json::to_value(&seed.sources).expect("sources serialise"),
        review_state: "UNREVIEWED".to_string(),
        decision: None,
        investment_score: None,
        decided_rules_version: None,
        decided_profile_version: None,
        assigned_to: None,
        research_note: None,
        updated_at: Utc.timestamp_opt(1_770_000_000, 0).single().expect("fixed instant"),
    }
}

/// Every listing in the snapshot, in the order SQL returns candidates.
fn whole_universe() -> Vec<EtfRow> {
    let mut rows: Vec<EtfRow> =
        load_etfs().into_iter().map(|etf| unreviewed(&etf.etf_id)).collect();
    rows.sort_by(|left, right| left.etf_id.cmp(&right.etf_id));
    rows
}

/// The ranking stage of `search_etfs`, isolated: evaluate, filter on the current
/// result, sort, collapse, then limit.
struct Search {
    spec: RulesSpec,
    profile: InvestorProfile,
    decision: Option<String>,
    min_investment_score: Option<i32>,
    include_all_listings: bool,
    limit: usize,
}

impl Search {
    fn new() -> Self {
        Self {
            spec: load_rules(),
            profile: load_profile(),
            decision: None,
            min_investment_score: None,
            include_all_listings: false,
            limit: 50,
        }
    }

    fn deciding(mut self, decision: &str) -> Self {
        // Canonicalised exactly as the tool does, so a test cannot pass on a
        // spelling the tool would have rejected or, worse, silently not matched.
        self.decision = Some(rules::normalize_decision(decision).expect("a real decision"));
        self
    }

    fn scoring_at_least(mut self, score: i32) -> Self {
        self.min_investment_score = Some(score);
        self
    }

    fn every_listing(mut self) -> Self {
        self.include_all_listings = true;
        self
    }

    fn limited_to(mut self, limit: usize) -> Self {
        self.limit = limit;
        self
    }

    /// Mirrors the tool body: the order of operations is the contract.
    fn run(&self, rows: &[EtfRow]) -> Vec<(String, i32, Vec<String>)> {
        let mut scored: Vec<(EtfRow, Evaluation)> = rows
            .iter()
            .map(|etf| {
                let evaluation = rules::evaluate(&self.spec, &self.profile, &etf.facts());
                (etf.clone(), evaluation)
            })
            .filter(|(_, evaluation)| match self.decision.as_deref() {
                Some(wanted) => evaluation.decision == wanted,
                None => true,
            })
            .filter(|(_, evaluation)| match self.min_investment_score {
                Some(minimum) => evaluation.investment_score >= minimum,
                None => true,
            })
            .collect();
        scored.sort_by(|left, right| {
            right
                .1
                .investment_score
                .cmp(&left.1.investment_score)
                .then_with(|| left.0.etf_id.cmp(&right.0.etf_id))
        });
        let ranked: Vec<(&EtfRow, &Evaluation, Vec<&str>)> = if self.include_all_listings {
            scored.iter().map(|(etf, evaluation)| (etf, evaluation, Vec::new())).collect()
        } else {
            collapse_listings(&scored)
        };
        ranked
            .into_iter()
            .take(self.limit)
            .map(|(etf, evaluation, others)| {
                (
                    etf.etf_id.clone(),
                    evaluation.investment_score,
                    others.iter().map(|id| (*id).to_string()).collect(),
                )
            })
            .collect()
    }
}

// -------------------------------------------------- a fresh, unreviewed universe

#[test]
fn filtering_by_deterministic_decision_works_on_an_unreviewed_universe() {
    let rows = whole_universe();
    assert!(
        rows.iter().all(|etf| etf.decision.is_none() && etf.investment_score.is_none()),
        "the fixture must start with no committed decisions at all"
    );

    for decision in DECISIONS {
        let results = Search::new().deciding(decision).run(&rows);
        assert!(
            !results.is_empty(),
            "filtering on the deterministic decision {decision} returned nothing on a fresh \
database; the filter is reading a committed column that is null"
        );
    }
}

/// Validation and matching must agree about case.
///
/// The decision filters are compared in Rust while every other text filter is
/// compared by SQL with `LOWER()` on both sides. A vocabulary check that accepts
/// `SHORTLIST` followed by a strict comparison that matches nothing reports "no
/// such ETFs" for a filter the caller spelled correctly — and reports it silently.
#[test]
fn the_decision_filter_accepts_the_casing_its_own_validation_accepts() {
    let rows = whole_universe();
    let canonical = Search::new().deciding("shortlist").run(&rows);
    assert!(!canonical.is_empty());

    for spelling in ["SHORTLIST", " Shortlist ", "ShortList"] {
        assert_eq!(
            Search::new().deciding(spelling).run(&rows),
            canonical,
            "the decision filter disagreed with its own vocabulary check on {spelling:?}"
        );
    }
    // ...and a value that is genuinely not a decision is still refused outright,
    // rather than being normalised into one.
    assert!(rules::normalize_decision("shortlisted").is_err());
}

#[test]
fn a_minimum_score_filter_works_on_an_unreviewed_universe() {
    let rows = whole_universe();
    let strong = Search::new().scoring_at_least(80).run(&rows);
    assert!(!strong.is_empty(), "min_investment_score returned nothing on a fresh database");
    assert!(strong.iter().all(|(_, score, _)| *score >= 80));

    // The bound is inclusive and monotonic in the filter.
    let all = Search::new().run(&rows);
    let weak = Search::new().scoring_at_least(0).run(&rows);
    assert_eq!(weak.len(), all.len());
    assert!(Search::new().scoring_at_least(101).run(&rows).is_empty());
}

#[test]
fn results_are_ordered_by_the_current_deterministic_score() {
    let rows = whole_universe();
    let results = Search::new().run(&rows);
    let scores: Vec<i32> = results.iter().map(|(_, score, _)| *score).collect();
    assert!(
        scores.windows(2).all(|pair| pair[0] >= pair[1]),
        "results are not in descending deterministic score order: {scores:?}"
    );

    // Ties break on etf_id ascending, so "the five highest-scoring" is the same
    // five on every call.
    for pair in results.windows(2) {
        if pair[0].1 == pair[1].1 {
            assert!(pair[0].0 < pair[1].0, "tie between {} and {} is unordered", pair[0].0, pair[1].0);
        }
    }
    assert_eq!(Search::new().limited_to(5).run(&rows), results[..5].to_vec());
}

/// The bug this ordering replaced: a stored score that is null, or stale, decides
/// nothing about the ranking any more.
#[test]
fn committed_values_do_not_affect_the_deterministic_ranking() {
    let rows = whole_universe();
    let baseline = Search::new().run(&rows);

    // Give the worst-scoring listing a committed score of 100 under an old policy,
    // and clear nothing else. If the ranking read the column, this would jump.
    let worst = baseline.last().expect("a non-empty universe").0.clone();
    let mut tampered = rows.clone();
    for etf in &mut tampered {
        if etf.etf_id == worst {
            etf.decision = Some("shortlist".to_string());
            etf.investment_score = Some(100);
            etf.decided_rules_version = Some("0.9.0-historic".to_string());
            etf.review_state = "SHORTLISTED".to_string();
        }
    }
    assert_eq!(Search::new().run(&tampered), baseline, "a committed value moved the ranking");
    assert_eq!(
        Search::new().scoring_at_least(90).run(&tampered),
        Search::new().scoring_at_least(90).run(&rows),
        "a committed score of 100 satisfied a deterministic score filter"
    );

    // ...and a committed decision cannot make a fund appear under a deterministic
    // decision it does not have.
    let deterministic = rules::evaluate(
        &load_rules(),
        &load_profile(),
        &tampered.iter().find(|etf| etf.etf_id == worst).expect("present").facts(),
    );
    assert_ne!(deterministic.decision, "shortlist", "pick a fund the engine does not shortlist");
    assert!(
        !Search::new()
            .deciding("shortlist")
            .run(&tampered)
            .iter()
            .any(|(etf_id, _, _)| etf_id == &worst),
        "a committed shortlist leaked into a deterministic shortlist filter"
    );
}

/// Policy is data, so changing it must change what search returns — immediately,
/// with no reseed and no stored-score refresh.
#[test]
fn changing_the_rules_changes_the_current_ranking_and_filtering() {
    let rows = whole_universe();
    let before = Search::new().deciding("shortlist").run(&rows);
    assert!(!before.is_empty());

    // Move the shortlist band out of reach: nothing can be shortlisted any more.
    let mut spec = load_rules();
    for band in &mut spec.decision_thresholds {
        match band.decision.as_str() {
            "research" => band.max_score = 100,
            "shortlist" => {
                band.min_score = 101;
                band.max_score = 101;
            }
            _ => {}
        }
    }
    // The tiling check would reject a 0..=101 range, so assert the band directly
    // rather than through validate().
    let harder = || Search { spec: spec.clone(), ..Search::new() };
    assert!(
        harder().deciding("shortlist").run(&rows).is_empty(),
        "a rules change did not reach the deterministic decision filter"
    );
    assert!(!harder().deciding("research").run(&rows).is_empty());

    // A profile change moves the ranking too: dropping the UCITS constraint stops
    // the non-UCITS funds being rejected outright.
    let mut profile = load_profile();
    let rejected_before = Search::new().deciding("reject").run(&rows).len();
    profile.hard_constraints.insert("require_ucits".to_string(), serde_json::json!(false));
    let relaxed = Search { profile, ..Search::new() };
    assert!(
        relaxed.deciding("reject").run(&rows).len() < rejected_before,
        "relaxing a hard constraint did not change what search returns"
    );
}

// ------------------------------------------------------------ cross-listed funds

#[test]
fn cross_listings_collapse_to_one_candidate_by_default() {
    let rows = whole_universe();
    let listings: Vec<&EtfRow> =
        rows.iter().filter(|etf| etf.etf_id.starts_with("VUSA-")).collect();
    assert_eq!(listings.len(), 2, "the snapshot must keep a cross-listed fund");
    assert_eq!(listings[0].isin, listings[1].isin);

    let collapsed = Search::new().run(&rows);
    let vusa: Vec<&(String, i32, Vec<String>)> =
        collapsed.iter().filter(|(etf_id, _, _)| etf_id.starts_with("VUSA-")).collect();
    assert_eq!(vusa.len(), 1, "one fund appeared twice in a ranking: {vusa:?}");
    // The other listing is named, not silently dropped.
    assert_eq!(vusa[0].2, vec!["VUSA-XETRA".to_string()]);
    assert_eq!(vusa[0].0, "VUSA-LSE", "the representative is the first in ranked order");

    // Collapsing must not lose any *other* fund.
    let every = Search::new().every_listing().run(&rows);
    assert_eq!(every.len(), rows.len());
    assert_eq!(collapsed.len(), rows.len() - 1);
    assert!(every.iter().any(|(etf_id, _, _)| etf_id == "VUSA-XETRA"));
}

/// A top-N request is the case where double counting is actually harmful: one
/// fund taking two of five slots hides a genuine fifth candidate.
#[test]
fn a_top_n_ranking_spends_one_slot_per_fund() {
    let rows = whole_universe();
    let top = Search::new().limited_to(5).run(&rows);
    assert_eq!(top.len(), 5);
    let funds: BTreeSet<&str> = top
        .iter()
        .map(|(etf_id, _, _)| {
            rows.iter()
                .find(|etf| &etf.etf_id == etf_id)
                .expect("ranked rows come from the universe")
                .fund_identity()
        })
        .collect();
    assert_eq!(funds.len(), 5, "a top-5 ranking contains the same fund twice");
}

#[test]
fn requesting_every_listing_is_opt_in_and_reports_both() {
    let rows = whole_universe();
    assert!(
        Search::new().every_listing().run(&rows).len() > Search::new().run(&rows).len(),
        "include_all_listings must be able to return more rows than the grouped default"
    );
}
