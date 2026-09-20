-- 039_drop_plan_status_debris.sql  — PROPOSED, NOT APPLIED
-- PLAN-STATUS-DEBRIS: drop health.plan.status and health.plan.original_date.
--
-- Both arrived with migration 030 for the ramp engine, which RAMP-RETIRE
-- deleted (#111, 2026-09-19). Since then NOTHING in the codebase writes
-- either column:
--   * `status` still held 'missed' on 53 rows, all dated 2026-07-25..09-15 —
--     written by the ramp, all pre-program, none in the current program.
--   * `original_date` was populated on 0 rows, ever.
-- Meanwhile api/app/routers/health.py still READ `status`, treating
-- 'completed' as done. A column that looks authoritative, is abandoned, and is
-- still consulted is exactly what later gets trusted.
--
-- After this, `done` derives from health.session_log alone — the one source
-- every other consumer already uses (EVAL-1, EXPORT-1, the check-in).
--
-- SHIP ORDER MATTERS. The Lambda change that stops reading `status` is in the
-- same PR and must be DEPLOYED BEFORE this migration runs. Dropping the column
-- while the old code is live would 500 every /plan and /overview call.
--
-- Not idempotent-by-accident: IF EXISTS makes a re-run a no-op.
--
-- IRREVERSIBLE. The 53 stale values are not backed up because they are ramp
-- output about pre-program days, superseded by session_log. If that judgement
-- is ever in doubt, export them first:
--   SELECT plan_date, status FROM health.plan WHERE status <> 'planned';

ALTER TABLE health.plan DROP COLUMN IF EXISTS status;
ALTER TABLE health.plan DROP COLUMN IF EXISTS original_date;
