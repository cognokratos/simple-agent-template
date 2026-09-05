//! Every statement this service issues against the ETF research database.
//!
//! One module owns the schema, so a column added to `etfs` or `audit_events` has
//! exactly one place to be threaded through, and the mutation paths cannot
//! quietly diverge in how they read or lock a row.

use std::collections::BTreeMap;

use rmcp::ErrorData as McpError;
use serde_json::{json, Value};
use sqlx::{PgPool, Postgres, Transaction};

use crate::approval::ApprovalClaims;
use crate::domain::{AuditRow, EtfRow, SeedEtf};
use crate::rules::Evaluation;

pub type Tx<'a> = Transaction<'a, Postgres>;

/// The projection behind [`EtfRow`], defined once.
///
/// A macro rather than a `const`, because sqlx 0.9 only accepts statements it can
/// see are static — a runtime-assembled query string is rejected outright, which
/// is the right guard to keep.
macro_rules! select_etf {
    ($tail:literal) => {
        concat!(
            "SELECT etf_id, ticker, isin, name, provider, exchange, asset_class, region, \
             index_name, domicile, ucits, distribution_policy, replication, ter, aum_usd, \
             fund_age_years, holdings_count, top_10_concentration, tracking_difference_3y, \
             volatility_3y, return_3y_annualized, description, data_as_of, sources, \
             review_state, decision, investment_score, decided_rules_version, \
             decided_profile_version, assigned_to, research_note, updated_at \
             FROM etfs ",
            $tail
        )
    };
}

/// The projection behind [`AuditRow`], defined once.
macro_rules! select_audit {
    ($tail:literal) => {
        concat!(
            "SELECT id, etf_id, occurred_at, actor_type, actor_id, action, previous_state, \
             new_state, rules_decision, llm_recommendation, final_decision, investment_score, \
             override_applied, override_rationale, justification, request_id, rules_version, \
             profile_version, details \
             FROM audit_events ",
            $tail
        )
    };
}

/// Largest history page a caller can ask for, and the default.
pub const MAX_AUDIT_EVENTS: i64 = 200;
pub const DEFAULT_AUDIT_EVENTS: i64 = 50;

/// Turn a database failure into a client-facing error.
///
/// The cause is logged, never returned: the client here is a language model whose
/// context reaches a user's screen, and raw driver text carries table, column and
/// constraint names for no benefit to the caller.
pub fn database_error(error: sqlx::Error) -> McpError {
    tracing::error!(%error, "database operation failed");
    McpError::internal_error("The ETF research database operation failed".to_string(), None)
}

/// Read one ETF without locking. For read-only paths only.
pub async fn fetch_etf(pool: &PgPool, etf_id: &str) -> Result<Option<EtfRow>, McpError> {
    sqlx::query_as::<_, EtfRow>(select_etf!("WHERE etf_id = $1"))
        .bind(etf_id)
        .fetch_optional(pool)
        .await
        .map_err(database_error)
}

/// Resolve a user-supplied identifier to a canonical `etf_id`.
///
/// A ticker is convenient and is *not* unique: the shipped snapshot deliberately
/// contains one fund cross-listed under the same ticker and ISIN on two
/// exchanges. So an exact `etf_id` wins outright, and an ambiguous ticker or name
/// is reported as ambiguous rather than silently resolved to whichever row the
/// planner happened to return first.
pub async fn resolve_etf_ids(pool: &PgPool, query: &str) -> Result<Vec<String>, McpError> {
    let rows = sqlx::query_as::<_, (String,)>(
        r#"
        SELECT etf_id FROM etfs
        WHERE UPPER(etf_id) = UPPER($1)
           OR UPPER(ticker) = UPPER($1)
           OR UPPER(isin)   = UPPER($1)
           OR LOWER(name)   = LOWER($1)
        ORDER BY etf_id
        "#,
    )
    .bind(query)
    .fetch_all(pool)
    .await
    .map_err(database_error)?;
    let exact: Vec<String> = rows
        .iter()
        .filter(|(etf_id,)| etf_id.eq_ignore_ascii_case(query))
        .map(|(etf_id,)| etf_id.clone())
        .collect();
    if !exact.is_empty() {
        return Ok(exact);
    }
    Ok(rows.into_iter().map(|(etf_id,)| etf_id).collect())
}

