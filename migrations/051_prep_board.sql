-- 051_prep_board.sql
-- PREP-1: shopping list + pantry. The procurement half of the nutrition
-- subsystem — what to BUY, as opposed to what was eaten (040) or planned.
--
-- WHERE THE TRUTH LIVES
--   Notion is where recipes / ingredients / stores are EDITED (MVP). RDS is the
--   system of record: every synced row is keyed by its Notion page id and
--   upserted, so a re-sync is idempotent and a Notion outage leaves the last
--   known state readable rather than blank.
--
--   These tables do NOT duplicate nutrition.food. food holds the MACRO identity
--   of a recipe or ingredient (kind, slug) and is what meal logging looks up;
--   prep_recipe / prep_ingredient hold the PROCUREMENT facts (units, package
--   sizes, shelf life, par levels, on-hand) that food has no columns for. The
--   two are joined on the Notion page id via food_id, which is NULLABLE on
--   purpose: the ingredients database also holds cleaning and hygiene rows, and
--   rows with no macros at all. Those must appear on a shopping list and must
--   NOT appear in a food lookup, so they get a prep_ingredient row and no food
--   row. Creating food rows for them to make the join total would put dish soap
--   in the meal-deviation search.
--
-- APP-OWNED vs SYNCED (the distinction the sync code must respect)
--   Synced from Notion, overwritten every run: name, category, unit, package
--   sizes, aisle, rank, shelf life, par level, yield factor, servings.
--   App-owned, NEVER overwritten by a sync: on_hand_base, rank_override,
--   plan_eligible. on_hand_base is SEEDED from Notion's `on hand (pkgs)` only
--   while it is NULL; after that RDS owns it and the write-back pushes it to
--   Notion. A sync that clobbered it would undo a pantry count the moment the
--   next sync ran.
--
-- ON-HAND IS STORED IN BASE UNITS (g / mL / count), not packages. Packages are
-- a property of a STORE ITEM, so storing packages silently rebases every count
-- the day the preferred store changes. The UI shows packages; the column holds
-- base units. (Alignment decision 7, 2026-09-29.)
--
-- ENUM-EXPAND: introduces no enum and widens no CHECK on an existing column.
-- COLUMN-GREP: drops and renames nothing. The two ALTERs on nutrition.target
-- are additive and nullable; every existing reader of that table names its
-- columns explicitly (dietitian_report.py, artemis/health.py), so a NULL sugar
-- target reads as "no sugar target", which is the honest answer.

-- ============================================================================
-- TARGETS — two more of Ryan's own numbers get a home (alignment decision 4)
--
-- NULLABLE, and no default. A target Artemis was never given stays NULL and is
-- reported as "not set", never as a zero that would make every day pass or
-- every day fail. `plant_meals_min` is a count of meals, not a macro, and lives
-- here because it is a target Ryan set and the Prep check reads it.
-- ============================================================================
ALTER TABLE nutrition.target ADD COLUMN IF NOT EXISTS sugar_g INT;
ALTER TABLE nutrition.target ADD COLUMN IF NOT EXISTS plant_meals_min INT;

-- ============================================================================
-- SYNC WATERMARKS — one row per Notion database
--
-- `last_edited` is the high-water mark of last_edited_time seen in a successful
-- run; the next incremental run asks Notion only for rows edited at or after
-- it. `last_full_sweep` is when every row was last enumerated — the only run
-- that can prove a row is GONE, because an incremental query cannot see a
-- deletion. A sweep marks the missing rows deleted_at rather than removing
-- them, so a shopping list built last week still renders.
-- ============================================================================
CREATE TABLE IF NOT EXISTS nutrition.prep_sync (
    db_key          TEXT PRIMARY KEY,
    last_edited     TIMESTAMPTZ,
    last_full_sweep TIMESTAMPTZ,
    last_run        TIMESTAMPTZ,
    last_status     TEXT,
    last_detail     TEXT,
    rows_seen       INT
);

-- ============================================================================
-- STORES
-- ============================================================================
CREATE TABLE IF NOT EXISTS nutrition.prep_store (
    notion_id     TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    chain         TEXT,
    city          TEXT,
    -- MPLS | WI in Notion. Free text here: a new location must not need a
    -- migration before the sync can store it.
    location      TEXT,
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    notes         TEXT,
    notion_edited TIMESTAMPTZ,
    seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at    TIMESTAMPTZ
);

