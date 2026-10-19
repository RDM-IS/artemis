-- 055_session_log_row_circuit.sql
-- ROW-CIRCUIT (2026-10-19): rower intervals with floor work between them.
--
-- Three nullable columns on health.session_log, all additive:
--
--   stroke_rate  strokes/min HELD in a rowing block. The progression is a rate
--                ladder (Ryan: 20/21/22/23 -> 21/22/23/24), so the rate is the
--                quantity being progressed and cannot live in free-text notes.
--   started_at   when the block or exercise began, from the iPad's clock.
--   ended_at     when it ended.
--
-- Why the times: `logged_at` is when the row was INSERTED. A round is saved on
-- its record card several minutes after its row began, so `logged_at` cannot
-- bound an exercise. With start and end on every row, heart rate per round and
-- per exercise is computed from the watch's samples over those windows -- no
-- pause after each exercise, no watch segments, and the same windows feed
-- ZONE-0 and the CARDIO-DETECT overlap guard.
--
-- Every existing row keeps NULL: "not recorded", never a guess.
--
-- Consumers grepped before this was written (SAME-FACT-MANY-PLACES):
--   * api/app/routers/health.py post_log -- BOTH insert bind dicts (the set
--     and the in-band session_summary) gain the three binds in the same change;
--     a missing bind 500s every POST (the 043 incident).
--   * Nothing else SELECTs these names. Readers that SELECT * get three extra
--     NULL columns.
--
-- Not an ENUM-EXPAND: no CHECK on an existing column changes.

ALTER TABLE health.session_log
    ADD COLUMN IF NOT EXISTS stroke_rate SMALLINT,
    ADD COLUMN IF NOT EXISTS started_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS ended_at    TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'health' AND t.relname = 'session_log'
          AND c.conname = 'session_log_stroke_rate_range'
    ) THEN
        ALTER TABLE health.session_log
            ADD CONSTRAINT session_log_stroke_rate_range
            CHECK (stroke_rate IS NULL OR stroke_rate BETWEEN 10 AND 60);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'health' AND t.relname = 'session_log'
          AND c.conname = 'session_log_window_order'
    ) THEN
        ALTER TABLE health.session_log
            ADD CONSTRAINT session_log_window_order
            CHECK (started_at IS NULL OR ended_at IS NULL OR ended_at >= started_at);
    END IF;
END $$;

-- The prescription reads the last two row-circuit sessions' Row blocks.
CREATE INDEX IF NOT EXISTS session_log_row_circuit_idx
    ON health.session_log (adhoc_session_type, started_at DESC)
    WHERE stroke_rate IS NOT NULL;
