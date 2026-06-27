-- 015_health_session_log_round_num.sql
-- Round index for round-structured circuits (cardio segments / strength rounds).
-- Additive + nullable: existing rows and the flat row-per-event contract are
-- unaffected. Circuit/round structure is captured by round_num + set_num +
-- exercise on the flat log — never nested. No CHECK constraint changes.

ALTER TABLE health.session_log ADD COLUMN IF NOT EXISTS round_num INT;
