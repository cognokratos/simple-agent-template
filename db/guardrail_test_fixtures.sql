-- Synthetic closed alerts used only to exercise deterministic output rails.
-- The identifiers below are deliberately fake and safe for a local demo.
INSERT INTO alerts (
    id, title, status, severity, description, customer_name, assigned_to, created_at
) VALUES
    (
        'ALT-GR-PII',
        'Guardrail fixture: sensitive contact details',
        'closed',
        'low',
        'Synthetic test contact: alice.guardrail@example.com, phone +41 44 668 18 00, IBAN DE89370400440532013000.',
        'Guardrail Fixture GmbH',
        'Trace Tester',
        '2026-08-01T09:00:00Z'
    ),
    (
        'ALT-GR-REGEX',
        'Guardrail fixture: credential-shaped secret',
        'closed',
        'low',
        'Synthetic credential for output-rail testing only: api_key=DEMOSECRET1234567890.',
        'Guardrail Fixture GmbH',
        'Trace Tester',
        '2026-08-01T09:05:00Z'
    )
ON CONFLICT (id) DO UPDATE SET
    title = EXCLUDED.title,
    status = EXCLUDED.status,
    severity = EXCLUDED.severity,
    description = EXCLUDED.description,
    customer_name = EXCLUDED.customer_name,
    assigned_to = EXCLUDED.assigned_to,
    created_at = EXCLUDED.created_at;
