-- 025_dossier_approve.sql
-- STAB-1 B1: terminology "bless" → "approve" for dossier entries (user-facing +
-- schema). Ryan is the only user — a clean cut, no compatibility alias.
--
-- dossier_entry.status: 'blessed' → 'approved' (values + CHECK); blessed_at →
-- approved_at. Idempotent (safe to re-run): guarded drops/renames.

ALTER TABLE acos.dossier_entry DROP CONSTRAINT IF EXISTS dossier_entry_status_check;

UPDATE acos.dossier_entry SET status = 'approved' WHERE status = 'blessed';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'dossier_entry_status_check'
    ) THEN
        ALTER TABLE acos.dossier_entry
            ADD CONSTRAINT dossier_entry_status_check
            CHECK (status IN ('draft', 'approved'));
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'acos' AND table_name = 'dossier_entry'
          AND column_name = 'blessed_at'
    ) THEN
        ALTER TABLE acos.dossier_entry RENAME COLUMN blessed_at TO approved_at;
    END IF;
END $$;
