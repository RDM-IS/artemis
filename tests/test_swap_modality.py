"""Tests for SWAP-2 (gated session-modality swap) and the HEALTH-1 Part A
deterministic-routing fix.

No live DB: knowledge.db primitives and the durable pending KV are mocked.

Covers (per SPEC):
  - detect_modality_swap / detect_swap_revert regex suite (positives + negatives)
  - translate_blocks round-trip (translate -> revert == original) for every
    office machine target across cardio_z2 (steady) and cardio_intervals shapes
  - structural + stimulus preservation through translation
  - CT-anchored target-date resolution (the ~20:00 CT / 01:00 UTC day-ahead bug)
  - lifecycle: propose -> yes applies; propose -> no cancels; "yes <reason>"
    captures the reason into the audit metadata
  - double-swap: pre_swap still holds the ORIGINAL after two swaps
  - refusals: strength day; revert with no pre_swap; yes with no pending swap
  - verify-from-reread: a write that doesn't persist -> failure, no confirmation
  - HEALTH-2: retired rower / bike trainer / walking pad targets refuse honestly
  - Part A: deterministic health intents bypass the LLM classifier (route_intent
    is never called); no live route reaches add_note or the "learning" re-route

Run:
    python tests/test_swap_modality.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import datetime as _dt
import json
import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

from artemis import health  # noqa: E402


# ── Block fixtures (cardio shapes the swap operates on) ─────────────────────

_INTERVALS_BLOCKS = {
    "type": "intervals", "rounds": 8,
    "warmup_sec": 300, "warmup_settings": "easy spin",
    "cooldown_sec": 300, "cooldown_settings": "easy spin",
    "intervals_template": {
        "work_sec": 60, "work_settings": "hard effort (Z4)",
        "rest_sec": 90, "rest_settings": "easy spin",
    },
    "equipment": ["stepmill", "upright bike"],
    "setup_notes": ["Stepmill or upright bike", "8 rounds: 60s hard / 90s easy"],
}

_Z2_BLOCKS = {
    "type": "steady", "duration_min": 55, "target_range_min": [45, 55],
    "intensity": "Zone 2", "warmup_sec": 300, "cooldown_sec": 300,
    "equipment": ["treadmill", "elliptical"],
    "setup_notes": ["Steady 45-55 min Zone 2", "Treadmill incline walk or elliptical"],
    "finisher": {"rounds": 3, "exercises": [{"name": "Plank", "duration_sec": 30}]},
}


def _plan_row(blocks, session_type="cardio_z2", plan_id=42, plan_date=None):
    return {
        "plan_id": plan_id,
        "plan_date": plan_date or date.today(),
        "session_type": session_type,
        "target_rpe": 4.5,
        "blocks": json.loads(json.dumps(blocks)),
    }


# ============================================================================
# B1 — regex suite
# ============================================================================

class TestModalitySwapRegex(unittest.TestCase):
    def test_positives(self):
        cases = [
            ("update outdoor workout to indoor elliptical", "elliptical", None),
            ("swap today to indoor bike", "upright_bike", None),
            ("switch my run to the treadmill", "treadmill", None),
            ("swap to stepmill due to wildfire smoke", "stepmill", "wildfire smoke"),
            ("indoor bike today", "upright_bike", None),     # bare form
            ("@artemis swap my cardio to recumbent bike", "recumbent_bike", None),
            ("swap my walk to the stair master", "stepmill", None),
            ("- change today's session to upright bike because it's raining",
             "upright_bike", "it's raining"),
        ]
        for msg, target, reason in cases:
            with self.subTest(msg=msg):
                r = health.detect_modality_swap(msg)
                self.assertIsNotNone(r, f"expected a match for {msg!r}")
                self.assertEqual(r["target"], target)
                self.assertEqual(r["reason"], reason)

    def test_reason_captured(self):
        r = health.detect_modality_swap("swap to stepmill due to wildfire smoke")
        self.assertEqual(r["reason"], "wildfire smoke")

    def test_revert(self):
        self.assertTrue(health.detect_swap_revert("swap revert"))
        self.assertTrue(health.detect_swap_revert("undo swap"))
        self.assertTrue(health.detect_swap_revert("@artemis revert swap"))
        # A revert is NOT a modality swap.
        self.assertIsNone(health.detect_modality_swap("swap revert"))

    def test_negatives(self):
        for neg in ("how was my workout", "trainer set indoor",
                    "what's today's workout", "hello", "how's my training going"):
            with self.subTest(neg=neg):
                self.assertIsNone(health.detect_modality_swap(neg),
                                  f"{neg!r} must NOT match a modality swap")

    def test_retired_trainer_command_not_swallowed(self):
        # 'trainer set indoor' is the retired bike-trainer command, never a swap.
        self.assertIsNone(health.detect_modality_swap("trainer set indoor"))
        self.assertEqual(health.detect_health_intent("trainer set indoor"),
                         health.INTENT_TRAINER_RETIRED)

    def test_retired_machine_is_not_a_swap_but_is_a_refusal(self):
        # HEALTH-2: the rower, bike trainer, and walking pad are retired. A
        # swap-shaped request naming one (or another unsupported option) must NOT
        # parse as a swap AND must route to the honest refusal, never the LLM.
        for msg in ("swap today to indoor rower",
                    "switch my workout to the walking pad",
                    "switch my cardio to rowing",
                    "change today's cardio to a swim"):
            with self.subTest(msg=msg):
                self.assertIsNone(health.detect_modality_swap(msg))
                self.assertTrue(health.looks_like_unsupported_workout_change(msg))


# ============================================================================
# B3 — translation: round-trip, structural + stimulus preservation
# ============================================================================

class TestTranslateBlocks(unittest.TestCase):
    def test_round_trip_all_targets_all_shapes(self):
        for shape, stype in ((_INTERVALS_BLOCKS, "cardio_intervals"),
                             (_Z2_BLOCKS, "cardio_z2")):
            for target in health._SWAP_TARGETS:
                with self.subTest(shape=stype, target=target):
                    new = health.apply_modality_swap(
                        shape, target, session_type=stype, reason="AQI",
                        swap_meta={"reason": "AQI"})
                    restored = health.revert_modality_swap(new)
                    self.assertEqual(restored, shape,
                                     "revert must reproduce the exact original")

    def test_structural_preservation(self):
        new = health.translate_blocks(_INTERVALS_BLOCKS, "stepmill",
                                      session_type="cardio_intervals")
        self.assertEqual(new["type"], "intervals")
        self.assertEqual(new["rounds"], 8)
        self.assertEqual(new["warmup_sec"], 300)
        self.assertEqual(new["cooldown_sec"], 300)
        self.assertEqual(new["intervals_template"]["work_sec"], 60)
        self.assertEqual(new["intervals_template"]["rest_sec"], 90)

    def test_stimulus_carries_unchanged(self):
        new = health.translate_blocks(_Z2_BLOCKS, "treadmill")
        self.assertEqual(new["intensity"], "Zone 2")
        self.assertEqual(new["target_range_min"], [45, 55])
        self.assertEqual(new["finisher"], _Z2_BLOCKS["finisher"])

    def test_modality_labels(self):
        r = health.translate_blocks(_INTERVALS_BLOCKS, "stepmill",
                                    session_type="cardio_intervals")
        self.assertEqual(r["equipment"], ["stepmill"])
        self.assertEqual(r["location"], "office gym")
        self.assertEqual(r["intervals_template"]["work_settings"], "hard climb")
        self.assertEqual(r["intervals_template"]["rest_settings"], "easy climb")
        self.assertEqual(r["warmup_settings"], "easy climb")
        self.assertEqual(r["display_name"], "Stepmill — Intervals")

        self.assertEqual(health.translate_blocks(_Z2_BLOCKS, "elliptical",
                                                 session_type="cardio_z2")["display_name"],
                         "Elliptical — Z2")
        self.assertEqual(health.translate_blocks(_Z2_BLOCKS, "upright_bike")["display_name"],
                         "Upright Bike — Z2")
        self.assertEqual(health.translate_blocks(_Z2_BLOCKS, "recumbent_bike")["equipment"],
                         ["recumbent bike"])
        self.assertEqual(health.translate_blocks(_Z2_BLOCKS, "treadmill")["display_name"],
                         "Treadmill — Z2")

    def test_setup_notes_reason_prefix(self):
        n = health.translate_blocks(_INTERVALS_BLOCKS, "stepmill", reason="wildfire AQI")
        self.assertTrue(n["setup_notes"][0].startswith("Stepmill (wildfire AQI)"))
        # trailing lines survive
        self.assertEqual(n["setup_notes"][1], _INTERVALS_BLOCKS["setup_notes"][1])

    def test_double_swap_preserves_original(self):
        s1 = health.apply_modality_swap(_INTERVALS_BLOCKS, "stepmill",
                                        session_type="cardio_intervals",
                                        swap_meta={"n": 1})
        s2 = health.apply_modality_swap(s1, "upright_bike",
                                        session_type="cardio_intervals",
                                        swap_meta={"n": 2})
        self.assertEqual(s2["pre_swap"], _INTERVALS_BLOCKS,
                         "double-swap must keep the true ORIGINAL under pre_swap")
        self.assertEqual(health.revert_modality_swap(s2), _INTERVALS_BLOCKS)

    def test_revert_no_pre_swap_returns_none(self):
        self.assertIsNone(health.revert_modality_swap(_Z2_BLOCKS))


# ============================================================================
# B2 — CT-anchored target-date resolution
# ============================================================================

_FIXED_UTC = _dt.datetime(2026, 7, 20, 1, 0, tzinfo=_dt.timezone.utc)  # 20:00 CT on 7/19


class _FakeDateTime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return _FIXED_UTC.astimezone(tz) if tz else _FIXED_UTC.replace(tzinfo=None)


class TestTargetDateResolution(unittest.TestCase):
    def test_ct_anchored_not_utc(self):
        """At 20:00 CT (01:00 UTC next day), the target must be the CT date
        (2026-07-19), never the UTC date (2026-07-20) — the day-ahead bug."""
        with patch.object(health, "datetime", _FakeDateTime), \
             patch.object(health, "_fetch_swap_plan_row",
                          return_value=_plan_row(_Z2_BLOCKS, "cardio_z2",
                                                  plan_date=date(2026, 7, 19))):
            target, row, refusal = health.resolve_swap_target()
        self.assertIsNone(refusal)
        self.assertEqual(target, date(2026, 7, 19))

    def test_strength_day_refuses(self):
        with patch.object(health, "_fetch_swap_plan_row",
                          return_value=_plan_row(_Z2_BLOCKS, "strength_a")):
            target, row, refusal = health.resolve_swap_target(today=date(2026, 7, 20))
        self.assertIsNone(target)
        self.assertIsNotNone(refusal)
        self.assertIn("strength", refusal.lower())

    def test_rest_day_falls_to_next_cardio(self):
        def fake_fetch(d):
            if d == date(2026, 7, 20):
                return _plan_row(_Z2_BLOCKS, "rest_mobility", plan_date=d)
            return _plan_row(_Z2_BLOCKS, "cardio_z2", plan_date=d)
        with patch.object(health, "_fetch_swap_plan_row", side_effect=fake_fetch), \
             patch.object(health, "_next_cardio_date", return_value=date(2026, 7, 22)):
            target, row, refusal = health.resolve_swap_target(today=date(2026, 7, 20))
        self.assertIsNone(refusal)
        self.assertEqual(target, date(2026, 7, 22))


# ============================================================================
# B4/B5 — lifecycle (propose / yes / no), verify-from-reread, audit
# ============================================================================

class _LifecycleBase(unittest.TestCase):
    def setUp(self):
        # In-memory durable pending store (stands in for system_state KV).
        self._kv = {}
        self._get = patch("artemis.quiet_hours.get_system_value",
                          side_effect=lambda k: self._kv.get(k))
        self._set = patch("artemis.quiet_hours.set_system_value",
                          side_effect=lambda k, v: self._kv.__setitem__(k, v))
        self._get.start()
        self._set.start()

        # Fake DB: execute_write captures the UPDATE; execute_one reflects it back
        # (a successful persist). Tests override _reread for the failure case.
        self._written = {}
        self._reread = None  # None → reflect what was written

        def fake_write(sql, params):
            if "UPDATE health.plan" in sql:
                self._written["blocks"] = json.loads(params[0])
                self._written["plan_id"] = params[1]
            return None

        def fake_one(sql, params):
            if self._reread is not None:
                return self._reread
            return {"plan_id": self._written.get("plan_id", 42),
                    "plan_date": date.today(),
                    "session_type": "cardio_z2",
                    "blocks": self._written.get("blocks")}

        self._write_patch = patch("knowledge.db.execute_write", side_effect=fake_write)
        self._one_patch = patch("knowledge.db.execute_one", side_effect=fake_one)
        self._write_patch.start()
        self._one_patch.start()

        self._audit = MagicMock(return_value="uuid-1")
        self._audit_patch = patch("knowledge.db.log_audit", self._audit)
        self._audit_patch.start()

    def tearDown(self):
        for p in (self._get, self._set, self._write_patch, self._one_patch,
                  self._audit_patch):
            p.stop()

    def _audit_meta(self):
        self.assertTrue(self._audit.called, "expected an audit_log write")
        return self._audit.call_args.kwargs


class TestSwapLifecycle(_LifecycleBase):
    def test_propose_writes_nothing_and_stages_pending(self):
        with patch.object(health, "_fetch_swap_plan_row",
                          return_value=_plan_row(_Z2_BLOCKS, "cardio_z2")):
            reply = health.propose_modality_swap(
                "swap today to elliptical", "chan1")
        self.assertIn("Elliptical", reply)
        self.assertIn("yes", reply.lower())
        self.assertNotIn("blocks", self._written)  # nothing written on propose
        self.assertIsNotNone(health.load_swap_pending("chan1"))

    def test_propose_then_yes_applies_and_audits(self):
        with patch.object(health, "_fetch_swap_plan_row",
                          return_value=_plan_row(_Z2_BLOCKS, "cardio_z2")):
            health.propose_modality_swap("swap today to elliptical", "chan1")
            reply = health.commit_modality_swap("chan1")
        self.assertIn("Swapped", reply)
        # The row was actually written and carries the swap + pre_swap.
        self.assertIn("swap", self._written["blocks"])
        self.assertIn("pre_swap", self._written["blocks"])
        self.assertEqual(self._written["blocks"]["display_name"], "Elliptical — Z2")
        self.assertEqual(self._written["blocks"]["location"], "office gym")
        # Pending cleared after commit.
        self.assertIsNone(health.load_swap_pending("chan1"))
        # Audit ledger row shape.
        meta = self._audit_meta()
        self.assertEqual(meta["action"], "plan_modality_swap")
        md = meta["metadata"]
        self.assertEqual(md["to"]["modality"], "elliptical")
        self.assertEqual(md["from"]["session_type"], "cardio_z2")
        self.assertNotIn("context", md)  # HEALTH-2: no weather snapshot

    def test_propose_then_no_cancels(self):
        with patch.object(health, "_fetch_swap_plan_row",
                          return_value=_plan_row(_Z2_BLOCKS, "cardio_z2")):
            health.propose_modality_swap("swap today to upright bike", "chan1")
            reply = health.cancel_modality_swap("chan1")
        self.assertIn("Cancelled", reply)
        self.assertNotIn("blocks", self._written)
        self.assertIsNone(health.load_swap_pending("chan1"))

    def test_yes_reason_captured_into_audit(self):
        with patch.object(health, "_fetch_swap_plan_row",
                          return_value=_plan_row(_Z2_BLOCKS, "cardio_z2")):
            health.propose_modality_swap("swap today to elliptical", "chan1")
            health.commit_modality_swap("chan1", reason_override="wildfire AQI")
        md = self._audit_meta()["metadata"]
        self.assertEqual(md["reason"], "wildfire AQI")
        # Reason also lands in the written swap metadata.
        self.assertEqual(self._written["blocks"]["swap"]["reason"], "wildfire AQI")

    def test_yes_with_no_pending(self):
        reply = health.commit_modality_swap("chan-empty")
        self.assertIn("Nothing pending", reply)
        self.assertFalse(self._audit.called)

    def test_verify_from_reread_failure_reports_no_confirmation(self):
        # The re-read returns the OLD blocks (swap did not persist).
        self._reread = {"plan_id": 42, "plan_date": date.today(),
                        "session_type": "cardio_z2",
                        "blocks": json.loads(json.dumps(_Z2_BLOCKS))}
        with patch.object(health, "_fetch_swap_plan_row",
                          return_value=_plan_row(_Z2_BLOCKS, "cardio_z2")):
            health.propose_modality_swap("swap today to elliptical", "chan1")
            reply = health.commit_modality_swap("chan1")
        self.assertIn("did not persist", reply)
        self.assertNotIn("Swapped", reply)
        self.assertFalse(self._audit.called, "no audit row on a failed persist")

class TestRevertLifecycle(_LifecycleBase):
    def _swapped_row(self):
        swapped = health.apply_modality_swap(
            _Z2_BLOCKS, "elliptical", session_type="cardio_z2", reason="AQI",
            swap_meta={"reason": "AQI", "requested_via": "mattermost"})
        return _plan_row(swapped, "cardio_z2")

    def test_revert_restores_original(self):
        row = self._swapped_row()
        with patch.object(health, "_fetch_swap_plan_row", return_value=row):
            propose = health.propose_swap_revert("chan1")
            reply = health.commit_swap_revert("chan1")
        self.assertIn("Revert", propose)
        self.assertIn("Reverted", reply)
        # The restored row drops swap + pre_swap.
        self.assertNotIn("swap", self._written["blocks"])
        self.assertNotIn("pre_swap", self._written["blocks"])
        self.assertEqual(self._written["blocks"], _Z2_BLOCKS)
        meta = self._audit_meta()
        self.assertEqual(meta["action"], "plan_modality_swap_revert")

    def test_revert_with_no_pre_swap_refuses(self):
        with patch.object(health, "_fetch_swap_plan_row",
                          return_value=_plan_row(_Z2_BLOCKS, "cardio_z2")):
            reply = health.propose_swap_revert("chan1")
        self.assertIn("isn't swapped", reply)
        self.assertIsNone(health.load_swap_pending("chan1"))


class TestUnsupportedTargetRefusal(_LifecycleBase):
    """An unknown/unsupported target must produce an honest refusal — never a
    confirmation — with ZERO plan writes and ZERO audit rows."""

    def test_unknown_target_at_parse_time_refuses_no_writes(self):
        # A retired modality isn't recognized as a swap at all → the
        # proposer returns the honest command list, writes nothing, audits
        # nothing, and stages no pending.
        self.assertIsNone(health.detect_modality_swap("swap today to indoor rower"))
        with patch.object(health, "_fetch_swap_plan_row",
                          return_value=_plan_row(_Z2_BLOCKS, "cardio_z2")):
            reply = health.propose_modality_swap(
                "swap today to indoor rower", "chanX")
        self.assertNotIn("✅", reply)
        self.assertIn("handler", reply.lower())          # format_health_help
        self.assertNotIn("blocks", self._written)        # no plan UPDATE
        self.assertFalse(self._audit.called)             # no audit row
        self.assertIsNone(health.load_swap_pending("chanX"))

    def test_commit_guard_rejects_stale_bad_target(self):
        # Defense in depth: a stale/hand-crafted pending with an unsupported
        # target must be refused at commit — no write, no audit, no "✅".
        health.store_swap_pending("chanX", {
            "kind": "swap", "target": "rower", "reason": None,
            "plan_id": 42, "plan_date": date.today().isoformat(),
            "session_type": "cardio_z2",
            "created_at": _dt.datetime.now(health.CT).isoformat(),
        })
        reply = health.commit_modality_swap("chanX")
        self.assertIn("Unsupported swap target", reply)
        self.assertNotIn("✅", reply)
        self.assertNotIn("blocks", self._written)        # no plan UPDATE
        self.assertFalse(self._audit.called)             # no audit row
        self.assertIsNone(health.load_swap_pending("chanX"))  # pending discarded


class TestConfirmationOnlyFromReread(_LifecycleBase):
    """Every success confirmation ('✅ Swapped' / '✅ Reverted') must be rendered
    from the RE-READ row — no code path may emit success without a verified
    persist, and the name shown must be the value the DB actually returned."""

    def _stage_swap(self):
        with patch.object(health, "_fetch_swap_plan_row",
                          return_value=_plan_row(_Z2_BLOCKS, "cardio_z2")):
            health.propose_modality_swap("swap today to elliptical", "chan1")

    def test_swap_success_name_comes_from_reread_not_intent(self):
        # The re-read returns a DELIBERATELY different display_name than intent.
        # Because the verify guard compares re-read vs intent, a mismatch must
        # FAIL — proving the confirmation is gated on the actual written row and
        # never synthesized from intent.
        self._stage_swap()
        self._reread = {"plan_id": 42, "plan_date": date.today(),
                        "session_type": "cardio_z2",
                        "blocks": {"display_name": "Something Else", "swap": {}}}
        with patch.object(health, "_fetch_swap_plan_row",
                          return_value=_plan_row(_Z2_BLOCKS, "cardio_z2")):
            reply = health.commit_modality_swap("chan1")
        self.assertNotIn("✅", reply)
        self.assertIn("did not persist", reply)
        self.assertFalse(self._audit.called)

    def test_swap_success_only_when_reread_shows_swap_key(self):
        # Re-read reflects the write but the persisted blocks lack the swap key
        # (as if a trigger stripped it) → NO success, NO audit.
        self._stage_swap()

        def strip_swap_write(sql, params):
            if "UPDATE health.plan" in sql:
                b = json.loads(params[0])
                b.pop("swap", None)
                self._written["blocks"] = b
                self._written["plan_id"] = params[1]

        with patch.object(health, "_fetch_swap_plan_row",
                          return_value=_plan_row(_Z2_BLOCKS, "cardio_z2")), \
             patch("knowledge.db.execute_write", side_effect=strip_swap_write):
            reply = health.commit_modality_swap("chan1")
        self.assertNotIn("✅", reply)
        self.assertFalse(self._audit.called)

    def test_swap_success_uses_verified_display_name(self):
        # Happy path: the '✅ Swapped to **X**' name is exactly the re-read
        # display_name (elliptical → 'Elliptical — Z2').
        self._stage_swap()
        with patch.object(health, "_fetch_swap_plan_row",
                          return_value=_plan_row(_Z2_BLOCKS, "cardio_z2")):
            reply = health.commit_modality_swap("chan1")
        self.assertIn("✅ Swapped to **Elliptical — Z2**", reply)
        self.assertEqual(self._audit.call_args.kwargs["metadata"]["to"]["display_name"],
                         self._written["blocks"]["display_name"])

    def test_revert_success_only_when_reread_clears_swap(self):
        # Re-read still shows a swap key (revert didn't persist) → NO success.
        swapped = health.apply_modality_swap(
            _Z2_BLOCKS, "elliptical", session_type="cardio_z2",
            swap_meta={"reason": None, "requested_via": "mattermost"})
        row = _plan_row(swapped, "cardio_z2")
        with patch.object(health, "_fetch_swap_plan_row", return_value=row):
            health.propose_swap_revert("chan1")
        # Write is a no-op so the re-read still carries swap/pre_swap.
        with patch.object(health, "_fetch_swap_plan_row", return_value=row), \
             patch("knowledge.db.execute_write", side_effect=lambda *a, **k: None), \
             patch("knowledge.db.execute_one",
                   side_effect=lambda *a, **k: {"plan_id": 42, "blocks": swapped}):
            reply = health.commit_swap_revert("chan1")
        self.assertNotIn("✅", reply)
        self.assertIn("did not persist", reply)
        self.assertFalse(self._audit.called)


class TestRestDayRefusal(unittest.TestCase):
    """rest / recovery / mobility / off-day changes are not modality swaps and
    aren't supported — they refuse honestly, never confirm."""

    def test_swap_to_rest_is_not_a_swap(self):
        for msg in ("swap to rest", "change today to a rest day",
                    "make today recovery", "switch to mobility",
                    "move me to an off day"):
            with self.subTest(msg=msg):
                self.assertIsNone(health.detect_modality_swap(msg))
                self.assertTrue(health.looks_like_unsupported_workout_change(msg))

    def test_rest_refusal_message_is_specific(self):
        reply = health.format_unsupported_change("swap to rest")
        self.assertNotIn("✅", reply)
        self.assertIn("rest", reply.lower())
        self.assertIn("aren't supported yet", reply)

    def test_non_rest_unsupported_change_uses_command_list(self):
        reply = health.format_unsupported_change("reschedule my run to Friday")
        self.assertNotIn("✅", reply)
        self.assertIn("handler", reply.lower())  # format_health_help