/// Read one ETF *and hold its row* for the rest of the transaction.
///
/// Every mutation goes through this. Reading the row on the pool and then opening
/// a transaction to write made each state precondition a check rather than an
/// invariant: two approvals racing on the same ETF both observed UNREVIEWED, both
/// passed, and both committed, leaving the history with two transitions out of a
/// state only one of them left. The lock makes the precondition, the deterministic
/// recomputation the approval is bound to, and the write a single atomic decision.
///
/// `SELECT` alone would not do it: the default isolation level is READ COMMITTED,
/// under which an unlocked read inside a transaction is just as stale.
pub async fn lock_etf(tx: &mut Tx<'_>, etf_id: &str) -> Result<Option<EtfRow>, McpError> {
    sqlx::query_as::<_, EtfRow>(select_etf!("WHERE etf_id = $1 FOR UPDATE"))
        .bind(etf_id)
        .fetch_optional(&mut **tx)
        .await
        .map_err(database_error)
}

/// The filters SQL can answer: every one of them reads a column that is *stored*
/// reference data or workflow state.
///
/// Deterministic decision and score are deliberately absent. They are not columns;
/// they are recomputed by the engine on every read, so filtering or ordering on
/// them here would mean either a second implementation of the scoring policy in
/// SQL or a stored value that goes stale the moment `rules_spec.json` changes.
/// The server applies those filters in Rust against the freshly computed
/// evaluation — see `EtfMcpServer::search_etfs`.
#[derive(Debug, Clone, Default)]
pub struct EtfFilters<'a> {
    pub query: Option<&'a str>,
    pub provider: Option<&'a str>,
    pub asset_class: Option<&'a str>,
    pub region: Option<&'a str>,
    pub ucits: Option<bool>,
    pub distribution_policy: Option<&'a str>,
    pub replication: Option<&'a str>,
    pub review_state: Option<&'a str>,
    pub assigned_to: Option<&'a str>,
    pub research_needed_only: bool,
}

/// Candidate rows for the deterministic ranking, in a stable order.
///
/// Unbounded on purpose: the caller must see every match before it can rank on a
/// score this query cannot compute, and truncating here would silently drop the
/// highest-scoring fund whenever it sorted late alphabetically. The universe is a
/// shipped snapshot of a few dozen funds; the limit the caller asked for is
/// applied after the engine has run.
pub async fn search_etfs(pool: &PgPool, filters: &EtfFilters<'_>) -> Result<Vec<EtfRow>, McpError> {
    sqlx::query_as::<_, EtfRow>(select_etf!(
        r#"
        WHERE ($1::TEXT IS NULL
                 OR UPPER(etf_id) LIKE '%' || UPPER($1) || '%'
                 OR UPPER(ticker) LIKE '%' || UPPER($1) || '%'
                 OR UPPER(isin)   LIKE '%' || UPPER($1) || '%'
                 OR LOWER(name)   LIKE '%' || LOWER($1) || '%')
          AND ($2::TEXT IS NULL OR LOWER(provider) = LOWER($2))
          AND ($3::TEXT IS NULL OR LOWER(asset_class) = LOWER($3))
          AND ($4::TEXT IS NULL OR LOWER(region) = LOWER($4))
          AND ($5::BOOLEAN IS NULL OR ucits = $5)
          AND ($6::TEXT IS NULL OR LOWER(distribution_policy) = LOWER($6))
          AND ($7::TEXT IS NULL OR LOWER(replication) = LOWER($7))
          AND ($8::TEXT IS NULL OR UPPER(review_state) = UPPER($8))
          AND ($9::TEXT IS NULL OR LOWER(assigned_to) = LOWER($9))
          AND ($10::BOOLEAN = FALSE OR review_state IN ('UNREVIEWED', 'RESEARCH'))
        ORDER BY etf_id ASC
        "#
    ))
    .bind(filters.query)
    .bind(filters.provider)
    .bind(filters.asset_class)
    .bind(filters.region)
    .bind(filters.ucits)
    .bind(filters.distribution_policy)
    .bind(filters.replication)
    .bind(filters.review_state)
    .bind(filters.assigned_to)
    .bind(filters.research_needed_only)
    .fetch_all(pool)
    .await
    .map_err(database_error)
}

/// Every ETF, for the paths that must recompute the deterministic result rather
/// than trust a stored score. Bounded by the size of the shipped snapshot.
pub async fn all_etfs(pool: &PgPool) -> Result<Vec<EtfRow>, McpError> {
    sqlx::query_as::<_, EtfRow>(select_etf!("ORDER BY etf_id"))
        .fetch_all(pool)
        .await
        .map_err(database_error)
}

