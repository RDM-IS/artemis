-- 019_quiet_hours_state.sql
-- Quiet-hours state migrated from SQLite (system_state / quiet_state /
-- timezone_overrides, all on the shared commitments.get_db hub) to RDS. Hard cut:
-- the SQLite quiet-hours data is abandoned; artemis/quiet_hours.py is repointed
-- here. Columns mirror the SQLite DDLs in commitments.py one-for-one.
--
-- TYPE MAPPING:
--   TEXT                         → TEXT
--   INTEGER 0/1 flags            → INTEGER (is_quiet / manual_override / override_active)
--   TEXT datetime('now') / ISO   → TIMESTAMPTZ   (set_at / updated_at / expires_at / last_interaction)
--   wake_time / override_until   → TEXT  (these hold "HH:MM" wall-clock strings, NOT instants)
-- Stored instants are TIMESTAMPTZ so the override-expiry / inactivity-elapsed
-- comparisons are unambiguous. No new timezone semantics live here — quiet_hours.py
-- resolves the active timezone itself (override-aware); these tables are storage only.
--
-- SINGLETONS: quiet_state and timezone_overrides keep the SQLite id=1 pattern
-- (INTEGER PK + CHECK (id = 1) — a fixed value set in the source schema). The
-- repointed code upserts via ON CONFLICT (id) DO UPDATE.
--
-- Idempotent; needs only the existing acos schema (migration 001).

CREATE TABLE IF NOT EXISTS acos.system_state (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS acos.quiet_state (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    is_quiet          INTEGER NOT NULL DEFAULT 0,
    manual_override   INTEGER NOT NULL DEFAULT 0,
    wake_time         TEXT,
    override_active   INTEGER NOT NULL DEFAULT 0,
    override_until    TEXT,
    last_interaction  TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS acos.timezone_overrides (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    timezone    TEXT NOT NULL,
    set_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    city_name   TEXT NOT NULL DEFAULT ''
);
