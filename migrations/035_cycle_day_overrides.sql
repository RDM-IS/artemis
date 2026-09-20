-- 035_cycle_day_overrides.sql
-- CYCLE-1: day-type overrides for the 14-day pay-period cycle.
--
-- The cycle itself is DERIVED: position = (date - anchor) % 14, so nothing is
-- stored per day. This table holds only the exceptions Ryan sets by hand — a
-- single date or a range (e.g. Thanksgiving week 2026 = all `wi`). An override
-- WINS over the derived position, and the cycle resumes afterwards with no
-- drift because position never counts forward from the last override.
--
-- The registry and the anchor live in acos.system_state (keys `cycle_anchor`
-- and `locations`), not here: they are single values, like `health_program`.
-- acos.timezone_overrides (019) is a singleton (CHECK id = 1) and is NOT a
-- model for this one — date ranges need real rows.
--
-- `wi` is NOT split into wi_richfield / wi_brown_deer: Sunday is Brown Deer
-- until 17:00 and Richfield after, so no single day type describes that date.
-- Day type answers "working?", location answers "where?", and location is
-- resolved per (date, time of day).
--
-- Idempotent: CREATE IF NOT EXISTS + a guarded constraint add. No row changes.

CREATE TABLE IF NOT EXISTS acos.cycle_day_overrides (
    override_id  SERIAL PRIMARY KEY,
    -- inclusive span; a single-date override has start_date = end_date
    start_date   DATE NOT NULL,
    end_date     DATE NOT NULL,
    -- Either may be set. day_type is work status; location is where he is, and
    -- they are independent: "working from Richfield" is
    -- day_type='msp_work' + location='richfield'.
    day_type     TEXT,
    -- A registry key (office, richfield, brown_deer, msp_home, outside). NOT
    -- constrained here on purpose: the registry lives in acos.system_state and
    -- can gain a location without a migration; code validates against it.
    location     TEXT,
    reason       TEXT,
    -- who/what set it, for the audit trail; 'ryan' for a chat command
    set_by       TEXT NOT NULL DEFAULT 'ryan',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- soft delete: a cleared override stays readable as history
    revoked_at   TIMESTAMPTZ
);

-- The day types CYCLE-1 defines. Widening this CHECK is a later migration,
-- the same way health.plan.session_type was widened in 033.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'acos' AND t.relname = 'cycle_day_overrides'
          AND c.conname = 'cycle_day_overrides_day_type_check'
    ) THEN
        ALTER TABLE acos.cycle_day_overrides
            ADD CONSTRAINT cycle_day_overrides_day_type_check
            CHECK (day_type IN ('msp_work', 'msp_home', 'wi', 'travel'));
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'acos' AND t.relname = 'cycle_day_overrides'
          AND c.conname = 'cycle_day_overrides_something_set_check'
    ) THEN
        ALTER TABLE acos.cycle_day_overrides
            ADD CONSTRAINT cycle_day_overrides_something_set_check
            CHECK (day_type IS NOT NULL OR location IS NOT NULL);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'acos' AND t.relname = 'cycle_day_overrides'
          AND c.conname = 'cycle_day_overrides_span_check'
    ) THEN
        ALTER TABLE acos.cycle_day_overrides
            ADD CONSTRAINT cycle_day_overrides_span_check
            CHECK (end_date >= start_date);
    END IF;
END $$;

-- Lookup is always "the live override covering this date", so index the span.
CREATE INDEX IF NOT EXISTS cycle_day_overrides_live_idx
    ON acos.cycle_day_overrides (start_date, end_date)
    WHERE revoked_at IS NULL;

COMMENT ON TABLE acos.cycle_day_overrides IS
    'CYCLE-1 day-type overrides. The cycle is derived from the anchor in '
    'acos.system_state; these are the hand-set exceptions. An override wins '
    'over the derived position; overlapping live rows resolve newest-first.';
