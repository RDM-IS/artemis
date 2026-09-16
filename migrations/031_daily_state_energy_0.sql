-- 031_daily_state_energy_0.sql
-- health.daily_state.energy: 013 allowed 1-5, but check-ins are 0-5 since
-- FRIDAY-1 — "energy 0" would fail the insert and lose the whole check-in.
--
-- Relaxes the CHECK only; no row changes. Idempotent: the constraint is
-- swapped only while it still reads >= 1. Constraint name confirmed on RDS
-- 2026-09-16: daily_state_energy_check = CHECK (energy >= 1 AND energy <= 5).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'health' AND t.relname = 'daily_state'
          AND c.conname = 'daily_state_energy_check'
          AND pg_get_constraintdef(c.oid) LIKE '%energy >= 1%'
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
