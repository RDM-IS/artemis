-- WEEK-TEMPLATE / FULLBODY-1 (approved by Ryan 2026-11-01 10:42): the one
-- full-body strength session gets its own session_type, `strength_full`.
--
-- ENUM-EXPAND. Consumers updated in the same change, gym-display's union FIRST:
--   * gym-display: `SessionType` union, SESSION_LABELS, the Library / Status
--     strength maps, and the segmented circuit renderer (blocks.segments).
--   * box: knowledge.session_types.STRENGTH_TYPES is now the one list that
--     RECOMMEND, the check-in and the session library read; health_office
--     (_DISPLAY, LEGAL_SESSION_TYPES, can_hold, the builder), health.py's
--     equipment / pretty-name / alias maps.
--   * Lambda: /strength-next returns strength_full; _LEGACY_PRETTY names it;
--     /recommend reads STRENGTH_TYPES. is_training() already matches it
--     (startswith "strength").
-- adhoc_session_type on session_log has no CHECK list (044), so a launched
-- full-body session needs nothing there.
--
-- Widens the CHECK only; no row changes. Idempotent.

ALTER TABLE health.plan DROP CONSTRAINT IF EXISTS plan_session_type_check;

ALTER TABLE health.plan
    ADD CONSTRAINT plan_session_type_check
    CHECK (session_type = ANY (ARRAY[
        'strength_a'::text, 'strength_b'::text, 'strength_c'::text,
        'strength_full'::text,
        'cardio_intervals'::text, 'cardio_z2'::text, 'walk'::text,
        'rest'::text, 'rest_mobility'::text, 'recovery_flow'::text,
        'bodyweight_circuit'::text
    ]));
