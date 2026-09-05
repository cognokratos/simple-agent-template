//! Loading the shipped ETF snapshot into the research database.

use anyhow::Context as _;
use sqlx::PgPool;

use crate::domain::SeedEtf;
use crate::rules::{self, InvestorProfile, RulesSpec};

/// Seed or refresh the ETF universe from `etfs.json`.
///
/// Reference data only. No decision and no score is written: those are the
/// *committed* record of a human approval, and seeding them would present the
/// whole universe as decided by nobody. The engine still runs here, so a fixture
/// that cannot be evaluated fails at boot rather than at the first query, and the
/// decision it produces is logged — but nothing about it is persisted, because
/// every read path recomputes it.
///
/// Text values are canonicalised on the way in. The database constrains the
/// vocabularies it can, but a mixed-case value from an upstream feed would
/// otherwise be stored verbatim, and matching that resolves case-insensitively in
/// one place and strictly in another is the dangerous asymmetry.
pub async fn seed_etfs(
    pool: &PgPool,
    rules: &RulesSpec,
    profile: &InvestorProfile,
    data_path: &str,
) -> anyhow::Result<()> {
    let raw = std::fs::read_to_string(data_path)
        .with_context(|| format!("failed to read the ETF snapshot from {data_path}"))?;
    let etfs: Vec<SeedEtf> = serde_json::from_str(&raw).context("etfs.json is invalid")?;
    anyhow::ensure!(!etfs.is_empty(), "etfs.json contains no ETFs");

    for seed in &etfs {
        // Provenance is checked before anything is written, so a record that
        // overstates its own evidence fails at boot rather than being served.
        seed.validate_sources().map_err(|error| anyhow::anyhow!(error))?;

        let facts = seed.facts();
        let evaluation = rules::evaluate(rules, profile, &facts);

        crate::store::upsert_seed_etf(
            pool,
            seed,
            &facts.asset_class,
            &facts.region,
            &facts.distribution_policy,
            &facts.replication,
        )
        .await
        .with_context(|| format!("failed to seed ETF {}", seed.etf_id))?;

        tracing::debug!(
            etf_id = %seed.etf_id,
            decision = %evaluation.decision,
            score = evaluation.investment_score,
            "seeded ETF"
        );
    }

    tracing::info!(
        count = etfs.len(),
        rules_version = %rules.version,
        profile = %profile.profile_id,
        "seeded the ETF universe"
    );
    Ok(())
}
