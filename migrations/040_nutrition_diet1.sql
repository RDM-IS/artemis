-- 040_nutrition_diet1.sql
-- DIET-1 (work-day scope): plan-first nutrition logging.
--
-- Recording is default-to-plan; logging is by exception. A work day is
-- pre-filled at 00:15 local from the Notion meal plan and every entry stays
-- `assumed` until Ryan corrects it. `fix` opens a correction for the previous
-- day; 48 hours after that day's end the day locks.
--
-- INVARIANTS
--   * Macros are never invented. Every entry carries a source and, unless it
--     is `estimated`, a source_id — enforced by entry_source_id_required.
--   * NO hard-coded timezone. 016's remaining_budget() defaulted p_date to
--     (now() AT TIME ZONE 'America/Chicago')::date; nothing here has a date
--     default. Callers pass a date resolved by quiet_hours.local_today(),
--     which follows a `set timezone to <place>` override. See CLAUDE.md.
--   * Status and confidence are SEPARATE axes. Status is how settled the
--     record is (assumed/corrected); confidence is how well-sourced the
--     macros are (exact/matched/estimated).
--
-- SCOPE NOTE: this migration is ADDITIVE. It does not drop the migration-016
-- tables (health.nutrition_target / health.meal / health.nutrition_log) or
-- health.remaining_budget(). Those are all empty in RDS (verified 2026-09-21),
-- but artemis/life_ops.py's grocery staple generator still READS
-- health.meal + health.nutrition_target, and retiring them without rehoming
-- that capability would silently drop it. Retirement is a separate,
-- coverage-checked change. See docs/ARTEMIS_STATE.md DIET-1.

CREATE SCHEMA IF NOT EXISTS nutrition;

-- ============================================================================
-- FOOD — saved foods. The first tier of the deviation source order
-- (saved foods -> USDA -> Open Food Facts).
--
-- Two kinds, both mirrored from Notion (a cache with provenance, refreshed by
-- the 00:15 job — Notion stays the source of truth):
--   recipe      a `recipes` row: one portion as eaten
--   ingredient  an `ingredients` row: macros per its `serving` (kept in portion)
-- Lookup order is recipe first, then ingredient. The two kinds share a
-- namespace in practice ("Protein bar — peanut" vs "protein bar (peanut)"
-- slugify identically), so uniqueness is per (kind, slug), not per slug.
--
-- `source` says where the macros came from; `source_id` identifies the record
-- there (Notion page id, USDA fdcId, Open Food Facts barcode). A row whose
-- macros are a stand-in carries is_placeholder = TRUE and is rendered as such
-- in every report, exactly like an `estimated` entry.
-- ============================================================================