/// Grouped counts for the research summary.
///
/// `BTreeMap`, not `HashMap`: the grouped JSON used to come out in Rust's
/// randomised hash order, so two identical calls produced two different
/// documents. That is noise in a model's context and in any diff of recorded
/// output.
pub async fn group_counts(
    pool: &PgPool,
    column: &str,
) -> Result<BTreeMap<String, i64>, McpError> {
    // The column name is chosen from a fixed allow-list by the caller, never from
    // caller input, and each variant is a distinct static statement so nothing is
    // interpolated into SQL.
    let statement = match column {
        "review_state" => "SELECT review_state, COUNT(*)::BIGINT FROM etfs GROUP BY 1 ORDER BY 1",
        "asset_class" => "SELECT asset_class, COUNT(*)::BIGINT FROM etfs GROUP BY 1 ORDER BY 1",
        "region" => "SELECT region, COUNT(*)::BIGINT FROM etfs GROUP BY 1 ORDER BY 1",
        "provider" => "SELECT provider, COUNT(*)::BIGINT FROM etfs GROUP BY 1 ORDER BY 1",
        "decision" => {
            "SELECT COALESCE(decision, 'undecided'), COUNT(*)::BIGINT FROM etfs GROUP BY 1 ORDER BY 1"
        }
        other => {
            tracing::error!(column = other, "group_counts called with an unsupported column");
            return Err(McpError::internal_error(
                "Unsupported grouping".to_string(),
                None,
            ));
        }
    };
    let rows = sqlx::query_as::<_, (String, i64)>(statement)
        .fetch_all(pool)
        .await
        .map_err(database_error)?;
    Ok(rows.into_iter().collect())
}

pub async fn assignment_counts(pool: &PgPool) -> Result<(i64, i64), McpError> {
    sqlx::query_as::<_, (i64, i64)>(
        r#"
        SELECT COUNT(*) FILTER (WHERE assigned_to IS NOT NULL)::BIGINT,
               COUNT(*) FILTER (WHERE assigned_to IS NULL)::BIGINT
        FROM etfs
        "#,
    )
    .fetch_one(pool)
    .await
    .map_err(database_error)
}

/// Apply a committed evaluation decision. The research note is only overwritten
/// when the approval carried one.
///
/// The score and the rules/profile versions are written together, from the same
/// evaluation, so the committed record always names the policy generation that
/// produced it. Writing the score without its versions is what makes a later
/// report unable to tell a v1 score from a v2 one.
pub async fn apply_evaluation(
    tx: &mut Tx<'_>,
    etf_id: &str,
    new_state: &str,
    decision: &str,
    evaluation: &Evaluation,
    research_note: Option<&str>,
) -> Result<(), McpError> {
    sqlx::query(
        r#"
        UPDATE etfs
        SET review_state=$2, decision=$3, investment_score=$4,
            decided_rules_version=$5, decided_profile_version=$6,
            research_note=COALESCE($7, research_note), updated_at=NOW()
        WHERE etf_id=$1
        "#,
    )
    .bind(etf_id)
    .bind(new_state)
    .bind(decision)
    .bind(evaluation.investment_score)
    .bind(&evaluation.rules_version)
    .bind(&evaluation.profile_version)
    .bind(research_note)
    .execute(&mut **tx)
    .await
    .map_err(database_error)?;
    Ok(())
}

pub async fn apply_shortlist(
    tx: &mut Tx<'_>,
    etf_id: &str,
    evaluation: &Evaluation,
    research_note: &str,
) -> Result<(), McpError> {
    sqlx::query(
        r#"
        UPDATE etfs
        SET review_state='SHORTLISTED', decision='shortlist', investment_score=$2,
            decided_rules_version=$3, decided_profile_version=$4,
            research_note=$5, updated_at=NOW()
        WHERE etf_id=$1
        "#,
    )
    .bind(etf_id)
    .bind(evaluation.investment_score)
    .bind(&evaluation.rules_version)
    .bind(&evaluation.profile_version)
    .bind(research_note)
    .execute(&mut **tx)
    .await
    .map_err(database_error)?;
    Ok(())
}

pub async fn apply_assignment(
    tx: &mut Tx<'_>,
    etf_id: &str,
    assignee: &str,
) -> Result<(), McpError> {
    sqlx::query(
        "UPDATE etfs SET review_state='ASSIGNED', assigned_to=$2, updated_at=NOW() WHERE etf_id=$1",
    )
    .bind(etf_id)
    .bind(assignee)
    .execute(&mut **tx)
    .await
    .map_err(database_error)?;
    Ok(())
}

