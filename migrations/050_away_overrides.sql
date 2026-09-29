-- AWAY (2026-09-29): work trips, vacations and hunting as cycle overrides.
--
-- ONE override kind with two ATTRIBUTES, not four day types. `work + hotel_gym`
-- and `vacation + no_gym` differ in what they plan, not in what kind of day they
-- are -- and four day types would mean four ENUM-EXPANDs, four sets of consumer
-- greps, and four chances for one of them to be missed.
--
-- ENUM-EXPAND on day_type, consumers grepped:
--   * artemis/cycle.py — the 14-day pattern and the wake/quiet tables are keyed
--     by day_type. `away` never appears in the PATTERN (it only ever arrives as
--     an override), and cycle.day_type() falls back to the pattern, so the
--     tables are unaffected.
--   * artemis/nutrition.py `day_kind()` — maps day_type to work/travel/off.
--     `away` maps to TRAVEL in the same change: no pre-fill, logging only.
--   * health_office / week_ahead / dietitian_report / session_library read
--     day_type to pick a location or label a day; none compares it against a
--     whitelist that would silently drop a new value.
--   * gym-display does not model day_type at all (grepped: no hits in src/).
--
-- PURPOSE AND LODGING ARE CONSTRAINED COLUMNS, not free text: "what do we do on
-- this day" is decided by them, and a typo that reads as an unknown purpose
-- would have to fail closed at every call site instead of once here.

ALTER TABLE acos.cycle_day_overrides
    DROP CONSTRAINT IF EXISTS cycle_day_overrides_day_type_check;

ALTER TABLE acos.cycle_day_overrides
    ADD CONSTRAINT cycle_day_overrides_day_type_check
    CHECK (day_type = ANY (ARRAY[
        'msp_work'::text, 'msp_home'::text, 'wi'::text, 'travel'::text,
        'away'::text
    ]));

ALTER TABLE acos.cycle_day_overrides
    ADD COLUMN IF NOT EXISTS purpose text,
    ADD COLUMN IF NOT EXISTS lodging text,
    -- Per-stay extras: the packed kit and a hotel gym's actual inventory when
    -- Ryan tells us it differs from the conservative generic one.
    ADD COLUMN IF NOT EXISTS attrs jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE acos.cycle_day_overrides
    DROP CONSTRAINT IF EXISTS cycle_day_overrides_purpose_check;
ALTER TABLE acos.cycle_day_overrides
    ADD CONSTRAINT cycle_day_overrides_purpose_check
    CHECK (purpose IS NULL OR purpose = ANY (ARRAY[
        'work'::text, 'vacation'::text, 'hunting'::text]));

ALTER TABLE acos.cycle_day_overrides
    DROP CONSTRAINT IF EXISTS cycle_day_overrides_lodging_check;
ALTER TABLE acos.cycle_day_overrides
    ADD CONSTRAINT cycle_day_overrides_lodging_check
    CHECK (lodging IS NULL OR lodging = ANY (ARRAY[
        'hotel_gym'::text, 'no_gym'::text]));

-- An `away` row must say what KIND of away it is. Without this a purposeless
-- away day would reach the planner, which would then have to invent a policy --
-- and the safe invention (do nothing) is wrong for a work trip.
ALTER TABLE acos.cycle_day_overrides
    DROP CONSTRAINT IF EXISTS cycle_day_overrides_away_has_purpose;
ALTER TABLE acos.cycle_day_overrides
    ADD CONSTRAINT cycle_day_overrides_away_has_purpose
    CHECK (day_type IS DISTINCT FROM 'away' OR purpose IS NOT NULL);
