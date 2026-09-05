//! The MCP surface: tools, resources, and the invariants every mutation holds.
//!
//! Two rules shape everything here.
//!
//! * The deterministic engine is authoritative. It is recomputed on the server
//!   for every decision, never taken from the caller, and its hard constraints
//!   are re-enforced at the moment of the write.
//! * A mutation's parameters come from a signed human approval, never from tool
//!   arguments. The model chooses *which* approval to spend, not what it says.

use std::sync::Arc;

use rmcp::{
    handler::server::{router::tool::ToolRouter, wrapper::Parameters},
    model::*,
    service::RequestContext,
    tool, tool_handler, tool_router, ErrorData as McpError, RoleServer, ServerHandler,
};
use schemars::JsonSchema;
use serde::Deserialize;
use serde_json::{json, Value};
use sqlx::PgPool;

use crate::approval::ApprovalVerifier;
use crate::domain::{
    etf_read_model, etf_search_model, policy_generations, untrusted_free_text, EtfRow,
    REVIEW_STATES,
};
use crate::rules::{self, Evaluation, InvestorProfile, RulesSpec, DECISIONS};
use crate::store::{self, AuditEvent, EtfFilters, EtfHistory};

#[cfg(test)]
mod tests;

const DEFAULT_SEARCH_LIMIT: i64 = 20;
const MAX_SEARCH_LIMIT: i64 = 50;

const ASSET_CLASSES: [&str; 5] = ["equity", "bond", "commodity", "multi_asset", "money_market"];
const DISTRIBUTION_POLICIES: [&str; 3] = ["accumulating", "distributing", "none"];
const REPLICATIONS: [&str; 3] = ["physical", "sampled", "synthetic"];

/// Review states an ETF may be assigned from. Assignment means "this person owns
/// the next research decision", so it is only meaningful once the candidate has
/// actually been decided on and is still live.
const ASSIGNABLE_STATES: [&str; 3] = ["RESEARCH", "SHORTLISTED", "ASSIGNED"];

