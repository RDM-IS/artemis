-- SESSION-OPEN step 3a (content approved by Ryan 2026-11-09): the bodyweight
-- full-body session gets its own session_type, `strength_bw`.
--
-- PROPOSED IN THE PR (feat/strength-bw), not yet approved for apply: it sits in
-- migrations/ on this feature branch only because the feature needs it
-- (PROPOSE-NOT-IN-MIGRATIONS covers unapproved DDL on main).
--
-- A LAUNCHED strength_bw needs nothing here: an ad-hoc log carries
-- session_log.adhoc_session_type, which has no CHECK list (044). This CHECK
-- matters the moment strength_bw is a health.plan row — a placement, a seeded
-- day, or the check-in's day-off rewrite of one.
--
-- ENUM-EXPAND. Consumers updated in the same change, gym-display's union FIRST:
--   * gym-display: `SessionType` union, SESSION_LABELS, STRENGTH_TYPES (keeps it
--     out of the "More" list), the QuickStart "Bodyweight" button.
--   * box: knowledge.session_types (STRENGTH_BW, LIFT_TYPES, STRENGTH_DAY_TYPES,
--     STRENGTH_LABEL — deliberately NOT STRENGTH_TYPES, the lift rotation);
--     health_office (_DISPLAY, LEGAL_SESSION_TYPES, can_hold, the builder
--     dispatch — no fallthrough); session_library (LIBRARY_TYPES);
--     health.py pretty names + aliases; health_regions; artemis/strength_bw.py.
--   * Lambda: _LEGACY_PRETTY; /recommend's 48 h lift clock reads LIFT_TYPES.
--
-- Widens the CHECK only; no row changes. Idempotent.

ALTER TABLE health.plan DROP CONSTRAINT IF EXISTS plan_session_type_check;

ALTER TABLE health.plan
    ADD CONSTRAINT plan_session_type_check
    CHECK (session_type = ANY (ARRAY[
        'strength_a'::text, 'strength_b'::text, 'strength_c'::text,
        'strength_full'::text, 'strength_bw'::text,
        'cardio_intervals'::text, 'cardio_z2'::text, 'walk'::text,
        'rest'::text, 'rest_mobility'::text, 'recovery_flow'::text,
        'bodyweight_circuit'::text
    ]));
