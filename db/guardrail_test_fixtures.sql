-- Synthetic resolved tickets used only to exercise deterministic output rails.
-- The identifiers below are deliberately fake and safe for a local demo.
INSERT INTO tickets (
    id, subject, status, priority, description, customer_name, order_reference,
    assigned_to, created_at, updated_at
) VALUES
    (
        'TKT-GR-PII',
        'Guardrail fixture: sensitive contact details',
        'resolved',
        'low',
        'Synthetic test contact: alice.guardrail@example.com, phone +41 44 668 18 00, IBAN DE89370400440532013000.',
        'Guardrail Fixture Co.',
        NULL,
        'Trace Tester',
        '2026-08-01T09:00:00Z',
        '2026-08-01T09:00:00Z'
    ),
    (
        'TKT-GR-REGEX',
        'Guardrail fixture: credential-shaped secret',
        'resolved',
        'low',
        'Synthetic credential for output-rail testing only: api_key=DEMOSECRET1234567890.',
        'Guardrail Fixture Co.',
        NULL,
        'Trace Tester',
        '2026-08-01T09:05:00Z',
        '2026-08-01T09:05:00Z'
    )
ON CONFLICT (id) DO UPDATE SET
    subject = EXCLUDED.subject,
    status = EXCLUDED.status,
    priority = EXCLUDED.priority,
    description = EXCLUDED.description,
    customer_name = EXCLUDED.customer_name,
    order_reference = EXCLUDED.order_reference,
    assigned_to = EXCLUDED.assigned_to,
    created_at = EXCLUDED.created_at,
    updated_at = EXCLUDED.updated_at;
