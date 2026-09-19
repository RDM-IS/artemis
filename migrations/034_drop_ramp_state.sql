-- 034 — RAMP-RETIRE (2026-09-19): drop the dormant ramp engine's state table.
--
-- The engine (artemis/health_ramp.py) and its `yes ramp` confirm route are
-- deleted in the same change. Before applying, the singleton row and the
-- ramp's audit history were exported on the box to
-- ~/backups/ramp_state_2026-09-19.json.
--
-- KEPT from migration 030: health.plan.status, health.plan.original_date and
-- plan_status_check — the Lambda /plan and /overview read plan.status
-- ('completed' => done), so they are not ramp-only.

DROP TABLE IF EXISTS health.ramp_state;