#[derive(Debug, Deserialize, JsonSchema)]
pub struct SearchEtfsArgs {
    /// Free-text match on etf_id, ticker, ISIN or fund name.
    query: Option<String>,
    /// Issuer name, for example "Vanguard".
    provider: Option<String>,
    /// equity, bond, commodity, multi_asset or money_market.
    asset_class: Option<String>,
    /// Region label, for example "global", "developed_world", "emerging".
    region: Option<String>,
    /// Restrict to UCITS-eligible funds when true, or non-UCITS when false.
    ucits: Option<bool>,
    /// accumulating, distributing or none.
    distribution_policy: Option<String>,
    /// physical, sampled or synthetic.
    replication: Option<String>,
    /// UNREVIEWED, RESEARCH, SHORTLISTED, ASSIGNED or REJECTED.
    review_state: Option<String>,
    /// Current deterministic decision from the rules engine, recomputed now:
    /// reject, research or shortlist. This is not the committed decision.
    decision: Option<String>,
    /// Minimum current deterministic investment score, 0 to 100.
    min_investment_score: Option<i32>,
    /// Exact research owner to filter by.
    assigned_to: Option<String>,
    /// Return only candidates still awaiting a decision or in research.
    research_needed_only: Option<bool>,
    /// Committed decision a human already approved: reject, research or
    /// shortlist. Null for every candidate nobody has decided yet, so this is a
    /// filter on workflow history, not on fund quality.
    committed_decision: Option<String>,
    /// Return every exchange listing separately. By default the listings of one
    /// cross-listed fund collapse to a single result, because they are one
    /// investment candidate.
    include_all_listings: Option<bool>,
    /// Maximum number of results, applied after deterministic ranking. Defaults
    /// to 20 and is capped at 50.
    limit: Option<i64>,
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct GetEtfArgs {
    /// Canonical etf_id, or a ticker/ISIN/name to resolve. A ticker is not
    /// unique; an ambiguous one is reported rather than guessed.
    etf: String,
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct EvaluateEtfArgs {
    /// Canonical etf_id, or a ticker/ISIN/name to resolve.
    etf: String,
    /// Optional model recommendation to compare with the deterministic decision.
    llm_recommendation: Option<String>,
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct ResearchContextArgs {
    /// Canonical etf_id, or a ticker/ISIN/name to resolve.
    etf: String,
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct CommitEvaluationArgs {
    /// Exact etf_id.
    pub etf_id: String,
    /// Human approval token minted by the approval-gated NAT function for this
    /// action. It carries the entire approved payload; no decision or research
    /// note is accepted here.
    pub approval_token: String,
    /// Authenticated gateway request ID. Must match the approval token.
    pub request_id: String,
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct ShortlistEtfArgs {
    /// Exact etf_id.
    pub etf_id: String,
    /// Human approval token minted for action=shortlist. It carries the approved
    /// research note; no note is accepted here.
    pub approval_token: String,
    /// Authenticated gateway request ID. Must match the approval token.
    pub request_id: String,
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct AssignEtfArgs {
    /// Exact etf_id.
    pub etf_id: String,
    /// Human approval token minted for action=assign. It carries the approved
    /// research owner.
    pub approval_token: String,
    /// Authenticated gateway request ID. Must match the approval token.
    pub request_id: String,
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct EtfHistoryArgs {
    /// Canonical etf_id, or a ticker/ISIN/name to resolve.
    etf: String,
    /// Most recent events to return. Defaults to 50 and is capped at 200.
    limit: Option<i64>,
}

/// A refusal the caller can act on, as opposed to a server fault.
fn tool_error(message: impl Into<String>) -> CallToolResult {
    CallToolResult::error(vec![ContentBlock::text(message.into())])
}

fn tool_json(value: Value) -> CallToolResult {
    CallToolResult::success(vec![ContentBlock::text(value.to_string())])
}

fn invalid_params(message: String) -> McpError {
    McpError::invalid_params(message, None)
}

fn non_empty(value: &Option<String>) -> Option<String> {
    value.as_deref().map(str::trim).filter(|v| !v.is_empty()).map(str::to_string)
}

fn check_vocabulary(label: &str, value: Option<&str>, allowed: &[&str]) -> Result<(), String> {
    match value {
        Some(value) if !allowed.iter().any(|known| known.eq_ignore_ascii_case(value)) => Err(
            format!("Unknown {label} '{value}'. Valid values are {}.", allowed.join(", ")),
        ),
        _ => Ok(()),
    }
}

/// Collapse listings of the same economic fund, preserving the ranked order.
///
/// `etf_id` identifies a listing. `VUSA-LSE` and `VUSA-XETRA` are one Irish
/// share class with one ISIN, so the engine scores them identically and they are
/// one investment candidate — but as two rows they take two of the five slots in
/// "the five highest-scoring ETFs", and a research summary counts the fund twice.
/// The first listing of each fund in ranked order represents it, and the rest are
/// named on it rather than dropped silently.
///
/// Resolution is untouched: an ambiguous ticker is still reported as ambiguous,
/// because collapsing a *ranking* and guessing which listing a *mutation* meant
/// are entirely different things.
#[allow(clippy::type_complexity)]
fn collapse_listings(
    ranked: &[(EtfRow, Evaluation)],
) -> Vec<(&EtfRow, &Evaluation, Vec<&str>)> {
    let mut representatives: Vec<(&EtfRow, &Evaluation, Vec<&str>)> = Vec::new();
    let mut index_of: std::collections::BTreeMap<&str, usize> = std::collections::BTreeMap::new();
    for (etf, evaluation) in ranked {
        match index_of.get(etf.fund_identity()) {
            Some(position) => representatives[*position].2.push(etf.etf_id.as_str()),
            None => {
                index_of.insert(etf.fund_identity(), representatives.len());
                representatives.push((etf, evaluation, Vec::new()));
            }
        }
    }
    representatives
}

#[derive(Clone)]
pub struct EtfMcpServer {
    pool: PgPool,
    rules: Arc<RulesSpec>,
    profile: Arc<InvestorProfile>,
    approvals: ApprovalVerifier,
    tool_router: ToolRouter<Self>,
}

impl EtfMcpServer {
    pub fn new(
        pool: PgPool,
        rules: Arc<RulesSpec>,
        profile: Arc<InvestorProfile>,
        approval_secret: Arc<Vec<u8>>,
    ) -> Self {
        Self {
            pool,
            rules,
            profile,
            approvals: ApprovalVerifier::new(approval_secret),
            tool_router: Self::tool_router(),
        }
    }

    /// The approval verifier, so the HTTP layer can read a token's action before
    /// dispatching to the tool that will verify it in full.
    pub fn approvals(&self) -> &ApprovalVerifier {
        &self.approvals
    }

    /// Recompute the deterministic evaluation. Always from the stored row and the
    /// configured profile, never from anything a caller supplied.
    fn evaluate(&self, etf: &EtfRow) -> Evaluation {
        rules::evaluate(&self.rules, &self.profile, &etf.facts())
    }

    /// Resolve a caller-supplied identifier to exactly one ETF, or explain why it
    /// could not be resolved.
    async fn resolve(&self, query: &str) -> Result<Result<EtfRow, CallToolResult>, McpError> {
        let query = query.trim();
        if query.is_empty() {
            return Ok(Err(tool_error("An ETF identifier is required")));
        }
        let matches = store::resolve_etf_ids(&self.pool, query).await?;
        match matches.len() {
            0 => Ok(Err(tool_error(format!(
                "No ETF matches '{query}'. Try search_etfs to find the canonical etf_id."
            )))),
            1 => match store::fetch_etf(&self.pool, &matches[0]).await? {
                Some(etf) => Ok(Ok(etf)),
                None => Ok(Err(tool_error(format!("ETF '{query}' was not found")))),
            },
            // A ticker or ISIN can name several listings of the same fund. Picking
            // one silently would make the canonical identifier optional in
            // practice and let a mutation land on the wrong listing.
            _ => Ok(Err(tool_error(format!(
                "'{query}' matches {} listings: {}. Use the exact etf_id.",
                matches.len(),
                matches.join(", ")
            )))),
        }
    }

    /// The general policy strings, kept in their own labelled block.
    ///
    /// They apply to every ETF, so nested alongside one evaluation they would
    /// read as facts about *that* ETF.
    fn policy_block(&self) -> Value {
        json!({
            "applies_to": "every ETF; these are policy statements, not facts about this ETF",
            "rules_version": self.rules.version,
            "decision_order": self.rules.decision_order,
            "decision_thresholds": self.rules.decision_thresholds,
            "hard_constraints": self.rules.hard_constraints,
            "missing_data_policy": self.rules.missing_data_policy,
            "decision_caps": self.rules.decision_caps,
            "override_policy": self.rules.override_policy,
            "score_meaning": rules::SCORE_MEANING
        })
    }

    /// The parts of the investor profile a decision actually depends on.
    fn profile_block(&self) -> Value {
        json!({
            "profile_id": self.profile.profile_id,
            "version": self.profile.version,
            "base_currency": self.profile.base_currency,
            "investment_horizon_years": self.profile.investment_horizon_years,
            "strategy": self.profile.strategy,
            "risk_tolerance": self.profile.risk_tolerance,
            "hard_constraints": self.profile.hard_constraints,
            "preferences": self.profile.preferences
        })
    }

    fn history_json(etf_id: &str, history: &EtfHistory) -> Value {
        json!({
            "etf_id": etf_id,
            "event_count": history.events.len(),
            "total_event_count": history.total_event_count,
            "truncated": history.truncated(),
            "events": history.events
        })
    }

    async fn research_summary(&self) -> Result<Value, McpError> {
        // Decisions are recomputed rather than read from the stored column, which
        // is only the *committed* record and is null until a human approves one.
        // A summary built from that column would report the whole universe as
        // undecided.
        //
        // Counted per *listing* and again per *fund*, because the two are
        // different questions and the snapshot contains a cross-listed fund. "How
        // many shortlist?" answered over listings double-counts one candidate;
        // answered over funds it does not. Publishing both, labelled, is the only
        // version a reader can act on.
        let mut deterministic: std::collections::BTreeMap<String, i64> = DECISIONS
            .iter()
            .map(|decision| ((*decision).to_string(), 0))
            .collect();
        let mut by_fund: std::collections::BTreeMap<String, i64> = deterministic.clone();
        let mut seen_funds: std::collections::BTreeSet<String> = std::collections::BTreeSet::new();
        let mut hard_constraint_rejections = 0_i64;
        let mut capped = 0_i64;
        let mut listings = 0_i64;
        for etf in store::all_etfs(&self.pool).await? {
            let evaluation = self.evaluate(&etf);
            listings += 1;
            *deterministic.entry(evaluation.decision.clone()).or_default() += 1;
            if seen_funds.insert(etf.fund_identity().to_string()) {
                *by_fund.entry(evaluation.decision.clone()).or_default() += 1;
            }
            if !evaluation.hard_constraints.is_empty() {
                hard_constraint_rejections += 1;
            }
            if !evaluation.applied_caps.is_empty() {
                capped += 1;
            }
        }
        let (assigned, unassigned) = store::assignment_counts(&self.pool).await?;

        Ok(json!({
            "universe": {
                "listings": listings,
                "distinct_funds": seen_funds.len(),
                "note": "A listing is one share class on one exchange; a fund is one ISIN. The \
shipped snapshot cross-lists one fund on two venues, so these differ by design."
            },
            "by_deterministic_decision": deterministic,
            "by_deterministic_decision_per_fund": by_fund,
            "by_committed_decision": store::group_counts(&self.pool, "decision").await?,
            "by_review_state": store::group_counts(&self.pool, "review_state").await?,
            "by_asset_class": store::group_counts(&self.pool, "asset_class").await?,
            "by_region": store::group_counts(&self.pool, "region").await?,
            "by_provider": store::group_counts(&self.pool, "provider").await?,
            "assignment": { "assigned": assigned, "unassigned": unassigned },
            "hard_constraint_rejections": hard_constraint_rejections,
            "decisions_capped_by_policy": capped,
            "note": "Every count except *_per_fund is per exchange listing. by_committed_decision \
counts decisions humans have approved, and its 'undecided' bucket holds everything nobody has \
reviewed; by_deterministic_decision is what the engine says about the same universe now. Counts \
describe the research universe in data/etfs.json. This is not a statement of holdings: this \
system has no brokerage connection and holds no positions."
        }))
    }
}

#[tool_router]
impl EtfMcpServer {
    #[tool(
        description = "Search the ETF research universe. Every result carries current_evaluation — the deterministic decision and investment score recomputed now from the rules engine and the investor profile — and, separately, workflow.committed_* — what a human previously approved, which is null until they do. The `decision` and `min_investment_score` filters and the descending score ordering all apply to current_evaluation, so they work on a universe nobody has reviewed; `committed_decision` filters the workflow record instead. Listings of one cross-listed fund collapse to a single result unless include_all_listings is true. Filters: query on etf_id/ticker/ISIN/name, provider, asset_class, region, ucits, distribution_policy, replication, review_state, assigned_to, research_needed_only, decision, min_investment_score, committed_decision."
    )]
    pub async fn search_etfs(
        &self,
        Parameters(args): Parameters<SearchEtfsArgs>,
    ) -> Result<CallToolResult, McpError> {
        let limit = args.limit.unwrap_or(DEFAULT_SEARCH_LIMIT).clamp(1, MAX_SEARCH_LIMIT);
        let query = non_empty(&args.query);
        let provider = non_empty(&args.provider);
        let asset_class = non_empty(&args.asset_class);
        let region = non_empty(&args.region);
        let distribution_policy = non_empty(&args.distribution_policy);
        let replication = non_empty(&args.replication);
        let review_state = non_empty(&args.review_state);
        let decision = non_empty(&args.decision);
        let committed_decision = non_empty(&args.committed_decision);
        let assigned_to = non_empty(&args.assigned_to);

        // An unrecognised filter used to return an empty result set, which reads
        // as "no such ETFs" rather than "you asked for a value that does not
        // exist". Say which it is.
        for (label, value, allowed) in [
            ("asset_class", asset_class.as_deref(), &ASSET_CLASSES[..]),
            ("distribution_policy", distribution_policy.as_deref(), &DISTRIBUTION_POLICIES[..]),
            ("replication", replication.as_deref(), &REPLICATIONS[..]),
            ("review_state", review_state.as_deref(), &REVIEW_STATES[..]),
            ("decision", decision.as_deref(), &DECISIONS[..]),
            ("committed_decision", committed_decision.as_deref(), &DECISIONS[..]),
        ] {
            if let Err(message) = check_vocabulary(label, value, allowed) {
                return Ok(tool_error(message));
            }
        }
        if let Some(score) = args.min_investment_score
            && !(0..=100).contains(&score)
        {
            return Ok(tool_error("min_investment_score must be between 0 and 100"));
        }

        // Canonicalise the two decision filters before comparing them.
        //
        // The vocabulary check above matches case-insensitively, and the remaining
        // text filters are compared by SQL with LOWER()/UPPER() on both sides. These
        // two are compared in Rust, so without this they would accept "SHORTLIST"
        // as valid and then match nothing — validation and matching disagreeing
        // about case is the asymmetry this codebase avoids everywhere else, and it
        // fails silently as "no such ETFs".
        let decision = match decision.as_deref().map(rules::normalize_decision).transpose() {
            Ok(value) => value,
            Err(message) => return Ok(tool_error(message)),
        };
        let committed_decision =
            match committed_decision.as_deref().map(rules::normalize_decision).transpose() {
                Ok(value) => value,
                Err(message) => return Ok(tool_error(message)),
            };

        let research_needed_only = args.research_needed_only.unwrap_or(false);
        let include_all_listings = args.include_all_listings.unwrap_or(false);

        // SQL answers only what it stores. Everything the deterministic engine
        // decides — the decision, the score, the ranking — is computed here, from
        // the same `rules::evaluate` every other read path calls. There is no
        // second implementation of the policy in SQL, and no stored score to go
        // stale when `rules_spec.json` changes.
        let candidates = store::search_etfs(
            &self.pool,
            &EtfFilters {
                query: query.as_deref(),
                provider: provider.as_deref(),
                asset_class: asset_class.as_deref(),
                region: region.as_deref(),
                ucits: args.ucits,
                distribution_policy: distribution_policy.as_deref(),
                replication: replication.as_deref(),
                review_state: review_state.as_deref(),
                assigned_to: assigned_to.as_deref(),
                research_needed_only,
            },
        )
        .await?;
        let matched_listings = candidates.len();

        let mut scored: Vec<(EtfRow, Evaluation)> = candidates
            .into_iter()
            .filter(|etf| match committed_decision.as_deref() {
                Some(wanted) => etf.decision.as_deref() == Some(wanted),
                None => true,
            })
            .map(|etf| {
                let evaluation = self.evaluate(&etf);
                (etf, evaluation)
            })
            .filter(|(_, evaluation)| match decision.as_deref() {
                Some(wanted) => evaluation.decision == wanted,
                None => true,
            })
            .filter(|(_, evaluation)| match args.min_investment_score {
                Some(minimum) => evaluation.investment_score >= minimum,
                None => true,
            })
            .collect();

        // Score descending, then etf_id ascending. The tie-break is not cosmetic:
        // without it two funds on the same score come back in whatever order the
        // planner chose, and "the five highest-scoring ETFs" is a different five
        // between calls.
        scored.sort_by(|left, right| {
            right
                .1
                .investment_score
                .cmp(&left.1.investment_score)
                .then_with(|| left.0.etf_id.cmp(&right.0.etf_id))
        });

        let ranked = if include_all_listings {
            scored.iter().map(|(etf, evaluation)| (etf, evaluation, Vec::new())).collect()
        } else {
            collapse_listings(&scored)
        };
        let matched_funds = if include_all_listings {
            scored
                .iter()
                .map(|(etf, _)| etf.fund_identity())
                .collect::<std::collections::BTreeSet<_>>()
                .len()
        } else {
            ranked.len()
        };

        let etfs: Vec<Value> = ranked
            .iter()
            .take(limit as usize)
            .map(|(etf, evaluation, others)| etf_search_model(etf, evaluation, others))
            .collect();

        Ok(tool_json(json!({
            "count": etfs.len(),
            "matched_listings": matched_listings,
            "matched_after_filters": ranked.len(),
            "distinct_funds_matched": matched_funds,
            "truncated": ranked.len() > etfs.len(),
            "ordering": "current_evaluation.investment_score descending, then etf_id ascending",
            "grouping": if include_all_listings {
                "one row per exchange listing"
            } else {
                "one row per economic fund (ISIN); other_listings_of_this_fund names the rest"
            },
            "filters": {
                "query": query,
                "provider": provider,
                "asset_class": asset_class,
                "region": region,
                "ucits": args.ucits,
                "distribution_policy": distribution_policy,
                "replication": replication,
                "review_state": review_state,
                "decision": decision,
                "min_investment_score": args.min_investment_score,
                "committed_decision": committed_decision,
                "assigned_to": assigned_to,
                "research_needed_only": research_needed_only,
                "include_all_listings": include_all_listings,
                "limit": limit
            },
            "etfs": etfs,
            "note": "current_evaluation is the authoritative deterministic result, recomputed for \
this request; it is what the ordering and the decision/min_investment_score filters use. \
workflow.committed_* is the historical record of a human decision and is null until one is \
approved. Call evaluate_etf or get_etf for the full component breakdown."
        })))
    }

    #[tool(
        description = "Read one ETF. Accepts an exact etf_id, or a ticker/ISIN/name which is resolved to one; an ambiguous ticker is reported rather than guessed, because one fund can be cross-listed under the same ticker. Returns verified structured data, untrusted free text, workflow state, data provenance and the deterministic evaluation recomputed now. Never mutates state."
    )]
    pub async fn get_etf(
        &self,
        Parameters(args): Parameters<GetEtfArgs>,
    ) -> Result<CallToolResult, McpError> {
        let etf = match self.resolve(&args.etf).await? {
            Ok(etf) => etf,
            Err(error) => return Ok(error),
        };
        let evaluation = self.evaluate(&etf);
        Ok(tool_json(etf_read_model(&etf, &evaluation)))
    }

    #[tool(
        description = "Run the deterministic evaluation engine for one ETF and return the investment score, the decision, every score component, matched rules, hard constraints, missing data, data completeness and profile fit. Optionally compare a model recommendation and report whether default policy permits it: a model may be equal or more conservative, never more optimistic. Read-only for ETF state; records the evaluation in the append-only history."
    )]
    pub async fn evaluate_etf(
        &self,
        Parameters(args): Parameters<EvaluateEtfArgs>,
    ) -> Result<CallToolResult, McpError> {
        let etf = match self.resolve(&args.etf).await? {
            Ok(etf) => etf,
            Err(error) => return Ok(error),
        };
        let evaluation = self.evaluate(&etf);
        let recommendation = args
            .llm_recommendation
            .as_deref()
            .map(rules::normalize_decision)
            .transpose()
            .map_err(invalid_params)?;
        let comparison = recommendation
            .as_deref()
            .map(|value| rules::compare_recommendation(&evaluation.decision, value));

        let mut tx = self.pool.begin().await.map_err(store::database_error)?;
        store::write_audit(
            &mut tx,
            AuditEvent {
                previous_state: Some(&etf.review_state),
                new_state: Some(&etf.review_state),
                rules_decision: Some(&evaluation.decision),
                llm_recommendation: recommendation.as_deref(),
                investment_score: Some(evaluation.investment_score),
                justification: Some("Deterministic evaluation requested"),
                rules_version: Some(&evaluation.rules_version),
                profile_version: Some(&evaluation.profile_version),
                // No request ID. This used to be a tool argument, which let the
                // model write arbitrary correlation identifiers into an
                // append-only table the rest of the system treats as
                // authoritative. The mutation paths bind request_id
                // cryptographically inside the signed approval token; a read-only
                // evaluation has no equivalent trusted source, so it records none
                // rather than an asserted one. These events stay correlated
                // through the OpenTelemetry trace, which carries the gateway
                // request ID.
                details: json!({
                    "score_decision": evaluation.score_decision,
                    "applied_caps": evaluation.applied_caps,
                    "hard_constraints": evaluation.hard_constraints
                }),
                ..AuditEvent::new(&etf.etf_id, "system", "deterministic-rules-engine", "ETF_EVALUATED")
            },
        )
        .await?;
        tx.commit().await.map_err(store::database_error)?;

        Ok(tool_json(json!({
            "etf_id": etf.etf_id,
            // The inputs the decision was derived from. Without these the tool
            // asks the model to explain a decision while withholding its own
            // premises, and a model that cannot see the inputs assembles a
            // justification from whatever related text is still in scope.
            "etf_facts": etf.verified_facts(),
            "investor_profile": self.profile_block(),
            // The prose describing the fund is stored, not computed, so it is
            // separated from the numbers that produced the decision.
            "untrusted_free_text": untrusted_free_text(&etf),
            "data_provenance": etf.provenance(),
            "evaluation": evaluation,
            "llm_recommendation": recommendation,
            "policy_comparison": comparison,
            "policy": self.policy_block()
        })))
    }

    #[tool(
        description = "Return counts across the ETF research universe: by deterministic decision, by committed decision, by review state, by asset class, by region, by provider, and assigned versus unassigned. This describes research candidates, not holdings; this system has no brokerage connection and owns no positions."
    )]
    pub async fn get_research_summary(&self) -> Result<CallToolResult, McpError> {
        // Deliberately argument-free: the summary always covers the whole
        // universe, and an empty schema is honest about that. A placeholder
        // parameter would be a field every client has to reason about and no
        // caller can use.
        Ok(tool_json(self.research_summary().await?))
    }

    #[tool(
        description = "Return a minimal, explicitly grounded fact bundle for explaining or comparing one ETF. The model must use only these facts and must not invent fund characteristics, fees, holdings, exposures or performance that are not present."
    )]
    pub async fn get_research_context(
        &self,
        Parameters(args): Parameters<ResearchContextArgs>,
    ) -> Result<CallToolResult, McpError> {
        let etf = match self.resolve(&args.etf).await? {
            Ok(etf) => etf,
            Err(error) => return Ok(error),
        };
        let evaluation = self.evaluate(&etf);
        Ok(tool_json(json!({
            "etf_id": etf.etf_id,
            "verified_metrics": etf.verified_facts(),
            "deterministic_conclusions": {
                "investment_score": evaluation.investment_score,
                "decision": evaluation.decision,
                "score_decision": evaluation.score_decision,
                "components": evaluation.components,
                "matched_rules": evaluation.matched_rules,
                "hard_constraints": evaluation.hard_constraints,
                "applied_caps": evaluation.applied_caps,
                "missing_data": evaluation.missing_data,
                "data_completeness": evaluation.data_completeness,
                "profile_fit": evaluation.profile_fit,
                "explanation": evaluation.explanation
            },
            "investor_profile": self.profile_block(),
            "data_provenance": etf.provenance(),
            "untrusted_free_text": untrusted_free_text(&etf),
            // What an explanation must contain. The constraints below are
            // entirely prohibitions, and a model that obeys them perfectly still
            // produces an incomplete answer, because nothing asked for the
            // deterministic result. Stating the required elements is the
            // counterpart to stating the forbidden ones.
            "required_elements": [
                "The etf_id, the ticker and the fund name.",
                "The deterministic decision and the investment score, stated explicitly as the rules engine's determination.",
                "The score components that drove the result, named from deterministic_conclusions.components.",
                "Any hard constraint or policy cap that applied, and what it means.",
                "Any missing metric, and the effect it had on the decision.",
                "The data_as_of date whenever recency is relevant to the claim being made."
            ],
            "explanation_constraints": [
                "Use only verified_metrics and deterministic_conclusions for factual claims.",
                "Do not assert returns, yields, holdings, sector or country exposures, fees, issuer facts or risk statistics that are not present here.",
                "Never present the investment score as a prediction of future return, and never state or imply a guaranteed outcome.",
                "Historical figures under context_only_not_scored are context, not forecast.",
                "Clearly distinguish verified data from model interpretation.",
                "This system supports research decisions only. It cannot buy, sell or hold anything."
            ]
        })))
    }

    #[tool(
        description = "Commit the initial review decision for an ETF after human confirmation. Only an UNREVIEWED ETF can be committed; this is the initial decision and is valid exactly once. State-changing and always approval-gated. Pass only etf_id, approval_token and request_id: the decision, override flag, rationale and research note are read from the signed approval token, so the committed decision is exactly the one the human approved. The deterministic result is recomputed and re-enforced, a more-optimistic model recommendation is rejected, overrides require a logged rationale, and hard constraints still apply."
    )]
    pub async fn commit_evaluation(
        &self,
        Parameters(args): Parameters<CommitEvaluationArgs>,
    ) -> Result<CallToolResult, McpError> {
        let etf_id = args.etf_id.trim();

        // The row is locked for the whole decision, so the precondition, the
        // deterministic result the approval is bound to, and the write all observe
        // one state and no concurrent transition can interleave. Every rejection
        // below returns before the commit, which rolls the transaction back and
        // releases the lock.
        let mut tx = self.pool.begin().await.map_err(store::database_error)?;
        let Some(etf) = store::lock_etf(&mut tx, etf_id).await? else {
            return Ok(tool_error(format!("ETF '{etf_id}' was not found")));
        };

        // The initial decision is valid exactly once. Checked before the token,
        // like the assignment precondition: an action that is not available on
        // this candidate never reaches its approval at all.
        if etf.review_state != "UNREVIEWED" {
            return Ok(tool_error(format!(
                "Only unreviewed ETFs can take an initial decision; '{etf_id}' is {}. Re-deciding a \
reviewed candidate is a separate action and is not implemented.",
                etf.review_state
            )));
        }
        let evaluation = self.evaluate(&etf);

        // Every mutation parameter comes from the signed token, never from tool
        // arguments. The model cannot drop, reword, or upgrade any part of what
        // the human approved, because it does not get to restate it.
        let claims = match self.approvals.verify(
            &args.approval_token,
            "commit",
            etf_id,
            &args.request_id,
            Some(&evaluation.decision),
        ) {
            Ok(claims) => claims,
            Err(message) => return Ok(tool_error(format!("Human approval rejected: {message}"))),
        };

        let Some(requested) = claims.requested_decision.as_deref() else {
            return Ok(tool_error("Approval token is missing the approved decision"));
        };

        // The whole trust matrix, in one pure function. The deterministic decision
        // is the default; a model recommendation is advisory and may never be more
        // optimistic; anything the human chose that differs from the engine is an
        // override and must declare itself as one.
        let authority = match rules::reconcile_decision(
            &evaluation.decision,
            claims.llm_recommendation.as_deref(),
            requested,
            claims.override_requested,
        ) {
            Ok(authority) => authority,
            Err(message) => return Ok(tool_error(message)),
        };
        let recommendation = authority.llm_recommendation.clone();
        let final_decision = authority.requested_decision.clone();
        let override_applied = authority.override_applied;
        let human_override = authority.human_override_decision.clone();

        let override_rationale = claims.effective_override_rationale();
        if override_applied && override_rationale.is_none() {
            return Ok(tool_error(
                "A human override in either direction requires a non-empty rationale",
            ));
        }
        if !override_applied && override_rationale.is_some() {
            return Ok(tool_error("Unexpected override rationale on a non-override action"));
        }

        // Persist exactly the note the human approved.
        let research_note = claims.effective_research_note();
        if final_decision == "shortlist" && research_note.is_none() {
            return Ok(tool_error(
                "Shortlisting requires a grounded research note recording why this is a candidate",
            ));
        }
        if let Some(hit) = rules::blocking_hard_constraint(
            &self.rules,
            &self.profile,
            &etf.facts(),
            &final_decision,
            override_applied,
        ) {
            return Ok(tool_error(format!("Hard constraint {}: {}", hit.code, hit.message)));
        }

        let new_state = match final_decision.as_str() {
            "reject" => "REJECTED",
            "research" => "RESEARCH",
            "shortlist" => "SHORTLISTED",
            // `normalize_decision` admits nothing else.
            other => unreachable!("unrecognised decision {other}"),
        };

        store::consume_approval_token(&mut tx, &claims).await?;
        store::apply_evaluation(&mut tx, etf_id, new_state, &final_decision, &evaluation, research_note)
            .await?;
        store::write_audit(
            &mut tx,
            AuditEvent {
                previous_state: Some(&etf.review_state),
                new_state: Some(new_state),
                rules_decision: Some(&evaluation.decision),
                llm_recommendation: recommendation.as_deref(),
                final_decision: Some(&final_decision),
                investment_score: Some(evaluation.investment_score),
                override_applied,
                override_rationale,
                justification: Some(if override_applied {
                    "Human override applied after explicit rationale"
                } else {
                    "Human confirmed the state-changing review decision"
                }),
                request_id: Some(&args.request_id),
                rules_version: Some(&evaluation.rules_version),
                profile_version: Some(&evaluation.profile_version),
                details: json!({
                    "approval_nonce": claims.nonce,
                    "score_decision": evaluation.score_decision,
                    "applied_caps": evaluation.applied_caps,
                    "decision_authority": authority
                }),
                ..AuditEvent::new(etf_id, "human", &claims.actor_id, "EVALUATION_COMMITTED")
            },
        )
        .await?;
        tx.commit().await.map_err(store::database_error)?;

        Ok(tool_json(json!({
            "etf_id": etf_id,
            "previous_state": etf.review_state,
            "new_state": new_state,
            // Three named roles, never merged: the engine decides, the model
            // advises, the human chooses.
            "rules_decision": evaluation.decision,
            "llm_recommendation": recommendation,
            "default_decision": authority.default_decision,
            "human_override_decision": human_override,
            "final_decision": final_decision,
            "investment_score": evaluation.investment_score,
            "rules_version": evaluation.rules_version,
            "profile_version": evaluation.profile_version,
            "override_applied": override_applied,
            "override_rationale": override_rationale,
            "actor_id": claims.actor_id,
            "note": "default_decision is the deterministic engine's result and is what stands \
without a human override; a model recommendation is advisory in either direction and never \
becomes the default. The ETF is now an investment candidate at this decision level. No position \
has been opened and no order has been placed; this system cannot trade."
        })))
    }

    #[tool(
        description = "Add an ETF to the shortlist after human confirmation. Available from UNREVIEWED or RESEARCH. Pass only etf_id, approval_token and request_id: the grounded research note is read from the signed approval token bound to action=shortlist. When the deterministic decision is only research, shortlisting is a human override and requires a rationale. A non-bypassable hard constraint can never be shortlisted past. Shortlisting records an investment candidate; it does not buy anything."
    )]
    pub async fn shortlist_etf(
        &self,
        Parameters(args): Parameters<ShortlistEtfArgs>,
    ) -> Result<CallToolResult, McpError> {
        let etf_id = args.etf_id.trim();

        let mut tx = self.pool.begin().await.map_err(store::database_error)?;
        let Some(etf) = store::lock_etf(&mut tx, etf_id).await? else {
            return Ok(tool_error(format!("ETF '{etf_id}' was not found")));
        };
        // Shortlisting stays available before a candidate has been decided as well
        // as after a research decision. It is not a way to revive a rejected or
        // already-assigned candidate.
        if etf.review_state != "UNREVIEWED" && etf.review_state != "RESEARCH" {
            return Ok(tool_error(format!(
                "Only unreviewed or in-research ETFs can be shortlisted; '{etf_id}' is {}.",
                etf.review_state
            )));
        }
        let evaluation = self.evaluate(&etf);
        let claims = match self.approvals.verify(
            &args.approval_token,
            "shortlist",
            etf_id,
            &args.request_id,
            Some(&evaluation.decision),
        ) {
            Ok(claims) => claims,
            Err(message) => return Ok(tool_error(format!("Human approval rejected: {message}"))),
        };
        if claims.requested_decision.as_deref() != Some("shortlist") {
            return Ok(tool_error("Approval token does not authorize a shortlist"));
        }
        // Same reconciliation as the initial decision, so the two paths cannot
        // disagree about what counts as an override. Shortlisting above the
        // deterministic decision is a promotion, which only a human may make and
        // only with a recorded rationale.
        let authority = match rules::reconcile_decision(
            &evaluation.decision,
            claims.llm_recommendation.as_deref(),
            "shortlist",
            claims.override_requested,
        ) {
            Ok(authority) => authority,
            Err(message) => return Ok(tool_error(message)),
        };
        let promotion = authority.override_applied;
        let override_rationale = claims.effective_override_rationale();
        if promotion && override_rationale.is_none() {
            return Ok(tool_error(
                "Shortlisting above the deterministic decision requires a non-empty human rationale",
            ));
        }
        if !promotion && override_rationale.is_some() {
            return Ok(tool_error("Unexpected override rationale on a non-override action"));
        }
        let Some(research_note) = claims.effective_research_note() else {
            return Ok(tool_error("Shortlisting requires a non-empty grounded research note"));
        };
        if let Some(hit) = rules::blocking_hard_constraint(
            &self.rules,
            &self.profile,
            &etf.facts(),
            "shortlist",
            claims.override_requested,
        ) {
            return Ok(tool_error(format!("Hard constraint {}: {}", hit.code, hit.message)));
        }

        store::consume_approval_token(&mut tx, &claims).await?;
        store::apply_shortlist(&mut tx, etf_id, &evaluation, research_note).await?;
        store::write_audit(
            &mut tx,
            AuditEvent {
                previous_state: Some(&etf.review_state),
                new_state: Some("SHORTLISTED"),
                rules_decision: Some(&evaluation.decision),
                llm_recommendation: claims.llm_recommendation.as_deref(),
                final_decision: Some("shortlist"),
                investment_score: Some(evaluation.investment_score),
                override_applied: promotion,
                override_rationale,
                justification: Some(if promotion {
                    "Human shortlisted above the deterministic decision, with rationale"
                } else {
                    "Human confirmed the shortlist"
                }),
                request_id: Some(&args.request_id),
                rules_version: Some(&evaluation.rules_version),
                profile_version: Some(&evaluation.profile_version),
                details: json!({
                    "approval_nonce": claims.nonce,
                    "decision_authority": authority
                }),
                ..AuditEvent::new(etf_id, "human", &claims.actor_id, "ETF_SHORTLISTED")
            },
        )
        .await?;
        tx.commit().await.map_err(store::database_error)?;

        Ok(tool_json(json!({
            "etf_id": etf_id,
            "previous_state": etf.review_state,
            "new_state": "SHORTLISTED",
            "rules_decision": evaluation.decision,
            "llm_recommendation": authority.llm_recommendation,
            "default_decision": authority.default_decision,
            "final_decision": "shortlist",
            "investment_score": evaluation.investment_score,
            "rules_version": evaluation.rules_version,
            "profile_version": evaluation.profile_version,
            "override_applied": promotion,
            "override_rationale": override_rationale,
            "actor_id": claims.actor_id,
            "note": "Shortlisted as an investment candidate. No position was opened and no order \
was placed."
        })))
    }

    #[tool(
        description = "Assign a research owner to an ETF after human confirmation. Available from RESEARCH, SHORTLISTED or ASSIGNED; reassignment is permitted. Unreviewed and rejected ETFs are not assignable. Pass only etf_id, approval_token and request_id: the owner is read from the signed approval token. A shortlisted ETF must already carry a research note. Assignment means this person owns the next research decision; it does not execute anything."
    )]
    pub async fn assign_etf(
        &self,
        Parameters(args): Parameters<AssignEtfArgs>,
    ) -> Result<CallToolResult, McpError> {
        let etf_id = args.etf_id.trim();

        let mut tx = self.pool.begin().await.map_err(store::database_error)?;
        let Some(etf) = store::lock_etf(&mut tx, etf_id).await? else {
            return Ok(tool_error(format!("ETF '{etf_id}' was not found")));
        };
        // Reassignment of an ASSIGNED ETF is deliberately allowed; the guard is
        // against handing someone work that has not been decided or is finished.
        if !ASSIGNABLE_STATES.contains(&etf.review_state.as_str()) {
            return Ok(tool_error(format!(
                "'{etf_id}' is {} and cannot be assigned; only ETFs in {} can be.",
                etf.review_state,
                ASSIGNABLE_STATES.join(", ")
            )));
        }
        // A shortlisted candidate must carry its reasoning before someone owns the
        // follow-up. Checked on either signal, since state and decision are
        // written together and a mismatch would mean the invariant is broken.
        if (etf.review_state == "SHORTLISTED" || etf.decision.as_deref() == Some("shortlist"))
            && etf.research_note.as_deref().map(str::trim).filter(|v| !v.is_empty()).is_none()
        {
            return Ok(tool_error(
                "Shortlisted ETFs require a grounded research note before a research owner is assigned",
            ));
        }
        let evaluation = self.evaluate(&etf);
        let claims =
            match self.approvals.verify(&args.approval_token, "assign", etf_id, &args.request_id, None)
            {
                Ok(claims) => claims,
                Err(message) => {
                    return Ok(tool_error(format!("Human approval rejected: {message}")))
                }
            };
        let Some(assignee) = claims.effective_assignee() else {
            return Ok(tool_error("Approval token does not carry a research owner"));
        };

        store::consume_approval_token(&mut tx, &claims).await?;
        store::apply_assignment(&mut tx, etf_id, assignee).await?;
        store::write_audit(
            &mut tx,
            AuditEvent {
                previous_state: Some(&etf.review_state),
                new_state: Some("ASSIGNED"),
                justification: Some("Human confirmed the research owner"),
                request_id: Some(&args.request_id),
                // The decision columns are deliberately absent. An assignment
                // creates no decision, and this event used to fill them with the
                // committed decision and score from whenever that decision was
                // made, stamped with the rules and profile versions in force
                // *now*. After a policy change that is a coherent-looking snapshot
                // of two different policies. Both generations are recorded below,
                // each with its own versions.
                details: json!({
                    "assignee": assignee,
                    "previous_assignee": etf.assigned_to,
                    "approval_nonce": claims.nonce,
                    "policy_generations": policy_generations(&etf, &evaluation)
                }),
                ..AuditEvent::new(etf_id, "human", &claims.actor_id, "ETF_ASSIGNED")
            },
        )
        .await?;
        tx.commit().await.map_err(store::database_error)?;

        Ok(tool_json(json!({
            "etf_id": etf_id,
            "previous_state": etf.review_state,
            "new_state": "ASSIGNED",
            "assignee": assignee,
            "actor_id": claims.actor_id,
            "committed_snapshot": etf.committed_snapshot(),
            "current_evaluation": {
                "decision": evaluation.decision,
                "investment_score": evaluation.investment_score,
                "rules_version": evaluation.rules_version,
                "profile_version": evaluation.profile_version
            },
            "note": "This person now owns the next research decision for this candidate. The \
committed decision is unchanged by an assignment; the current evaluation is reported separately \
because the policy may have moved since. Nothing was bought, sold or held."
        })))
    }

    #[tool(
        description = "Return the append-only history for one ETF: deterministic evaluations, scores, decisions, human approvals, state transitions, assignments, overrides and rationales, with actor, request correlation and the rules/profile versions in force. Returns the most recent events in chronological order and reports whether older events were omitted."
    )]
    pub async fn get_etf_history(
        &self,
        Parameters(args): Parameters<EtfHistoryArgs>,
    ) -> Result<CallToolResult, McpError> {
        let etf = match self.resolve(&args.etf).await? {
            Ok(etf) => etf,
            Err(error) => return Ok(error),
        };
        let limit = args.limit.unwrap_or(store::DEFAULT_AUDIT_EVENTS);
        let history = store::fetch_etf_history(&self.pool, &etf.etf_id, limit).await?;
        Ok(tool_json(Self::history_json(&etf.etf_id, &history)))
    }
}

