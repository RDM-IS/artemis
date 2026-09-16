-- 031_pain_patterns.sql
-- feat/pain-ladder (PAIN-1): pain pattern surfacing + reflections, and the
-- FRIDAY-1 energy scale fix.
--
-- Idempotent (IF NOT EXISTS guards + a guarded CHECK swap). Nothing is
-- dropped except the old energy CHECK, which is replaced in the same statement
-- block. No existing rows change.

-- ---------------------------------------------------------------------------
-- health.pain_pattern — one row per exercise x region that has ever qualified.
--
-- Recomputed nightly (21:55 local) from health.session_log (exposures,
-- in-session `pain=<region>:<n>` notes) and health.daily_state (next-morning
-- check-in pain). A pattern is a CANDIDATE while `qualifies` is true:
-- hits >= 3 and hits / exposures >= 0.6 over the last 8 weeks.
--
--   hits / exposures     current counts over the window
--   qualifies            meets the threshold right now
--   first_seen/last_seen first and latest hit dates in the window
--   last_surfaced        when a Sunday review last posted it
--   surfaced_hits/_exposures  the counts that post showed ("new or changed" test)
--   mentioned_at         the one check-in reply mention
--   post_ids             Mattermost post ids that surfaced it; a thread reply to
--                        any of them is stored as a reflection on this pattern
--   status               open | dismissed | resolved
--   dismissed_at_hits    hits when dismissed; hidden until hits >= this + 2
--   resolved_at(_hits)   when resolved, and hits then; reopens when new data
--                        re-qualifies it (hits > resolved_at_hits)
--   evidence             {"hit_days": [...], "exposure_days": [...],
--                         "shared": [other same-day exercises on the region]}
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS health.pain_pattern (
    id                  serial PRIMARY KEY,
    exercise            text NOT NULL,
    region              text NOT NULL,
    hits                int  NOT NULL DEFAULT 0,
    exposures           int  NOT NULL DEFAULT 0,
    qualifies           boolean NOT NULL DEFAULT false,
    first_seen          date,
    last_seen           date,
    last_surfaced       timestamptz,
    surfaced_hits       int,
    surfaced_exposures  int,
    mentioned_at        timestamptz,
    post_ids            text[] NOT NULL DEFAULT '{}',
    status              text NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'dismissed', 'resolved')),
    dismissed_at_hits   int,
    resolved_at         timestamptz,
    resolved_at_hits    int,
    evidence            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (exercise, region)
);

-- ---------------------------------------------------------------------------
-- health.reflection — Ryan's own words about a pattern, stored verbatim.
-- Never changes a plan. pattern_id is null when the reply can't be tied to
-- exactly one pattern. source_post_id makes a redelivered post a no-op.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS health.reflection (
    id              serial PRIMARY KEY,
    pattern_id      int REFERENCES health.pain_pattern(id) ON DELETE SET NULL,
    text            text NOT NULL,
    post_id         text,
    source_post_id  text UNIQUE,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS reflection_pattern_idx ON health.reflection (pattern_id);

-- ---------------------------------------------------------------------------
-- health.daily_state.energy: 013 allowed 1-5, but check-ins are 0-5 since
-- FRIDAY-1 ("energy 0" would fail the insert and lose the whole check-in).
-- Swap the inline CHECK (auto-named daily_state_energy_check) for 0-5.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'health' AND t.relname = 'daily_state'
          AND c.conname = 'daily_state_energy_check'
          AND pg_get_constraintdef(c.oid) NOT LIKE '%>= 0%'
    ) THEN
        ALTER TABLE health.daily_state DROP CONSTRAINT daily_state_energy_check;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'health' AND t.relname = 'daily_state'
          AND c.conname = 'daily_state_energy_check'
    ) THEN
        ALTER TABLE health.daily_state
            ADD CONSTRAINT daily_state_energy_check CHECK (energy BETWEEN 0 AND 5);
    END IF;
END$$;
