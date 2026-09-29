-- BW-CIRCUIT (2026-09-29): a bodyweight circuit Ryan can swap onto any day.
--
-- ENUM-EXPAND. `bodyweight_circuit` is written to health.plan (a swap replaces
-- that day's row), so the CHECK has to know it. Consumers grepped before this
-- ran:
--   * gym-display's `SessionType` union — added in the same change, FIRST, so
--     the `never` defaults and Record<> lookups force every call site.
--   * `SESSION_LABELS` in format.ts — the union addition breaks the build until
--     it names the new value, which is the point.
--   * the box's _DISPLAY / session_library / health_regions maps.
--   * Nothing in the Lambda compares session_type against a whitelist; the
--     rest/cardio predicates test specific values and a new one is simply not
--     one of them, which is correct — a circuit is neither rest nor cardio.
--
-- `yoga_strength`, `core` and `mobility` are deliberately NOT added: those are
-- library EXTRAS, never seeded into health.plan, which is why they are absent
-- from this constraint today.

ALTER TABLE health.plan DROP CONSTRAINT IF EXISTS plan_session_type_check;

ALTER TABLE health.plan
    ADD CONSTRAINT plan_session_type_check
    CHECK (session_type = ANY (ARRAY[
        'strength_a'::text, 'strength_b'::text, 'strength_c'::text,
        'cardio_intervals'::text, 'cardio_z2'::text, 'walk'::text,
        'rest'::text, 'rest_mobility'::text, 'recovery_flow'::text,
        'bodyweight_circuit'::text
    ]));
