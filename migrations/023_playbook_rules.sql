-- 023_playbook_rules.sql
-- Chat-authored declarative inbox rules (feature #1 — automation on-ramp).
--
-- A rule is a match (sender/subject/body substrings) → action (archive/spam/file).
-- Rows are read at RUNTIME by the triage loop, so adding a rule takes effect
-- WITHOUT a redeploy. Every rule action flows through the audited disposition
-- primitive and writes an acos.audit_log row — automation is inspectable, never
-- trusted. Activation is propose-then-confirm only (Brad Spaits descendant):
-- the LLM never writes a rule; create_rule() does, after explicit confirmation.

CREATE TABLE IF NOT EXISTS acos.playbook_rules (
    id             SERIAL PRIMARY KEY,
    name           TEXT        NOT NULL,
    match_sender   TEXT,                    -- case-insensitive substring on from_email (NULL = any)
    match_subject  TEXT,                    -- case-insensitive substring on subject   (NULL = any)
    match_body     TEXT,                    -- case-insensitive substring on body       (NULL = any)
    action         TEXT        NOT NULL,    -- 'archive' | 'spam' | 'file'
    action_label   TEXT,                    -- required when action = 'file' (@artemis/<label>)
    active         BOOLEAN     NOT NULL DEFAULT TRUE,
    created_by     TEXT        NOT NULL DEFAULT 'ryan',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    times_fired    INTEGER     NOT NULL DEFAULT 0,
    last_fired_at  TIMESTAMPTZ,
    CONSTRAINT playbook_rules_action_chk
        CHECK (action IN ('archive', 'spam', 'file')),
    CONSTRAINT playbook_rules_has_match_chk
        CHECK (match_sender IS NOT NULL OR match_subject IS NOT NULL OR match_body IS NOT NULL),
    CONSTRAINT playbook_rules_file_label_chk
        CHECK (action <> 'file' OR (action_label IS NOT NULL AND action_label <> ''))
);

CREATE INDEX IF NOT EXISTS idx_playbook_rules_active ON acos.playbook_rules (active);
