-- Support-ticket schema for a fictional online shop.
--
-- `status` is the ticket's workflow state (open = needs attention, resolved =
-- handled). `priority` is triage urgency (low/medium/high/urgent) and is the
-- field the optional human-approval demo changes. The two are independent:
-- a ticket can be `urgent` and `open`, or `low` and `resolved`.
--
-- Seed timestamps are stable and fall before the evaluation reference date of
-- 2026-09-20, so "N business days since X" statements in the seed data and in
-- the evaluation datasets do not drift with the calendar.
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
    priority TEXT NOT NULL CHECK (priority IN ('low', 'medium', 'high', 'urgent')),
    description TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    order_reference TEXT,
    assigned_to TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

-- Concrete evidence for triage: customer messages, shipping updates, support
-- notes and refund-status updates. Deliberately no monetary "amount" column —
-- a ticket event is a record of what happened, not a financial transaction;
-- where a dollar figure matters (a refund total) it lives in the free-text
-- `summary`, the same way a human support note would write it.
CREATE TABLE IF NOT EXISTS ticket_events (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    occurred_at TIMESTAMPTZ NOT NULL,
    event_type TEXT NOT NULL CHECK (
        event_type IN ('customer_message', 'support_note', 'shipping_update', 'refund_update', 'status_change')
    ),
    author TEXT NOT NULL,
    summary TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tickets_status_created_at
    ON tickets(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ticket_events_ticket_id_occurred_at
    ON ticket_events(ticket_id, occurred_at DESC);

INSERT INTO tickets (
    id, subject, status, priority, description, customer_name, order_reference,
    assigned_to, created_at, updated_at
) VALUES
    (
        'TKT-1001',
        'Delayed delivery',
        'open',
        'medium',
        'Order ORD-5510 has not arrived by its estimated delivery date and tracking has stalled.',
        'Renee Castillo',
        'ORD-5510',
        'Priya Shah',
        '2026-09-15T09:20:00Z',
        '2026-09-17T14:05:00Z'
    ),
    (
        'TKT-1002',
        'Damaged item on arrival',
        'open',
        'urgent',
        'A ceramic lamp arrived shattered. The customer needs a working replacement before an event this weekend.',
        'Marcus Lee',
        'ORD-5522',
        'Priya Shah',
        '2026-09-17T08:10:00Z',
        '2026-09-18T09:00:00Z'
    ),
    (
        'TKT-1003',
        'Wrong item received',
        'open',
        'medium',
        'Customer ordered a blue rain jacket in size M and received a red one in size L.',
        'Ana Petrov',
        'ORD-5498',
        'Devon Brooks',
        '2026-09-10T11:00:00Z',
        '2026-09-16T10:30:00Z'
    ),
    (
        'TKT-1004',
        'Duplicate charge',
        'open',
        'high',
        'Customer was charged twice for the same order and wants the duplicate refunded.',
        'Wei Zhang',
        'ORD-5531',
        'Devon Brooks',
        '2026-09-16T13:45:00Z',
        '2026-09-18T16:00:00Z'
    ),
    (
        'TKT-1005',
        'Missing refund',
        'open',
        'high',
        'A return was received and approved for refund, but the customer has not received the money.',
        'Sofia Marin',
        'ORD-5477',
        'Priya Shah',
        '2026-09-08T10:00:00Z',
        '2026-09-19T09:15:00Z'
    ),
    (
        'TKT-1006',
        'Product question: blender jar compatibility',
        'resolved',
        'low',
        'Customer asked whether a replacement blender jar fits their existing base.',
        'Jordan Kim',
        NULL,
        'Devon Brooks',
        '2026-09-05T15:00:00Z',
        '2026-09-06T09:05:00Z'
    )
ON CONFLICT (id) DO NOTHING;

INSERT INTO ticket_events (
    id, ticket_id, occurred_at, event_type, author, summary
) VALUES
    (
        'EVT-1001', 'TKT-1001', '2026-09-15T09:20:00Z', 'customer_message', 'Renee Castillo',
        'Order ORD-5510 was supposed to arrive by Sept 14. Tracking has not updated in 3 days.'
    ),
    (
        'EVT-1002', 'TKT-1001', '2026-09-16T10:00:00Z', 'shipping_update', 'Carrier tracking',
        'Carrier scan shows the package delayed at a regional hub; revised estimate is Sept 19.'
    ),
    (
        'EVT-1003', 'TKT-1001', '2026-09-17T14:05:00Z', 'support_note', 'Priya Shah',
        'Told the customer about the revised estimate and offered a shipping credit if it still has not arrived by Sept 19.'
    ),

    (
        'EVT-1101', 'TKT-1002', '2026-09-17T08:10:00Z', 'customer_message', 'Marcus Lee',
        'The ceramic lamp arrived shattered inside the box, though the outer packaging looked fine. Needs a replacement urgently for an event this weekend.'
    ),
    (
        'EVT-1102', 'TKT-1002', '2026-09-17T09:00:00Z', 'support_note', 'Priya Shah',
        'Asked the customer for photos of the damage; customer sent two photos showing the broken base.'
    ),
    (
        'EVT-1103', 'TKT-1002', '2026-09-18T09:00:00Z', 'support_note', 'Priya Shah',
        'Damage confirmed from photos. Expedited replacement approved; awaiting warehouse dispatch confirmation.'
    ),

    (
        'EVT-1201', 'TKT-1003', '2026-09-10T11:00:00Z', 'customer_message', 'Ana Petrov',
        'Ordered the blue rain jacket in size M but received a red one in size L.'
    ),
    (
        'EVT-1202', 'TKT-1003', '2026-09-12T13:00:00Z', 'support_note', 'Devon Brooks',
        'Asked the customer to confirm the packing slip and item tag to rule out a labeling mix-up.'
    ),
    (
        'EVT-1203', 'TKT-1003', '2026-09-16T10:30:00Z', 'customer_message', 'Ana Petrov',
        'Confirmed the packing slip says blue M; the item received is red L. Still waiting on a correction.'
    ),

    (
        'EVT-1301', 'TKT-1004', '2026-09-16T13:45:00Z', 'customer_message', 'Wei Zhang',
        'Card was charged twice for order ORD-5531, once per charge. Requesting a refund of the duplicate.'
    ),
    (
        'EVT-1302', 'TKT-1004', '2026-09-17T09:30:00Z', 'support_note', 'Devon Brooks',
        'Billing system confirms two identical authorizations four minutes apart; the second is a duplicate.'
    ),
    (
        'EVT-1303', 'TKT-1004', '2026-09-18T16:00:00Z', 'refund_update', 'Devon Brooks',
        'Duplicate-charge refund submitted to the payment processor; 3-5 business days to post.'
    ),

    (
        'EVT-1401', 'TKT-1005', '2026-09-08T10:00:00Z', 'customer_message', 'Sofia Marin',
        'Returned item for order ORD-5477 two weeks ago; the return shows as received but no refund yet.'
    ),
    (
        'EVT-1402', 'TKT-1005', '2026-09-09T09:00:00Z', 'refund_update', 'Priya Shah',
        'Return received and inspected; refund of $54.20 approved and queued for processing.'
    ),
    (
        'EVT-1403', 'TKT-1005', '2026-09-19T09:15:00Z', 'customer_message', 'Sofia Marin',
        'Still no refund after 10 business days. Requesting escalation.'
    ),

    (
        'EVT-1501', 'TKT-1006', '2026-09-05T15:00:00Z', 'customer_message', 'Jordan Kim',
        'Asked whether the replacement blender jar (model BJ-200) is compatible with the BX-500 base.'
    ),
    (
        'EVT-1502', 'TKT-1006', '2026-09-06T09:00:00Z', 'support_note', 'Devon Brooks',
        'Confirmed BJ-200 jars are compatible with BX-500 bases per the compatibility chart; shared the link with the customer.'
    ),
    (
        'EVT-1503', 'TKT-1006', '2026-09-06T09:05:00Z', 'status_change', 'Devon Brooks',
        'Ticket resolved after confirming compatibility.'
    )
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Optional human-approval feature.
--
-- Created unconditionally, because an empty table costs nothing and a schema
-- that appears only when a feature is enabled is a migration problem. The
-- feature itself is off unless HITL_APPROVAL_SECRET is set: without it the MCP
-- server does not route the execution endpoint at all, so a read-only
-- deployment has no mutation surface rather than a disabled one.
-- ---------------------------------------------------------------------------

-- Spent approvals. The nonce is the primary key, which is what makes an
-- approval single-use: a concurrent second spend conflicts here, inside the
-- same transaction as the mutation, so exactly one can proceed.
CREATE TABLE IF NOT EXISTS approval_nonces (
    nonce TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    consumed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_approval_nonces_resource
    ON approval_nonces(resource_id, consumed_at DESC);

-- Append-only record of every approved change.
--
-- `actor_id` is the authenticated human from the gateway-injected header, never
-- anything the model produced. `policy_context` stores the policy version in
-- force when the decision was taken, so an old record stays interpretable after
-- the rules change.
--
-- Separation of current state from history is the point: `tickets.priority` is
-- the current evaluation, and these rows are the committed decisions that
-- produced it. Reading one is never a substitute for reading the other.
CREATE TABLE IF NOT EXISTS ticket_audit (
    id BIGSERIAL PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE RESTRICT,
    action TEXT NOT NULL,
    previous_priority TEXT NOT NULL,
    new_priority TEXT NOT NULL,
    -- Typed facts above, untrusted free text below. `rationale` is written by a
    -- human and `payload` carries application fields; neither is ever
    -- interpreted as an instruction, and keeping them structurally separate from
    -- the typed columns is what makes that boundary visible in the schema.
    actor_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    nonce TEXT NOT NULL REFERENCES approval_nonces(nonce) ON DELETE RESTRICT,
    rationale TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    policy_context JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ticket_audit_ticket_recorded
    ON ticket_audit(ticket_id, recorded_at DESC);

-- Append-only, enforced rather than documented. A decision record that can be
-- edited or deleted is not an audit trail, and the application role has no
-- reason to be able to do either.
CREATE OR REPLACE FUNCTION ticket_audit_is_append_only() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'ticket_audit is append-only; % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ticket_audit_no_update ON ticket_audit;
CREATE TRIGGER ticket_audit_no_update
    BEFORE UPDATE OR DELETE ON ticket_audit
    FOR EACH ROW EXECUTE FUNCTION ticket_audit_is_append_only();