/// Burn an approval token's nonce, in the same transaction as the mutation it
/// authorises, so a replay cannot produce a second state change.
pub async fn consume_approval_token(
    tx: &mut Tx<'_>,
    claims: &ApprovalClaims,
) -> Result<(), McpError> {
    let result = sqlx::query(
        r#"
        INSERT INTO consumed_approval_tokens (nonce, etf_id, action, actor_id, request_id)
        VALUES ($1,$2,$3,$4,$5)
        ON CONFLICT (nonce) DO NOTHING
        "#,
    )
    .bind(&claims.nonce)
    .bind(&claims.etf_id)
    .bind(&claims.action)
    .bind(&claims.actor_id)
    .bind(&claims.request_id)
    .execute(&mut **tx)
    .await
    .map_err(database_error)?;

    if result.rows_affected() != 1 {
        return Err(McpError::invalid_params(
            "approval token has already been consumed".to_string(),
            Some(json!({ "etf_id": claims.etf_id, "action": claims.action })),
        ));
    }
    Ok(())
}

/// One append-only history record.
///
/// A struct rather than eighteen positional parameters. Six consecutive
/// `Option<&str>` arguments meant that transposing, say, `llm_recommendation` and
/// `final_decision` compiled cleanly and wrote the wrong record. Named fields make
/// that class of mistake unrepresentable.
///
/// **The decision columns are one coherent snapshot, or they are absent.**
/// `rules_decision`, `llm_recommendation`, `final_decision`, `investment_score`,
/// `rules_version` and `profile_version` must all describe the *same* evaluation.
/// An event that is not itself a decision — an assignment, say — leaves them null
/// and records the facts it does have under [`policy_generations`] instead. The
/// alternative was an assignment event carrying a committed score of 84 from
/// rules v1 alongside a `rules_version` of v2, which reads as a single snapshot
/// and is not one.
#[derive(Debug, Clone)]
pub struct AuditEvent<'a> {
    pub etf_id: &'a str,
    pub actor_type: &'a str,
    pub actor_id: &'a str,
    pub action: &'a str,
    pub previous_state: Option<&'a str>,
    pub new_state: Option<&'a str>,
    pub rules_decision: Option<&'a str>,
    pub llm_recommendation: Option<&'a str>,
    pub final_decision: Option<&'a str>,
    pub investment_score: Option<i32>,
    pub override_applied: bool,
    pub override_rationale: Option<&'a str>,
    pub justification: Option<&'a str>,
    pub request_id: Option<&'a str>,
    pub rules_version: Option<&'a str>,
    pub profile_version: Option<&'a str>,
    pub details: Value,
}

impl<'a> AuditEvent<'a> {
    /// The four fields the schema requires; everything else defaults to absent
    /// and is filled in with struct-update syntax at the call site.
    pub fn new(etf_id: &'a str, actor_type: &'a str, actor_id: &'a str, action: &'a str) -> Self {
        Self {
            etf_id,
            actor_type,
            actor_id,
            action,
            previous_state: None,
            new_state: None,
            rules_decision: None,
            llm_recommendation: None,
            final_decision: None,
            investment_score: None,
            override_applied: false,
            override_rationale: None,
            justification: None,
            request_id: None,
            rules_version: None,
            profile_version: None,
            details: json!({}),
        }
    }
}

