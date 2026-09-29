-- 052_prep_board.sql
-- PREP-2: the prep board. Recipe steps, the scheduled session, and the event log.
--
-- PREP-1 answered "what do I buy". This answers "in what order do I cook it, and
-- what is running right now".
--
-- WHY STEPS ARE APP-OWNED AND NOT SYNCED
--   Notion has no step table and is not going to grow one: steps are the input to
--   a scheduler, not something to read in a page. So `recipe_step` is the first
--   prep table RDS owns outright rather than mirrors, and it is edited in the Prep
--   tab. It joins the synced recipe by Notion page id, the same join everything
--   else in this schema uses.
--
-- WHAT THE SCHEDULER IS AND IS NOT ALLOWED TO DO
--   The scheduler runs in the BROWSER (knowledge of it is not in this database at
--   all) so that a re-flow works with the Wi-Fi off — which is the point of a
--   kitchen tool. `prep_session.schedule_json` is therefore a RECORD of what was
--   computed, never the authority: reopening the board recomputes it. A stored
--   schedule that the code no longer agrees with is exactly the stale-answer
--   problem the shopping list avoids by recomputing on every read.
--
-- WHY prep_event EXISTS NOW AND IS USED LATER
--   Every start/done/extend is logged with its planned and actual minutes, so the
--   duration estimates can be learned from real sessions (phase 2). Recording from
--   the first session is free; back-filling it later is impossible. Nothing reads
--   it yet, and nothing should pretend to: the estimates stay the spec's starting
--   numbers until there is data.
--
-- COLUMN-GREP: drops and renames nothing. ENUM-EXPAND: the CHECKs below are new
-- columns on new tables, so no existing consumer reads them.

