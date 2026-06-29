-- 022_audit_log_email_corpus.sql
-- Extend acos.audit_log with the EMAIL_MODEL.md ML-corpus columns (Phase E3).
--
-- WHY (docs/EMAIL_MODEL.md "audit_log — actions + ML corpus"): audit_log is the
-- permanent accountability ledger AND the future training corpus. Disposition
-- actions (archive/file/delete/spam) must capture the features a learner will
-- need so "how does Ryan dispose of emails like this?" becomes a SQL query
-- (Layer 1: "archived 14/14 from Etsy marketing → propose a rule"). The base
-- table (migration 002) has agent/action/outcome/metadata only — the email
-- features live nowhere queryable. This adds them as first-class columns.
--
-- ADDITIVE + IDEMPOTENT: ALTER ... ADD COLUMN IF NOT EXISTS, all nullable, no
-- backfill. Existing audit_log rows and existing log_audit() callers are
-- unaffected (the extended log_audit only references these columns when an
-- email/disposition arg is supplied). MIGRATE-FIRST: apply this before deploying
-- the E3 code that writes the new columns, or those writes hit missing columns.
--
-- Columns mirror the EMAIL_MODEL.md audit_log field list:
--   source        — who decided: user_directed | playbook:<id> | playbook:<id>:confirmed
--   action_class  — disposition | outbound
--   prior/applied/removed_labels — Gmail label deltas at decision time (TEXT[])
--   verified      — did a post-action Gmail re-read confirm the change
-- (`action`, message_id, thread_id, sender, sender_domain, subject round out the
-- decision-time features; received_hour / had_attachment stay in metadata JSONB.)

ALTER TABLE acos.audit_log ADD COLUMN IF NOT EXISTS message_id     TEXT;
ALTER TABLE acos.audit_log ADD COLUMN IF NOT EXISTS thread_id      TEXT;
ALTER TABLE acos.audit_log ADD COLUMN IF NOT EXISTS source         TEXT;
ALTER TABLE acos.audit_log ADD COLUMN IF NOT EXISTS action_class   TEXT;
ALTER TABLE acos.audit_log ADD COLUMN IF NOT EXISTS sender         TEXT;
ALTER TABLE acos.audit_log ADD COLUMN IF NOT EXISTS sender_domain  TEXT;
ALTER TABLE acos.audit_log ADD COLUMN IF NOT EXISTS subject        TEXT;
ALTER TABLE acos.audit_log ADD COLUMN IF NOT EXISTS prior_labels   TEXT[];
ALTER TABLE acos.audit_log ADD COLUMN IF NOT EXISTS applied_labels TEXT[];
ALTER TABLE acos.audit_log ADD COLUMN IF NOT EXISTS removed_labels TEXT[];
ALTER TABLE acos.audit_log ADD COLUMN IF NOT EXISTS verified       BOOLEAN;

-- Corpus-mining indexes (Layer-1 frequency stats group/filter by these):
CREATE INDEX IF NOT EXISTS idx_audit_log_sender_domain ON acos.audit_log (sender_domain);
CREATE INDEX IF NOT EXISTS idx_audit_log_action        ON acos.audit_log (action);
CREATE INDEX IF NOT EXISTS idx_audit_log_message_id    ON acos.audit_log (message_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_action_class  ON acos.audit_log (action_class);
