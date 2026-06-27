-- 016_nutrition_schema.sql
-- Nutrition tables in the health schema: RD-authored target envelope, plan meals,
-- per-entry intake log, and a today's-rollup function.
--
-- INVARIANT: these are health-schema tables only. The health subsystem's own
-- handlers write only to `health.*`. The single cross-schema write (staple
-- generator -> acos.grocery_list) lives in life_ops.py as an orchestrator that
-- READS health.meal and WRITES acos.grocery_list — it is not a health handler
-- reaching out.
--
-- All date math anchors to America/Chicago.

CREATE SCHEMA IF NOT EXISTS health;

-- ============================================================================
-- NUTRITION_TARGET — the RD-authored envelope, entered by Ryan, never invented.
-- Joy (the RD) authors the numbers; Artemis only records what Ryan types.
-- At most one open target (effective_to IS NULL) may exist at a time.
-- ============================================================================

CREATE TABLE IF NOT EXISTS health.nutrition_target (
    id             SERIAL PRIMARY KEY,
    effective_from DATE NOT NULL,
    effective_to   DATE,                       -- NULL = currently open
    kcal           INT NOT NULL,
    protein_g      INT NOT NULL,
    carb_g         INT,
    fat_g          INT,
    fiber_g        INT,
    set_by         TEXT NOT NULL DEFAULT 'joy',
    notes          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- At most one open target. The partial unique index keys on the constant
-- expression (effective_to IS NULL) and only applies to open rows, so a second
-- open target is rejected while any number of closed (dated) rows coexist.
CREATE UNIQUE INDEX IF NOT EXISTS one_open_target
    ON health.nutrition_target ((effective_to IS NULL))
    WHERE effective_to IS NULL;

-- ============================================================================
-- MEAL — the plan's meals; the spine that joins the macros side to the
-- ingredients side. ingredients drive the grocery staple generator.
-- ============================================================================

CREATE TABLE IF NOT EXISTS health.meal (
    id             SERIAL PRIMARY KEY,
    target_id      INT REFERENCES health.nutrition_target(id),
    slot           TEXT NOT NULL,              -- breakfast | lunch | dinner | snack
    name           TEXT NOT NULL,
    kcal           INT,
    protein_g      INT,
    carb_g         INT,
    fat_g          INT,
    fiber_g        INT,
    times_per_week INT NOT NULL DEFAULT 7,
    ingredients    JSONB NOT NULL DEFAULT '[]',  -- [{"item","qty","unit"}]
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_health_meal_target
    ON health.meal(target_id);
CREATE INDEX IF NOT EXISTS idx_health_meal_slot
    ON health.meal(slot);

-- ============================================================================
-- NUTRITION_LOG — per-entry intake. Append-only.
--   meal_id NULL      = off-plan entry
--   estimated = FALSE = on-plan (macros copied verbatim from the meal row)
--   estimated = TRUE  = off-plan, LLM-estimated; confidence set accordingly
-- ============================================================================

CREATE TABLE IF NOT EXISTS health.nutrition_log (
    id           SERIAL PRIMARY KEY,
    logged_date  DATE NOT NULL,
    logged_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    meal_id      INT REFERENCES health.meal(id),  -- NULL = off-plan
    description  TEXT NOT NULL,
    kcal         INT,
    protein_g    INT,
    carb_g       INT,
    fat_g        INT,
    fiber_g      INT,
    estimated    BOOLEAN NOT NULL DEFAULT FALSE,   -- TRUE = LLM-estimated off-plan
    confidence   TEXT,                             -- 'high'|'medium'|'low', only when estimated
    source       TEXT NOT NULL DEFAULT 'artemis'
);

CREATE INDEX IF NOT EXISTS idx_health_nutrition_log_date
    ON health.nutrition_log(logged_date);

-- ============================================================================
-- REMAINING_BUDGET(p_date) — today's rollup.
-- Sums nutrition_log for p_date and subtracts from the target applicable on
-- p_date (the open target, or the dated target whose window covers p_date).
-- Directional by design: estimated off-plan entries make this a coach, not a
-- gram-accurate ledger. Defaults p_date to CT-today.
-- ============================================================================

CREATE OR REPLACE FUNCTION health.remaining_budget(
    p_date DATE DEFAULT (now() AT TIME ZONE 'America/Chicago')::date
)
RETURNS TABLE (
    target_kcal        INT,
    consumed_kcal      BIGINT,
    remaining_kcal     BIGINT,
    target_protein_g   INT,
    consumed_protein_g BIGINT,
    remaining_protein_g BIGINT,
    target_carb_g      INT,
    consumed_carb_g    BIGINT,
    remaining_carb_g   BIGINT,
    target_fat_g       INT,
    consumed_fat_g     BIGINT,
    remaining_fat_g    BIGINT,
    target_fiber_g     INT,
    consumed_fiber_g   BIGINT,
    remaining_fiber_g  BIGINT
)
LANGUAGE sql
STABLE
AS $$
    WITH tgt AS (
        SELECT *
        FROM health.nutrition_target
        WHERE effective_from <= p_date
          AND (effective_to IS NULL OR p_date <= effective_to)
        ORDER BY effective_from DESC, id DESC
        LIMIT 1
    ),
    eaten AS (
        SELECT
            COALESCE(SUM(kcal), 0)      AS kcal,
            COALESCE(SUM(protein_g), 0) AS protein_g,
            COALESCE(SUM(carb_g), 0)    AS carb_g,
            COALESCE(SUM(fat_g), 0)     AS fat_g,
            COALESCE(SUM(fiber_g), 0)   AS fiber_g
        FROM health.nutrition_log
        WHERE logged_date = p_date
    )
    SELECT
        tgt.kcal,      eaten.kcal,      tgt.kcal      - eaten.kcal,
        tgt.protein_g, eaten.protein_g, tgt.protein_g - eaten.protein_g,
        tgt.carb_g,    eaten.carb_g,    tgt.carb_g    - eaten.carb_g,
        tgt.fat_g,     eaten.fat_g,     tgt.fat_g     - eaten.fat_g,
        tgt.fiber_g,   eaten.fiber_g,   tgt.fiber_g   - eaten.fiber_g
    FROM tgt, eaten;
$$;
