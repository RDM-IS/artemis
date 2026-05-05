-- 013_health_schema.sql
-- Personal training schema: plans, session logs, daily state, autoregulator audit trail
-- Schema is fully isolated from public/crm/acos. Safe to add/drop without affecting other modules.

CREATE SCHEMA IF NOT EXISTS health;

-- ============================================================================
-- PLAN — what to do on a given day
-- The autoregulator (later ticket) respects is_override=TRUE and is_skipped
-- when generating future days. baseline seed sets generated_by='baseline'.
-- ============================================================================

CREATE TABLE IF NOT EXISTS health.plan (
    plan_id          BIGSERIAL PRIMARY KEY,
    plan_date        DATE NOT NULL UNIQUE,
    phase            INT NOT NULL CHECK (phase BETWEEN 1 AND 4),
    week_num         INT NOT NULL CHECK (week_num BETWEEN 1 AND 19),
    session_type     TEXT NOT NULL CHECK (session_type IN (
        'strength_a', 'strength_b', 'strength_c',
        'cardio_intervals', 'cardio_z2', 'walk', 'rest_mobility'
    )),
    blocks           JSONB NOT NULL,
    target_rpe       NUMERIC(3,1),
    target_hr_zone   INT CHECK (target_hr_zone BETWEEN 1 AND 5),
    est_duration_min INT,
    is_override      BOOLEAN NOT NULL DEFAULT FALSE,
    is_skipped       BOOLEAN NOT NULL DEFAULT FALSE,
    skip_reason      TEXT,
    generated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    generated_by     TEXT NOT NULL CHECK (generated_by IN (
        'baseline', 'autoreg_morning', 'autoreg_evening', 'manual'
    )),
    notes            TEXT
);

CREATE INDEX IF NOT EXISTS idx_health_plan_date
    ON health.plan(plan_date);
CREATE INDEX IF NOT EXISTS idx_health_plan_phase_week
    ON health.plan(phase, week_num);

-- ============================================================================
-- SESSION_LOG — what actually happened during/after a session
-- log_type='session_summary' is the per-day overall RPE row;
-- 'strength_set' / 'cardio_block' are per-exercise rows.
-- ============================================================================

CREATE TABLE IF NOT EXISTS health.session_log (
    log_id          BIGSERIAL PRIMARY KEY,
    plan_id         BIGINT REFERENCES health.plan(plan_id),
    log_type        TEXT NOT NULL CHECK (log_type IN (
        'strength_set', 'cardio_block', 'session_summary'
    )),
    exercise        TEXT,
    set_num         INT,
    reps_done       INT,
    weight_lbs      NUMERIC(5,1),
    duration_sec    INT,
    distance_m      NUMERIC(7,1),
    hr_avg          INT,
    hr_peak         INT,
    rpe_actual      NUMERIC(3,1),
    notes           TEXT,
    user_suggestion TEXT,
    is_skipped      BOOLEAN NOT NULL DEFAULT FALSE,
    logged_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    logged_via      TEXT NOT NULL CHECK (logged_via IN (
        'mattermost', 'voice', 'manual', 'inferred'
    ))
);

CREATE INDEX IF NOT EXISTS idx_health_session_log_plan
    ON health.session_log(plan_id);
CREATE INDEX IF NOT EXISTS idx_health_session_log_logged_at
    ON health.session_log(logged_at);

-- ============================================================================
-- DAILY_STATE — morning check-in
-- Keyed by date; second entry on same day is an UPSERT.
-- ============================================================================

CREATE TABLE IF NOT EXISTS health.daily_state (
    state_date    DATE PRIMARY KEY,
    weight_lbs    NUMERIC(5,1),
    sleep_hrs     NUMERIC(3,1),
    energy        INT CHECK (energy BETWEEN 1 AND 5),
    soreness      JSONB,
    resting_hr    INT,
    free_text     TEXT,
    logged_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- ADJUSTMENTS — autoregulator audit trail
-- Captures every change the autoregulator makes: from_plan_id is the
-- pre-adjustment plan, to_plan_id is the new plan after adjustment.
-- ============================================================================

CREATE TABLE IF NOT EXISTS health.adjustments (
    adjustment_id BIGSERIAL PRIMARY KEY,
    from_plan_id  BIGINT REFERENCES health.plan(plan_id),
    to_plan_id    BIGINT REFERENCES health.plan(plan_id),
    reason        TEXT NOT NULL,
    signals       JSONB NOT NULL,
    rules_fired   TEXT[],
    trigger       TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- TRAINING_RULES — hard constraints checked by the autoregulator
-- ============================================================================

CREATE TABLE IF NOT EXISTS health.training_rules (
    rule_id     TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    active      BOOLEAN NOT NULL DEFAULT TRUE
);

INSERT INTO health.training_rules (rule_id, description) VALUES
    ('no_overhead_press',    'Ceiling is 7ft. No overhead pressing movements.'),
    ('no_running_above_245', 'No running until weight is below 245 lbs.'),
    ('mandatory_deload_w19', 'Week 19 is a mandatory deload (50% volume).'),
    ('recovery_overrides',   'Sleep <5hrs OR energy <=2 forces a recovery day.')
ON CONFLICT (rule_id) DO NOTHING;

-- ============================================================================
-- PHASE_CONFIG — per-phase intensity envelope
-- ============================================================================

CREATE TABLE IF NOT EXISTS health.phase_config (
    phase               INT PRIMARY KEY CHECK (phase BETWEEN 1 AND 4),
    phase_name          TEXT NOT NULL,
    max_session_rpe     NUMERIC(3,1) NOT NULL,
    allow_upward_adjust BOOLEAN NOT NULL DEFAULT FALSE,
    notes               TEXT
);

INSERT INTO health.phase_config (phase, phase_name, max_session_rpe, allow_upward_adjust, notes) VALUES
    (1, 'Foundation', 7.0, FALSE, 'Build the habit. Downward-only adjustment.'),
    (2, 'Build',      8.5, TRUE,  'Add 3rd lift. True HIIT introduced wks 9-10.'),
    (3, 'Peak',       9.0, TRUE,  'Hardest block. Real intensity.'),
    (4, 'Polish',     8.5, TRUE,  'Hold Phase 3 volume. Wk 19 deload mandatory.')
ON CONFLICT (phase) DO NOTHING;
