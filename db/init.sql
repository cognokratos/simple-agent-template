-- ETF research schema.
--
-- `etfs` holds the reference data, the workflow state and the committed decision.
-- `audit_events` is append-only at the database level. `consumed_approval_tokens`
-- makes a human approval spendable exactly once.

CREATE TABLE IF NOT EXISTS etfs (
    etf_id TEXT PRIMARY KEY,
    -- Convenient for lookup and deliberately NOT unique: one fund can be
    -- cross-listed under the same ticker and ISIN on several exchanges.
    ticker TEXT NOT NULL,
    isin TEXT NOT NULL,
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    exchange TEXT NOT NULL,

    asset_class TEXT NOT NULL
        CHECK (asset_class IN ('equity', 'bond', 'commodity', 'multi_asset', 'money_market')),
    region TEXT NOT NULL,
    index_name TEXT NOT NULL,
    domicile TEXT NOT NULL,

    ucits BOOLEAN NOT NULL,
    distribution_policy TEXT NOT NULL
        CHECK (distribution_policy IN ('accumulating', 'distributing', 'none')),
    replication TEXT NOT NULL
        CHECK (replication IN ('physical', 'sampled', 'synthetic')),

    -- Every metric is nullable on purpose. Real ETF reference data has gaps, and
    -- the deterministic engine has an explicit policy for them; a NOT NULL column
    -- would force a fabricated value instead.
    ter DOUBLE PRECISION CHECK (ter IS NULL OR ter >= 0),
    aum_usd BIGINT CHECK (aum_usd IS NULL OR aum_usd >= 0),
    fund_age_years DOUBLE PRECISION CHECK (fund_age_years IS NULL OR fund_age_years >= 0),
    holdings_count INTEGER CHECK (holdings_count IS NULL OR holdings_count >= 0),
    top_10_concentration DOUBLE PRECISION
        CHECK (top_10_concentration IS NULL OR top_10_concentration BETWEEN 0 AND 1),
    tracking_difference_3y DOUBLE PRECISION,
    volatility_3y DOUBLE PRECISION CHECK (volatility_3y IS NULL OR volatility_3y >= 0),
    return_3y_annualized DOUBLE PRECISION,

    -- Untrusted free text. Marked as such in every MCP read model.
    description TEXT NOT NULL DEFAULT '',

    data_as_of DATE NOT NULL,
    sources JSONB NOT NULL DEFAULT '[]'::jsonb,

    review_state TEXT NOT NULL DEFAULT 'UNREVIEWED'
        CHECK (review_state IN ('UNREVIEWED', 'RESEARCH', 'SHORTLISTED', 'ASSIGNED', 'REJECTED')),

    -- The *committed* decision: what a human approved, and the score as it stood
    -- when they approved it. NULL until that happens, and never written by the
    -- seeder — a pre-filled score would look like a decision nobody made.
    --
    -- The current authoritative result is not stored at all. It is recomputed from
    -- rules_spec.json and investor_profile.json on every read, so it cannot go
    -- stale against a policy change; these columns are history.
    decision TEXT CHECK (decision IN ('reject', 'research', 'shortlist')),
    investment_score INTEGER CHECK (investment_score BETWEEN 0 AND 100),
    -- Which policy generation produced the committed decision. Without these, a
    -- historical decision and a current evaluation are indistinguishable numbers
    -- and any report mixing them is silently incoherent.
    decided_rules_version TEXT,
    decided_profile_version TEXT,
    assigned_to TEXT,
    -- Untrusted free text, whoever wrote it.
    research_note TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS audit_events (
    id BIGSERIAL PRIMARY KEY,
    etf_id TEXT NOT NULL REFERENCES etfs(etf_id) ON DELETE RESTRICT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor_type TEXT NOT NULL CHECK (actor_type IN ('system', 'llm', 'human')),
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    previous_state TEXT,
    new_state TEXT,
    rules_decision TEXT
        CHECK (rules_decision IS NULL OR rules_decision IN ('reject', 'research', 'shortlist')),
    llm_recommendation TEXT
        CHECK (llm_recommendation IS NULL OR llm_recommendation IN ('reject', 'research', 'shortlist')),
    final_decision TEXT
        CHECK (final_decision IS NULL OR final_decision IN ('reject', 'research', 'shortlist')),
    investment_score INTEGER CHECK (investment_score IS NULL OR investment_score BETWEEN 0 AND 100),
    override_applied BOOLEAN NOT NULL DEFAULT FALSE,
    override_rationale TEXT,
    justification TEXT,
    request_id TEXT,
    -- Which policy produced the recorded decision. Without these, a historical
    -- decision cannot be reproduced after the rules or the profile change.
    rules_version TEXT,
    profile_version TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE OR REPLACE FUNCTION reject_audit_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events;
CREATE TRIGGER audit_events_append_only
BEFORE UPDATE OR DELETE ON audit_events
FOR EACH ROW EXECUTE FUNCTION reject_audit_event_mutation();

CREATE TABLE IF NOT EXISTS consumed_approval_tokens (
    nonce TEXT PRIMARY KEY,
    consumed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    etf_id TEXT NOT NULL REFERENCES etfs(etf_id) ON DELETE RESTRICT,
    action TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    request_id TEXT NOT NULL
);

-- Indexed on the columns the server actually filters by. There is deliberately no
-- index on investment_score: nothing sorts by the committed score, because search
-- ranks on the deterministic evaluation recomputed in Rust, not on a stored value.
CREATE INDEX IF NOT EXISTS idx_etfs_review
    ON etfs(review_state, asset_class, region);
CREATE INDEX IF NOT EXISTS idx_etfs_decision
    ON etfs(decision);
CREATE INDEX IF NOT EXISTS idx_etfs_ticker
    ON etfs(UPPER(ticker));
CREATE INDEX IF NOT EXISTS idx_etfs_isin
    ON etfs(UPPER(isin));
CREATE INDEX IF NOT EXISTS idx_etfs_assigned
    ON etfs(assigned_to);
CREATE INDEX IF NOT EXISTS idx_audit_etf_time
    ON audit_events(etf_id, occurred_at, id);
CREATE INDEX IF NOT EXISTS idx_consumed_approval_request
    ON consumed_approval_tokens(request_id, consumed_at);
