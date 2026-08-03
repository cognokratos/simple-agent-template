CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    severity TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    description TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    assigned_to TEXT,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    occurred_at TIMESTAMPTZ NOT NULL,
    amount DOUBLE PRECISION NOT NULL,
    currency TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('incoming', 'outgoing')),
    counterparty TEXT NOT NULL,
    country TEXT NOT NULL,
    risk_score INTEGER NOT NULL CHECK (risk_score BETWEEN 0 AND 100),
    description TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_status_created_at
    ON alerts(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_transactions_alert_id_occurred_at
    ON transactions(alert_id, occurred_at DESC);

INSERT INTO alerts (
    id, title, status, severity, description, customer_name, assigned_to, created_at
) VALUES
    (
        'ALT-1001',
        'Rapid movement of funds after onboarding',
        'open',
        'high',
        'A newly onboarded customer received several deposits and moved most funds to a foreign beneficiary within hours.',
        'Northstar Trading AG',
        'Maya Chen',
        '2026-08-03T08:15:00Z'
    ),
    (
        'ALT-1002',
        'Structuring across related counterparties',
        'open',
        'critical',
        'Multiple payments just below the monitoring threshold were sent to counterparties sharing ownership information.',
        'Blue Harbor Imports SA',
        'Jonas Weber',
        '2026-08-02T14:30:00Z'
    ),
    (
        'ALT-1003',
        'Unusual cash-equivalent purchase',
        'closed',
        'medium',
        'The transaction was reviewed and supported by customer documentation.',
        'Alpine Retail GmbH',
        'Sofia Rossi',
        '2026-07-29T10:00:00Z'
    )
ON CONFLICT (id) DO NOTHING;

INSERT INTO transactions (
    id, alert_id, occurred_at, amount, currency, direction,
    counterparty, country, risk_score, description
) VALUES
    (
        'TX-9001', 'ALT-1001', '2026-08-03T07:55:00Z', 125000.00, 'CHF',
        'incoming', 'Orion Consulting Ltd', 'GB', 68,
        'Incoming corporate payment received shortly after account activation.'
    ),
    (
        'TX-9002', 'ALT-1001', '2026-08-03T08:08:00Z', 118500.00, 'CHF',
        'outgoing', 'Baltic Components OÜ', 'EE', 82,
        'Most of the incoming funds were transferred to a new foreign beneficiary.'
    ),
    (
        'TX-9101', 'ALT-1002', '2026-08-02T09:12:00Z', 9800.00, 'EUR',
        'outgoing', 'Meridian Services Ltd', 'CY', 74,
        'Payment below the EUR 10,000 review threshold.'
    ),
    (
        'TX-9102', 'ALT-1002', '2026-08-02T10:05:00Z', 9750.00, 'EUR',
        'outgoing', 'Aster Holdings Ltd', 'CY', 79,
        'Second payment to a counterparty linked by beneficial ownership.'
    ),
    (
        'TX-9103', 'ALT-1002', '2026-08-02T11:43:00Z', 9900.00, 'EUR',
        'outgoing', 'Solstice Management Ltd', 'MT', 86,
        'Third related payment made within the same business day.'
    ),
    (
        'TX-9201', 'ALT-1003', '2026-07-29T09:35:00Z', 15000.00, 'CHF',
        'outgoing', 'Swiss Bullion Marketplace', 'CH', 45,
        'Purchase was supported by an invoice and matched the customer profile.'
    )
ON CONFLICT (id) DO NOTHING;
