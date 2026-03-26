-- 002: Create all acos tables
-- Does NOT touch crm schema.

-- ── entities ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS acos.entities (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    entity_type     VARCHAR(50) NOT NULL,
    name            VARCHAR(255) NOT NULL,
    content         TEXT,
    confidence      FLOAT DEFAULT 0.0,
    layer           VARCHAR(20) DEFAULT 'quarantine',
    domain          VARCHAR(50),
    crm_contact_id  UUID,
    metadata        JSONB DEFAULT '{}',
    tags            TEXT[] DEFAULT '{}',
    novelty_score   FLOAT DEFAULT 0.0,
    osint_source    VARCHAR(100)
);

-- ── relationships ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS acos.relationships (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at            TIMESTAMPTZ DEFAULT now(),
    source_entity_id      UUID REFERENCES acos.entities(id) ON DELETE CASCADE,
    target_entity_id      UUID REFERENCES acos.entities(id) ON DELETE CASCADE,
    relationship_type     VARCHAR(100) NOT NULL,
    relationship_context  TEXT NOT NULL,
    confidence            FLOAT DEFAULT 0.0,
    weight                FLOAT DEFAULT 1.0,
    layer                 VARCHAR(20) DEFAULT 'bronze',
    metadata              JSONB DEFAULT '{}'
);

-- ── osint_signals ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS acos.osint_signals (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at            TIMESTAMPTZ DEFAULT now(),
    entity_id             UUID REFERENCES acos.entities(id) ON DELETE CASCADE,
    source                VARCHAR(100),
    signal_type           VARCHAR(100),
    raw_content           TEXT,
    summary               TEXT,
    confidence            FLOAT DEFAULT 0.0,
    corroboration_count   INTEGER DEFAULT 0,
    manually_validated    BOOLEAN DEFAULT false,
    processed             BOOLEAN DEFAULT false,
    layer                 VARCHAR(20) DEFAULT 'quarantine'
);

-- ── data_vault_satellites ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS acos.data_vault_satellites (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      TIMESTAMPTZ DEFAULT now(),
    entity_id       UUID REFERENCES acos.entities(id) ON DELETE CASCADE,
    satellite_type  VARCHAR(100),
    content         TEXT,
    crm_syncable    BOOLEAN DEFAULT false,
    layer           VARCHAR(20) DEFAULT 'silver',
    metadata        JSONB DEFAULT '{}',
    -- HARD RULE: sensitive satellites can NEVER be crm_syncable
    CONSTRAINT chk_sensitive_not_syncable
        CHECK (NOT (satellite_type = 'sensitive' AND crm_syncable = true))
);

-- ── audit_log ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS acos.audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      TIMESTAMPTZ DEFAULT now(),
    agent           VARCHAR(100),
    persona         VARCHAR(100),
    action          VARCHAR(255),
    domain          VARCHAR(50),
    confidence      FLOAT,
    outcome         VARCHAR(50),
    token_count     INTEGER DEFAULT 0,
    api_cost_usd    FLOAT DEFAULT 0.0,
    metadata        JSONB DEFAULT '{}'
);

-- ── velocity_ledger ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS acos.velocity_ledger (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      TIMESTAMPTZ DEFAULT now(),
    agent           VARCHAR(100),
    action_type     VARCHAR(100),
    token_count     INTEGER DEFAULT 0,
    api_cost_usd    FLOAT DEFAULT 0.0,
    external_target VARCHAR(255),
    metadata        JSONB DEFAULT '{}'
);

-- ── circuit_breaker_status ────────────────────────────────────
CREATE TABLE IF NOT EXISTS acos.circuit_breaker_status (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent               VARCHAR(100) NOT NULL,
    suspended_at        TIMESTAMPTZ,
    suspended_by        VARCHAR(100),
    resumed_at          TIMESTAMPTZ,
    resume_required_by  VARCHAR(100) DEFAULT 'manual',
    reason              TEXT,
    threshold_breached  VARCHAR(100),
    breach_value        FLOAT,
    metadata            JSONB DEFAULT '{}'
);

-- ── guardrail_violations ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS acos.guardrail_violations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at          TIMESTAMPTZ DEFAULT now(),
    guardrail_type      VARCHAR(100),
    event_summary       TEXT,
    external_attendees  TEXT[] DEFAULT '{}',
    outcome             VARCHAR(50),
    agent               VARCHAR(100),
    metadata            JSONB DEFAULT '{}'
);
