-- 036_watch_ingest.sql
-- WATCH-1: Apple Watch ingest via Health Auto Export.
--
-- Two tables, because samples and workouts have genuinely different shapes.
-- Both keep the payload's own JSON in `raw`, so a parser fix can be replayed
-- without re-exporting from the phone, and both are idempotent by a natural
-- key so a re-sent or overlapping export is a no-op.
--
-- measured_at is UTC (the payload's own offset is honoured, never assumed).
-- local_date is the ACTIVE-timezone date fixed AT INGEST, so "last night"
-- never depends on a later timezone override or on UTC rollover.
--
-- Also: per-field source markers on health.daily_state (Ryan, 2026-09-19).
-- A manual value is FINAL for that date. A late-arriving watch sample must
-- never overwrite it, whatever the arrival order.
--
-- Idempotent: CREATE IF NOT EXISTS + guarded ADD COLUMN. No row changes.

CREATE TABLE IF NOT EXISTS health.watch_sample (
    sample_id    BIGSERIAL PRIMARY KEY,
    -- 'sleep_asleep', 'sleep_deep', 'resting_heart_rate', 'hrv', 'weight',
    -- 'active_energy', 'basal_energy', … and anything else that arrives.
    metric       TEXT NOT NULL,
    measured_at  TIMESTAMPTZ NOT NULL,
    local_date   DATE NOT NULL,
    value        NUMERIC,
    unit         TEXT,
    source       TEXT NOT NULL DEFAULT 'health_auto_export',
    raw          JSONB NOT NULL,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (metric, measured_at)
);

CREATE INDEX IF NOT EXISTS watch_sample_metric_day_idx
    ON health.watch_sample (metric, local_date);
CREATE INDEX IF NOT EXISTS watch_sample_day_idx
    ON health.watch_sample (local_date);

CREATE TABLE IF NOT EXISTS health.watch_workout (
    workout_id   BIGSERIAL PRIMARY KEY,
    kind         TEXT NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL,
    ended_at     TIMESTAMPTZ,
    local_date   DATE NOT NULL,
    duration_sec INTEGER,
    hr_avg       INTEGER,
    hr_max       INTEGER,
    kcal         NUMERIC,
    raw          JSONB NOT NULL,
    -- set ONLY on a real time overlap with that day's session; never by date
    plan_id      INTEGER REFERENCES health.plan(plan_id),
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kind, started_at)
);

CREATE INDEX IF NOT EXISTS watch_workout_day_idx
    ON health.watch_workout (local_date);

-- ── Per-field provenance on daily_state ────────────────────────────────────
-- NULL means "unknown" (every row written before WATCH-1). Only the three
-- fields a watch can supply get a marker; energy and soreness are manual by
-- definition and have none.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'health' AND table_name = 'daily_state'
                     AND column_name = 'sleep_source') THEN
        ALTER TABLE health.daily_state
            ADD COLUMN sleep_source      TEXT,
            ADD COLUMN weight_source     TEXT,
            ADD COLUMN resting_hr_source TEXT;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'health' AND t.relname = 'daily_state'
          AND c.conname = 'daily_state_source_markers_check'
    ) THEN
        ALTER TABLE health.daily_state
            ADD CONSTRAINT daily_state_source_markers_check
            CHECK (
                (sleep_source      IS NULL OR sleep_source      IN ('watch', 'manual'))
            AND (weight_source     IS NULL OR weight_source     IN ('watch', 'manual'))
            AND (resting_hr_source IS NULL OR resting_hr_source IN ('watch', 'manual'))
            );
    END IF;
END $$;

COMMENT ON COLUMN health.daily_state.sleep_source IS
    'WATCH-1: ''manual'' is final for that date — a later watch sample never '
    'overwrites it, whatever the arrival order. NULL = written before WATCH-1.';