/// `router = self.tool_router` is not decoration. Left to its default the macro
/// calls `Self::tool_router()` on every `tools/call` and `tools/list`, rebuilding
/// the JSON schema of every tool per request while the instance's own router sat
/// unread — which is exactly what the dead-code warning on that field reports.
#[tool_handler(router = self.tool_router)]
impl ServerHandler for EtfMcpServer {
    fn get_info(&self) -> ServerInfo {
        ServerInfo::new(ServerCapabilities::builder().enable_tools().enable_resources().build())
            .with_protocol_version(ProtocolVersion::V_2024_11_05)
            .with_server_info(Implementation::from_build_env())
            .with_instructions(
                "ETF research MCP: use Resources for read-only policy and profile context and \
Tools for queries or actions. All mutations require a short-lived human approval token. The \
deterministic engine is authoritative by default; a model may be equal or more conservative but \
never more optimistic, while a human override requires a logged rationale and can never bypass a \
non-bypassable hard constraint. investment_score is quality and profile fit for a dated data \
snapshot, not a return forecast. Never invent ETF facts. This service supports research decisions \
only and has no brokerage capability."
                    .to_string(),
            )
    }

    async fn list_resources(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListResourcesResult, McpError> {
        Ok(ListResourcesResult {
            resources: vec![
                Resource::new("etf://rules", "evaluation-rules")
                    .with_description(
                        "Exact rules_spec.json used by the deterministic evaluation engine",
                    )
                    .with_mime_type("application/json"),
                Resource::new("etf://investor-profile", "investor-profile")
                    .with_description(
                        "The investor profile every ETF is evaluated against: hard constraints and preferences",
                    )
                    .with_mime_type("application/json"),
                Resource::new("etf://research-summary", "research-summary")
                    .with_description(
                        "Counts by deterministic decision, review state, asset class, region, provider and assignment",
                    )
                    .with_mime_type("application/json"),
            ],
            next_cursor: None,
            meta: None,
        })
    }

    async fn list_resource_templates(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListResourceTemplatesResult, McpError> {
        Ok(ListResourceTemplatesResult {
            resource_templates: vec![
                ResourceTemplate::new("etf://etfs/{etf_id}", "etf-detail").with_description(
                    "Read-only current state of one ETF, with the deterministic evaluation recomputed now",
                ),
                ResourceTemplate::new("etf://etfs/{etf_id}/history", "etf-history")
                    .with_description("Read-only append-only history for one ETF"),
            ],
            next_cursor: None,
            meta: None,
        })
    }

    /// Resources are a second door onto the same data, so they render through the
    /// same read models as the tools. When they do not, the ETF resource omits the
    /// deterministic evaluation and the provenance block that `get_etf` was
    /// specifically built to carry, and the weaker path becomes the one an
    /// injected note can talk over.
    async fn read_resource(
        &self,
        request: ReadResourceRequestParams,
        _context: RequestContext<RoleServer>,
    ) -> Result<ReadResourceResult, McpError> {
        let uri = request.uri.as_str();

        let payload: Option<Value> = if uri == "etf://rules" {
            Some(serde_json::to_value(&*self.rules).map_err(serialisation_error)?)
        } else if uri == "etf://investor-profile" {
            Some(serde_json::to_value(&*self.profile).map_err(serialisation_error)?)
        } else if uri == "etf://research-summary" {
            Some(self.research_summary().await?)
        } else if let Some(rest) = uri.strip_prefix("etf://etfs/") {
            match rest.strip_suffix("/history") {
                Some(etf_id) => {
                    let history =
                        store::fetch_etf_history(&self.pool, etf_id, store::MAX_AUDIT_EVENTS).await?;
                    Some(Self::history_json(etf_id, &history))
                }
                None => match store::fetch_etf(&self.pool, rest).await? {
                    Some(etf) => {
                        let evaluation = self.evaluate(&etf);
                        Some(etf_read_model(&etf, &evaluation))
                    }
                    None => None,
                },
            }
        } else {
            None
        };

        let Some(payload) = payload else {
            return Err(McpError::resource_not_found(
                "resource_not_found",
                Some(json!({ "uri": request.uri })),
            ));
        };
        let text = serde_json::to_string_pretty(&payload).map_err(serialisation_error)?;
        Ok(ReadResourceResult::new(vec![
            ResourceContents::text(text, uri).with_mime_type("application/json")
        ]))
    }
}

fn serialisation_error(error: serde_json::Error) -> McpError {
    tracing::error!(%error, "failed to serialise a resource");
    McpError::internal_error("Failed to render the requested resource".to_string(), None)
}
