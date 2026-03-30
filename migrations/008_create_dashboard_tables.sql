CREATE TABLE IF NOT EXISTS acos.expenses (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at      TIMESTAMPTZ DEFAULT now(),
  month           VARCHAR(7) NOT NULL, -- YYYY-MM
  vendor          TEXT NOT NULL,
  category        VARCHAR(100), -- Infrastructure, SaaS, Legal, etc.
  amount_cents    INTEGER NOT NULL,
  description     TEXT,
  source          VARCHAR(50) DEFAULT 'manual', -- manual, billing_intake
  gmail_message_id TEXT,
  reimbursable    BOOLEAN DEFAULT false
);

CREATE TABLE IF NOT EXISTS acos.founder_loans (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at      TIMESTAMPTZ DEFAULT now(),
  date            DATE NOT NULL,
  amount_cents    INTEGER NOT NULL, -- positive = advance, negative = repayment
  description     TEXT,
  balance_cents   INTEGER NOT NULL -- running balance
);

CREATE TABLE IF NOT EXISTS acos.mrr_snapshots (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  snapshot_date   DATE NOT NULL UNIQUE,
  mrr_cents       INTEGER NOT NULL DEFAULT 0,
  arr_cents       INTEGER GENERATED ALWAYS AS (mrr_cents * 12) STORED,
  client_count    INTEGER NOT NULL DEFAULT 0,
  notes           TEXT
);

CREATE TABLE IF NOT EXISTS acos.pipeline_events (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at      TIMESTAMPTZ DEFAULT now(),
  deal_id         UUID REFERENCES public.deals(id),
  from_stage      VARCHAR(100),
  to_stage        VARCHAR(100),
  note            TEXT,
  triggered_by    VARCHAR(50) DEFAULT 'manual' -- manual, artemis, webhook
);

-- Seed current state
INSERT INTO acos.founder_loans (date, amount_cents, description, balance_cents)
VALUES ('2026-03-01', 240000, 'Infrastructure and tooling advances Q1 2026', 240000)
ON CONFLICT DO NOTHING;

INSERT INTO acos.mrr_snapshots (snapshot_date, mrr_cents, client_count)
VALUES (CURRENT_DATE, 0, 0)
ON CONFLICT (snapshot_date) DO NOTHING;
