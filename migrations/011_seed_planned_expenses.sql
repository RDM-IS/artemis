-- 011: Seed planned expenses — RDMIS operating budget baseline
-- Idempotent: adds unique constraint + ON CONFLICT DO NOTHING

-- Ensure unique constraint for idempotent inserts
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_planned_expenses_desc_date'
    ) THEN
        ALTER TABLE public.planned_expenses
            ADD CONSTRAINT uq_planned_expenses_desc_date
            UNIQUE (description, effective_date);
    END IF;
END $$;

-- ═══════════════════════════════════════════════════════════════
-- INFRASTRUCTURE (monthly)
-- ═══════════════════════════════════════════════════════════════

INSERT INTO public.planned_expenses (description, category, amount, frequency, effective_date, notes)
VALUES
    ('EC2 t3.small acos-primary', 'Infrastructure', 17.00, 'monthly', '2026-02-01', NULL),
    ('RDS db.t3.micro rdmis-dev', 'Infrastructure', 15.00, 'monthly', '2026-02-01', NULL),
    ('AWS Secrets Manager (~10 secrets)', 'Infrastructure', 4.00, 'monthly', '2026-02-01', NULL),
    ('AWS Data Transfer + misc', 'Infrastructure', 5.00, 'monthly', '2026-02-01', NULL),
    ('Namecheap domains (rdm.is + others)', 'Infrastructure', 4.17, 'monthly', '2026-02-01', '$50/yr annualized'),
    ('Cloudflare Pages (free tier)', 'Infrastructure', 0.00, 'monthly', '2026-02-01', NULL)
ON CONFLICT ON CONSTRAINT uq_planned_expenses_desc_date DO NOTHING;

-- ═══════════════════════════════════════════════════════════════
-- SAAS / SOFTWARE (monthly)
-- ═══════════════════════════════════════════════════════════════

INSERT INTO public.planned_expenses (description, category, amount, frequency, effective_date, notes)
VALUES
    ('Claude Pro (Anthropic)', 'SaaS', 20.00, 'monthly', '2026-01-01', NULL),
    ('Claude API usage (estimated)', 'SaaS', 30.00, 'monthly', '2026-02-01', NULL),
    ('GitHub (free tier)', 'SaaS', 0.00, 'monthly', '2026-01-01', NULL),
    ('Gladia STT Pro (estimated)', 'SaaS', 5.00, 'monthly', '2026-04-01', 'Voice feature, activates when voice deployed'),
    ('ElevenLabs Starter', 'SaaS', 5.00, 'monthly', '2026-04-01', 'Voice feature, activates when voice deployed'),
    ('Mattermost (self-hosted, no license cost)', 'SaaS', 0.00, 'monthly', '2026-02-01', NULL),
    ('Zoho Invoice (free tier)', 'SaaS', 0.00, 'monthly', '2026-01-01', NULL),
    ('1Password (personal, pre-business upgrade)', 'SaaS', 3.00, 'monthly', '2026-01-01', NULL)
ON CONFLICT ON CONSTRAINT uq_planned_expenses_desc_date DO NOTHING;

-- ═══════════════════════════════════════════════════════════════
-- INSURANCE (annual)
-- ═══════════════════════════════════════════════════════════════

INSERT INTO public.planned_expenses (description, category, amount, frequency, effective_date, notes)
VALUES
    ('E&O + Cyber Insurance', 'Insurance', 2400.00, 'annual', '2026-01-01', '$200/month equivalent')
ON CONFLICT ON CONSTRAINT uq_planned_expenses_desc_date DO NOTHING;

-- ═══════════════════════════════════════════════════════════════
-- LEGAL / ACCOUNTING (one_time and recurring)
-- ═══════════════════════════════════════════════════════════════

INSERT INTO public.planned_expenses (description, category, amount, frequency, effective_date, notes)
VALUES
    ('Wisconsin LLC Annual Report', 'Legal/Accounting', 25.00, 'annual', '2027-03-31', 'First due March 31, 2027'),
    ('Attorney — NDA template review (estimated)', 'Legal/Accounting', 500.00, 'one_time', '2026-04-01', NULL),
    ('Attorney — MSA review (estimated)', 'Legal/Accounting', 1500.00, 'one_time', '2026-05-01', NULL),
    ('Accountant — 2026 tax prep (estimated)', 'Legal/Accounting', 800.00, 'one_time', '2027-01-01', NULL)
ON CONFLICT ON CONSTRAINT uq_planned_expenses_desc_date DO NOTHING;

-- ═══════════════════════════════════════════════════════════════
-- KIVA LOAN (placeholder)
-- ═══════════════════════════════════════════════════════════════

INSERT INTO public.planned_expenses (description, category, amount, frequency, effective_date, notes)
VALUES
    ('Kiva US loan repayment (est. $15K, 0% interest)', 'Other', 0.00, 'monthly', '2026-06-01',
     'Amount TBD pending loan approval; placeholder')
ON CONFLICT ON CONSTRAINT uq_planned_expenses_desc_date DO NOTHING;