CREATE TABLE IF NOT EXISTS nutrition.food (
    id             SERIAL PRIMARY KEY,
    kind           TEXT NOT NULL DEFAULT 'recipe',
    name           TEXT NOT NULL,
    -- lowercase, punctuation-stripped match key; unique within a kind
    slug           TEXT NOT NULL,
    kcal           INT NOT NULL,
    protein_g      NUMERIC(6,2) NOT NULL,
    carb_g         NUMERIC(6,2),
    fat_g          NUMERIC(6,2),
    fiber_g        NUMERIC(6,2),
    sodium_mg      INT,
    -- recipe: one portion as eaten; ingredient: the Notion `serving` the
    -- macros are per. Free text because "1 bar (68 g)" is not a number.
    portion        TEXT,
    source         TEXT NOT NULL,
    source_id      TEXT,
    source_detail  TEXT,          -- mirrors the Notion `source` column verbatim
    is_placeholder BOOLEAN NOT NULL DEFAULT FALSE,
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT food_kind_known
        CHECK (kind IN ('recipe', 'ingredient')),
    CONSTRAINT food_kind_slug_unique
        UNIQUE (kind, slug),
    CONSTRAINT food_source_known
        CHECK (source IN ('notion', 'usda', 'off', 'manual')),
    -- A saved food always knows where it came from. `manual` is the one
    -- source that may omit an id (Ryan typed the macros off a label himself).
    CONSTRAINT food_source_id_required
        CHECK (source = 'manual' OR source_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_nutrition_food_active
    ON nutrition.food(active) WHERE active;

-- ============================================================================
-- DAY — one row per date. Carries the day-level status and how the day was
-- populated, so a day with no plan is distinguishable from a day that was
-- planned and eaten as planned.
--
--   assumed             pre-filled, untouched
--   corrected           at least one correction was accepted
--   locked_unconfirmed  still assumed when the 48h window closed
--
-- prefill_outcome records WHY a day has no entries. `unavailable` is the
-- Notion-unreachable case: nothing is pre-filled and the day says so.
-- ============================================================================

CREATE TABLE IF NOT EXISTS nutrition.day (
    day_date        DATE PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'assumed',
    -- cycle.day_type at pre-fill time; work-day scope means msp_work only
    day_type        TEXT,
    prefilled       BOOLEAN NOT NULL DEFAULT FALSE,
    prefill_outcome TEXT,
    prefill_note    TEXT,
    plan_source_id  TEXT,          -- Notion page id of the default-day row
    locked_at       TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT day_status_known
        CHECK (status IN ('assumed', 'corrected', 'locked_unconfirmed')),
    CONSTRAINT day_prefill_outcome_known
        CHECK (prefill_outcome IS NULL OR prefill_outcome IN (
            'planned',        -- pre-filled from the default day
            'not_work_day',   -- out of scope tonight: no non-work meal set
            'unavailable',    -- Notion unreachable / not configured
            'no_plan'         -- reachable, but no default-day row found
        )),
    -- A locked day is exactly a locked_unconfirmed day.
    CONSTRAINT day_locked_consistent
        CHECK ((status = 'locked_unconfirmed') = (locked_at IS NOT NULL))
);

-- ============================================================================
-- ENTRY — one food eaten (or planned) in a slot on a date.
--
-- Pre-fill writes status='assumed', confidence='exact', source='notion'.
-- A correction writes status='corrected' and REPLACES the slot's entries
-- (the superseded rows are deleted, not kept — the day's truth is one set of
-- rows; acos.audit_log is the ledger of what changed).
-- ============================================================================

CREATE TABLE IF NOT EXISTS nutrition.entry (
    id           SERIAL PRIMARY KEY,
    day_date     DATE NOT NULL REFERENCES nutrition.day(day_date) ON DELETE CASCADE,
    slot         TEXT NOT NULL,
    food_id      INT REFERENCES nutrition.food(id),
    description  TEXT NOT NULL,
    quantity     NUMERIC(6,2) NOT NULL DEFAULT 1,
    kcal         INT,
    protein_g    NUMERIC(6,2),
    carb_g       NUMERIC(6,2),
    fat_g        NUMERIC(6,2),
    fiber_g      NUMERIC(6,2),
    source       TEXT NOT NULL,
    source_id    TEXT,
    confidence   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'assumed',
    logged_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT entry_slot_known
        CHECK (slot IN ('breakfast', 'lunch', 'dinner', 'snacks')),
    CONSTRAINT entry_status_known
        CHECK (status IN ('assumed', 'corrected')),
    CONSTRAINT entry_confidence_known
        CHECK (confidence IN ('exact', 'matched', 'estimated')),
    CONSTRAINT entry_source_known
        CHECK (source IN ('notion', 'usda', 'off', 'saved', 'manual', 'estimate')),
    -- THE no-invented-macros guard. Anything carrying macros must say where
    -- they came from, unless it is explicitly `estimated` — and an estimated
    -- entry must still declare its origin in `source`.
    CONSTRAINT entry_source_id_required
        CHECK (
            confidence = 'estimated'
            OR source = 'manual'
            OR source_id IS NOT NULL
        ),
    CONSTRAINT entry_estimated_is_estimate_source
        CHECK (confidence <> 'estimated' OR source = 'estimate')
);

CREATE INDEX IF NOT EXISTS idx_nutrition_entry_day
    ON nutrition.entry(day_date);
CREATE INDEX IF NOT EXISTS idx_nutrition_entry_day_slot
    ON nutrition.entry(day_date, slot);

-- ============================================================================
-- TARGET — the dietitian's envelope. Same shape and same one-open-row rule as
-- health.nutrition_target (migration 016), which this supersedes for DIET-1.
-- Artemis never sets or changes a target itself.
-- ============================================================================

CREATE TABLE IF NOT EXISTS nutrition.target (
    id             SERIAL PRIMARY KEY,
    effective_from DATE NOT NULL,
    effective_to   DATE,
    kcal           INT NOT NULL,
    protein_g      INT NOT NULL,
    carb_g         INT,
    fat_g          INT,
    fiber_g        INT,
    set_by         TEXT NOT NULL DEFAULT 'ryan',
    provisional    BOOLEAN NOT NULL DEFAULT FALSE,
    notes          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_nutrition_target
    ON nutrition.target ((effective_to IS NULL))
    WHERE effective_to IS NULL;

-- ============================================================================
-- day_totals(p_date) — the day's rollup.
--
-- p_date is REQUIRED: no timezone default. The caller resolves "today" with
-- quiet_hours.local_today() so a `set timezone to <place>` override is
-- honoured. Returns one row even for a day with no entries (zeros), so a
-- report can tell "nothing logged" from "no such day".
-- ============================================================================

CREATE OR REPLACE FUNCTION nutrition.day_totals(p_date DATE)
RETURNS TABLE (
    kcal       BIGINT,
    protein_g  NUMERIC,
    carb_g     NUMERIC,
    fat_g      NUMERIC,
    fiber_g    NUMERIC,
    n_entries  BIGINT
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        COALESCE(SUM(kcal), 0)::BIGINT,
        COALESCE(SUM(protein_g), 0),
        COALESCE(SUM(carb_g), 0),
        COALESCE(SUM(fat_g), 0),
        COALESCE(SUM(fiber_g), 0),
        COUNT(*)::BIGINT
    FROM nutrition.entry
    WHERE day_date = p_date;
$$;