-- ============================================================================
-- RECIPE STEP — one step of one recipe
--
-- DURATION IS TWO NUMBERS, not one: `base_min` is what the step costs regardless
-- of batch size (press the tofu, preheat) and `per_serving_min` is what scales
-- with it (portion 2 min per serving). One combined number cannot express either
-- honestly — "portion: 8 min" is wrong for every batch size except the one it was
-- measured at.
--
-- `batch_key` is what merges shared prep across recipes. Two recipes that both
-- need 450 g of the same cut vegetables carry the same key, and the scheduler
-- makes them ONE task whose duration is the sum of the per-serving parts plus the
-- LARGEST base — because you wash the board once.
-- ============================================================================
CREATE TABLE IF NOT EXISTS nutrition.recipe_step (
    id               SERIAL PRIMARY KEY,
    recipe_notion_id TEXT NOT NULL REFERENCES nutrition.prep_recipe(notion_id)
                          ON DELETE CASCADE,
    -- 1-based order within the recipe. A step depends on the one before it.
    step_no          INT NOT NULL,
    name             TEXT NOT NULL,
    -- hands | oven | stove | air_fryer | counter | fridge
    -- `counter` and `fridge` are resources with no contention (unlimited), and
    -- they exist so "pressing" and "cooling" occupy a lane rather than vanishing.
    resource         TEXT NOT NULL,
    -- active   : the hands are busy for the whole duration
    -- passive  : a device runs it; the hands are free
    -- unattended: takes NO session time at all ("oats -> fridge overnight").
    --             Zero, not small: it must never extend a session.
    mode             TEXT NOT NULL DEFAULT 'active',
    base_min         NUMERIC(6,2) NOT NULL DEFAULT 0,
    per_serving_min  NUMERIC(6,2) NOT NULL DEFAULT 0,
    -- oven steps only; a different temperature needs its own oven window
    temp_f           INT,
    -- shared-prep merge key, e.g. 'roast:veg'. NULL = never merged.
    batch_key        TEXT,
    -- travel: this component must be packed separately, with the reason
    keep_separate    BOOLEAN NOT NULL DEFAULT FALSE,
    keep_separate_note TEXT,
    -- a shortcut toggle that can skip this step, e.g. 'precut_veg'
    shortcut_key     TEXT,
    notes            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT recipe_step_resource CHECK (resource = ANY (ARRAY[
        'hands', 'oven', 'stove', 'air_fryer', 'counter', 'fridge'])),
    CONSTRAINT recipe_step_mode CHECK (mode = ANY (ARRAY[
        'active', 'passive', 'unattended'])),
    CONSTRAINT recipe_step_minutes CHECK (base_min >= 0 AND per_serving_min >= 0),
    -- An oven step without a temperature cannot be shared with another oven step,
    -- and the scheduler would have to guess. Required where it matters, absent
    -- everywhere else.
    CONSTRAINT recipe_step_oven_temp CHECK (resource <> 'oven' OR temp_f IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS recipe_step_order
    ON nutrition.recipe_step (recipe_notion_id, step_no);
CREATE INDEX IF NOT EXISTS recipe_step_batch ON nutrition.recipe_step (batch_key)
    WHERE batch_key IS NOT NULL;

-- ============================================================================
-- PREP SESSION — one cooking session
--
-- `started_at` is the ABSOLUTE clock time the session began, and every timer on
-- the board is derived from it plus the task's offset. That is deliberate: an
-- interval-based timer drifts and then dies when the tab sleeps, and an iPad on a
-- kitchen counter locks constantly. Absolute timestamps are correct after a lock,
-- a reload and an aeroplane-mode blip alike.
-- ============================================================================
CREATE TABLE IF NOT EXISTS nutrition.prep_session (
    id            SERIAL PRIMARY KEY,
    stay_id       INT REFERENCES nutrition.prep_stay(id) ON DELETE SET NULL,
    session_date  DATE NOT NULL,
    -- planned | running | done | abandoned
    status        TEXT NOT NULL DEFAULT 'planned',
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    -- the computed plan, as a RECORD. See the header: the board recomputes.
    schedule_json JSONB,
    -- which shortcut toggles were on, e.g. ["precut_veg"]
    shortcuts     JSONB NOT NULL DEFAULT '[]'::jsonb,
    planned_min   INT,
    hands_on_min  INT,
    actual_min    INT,
    notes         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT prep_session_status CHECK (status = ANY (ARRAY[
        'planned', 'running', 'done', 'abandoned']))
);
CREATE INDEX IF NOT EXISTS prep_session_stay ON nutrition.prep_session (stay_id);
CREATE INDEX IF NOT EXISTS prep_session_date ON nutrition.prep_session (session_date);

-- ============================================================================
-- PREP EVENT — what actually happened, for later duration learning
--
-- `planned_min` and `actual_min` side by side is the whole point: the difference
-- is the correction the estimates need. Nothing reads this yet.
-- ============================================================================
CREATE TABLE IF NOT EXISTS nutrition.prep_event (
    id           SERIAL PRIMARY KEY,
    session_id   INT NOT NULL REFERENCES nutrition.prep_session(id) ON DELETE CASCADE,
    task_key     TEXT NOT NULL,
    task_name    TEXT,
    resource     TEXT,
    -- start | done | extend | skip
    kind         TEXT NOT NULL,
    at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    planned_min  NUMERIC(6,2),
    actual_min   NUMERIC(6,2),
    delta_min    NUMERIC(6,2),
    CONSTRAINT prep_event_kind CHECK (kind = ANY (ARRAY[
        'start', 'done', 'extend', 'skip']))
);
CREATE INDEX IF NOT EXISTS prep_event_session ON nutrition.prep_event (session_id, at);

-- ============================================================================
-- KITCHEN PROFILE — editable, in prep_config rather than its own table
--
-- Ryan's answer to the open question, taken as the default until he says
-- otherwise: 1 oven with 2 usable rack slots, 2 burners, 1 air fryer. Counter and
-- fridge are unlimited, which is why they have no slot count — they are lanes for
-- legibility, not resources under contention.
--
-- `temp_change_min` is the 5 minutes an oven needs between two different
-- temperatures, and `preheat_min` the 10 before the first oven step. Both are the
-- spec's numbers and both are here rather than in code, because they are the kind
-- of thing a different oven changes.
-- ============================================================================
INSERT INTO nutrition.prep_config (key, value) VALUES
    ('kitchen_profile', '{
       "oven_slots": 2,
       "burners": 2,
       "air_fryer_slots": 1,
       "preheat_min": 10,
       "temp_change_min": 5,
       "filler_min": 6,
       "filler_name": "Clean as you go"
     }'::jsonb)
ON CONFLICT (key) DO NOTHING;