-- ============================================================================
-- INGREDIENTS — the procurement view of one `ingredients` row
-- ============================================================================
CREATE TABLE IF NOT EXISTS nutrition.prep_ingredient (
    notion_id       TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL,
    -- the macro identity, when this row has macros at all. See the header.
    food_id         INT REFERENCES nutrition.food(id) ON DELETE SET NULL,
    category        TEXT,
    -- g | mL | count. The BASE unit every quantity below is expressed in.
    -- NULL means Notion has not said, and a row with no unit cannot be
    -- converted to packages — it is flagged, not guessed.
    unit            TEXT,
    -- as-used per 1 purchased unit (dry lentils 2.5, raw chicken 0.75).
    -- NULL = 1; stored as given so "not set" and "exactly 1" stay distinct.
    yield_factor    NUMERIC(8,3),
    shelf_life_days INT,
    par_level_pkgs  NUMERIC(8,2),
    bulk            BOOLEAN NOT NULL DEFAULT FALSE,
    pantry          BOOLEAN NOT NULL DEFAULT FALSE,
    frequency       TEXT,
    -- APP-OWNED. Base units. NULL = never counted, which is not zero: an
    -- uncounted ingredient is reported as unknown so the list says so instead
    -- of confidently telling him to buy a full package he may already have.
    on_hand_base    NUMERIC(12,3),
    on_hand_at      TIMESTAMPTZ,
    on_hand_source  TEXT,
    -- Notion's own `on hand (pkgs)`, kept raw. Two jobs: it SEEDS on_hand_base
    -- once a package size is known (which is only after the store items sync, so
    -- seeding is a second pass), and it makes a divergence between what Notion
    -- says and what RDS holds visible instead of silent.
    notion_on_hand_pkgs NUMERIC(10,2),
    -- last value pushed to Notion, in packages, so the write-back can skip
    -- a row it has already pushed and report what it actually changed.
    pushed_pkgs     NUMERIC(10,2),
    pushed_at       TIMESTAMPTZ,
    notion_edited   TIMESTAMPTZ,
    seen_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS prep_ingredient_slug ON nutrition.prep_ingredient (slug);
CREATE INDEX IF NOT EXISTS prep_ingredient_food ON nutrition.prep_ingredient (food_id);

-- ============================================================================
-- STORE ITEMS — one row per (ingredient x store): the thing on a shelf
--
-- `rank` is Notion's first-choice ordering (1 = first choice). `rank_override`
-- is APP-OWNED and wins, so Ryan can re-rank from the phone without editing
-- Notion. Neither is required: an item with no rank anywhere falls back to the
-- seed rule in knowledge/prep_math.py, and the list says which rule it used.
-- ============================================================================
CREATE TABLE IF NOT EXISTS nutrition.prep_store_item (
    notion_id            TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    ingredient_notion_id TEXT REFERENCES nutrition.prep_ingredient(notion_id) ON DELETE CASCADE,
    store_notion_id      TEXT REFERENCES nutrition.prep_store(notion_id) ON DELETE CASCADE,
    rank                 INT,
    rank_override        INT,
    aisle                TEXT,
    -- in the ingredient's base unit
    package_size         NUMERIC(12,3),
    package_label        TEXT,
    price                NUMERIC(10,2),
    notion_edited        TIMESTAMPTZ,
    seen_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at           TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS prep_store_item_ing ON nutrition.prep_store_item (ingredient_notion_id);
CREATE INDEX IF NOT EXISTS prep_store_item_store ON nutrition.prep_store_item (store_notion_id);

-- ============================================================================
-- RECIPES — the procurement view of one `recipes` row
-- ============================================================================
CREATE TABLE IF NOT EXISTS nutrition.prep_recipe (
    notion_id       TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL,
    food_id         INT REFERENCES nutrition.food(id) ON DELETE SET NULL,
    course          TEXT,
    status          TEXT,
    archived        BOOLEAN NOT NULL DEFAULT FALSE,
    -- how many portions one batch yields. NULL = Notion has not said; the
    -- batch count cannot be derived and the recipe is flagged.
    servings        NUMERIC(8,2),
    prepped_on_hand NUMERIC(8,2),
    -- APP-OWNED (alignment decision 12): a recipe that may be eaten but must
    -- never be planned into a stay, e.g. the Protein PB cup.
    plan_eligible   BOOLEAN NOT NULL DEFAULT TRUE,
    notion_edited   TIMESTAMPTZ,
    seen_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS prep_recipe_slug ON nutrition.prep_recipe (slug);

-- ============================================================================
-- RECIPE LINES — ingredient x qty per serving
-- ============================================================================
CREATE TABLE IF NOT EXISTS nutrition.prep_recipe_line (
    notion_id            TEXT PRIMARY KEY,
    label                TEXT,
    recipe_notion_id     TEXT REFERENCES nutrition.prep_recipe(notion_id) ON DELETE CASCADE,
    ingredient_notion_id TEXT REFERENCES nutrition.prep_ingredient(notion_id) ON DELETE CASCADE,
    -- AS USED, in the ingredient's base unit. Purchased quantity is this
    -- divided by yield_factor; the two are different numbers for anything dry.
    qty_per_serving      NUMERIC(12,3),
    note                 TEXT,
    notion_edited        TIMESTAMPTZ,
    seen_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at           TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS prep_recipe_line_recipe ON nutrition.prep_recipe_line (recipe_notion_id);
CREATE INDEX IF NOT EXISTS prep_recipe_line_ing ON nutrition.prep_recipe_line (ingredient_notion_id);

-- ============================================================================
-- STAYS — a Minneapolis stay, PROPOSED from the cycle and confirmed by Ryan
--
-- Not typed in Notion (alignment decision 5): a stay is a maximal run of MSP
-- days read from `cycle` WITH overrides, so a leave week or an away trip moves
-- it automatically. `source` records whether these dates came from the cycle or
-- from Ryan editing them, because a proposal and a decision are different
-- things and the list header says which one it is built on.
-- ============================================================================
CREATE TABLE IF NOT EXISTS nutrition.prep_stay (
    id           SERIAL PRIMARY KEY,
    start_date   DATE NOT NULL,
    end_date     DATE NOT NULL,
    -- cycle | ryan
    source       TEXT NOT NULL DEFAULT 'cycle',
    -- the day the shopping happens; the top-up split is measured from it
    shop_date    DATE,
    confirmed_at TIMESTAMPTZ,
    notes        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT prep_stay_dates CHECK (end_date >= start_date)
);
CREATE UNIQUE INDEX IF NOT EXISTS prep_stay_start ON nutrition.prep_stay (start_date);

-- ============================================================================
-- STAY DAYS — the resolved menu, one row per (stay, day, slot, recipe)
--
-- Written by the BOX, which is the only side that can read Notion: the menu for
-- a day comes from NUTRITION-2's source order (dated pick, then the day kind's
-- default row). Persisting it is what lets the Lambda recompute the shopping
-- list from RDS alone on every read, so a pantry count entered on the phone
-- changes the list immediately instead of waiting for the next box run.
--
-- `servings` is how many portions of that recipe that day's slot calls for.
-- ============================================================================
CREATE TABLE IF NOT EXISTS nutrition.prep_stay_day (
    id               SERIAL PRIMARY KEY,
    stay_id          INT NOT NULL REFERENCES nutrition.prep_stay(id) ON DELETE CASCADE,
    day_date         DATE NOT NULL,
    day_type         TEXT,
    slot             TEXT NOT NULL,
    recipe_notion_id TEXT,
    recipe_name      TEXT,
    servings         NUMERIC(8,2) NOT NULL DEFAULT 1,
    -- picked | default : which NUTRITION-2 tier this day came from
    source           TEXT,
    resolved_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS prep_stay_day_stay ON nutrition.prep_stay_day (stay_id, day_date);

-- ============================================================================
-- CONFIG — editable knobs, not constants in source (alignment decision 12)
-- ============================================================================
CREATE TABLE IF NOT EXISTS nutrition.prep_config (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The store-rank seed rule, used only where a store item carries no rank.
-- Chain names are the Notion `chain` values.
INSERT INTO nutrition.prep_config (key, value) VALUES
    ('store_rank_seed', '{
       "produce_or_bulk": ["Eastside Co-op", "Aldi", "Cub Foods"],
       "default":         ["Aldi", "Eastside Co-op", "Cub Foods"],
       "produce_categories": ["produce"]
     }'::jsonb)
ON CONFLICT (key) DO NOTHING;

-- PANTRY-NUDGE is a standing automation, so it ships OFF (alignment 8).
INSERT INTO nutrition.prep_config (key, value) VALUES
    ('pantry_nudge', '{"enabled": false}'::jsonb)
ON CONFLICT (key) DO NOTHING;