class TestActionClaimGate(unittest.TestCase):
    """Output-side no-fabrication gate: the deterministic filter that makes the
    LLM path structurally unable to CLAIM an action happened."""

    def test_flags_action_success_claims(self):
        for txt in (
            "✅ Swapped to Indoor Row.",
            "Logged 3 rows to session_log.",
            "Archived and marked DONE.",
            "Filed under Receipts.",
            "Sent the follow-up email.",
            "Reverted to the outdoor plan.",
            "I've updated your plan.",
            "gym.rdm.is is up to date.",
            "Got it — I've learned that Z2 means easy.",
        ):
            with self.subTest(txt=txt):
                self.assertIsNotNone(health.claims_unverified_action(txt),
                                     f"should flag: {txt!r}")

    def test_passes_benign_replies(self):
        for txt in (
            "Zone 2 keeps your HR around 130 — conversational pace.",
            "Here's what today's session looks like: 6 rounds of intervals.",
            "Your next cardio day is Thursday.",
            "",
        ):
            with self.subTest(txt=txt):
                self.assertIsNone(health.claims_unverified_action(txt),
                                  f"should NOT flag: {txt!r}")

    def test_no_handler_reply_is_honest(self):
        reply = health.format_no_handler_reply()
        self.assertIn("nothing was changed", reply.lower())
        self.assertNotIn("✅", reply)


if __name__ == "__main__":
    unittest.main(verbosity=2)
