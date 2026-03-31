-- 010: Financial operations — planned expenses, monthly financials,
--      processed billing stub, budget vs actual + founder loan views
-- Idempotent: uses IF NOT EXISTS and CREATE OR REPLACE

-- ═══════════════════════════════════════════════════════════════
-- Planned recurring and one-time expenses (operating budget baseline)
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.planned_expenses (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    description     TEXT NOT NULL,
    category        VARCHAR(100) NOT NULL,
    -- categories: Infrastructure, SaaS, Legal/Accounting,
    --   Insurance, Personnel, Marketing, Equipment, Other
    amount          NUMERIC(10,2) NOT NULL,
    frequency       VARCHAR(20) NOT NULL,
    -- monthly | annual | one_time | quarterly
    effective_date  DATE NOT NULL,
    end_date        DATE,         -- null = ongoing
    notes           TEXT,
    active          BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ═══════════════════════════════════════════════════════════════
-- Monthly financial summary: actuals vs plan, cash position
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.monthly_financials (
    id                    UUID PRIMARY KEY
                          DEFAULT gen_random_uuid(),
    month                 DATE NOT NULL,  -- always 1st of month
    revenue_received      NUMERIC(12,2) DEFAULT 0,
    expenses_actual       NUMERIC(12,2) DEFAULT 0,
    expenses_planned      NUMERIC(12,2) DEFAULT 0,
    founder_loans_in      NUMERIC(12,2) DEFAULT 0,
    founder_loans_repaid  NUMERIC(12,2) DEFAULT 0,
    opening_balance       NUMERIC(12,2) DEFAULT 0,
    closing_balance       NUMERIC(12,2) GENERATED ALWAYS AS
        (opening_balance + revenue_received + founder_loans_in
         - expenses_actual - founder_loans_repaid) STORED,
    notes                 TEXT,
    locked                BOOLEAN DEFAULT FALSE,
    -- locked=true means month is closed, no auto-updates
    updated_at            TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(month)
);

-- ═══════════════════════════════════════════════════════════════
-- Processed billing stub (public schema — for budget view joins)
-- Note: acos.processed_billing tracks Gmail message IDs;
-- this table tracks actual expense line items.
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.processed_billing (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source          VARCHAR(100),   -- email, manual, import
    description     TEXT NOT NULL,
    category        VARCHAR(100),
    amount          NUMERIC(10,2),
    transaction_date DATE,
    is_founder_loan BOOLEAN DEFAULT FALSE,
    reimbursed      BOOLEAN DEFAULT FALSE,
    vendor          VARCHAR(255),
    receipt_url     TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ═══════════════════════════════════════════════════════════════
-- View: budget vs actuals by category for current month
-- ═══════════════════════════════════════════════════════════════

CREATE OR REPLACE VIEW public.v_budget_vs_actual AS
SELECT
    pe.category,
    SUM(CASE
        WHEN pe.frequency = 'monthly' THEN pe.amount
        WHEN pe.frequency = 'annual' THEN pe.amount / 12.0
        WHEN pe.frequency = 'quarterly' THEN pe.amount / 3.0
        ELSE 0
    END) AS planned_monthly,
    COALESCE(SUM(pb.amount), 0) AS actual_mtd,
    SUM(CASE
        WHEN pe.frequency = 'monthly' THEN pe.amount
        WHEN pe.frequency = 'annual' THEN pe.amount / 12.0
        WHEN pe.frequency = 'quarterly' THEN pe.amount / 3.0
        ELSE 0
    END) - COALESCE(SUM(pb.amount), 0) AS variance
FROM public.planned_expenses pe
LEFT JOIN public.processed_billing pb
    ON pb.category = pe.category
    AND pb.transaction_date >= date_trunc('month', CURRENT_DATE)
    AND pb.transaction_date < date_trunc('month', CURRENT_DATE)
                              + interval '1 month'
WHERE pe.active = TRUE
  AND pe.effective_date <= CURRENT_DATE
  AND (pe.end_date IS NULL OR pe.end_date >= CURRENT_DATE)
GROUP BY pe.category;

-- ═══════════════════════════════════════════════════════════════
-- View: founder loan running balance
-- References acos.founder_loans (created in 008_create_dashboard_tables)
-- amount_cents is signed: positive = advance, negative = repayment
-- ═══════════════════════════════════════════════════════════════

CREATE OR REPLACE VIEW public.v_founder_loan_balance AS
SELECT
    COUNT(*) AS loan_count,
    SUM(CASE WHEN amount_cents > 0 THEN amount_cents ELSE 0 END) / 100.0
        AS total_loaned,
    SUM(CASE WHEN amount_cents < 0 THEN ABS(amount_cents) ELSE 0 END) / 100.0
        AS total_repaid,
    (SUM(amount_cents)) / 100.0
        AS outstanding_balance,
    MIN(date) AS earliest_loan,
    MAX(date) AS most_recent_loan
FROM acos.founder_loans;
