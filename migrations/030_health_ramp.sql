-- 030_health_ramp.sql
-- feat/health-ramp: the ramp slide/tier engine's durable state.
--
-- Adds the per-row lifecycle needed to reconcile the reseeded weeks 1-7 plan
-- against session_log (planned -> completed | slid | missed), the slide bookkeeping
-- column, and a single-row engine state record (tier standing / consecutive-success
-- count / open proposal id).
--
-- Idempotent (IF NOT EXISTS guards + a guarded CHECK add, matching prior
-- migrations). No data is destroyed. NOTE on scope: `status` and `original_date`
-- are meaningful ONLY for the ramp rows (plan_date >= 2026-07-25); pre-existing
-- historical rows get status='planned' by default and are never read by the engine
-- (the nightly job filters strictly by date window), so back-filling them is inert.

-- ---------------------------------------------------------------------------
-- health.plan lifecycle columns.
--
-- status:        planned  -> the seeded/regenerated state (default)
--                completed-> a matching session_log row exists on/after plan_date (CT)
--                slid     -> the nightly job auto-moved this session inside its week
--                missed   -> terminal; could not be completed or fit a makeup slot
-- original_date: NULL until a slide; on slide it records the session's first planned
--                date so the diff/audit can show where it came from.
-- ---------------------------------------------------------------------------
ALTER TABLE health.plan ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'planned';
ALTER TABLE health.plan ADD COLUMN IF NOT EXISTS original_date date;

-- Guarded CHECK add (no ADD CONSTRAINT IF NOT EXISTS in this PG line). Re-running
-- the migration is a no-op once the constraint exists.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'health' AND t.relname = 'plan'
          AND c.conname = 'plan_status_check'
    ) THEN
        ALTER TABLE health.plan
            ADD CONSTRAINT plan_status_check
            CHECK (status IN ('planned', 'completed', 'slid', 'missed'));
    END IF;
END$$;

-- ---------------------------------------------------------------------------
-- health.ramp_state — single-row engine standing.
--
-- Singleton enforced by `id = 1`. The nightly job reads/writes exactly this row.
--   consecutive_success_count: successive 5/5 weeks (ramp complete at 2).
--   consecutive_nonsuccess_count: successive weeks that were NOT 5/5 (2 -> restart).
--   last_evaluated_end_date: the window-end (Saturday) of the most recent week
--                            already evaluated. A week is evaluated only when its
--                            end date is strictly greater — a monotonic guard that
--                            survives a restart (week_num repeats; end dates don't).
--   pending_proposal_id:     the open repeat/restart proposal's id (NULL when none);
--                            the engine never stacks a second proposal on top of one.
--   pending_proposal:        the FULL proposal payload (regenerated rows + diff) as
--                            jsonb. Kept in THIS row — same table, same transaction as
--                            the id/flag — so the gate flag and the data it needs can
--                            never diverge (no cross-store wedge). NULL when none.
--   revisit_prompted:        set true when the one-time week-2 ramp-revisit prompt has
--                            fired, so it never re-fires; a restart commit re-arms it.
--   ramp_complete:           set true once two consecutive successful weeks land.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS health.ramp_state (
    id                            int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    consecutive_success_count     int NOT NULL DEFAULT 0,
    consecutive_nonsuccess_count  int NOT NULL DEFAULT 0,
    last_evaluated_end_date       date,
    pending_proposal_id           text,
    pending_proposal              jsonb,
    revisit_prompted              boolean NOT NULL DEFAULT false,
    ramp_complete                 boolean NOT NULL DEFAULT false,
    updated_at                    timestamptz NOT NULL DEFAULT now()
);

-- If the table pre-existed an earlier cut of this migration, add the newer columns.
ALTER TABLE health.ramp_state ADD COLUMN IF NOT EXISTS pending_proposal jsonb;
ALTER TABLE health.ramp_state ADD COLUMN IF NOT EXISTS revisit_prompted boolean NOT NULL DEFAULT false;

-- Seed the singleton. ON CONFLICT DO NOTHING keeps re-runs safe and never resets
-- a live count made after the first apply.
INSERT INTO health.ramp_state (id) VALUES (1)
ON CONFLICT (id) DO NOTHING;
