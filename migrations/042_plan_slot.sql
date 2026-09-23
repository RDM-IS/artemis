-- EVENING-1 (Ryan, 2026-09-23): a morning session AND an evening session.
--
-- One row per (date, slot) instead of one per date. Every existing row is a
-- morning by construction, which is what the DEFAULT backfills.
--
-- `rest` joins the session_type CHECK: a rest morning gets a REAL row typed
-- rest, never an absent row. An absent row means "no plan", which is a data
-- problem; a rest row means "rest today", which is the plan.
--
-- Additive and reversible. No column is dropped or renamed, so no code has to
-- ship ahead of it (COLUMN-GREP).

ALTER TABLE health.plan
    ADD COLUMN IF NOT EXISTS slot TEXT NOT NULL DEFAULT 'morning'
    CHECK (slot IN ('morning', 'evening'));

ALTER TABLE health.plan DROP CONSTRAINT IF EXISTS plan_session_type_check;
ALTER TABLE health.plan ADD CONSTRAINT plan_session_type_check
    CHECK (session_type IN ('strength_a', 'strength_b', 'strength_c',
                            'cardio_intervals', 'cardio_z2', 'walk',
                            'rest', 'rest_mobility', 'recovery_flow'));

ALTER TABLE health.plan DROP CONSTRAINT IF EXISTS plan_plan_date_key;
ALTER TABLE health.plan ADD CONSTRAINT plan_date_slot_key UNIQUE (plan_date, slot);

CREATE INDEX IF NOT EXISTS idx_health_plan_date_slot ON health.plan (plan_date, slot);

-- Down (by hand; only possible while no date carries two rows):
--   ALTER TABLE health.plan DROP CONSTRAINT plan_date_slot_key;
--   ALTER TABLE health.plan ADD CONSTRAINT plan_plan_date_key UNIQUE (plan_date);
--   ALTER TABLE health.plan DROP COLUMN slot;
--   (and restore the previous session_type CHECK, without 'rest')