pub async fn write_audit(tx: &mut Tx<'_>, event: AuditEvent<'_>) -> Result<(), McpError> {
    sqlx::query(
        r#"
        INSERT INTO audit_events (
            etf_id, actor_type, actor_id, action, previous_state, new_state,
            rules_decision, llm_recommendation, final_decision,
            investment_score, override_applied, override_rationale,
            justification, request_id, rules_version, profile_version, details
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
        "#,
    )
    .bind(event.etf_id)
    .bind(event.actor_type)
    .bind(event.actor_id)
    .bind(event.action)
    .bind(event.previous_state)
    .bind(event.new_state)
    .bind(event.rules_decision)
    .bind(event.llm_recommendation)
    .bind(event.final_decision)
    .bind(event.investment_score)
    .bind(event.override_applied)
    .bind(event.override_rationale)
    .bind(event.justification)
    .bind(event.request_id)
    .bind(event.rules_version)
    .bind(event.profile_version)
    .bind(event.details)
    .execute(&mut **tx)
    .await
    .map_err(database_error)?;
    Ok(())
}

/// A page of history, honest about what it left out.
pub struct EtfHistory {
    pub total_event_count: i64,
    pub events: Vec<AuditRow>,
}

impl EtfHistory {
    pub fn truncated(&self) -> bool {
        (self.events.len() as i64) < self.total_event_count
    }
}

/// The most recent `limit` history events, returned oldest-first.
///
/// Every other read caps its output; this one used to return the entire history
/// unbounded into a model's context. Taking the newest slice and then restoring
/// chronological order keeps the useful end of a long trail while staying
/// readable as a sequence.
pub async fn fetch_etf_history(
    pool: &PgPool,
    etf_id: &str,
    limit: i64,
) -> Result<EtfHistory, McpError> {
    let limit = limit.clamp(1, MAX_AUDIT_EVENTS);
    let (total_event_count,) =
        sqlx::query_as::<_, (i64,)>("SELECT COUNT(*)::BIGINT FROM audit_events WHERE etf_id=$1")
            .bind(etf_id)
            .fetch_one(pool)
            .await
            .map_err(database_error)?;

    let mut events = sqlx::query_as::<_, AuditRow>(select_audit!(
        "WHERE etf_id=$1 ORDER BY occurred_at DESC, id DESC LIMIT $2"
    ))
    .bind(etf_id)
    .bind(limit)
    .fetch_all(pool)
    .await
    .map_err(database_error)?;
    events.reverse();

    Ok(EtfHistory { total_event_count, events })
}

/// Insert or refresh one seeded ETF. Review state, committed decision, committed
/// score, assignment and research note are never touched: seeding refreshes the
/// reference data, it does not reopen decided candidates — and it does not invent
/// a decision either.
///
/// The committed score is deliberately not seeded. It used to be, so that
/// `search_etfs` could `ORDER BY investment_score` in SQL; the consequence was a
/// column documented as "null until a human approves" that was in fact populated
/// for the whole universe from the moment the database came up, and that went
/// stale against any later change to `rules_spec.json`. Search now ranks on the
/// engine, so the column can mean exactly what it says.
pub async fn upsert_seed_etf(
    pool: &PgPool,
    etf: &SeedEtf,
    asset_class: &str,
    region: &str,
    distribution_policy: &str,
    replication: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        r#"
        INSERT INTO etfs (
          etf_id, ticker, isin, name, provider, exchange, asset_class, region, index_name,
          domicile, ucits, distribution_policy, replication, ter, aum_usd, fund_age_years,
          holdings_count, top_10_concentration, tracking_difference_3y, volatility_3y,
          return_3y_annualized, description, data_as_of, sources
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,
                  $21,$22,$23::DATE,$24)
        ON CONFLICT (etf_id) DO UPDATE SET
          ticker=EXCLUDED.ticker,
          isin=EXCLUDED.isin,
          name=EXCLUDED.name,
          provider=EXCLUDED.provider,
          exchange=EXCLUDED.exchange,
          asset_class=EXCLUDED.asset_class,
          region=EXCLUDED.region,
          index_name=EXCLUDED.index_name,
          domicile=EXCLUDED.domicile,
          ucits=EXCLUDED.ucits,
          distribution_policy=EXCLUDED.distribution_policy,
          replication=EXCLUDED.replication,
          ter=EXCLUDED.ter,
          aum_usd=EXCLUDED.aum_usd,
          fund_age_years=EXCLUDED.fund_age_years,
          holdings_count=EXCLUDED.holdings_count,
          top_10_concentration=EXCLUDED.top_10_concentration,
          tracking_difference_3y=EXCLUDED.tracking_difference_3y,
          volatility_3y=EXCLUDED.volatility_3y,
          return_3y_annualized=EXCLUDED.return_3y_annualized,
          description=EXCLUDED.description,
          data_as_of=EXCLUDED.data_as_of,
          sources=EXCLUDED.sources
        "#,
    )
    .bind(&etf.etf_id)
    .bind(&etf.ticker)
    .bind(&etf.isin)
    .bind(&etf.name)
    .bind(&etf.provider)
    .bind(&etf.exchange)
    .bind(asset_class)
    .bind(region)
    .bind(&etf.index_name)
    .bind(&etf.domicile)
    .bind(etf.ucits)
    .bind(distribution_policy)
    .bind(replication)
    .bind(etf.ter)
    .bind(etf.aum_usd)
    .bind(etf.fund_age_years)
    .bind(etf.holdings_count)
    .bind(etf.top_10_concentration)
    .bind(etf.tracking_difference_3y)
    .bind(etf.volatility_3y)
    .bind(etf.return_3y_annualized)
    .bind(&etf.description)
    .bind(&etf.data_as_of)
    .bind(serde_json::to_value(&etf.sources).unwrap_or_else(|_| json!([])))
    .execute(pool)
    .await?;
    Ok(())
}
