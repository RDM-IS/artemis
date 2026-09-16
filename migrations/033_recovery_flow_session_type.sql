-- 033_recovery_flow_session_type.sql
-- YOGA-1: health.plan.session_type gains 'recovery_flow' (Thu office / Sat
-- home guided flow, replacing rest_mobility on those days).
--
-- Widens the CHECK only; no row changes. Idempotent: the constraint is swapped
-- only while it lacks 'recovery_flow'. Constraint name confirmed on RDS
-- 2026-09-16: plan_session_type_check.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'health' AND t.relname = 'plan'
          AND c.conname = 'plan_session_type_check'
          AND pg_get_constraintdef(c.oid) NOT LIKE '%recovery_flow%'
    ) THEN
        ALTER TABLE health.plan DROP CONSTRAINT plan_session_type_check;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'health' AND t.relname = 'plan'
          AND c.conname = 'plan_session_type_check'
    ) THEN
        ALTER TABLE health.plan ADD CONSTRAINT plan_session_type_check
            CHECK (session_type IN ('strength_a', 'strength_b', 'strength_c',
                                    'cardio_intervals', 'cardio_z2', 'walk',
                                    'rest_mobility', 'recovery_flow'));
    END IF;
END$$;
