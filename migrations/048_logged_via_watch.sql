-- CARDIO-DETECT (2026-09-29): a session Ryan confirmed from the watch's own
-- record needs a logged_via that says so.
--
-- ENUM-EXPAND. Every consumer was grepped before this ran:
--   * box, Lambda and scripts test `logged_via <> 'inferred'` to mean "a real
--     log". `watch_confirmed` is <> 'inferred', so it counts as real for the
--     interval gate, the makeup rules and ZONE-0 -- which is correct: Ryan
--     confirming what the watch recorded IS him saying he trained.
--   * NOTHING compares logged_via against a whitelist in code. The only list of
--     values was a comment in api/app/routers/health.py, updated in the same
--     change.
--   * gym-display types it as `string`, not a union, and its one consumer
--     (strength-cues.ts) tests `!== "inferred"`. No union to expand.
--
-- 'manual' would have been wrong: it is indistinguishable from him typing the
-- numbers, and the difference matters -- the duration and modality here came
-- from a heart-rate trace he agreed with, not from him reading a machine.

ALTER TABLE health.session_log
    DROP CONSTRAINT IF EXISTS session_log_logged_via_check;

ALTER TABLE health.session_log
    ADD CONSTRAINT session_log_logged_via_check
    CHECK (logged_via = ANY (ARRAY[
        'mattermost'::text, 'voice'::text, 'manual'::text, 'inferred'::text,
        'watch_confirmed'::text
    ]));
