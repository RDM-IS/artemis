-- 046_cognition_log_view.sql
-- COGNITION-1 decision (b): acos.cognition_log survives as a VIEW NAME, never a
-- table (Ryan, 2026-09-28).
--
-- WHY A VIEW AND NOT A TABLE: §2's original sketch called for a cognition_log
-- table. Bronze is acos.audit_log plus three columns instead, so the name would
-- otherwise mean nothing -- and a second table would be the two-store seam CRM-2
-- and COMMIT-1 both record as a mistake. A view keeps the name meaningful with no
-- second store and nothing to keep in sync.
--
-- A DECISION is an audit row carrying `assumptions`. Its OUTCOME is a separate,
-- later row referencing it through metadata.decides (decision (a): append, never
-- UPDATE). This view is the join, one row per decision, outcome columns NULL
-- while it is still open.
--
-- The LATERAL is deliberate. A plain LEFT JOIN would emit TWO rows for a decision
-- that somehow acquired two outcome rows, quietly breaking the one-row-per-decision
-- contract that every reader of this view will assume. cognition_outcomes.run()
-- guards against that on the write side; this guarantees it on the read side, so
-- the contract does not depend on the writer staying correct forever.
--
-- CREATE OR REPLACE, so re-running the migration set is safe. No DDL on
-- audit_log itself, no data written or moved.

CREATE OR REPLACE VIEW acos.cognition_log AS
SELECT
    d.id                     AS decision_id,
    d.created_at             AS decided_at,
    d.agent,
    d.action,
    d.domain,
    d.outcome                AS decided,           -- what the code chose
    d.confidence,
    d.assumptions,                                 -- what it believed at the time
    d.metadata               AS decision_metadata,
    d.manual_gap,
    d.source,
    d.correction             AS decision_correction,
    o.id                     AS outcome_id,
    o.created_at             AS outcome_at,
    o.outcome                AS observed,           -- what became of it
    o.metadata               AS outcome_metadata,
    o.correction             AS outcome_correction,
    (o.id IS NOT NULL)       AS closed
FROM acos.audit_log d
LEFT JOIN LATERAL (
    SELECT o2.id, o2.created_at, o2.outcome, o2.metadata, o2.correction
      FROM acos.audit_log o2
     WHERE o2.action = d.action || '.outcome'
       AND o2.metadata->>'decides' = d.id::text
     ORDER BY o2.created_at
     LIMIT 1
) o ON TRUE
WHERE d.assumptions IS NOT NULL;

COMMENT ON VIEW acos.cognition_log IS
    'COGNITION-1 bronze: one row per decision (acos.audit_log with assumptions), '
    'left-joined to its appended outcome row. Read-only; the store is audit_log.';
