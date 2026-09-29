-- ZONE-0 (2026-09-29): minutes per heart-rate zone for a cardio session,
-- computed from WATCH-1's samples.
--
-- WHY A TABLE AND NOT A COLUMN ON session_log. Zone minutes belong to a
-- SESSION, and session_log holds one row per set or block -- attaching them to
-- one of those rows would make "which row carries the truth" a question. This
-- is keyed by plan_id, which is already one row per (date, slot).
--
-- STATUS IS NOT NULLABLE AND THE MINUTES ARE. A session whose samples were too
-- sparse stores status='insufficient_hr_data' with NULL minutes, so a reader
-- cannot mistake "not measured" for "no time in that zone". That is the whole
-- point of the table: a 0 here means measured-and-zero.

CREATE TABLE IF NOT EXISTS health.session_hr_zones (
    plan_id          integer PRIMARY KEY
                     REFERENCES health.plan(plan_id) ON DELETE CASCADE,
    -- The window the samples were read from, so a recompute is reproducible
    -- and a reader can see what was actually covered.
    window_start     timestamptz NOT NULL,
    window_end       timestamptz NOT NULL,
    window_source    text NOT NULL,      -- how the window was decided
    status           text NOT NULL
                     CHECK (status IN ('ok', 'insufficient_hr_data',
                                       'no_samples', 'bad_window')),
    sample_count     integer NOT NULL DEFAULT 0,
    counted_sec      integer NOT NULL DEFAULT 0,
    unaccounted_sec  integer NOT NULL DEFAULT 0,
    -- NULL unless status = 'ok'. Never zero-filled.
    z1_min           integer,
    z2_min           integer,
    z3_min           integer,
    z4_min           integer,
    z5_min           integer,
    hr_max_used      integer NOT NULL,   -- so a ZONE-1 recalibration is visible
    zones_source     text NOT NULL,      -- knowledge.zones.SOURCE at compute time
    computed_at      timestamptz NOT NULL DEFAULT now(),

    -- The invariant, enforced rather than documented: minutes exist if and only
    -- if the status says they were measured.
    CONSTRAINT session_hr_zones_minutes_match_status CHECK (
        (status = 'ok'     AND z1_min IS NOT NULL AND z2_min IS NOT NULL
                           AND z3_min IS NOT NULL AND z4_min IS NOT NULL
                           AND z5_min IS NOT NULL)
     OR (status <> 'ok'    AND z1_min IS NULL AND z2_min IS NULL
                           AND z3_min IS NULL AND z4_min IS NULL
                           AND z5_min IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS session_hr_zones_status_idx
    ON health.session_hr_zones (status);
