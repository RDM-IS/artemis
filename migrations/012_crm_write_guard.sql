-- 012_crm_write_guard.sql
-- CRM Write Guard — normalized CRM tables + pending review queue + funding events

-- ============================================================================
-- PUBLIC SCHEMA — Core CRM entities
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.persons (
    person_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    name_variants   TEXT[],
    email_primary   TEXT,
    emails          TEXT[],
    phone           TEXT,
    linkedin_url    TEXT,
    location        TEXT,
    timezone        TEXT,
    source          TEXT,
    source_detail   TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.companies (
    company_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    name_variants   TEXT[],
    domain          TEXT,
    types           TEXT[],
    industry        TEXT,
    hq_location     TEXT,
    website         TEXT,
    linkedin_url    TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.relationships (
    relationship_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id       UUID REFERENCES public.persons(person_id),
    company_id      UUID REFERENCES public.companies(company_id),
    role            TEXT,
    title           TEXT,
    status          TEXT DEFAULT 'Active',
    is_primary      BOOLEAN DEFAULT FALSE,
    start_date      DATE,
    end_date        DATE,
    source          TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.engagements (
    engagement_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id      UUID REFERENCES public.companies(company_id),
    type            TEXT,
    gate            INTEGER,
    status          TEXT DEFAULT 'Active',
    pilot_start     DATE,
    pilot_end       DATE,
    msa_signed      DATE,
    arr             NUMERIC,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.touch_events (
    touch_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id       UUID REFERENCES public.persons(person_id),
    company_id      UUID REFERENCES public.companies(company_id),
    type            TEXT,
    direction       TEXT,
    subject         TEXT,
    summary         TEXT,
    gmail_message_id TEXT,
    playbook        TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- ============================================================================
-- ACOS SCHEMA — Operational tables
-- ============================================================================

CREATE TABLE IF NOT EXISTS acos.pending_crm_writes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type     TEXT NOT NULL,
    data            JSONB NOT NULL,
    candidates      JSONB,
    source_pb       TEXT,
    gmail_message_id TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    expires_at      TIMESTAMPTZ DEFAULT now() + INTERVAL '7 days'
);

CREATE TABLE IF NOT EXISTS acos.funding_events (
    funding_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source          TEXT NOT NULL,
    type            TEXT,
    amount          NUMERIC,
    status          TEXT DEFAULT 'Active',
    activation_date DATE,
    expiry_date     DATE,
    renewal_date    DATE,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- ============================================================================
-- INDEXES
-- ============================================================================

CREATE UNIQUE INDEX IF NOT EXISTS idx_persons_email_primary
    ON public.persons (LOWER(email_primary))
    WHERE email_primary IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_domain
    ON public.companies (LOWER(domain))
    WHERE domain IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_relationships_active
    ON public.relationships (person_id, company_id)
    WHERE status = 'Active';

CREATE INDEX IF NOT EXISTS idx_engagements_active
    ON public.engagements (company_id, type)
    WHERE status = 'Active';

CREATE INDEX IF NOT EXISTS idx_touch_events_company
    ON public.touch_events (company_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_touch_events_person
    ON public.touch_events (person_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_pending_crm_writes_expires
    ON acos.pending_crm_writes (expires_at)


CREATE INDEX IF NOT EXISTS idx_funding_events_source
    ON acos.funding_events (source, status);
