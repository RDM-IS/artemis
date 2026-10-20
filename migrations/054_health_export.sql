-- 054_health_export.sql
-- HEALTH-EXPORT-1: the ledger of what Artemis has handed to Apple Health.
--
-- The path is  nutrition.entry -> Lambda -> an iOS Shortcut -> Apple Health ->
-- VA "Share My Health Data" -> the nutritionist. Apple Health is WRITE-ONLY
-- from a Shortcut: a sample can be logged and cannot be deleted or corrected.
-- So every rule here exists to make sure a sample is written at most once and
-- only after the day it describes can no longer change.
--
-- ONE ROW PER (day, slot). Not per entry: a correction DELETES and replaces a
-- slot's entry rows (040), so an entry id is not a stable identity, and Apple
-- Health carries no food name anyway — only numbers and a timestamp.
--
-- The macro columns are a SNAPSHOT of what was sent, not a view over
-- nutrition.entry. That is the point of a ledger: if the day's rows are ever
-- changed after the export, the difference is detectable (GET /status reports
-- it as drift) instead of silently becoming the new truth.
--
-- STATES
--   claimed   handed to the Shortcut; the write to Health is not yet confirmed
--   sent      the Shortcut confirmed it wrote the batch
-- A release DELETES the claimed rows, so the slots become eligible again. There
-- is deliberately no third state: nothing would read it.
--
-- ADDITIVE. No existing table is altered; nothing reads this table but the
-- export itself.

CREATE TABLE IF NOT EXISTS nutrition.health_export (
    id          SERIAL PRIMARY KEY,
    day_date    DATE NOT NULL,
    slot        TEXT NOT NULL,
    kcal        INT NOT NULL,
    protein_g   NUMERIC(7,2) NOT NULL,
    carb_g      NUMERIC(7,2) NOT NULL,
    fat_g       NUMERIC(7,2) NOT NULL,
    fiber_g     NUMERIC(7,2) NOT NULL,
    n_entries   INT NOT NULL,
    -- nutrition.day.status at claim time: was this a day Ryan touched
    -- ('corrected') or the plan standing unconfirmed?
    day_status  TEXT NOT NULL,
    -- the timestamp given to Health. A CONVENTION (the slot's configured clock
    -- time on that date), not a measured eating time: nutrition.entry records
    -- no time eaten.
    sample_at   TIMESTAMPTZ NOT NULL,
    batch_id    UUID NOT NULL,
    state       TEXT NOT NULL DEFAULT 'claimed',
    claimed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at     TIMESTAMPTZ,
    CONSTRAINT health_export_slot_known
        CHECK (slot IN ('breakfast', 'lunch', 'dinner', 'snacks')),
    CONSTRAINT health_export_state_known
        CHECK (state IN ('claimed', 'sent')),
    CONSTRAINT health_export_sent_consistent
        CHECK ((state = 'sent') = (sent_at IS NOT NULL)),
    -- THE at-most-once guard. Two runs racing for the same slot cannot both win.
    CONSTRAINT health_export_once UNIQUE (day_date, slot)
);

CREATE INDEX IF NOT EXISTS idx_health_export_batch
    ON nutrition.health_export(batch_id);
CREATE INDEX IF NOT EXISTS idx_health_export_claimed
    ON nutrition.health_export(state) WHERE state = 'claimed';

-- Ships OFF. The export pushes health data to a third party (the VA) by way of
-- Ryan's phone, so activation is his: see docs/HEALTH_EXPORT.md. `enabled`
-- alone is not enough — start_date and slot_times are required and are Ryan's
-- facts, so there is no default for either.
INSERT INTO nutrition.prep_config (key, value) VALUES
    ('health_export', '{"enabled": false}'::jsonb)
ON CONFLICT (key) DO NOTHING;
