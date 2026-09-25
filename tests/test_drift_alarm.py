"""DRIFT-ALARM — the three drift checks, their guard key, and their failure modes.

The whole point of this job is to fail LOUDLY, so most of these tests are about
what happens when a check cannot reach its target: `unknown`, never "no drift".

Run:
    python3.11 tests/test_drift_alarm.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artemis import drift  # noqa: E402

MAIN = "c69a86a0d5871747351b56bc20c2e6935b6b33f3"
OLD = "8e675df2b4b3b1f62c63981dce95491498381782"


def _response(body: str):
    """A urlopen context manager returning `body`."""
    resp = MagicMock()
    resp.read.return_value = body.encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda *a: False
    return resp


class TestLambdaDescription(unittest.TestCase):
    def test_the_sha_token_is_read(self):
        self.assertEqual(
            drift.parse_lambda_sha(f"sha={MAIN} branch=main deployed=2026-09-25T12:00:00Z"), MAIN)

    def test_the_stale_force_refresh_string_yields_nothing(self):
        self.assertIsNone(drift.parse_lambda_sha("force-refresh-1777952455"))
        self.assertIsNone(drift.parse_lambda_sha(None))
        self.assertIsNone(drift.parse_lambda_sha("sha="))

    def test_a_description_without_a_sha_is_unknown_not_ok(self):
        with patch.object(drift, "lambda_description", return_value="force-refresh-1777952455"):
            r = drift.check_lambda(MAIN)
        self.assertEqual(r["state"], drift.UNKNOWN)

    def test_matching_and_mismatching_shas(self):
        with patch.object(drift, "lambda_description", return_value=f"sha={MAIN}"):
            self.assertEqual(drift.check_lambda(MAIN)["state"], drift.OK)
        with patch.object(drift, "lambda_description", return_value=f"sha={OLD}"):
            r = drift.check_lambda(MAIN)
        self.assertEqual(r["state"], drift.DRIFT)
        self.assertIn(OLD[:7], r["detail"])


class TestVersionJson(unittest.TestCase):
    """The regression this alarm exists for: /version.json served the SPA
    fallback — HTML, status 200 — so a status check called a stale site fine."""

    def test_html_with_a_200_is_rejected(self):
        with patch.object(drift.urllib.request, "urlopen",
                          return_value=_response("<!DOCTYPE html><html>…")):
            with self.assertRaises(RuntimeError) as caught:
                drift.fetch_version_json()
        self.assertIn("did not return JSON", str(caught.exception))

    def test_json_without_a_sha_is_rejected(self):
        with patch.object(drift.urllib.request, "urlopen",
                          return_value=_response('{"builtAt": "2026-09-25T12:00:00Z"}')):
            with self.assertRaises(RuntimeError):
                drift.fetch_version_json()

    def test_a_real_version_json_parses(self):
        with patch.object(drift.urllib.request, "urlopen",
                          return_value=_response(f'{{"sha": "{MAIN}", "short": "c69a86a"}}')):
            self.assertEqual(drift.fetch_version_json()["sha"], MAIN)

    def test_a_stale_site_is_drift_and_a_current_one_is_ok(self):
        with patch.object(drift, "gym_display_main_sha", return_value=MAIN):
            with patch.object(drift, "fetch_version_json", return_value={"sha": OLD}):
                self.assertEqual(drift.check_gym_display()["state"], drift.DRIFT)
            with patch.object(drift, "fetch_version_json", return_value={"sha": MAIN}):
                self.assertEqual(drift.check_gym_display()["state"], drift.OK)


class TestNothingRaises(unittest.TestCase):
    """A monitor that dies in the scheduler is worse than none."""

    def test_an_unreachable_target_is_unknown_not_ok(self):
        with patch.object(drift, "gym_display_main_sha",
                          side_effect=urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b""))):
            r = drift._safe(drift.check_gym_display, "gym_display")
        self.assertEqual(r["state"], drift.UNKNOWN)
        self.assertIn("401", r["detail"])

    def test_an_unwritten_ssm_parameter_is_unknown_not_ok(self):
        """CI has not pushed to main yet: no parameter, so no answer — which is
        unknown, not 'the site is current'."""
        with patch.object(drift, "gym_display_main_sha",
                          side_effect=RuntimeError("ParameterNotFound")):
            r = drift._safe(drift.check_gym_display, "gym_display")
        self.assertEqual(r["state"], drift.UNKNOWN)
        self.assertIn("ParameterNotFound", r["detail"])

    def test_one_broken_check_still_reports_the_other_two(self):
        with patch.object(drift, "origin_main_sha", return_value=MAIN), \
             patch.object(drift, "check_box", return_value=drift._result("box", drift.OK, "on c69a86a", MAIN, MAIN)), \
             patch.object(drift, "lambda_description", side_effect=RuntimeError("boto3 exploded")), \
             patch.object(drift, "gym_display_main_sha", return_value=MAIN), \
             patch.object(drift, "fetch_version_json", return_value={"sha": MAIN}):
            results = drift.check_all()
        by = {r["component"]: r["state"] for r in results}
        self.assertEqual(by, {"box": drift.OK, "lambda": drift.UNKNOWN, "gym_display": drift.OK})

    def test_no_origin_main_makes_both_git_checks_unknown_and_still_checks_pages(self):
        with patch.object(drift, "origin_main_sha", side_effect=RuntimeError("no network")), \
             patch.object(drift, "gym_display_main_sha", return_value=MAIN), \
             patch.object(drift, "fetch_version_json", return_value={"sha": MAIN}):
            results = drift.check_all()
        by = {r["component"]: r["state"] for r in results}
        self.assertEqual(by["box"], drift.UNKNOWN)
        self.assertEqual(by["lambda"], drift.UNKNOWN)
        self.assertEqual(by["gym_display"], drift.OK)


class TestSignatureGuard(unittest.TestCase):
    """Keyed on the drift STATE, not the date: the same drift must not repost
    every hour, but a new one must post the hour it appears."""

    def _results(self, box_state, live=OLD):
        return [drift._result("box", box_state, "d", live, MAIN),
                drift._result("lambda", drift.OK, "d", MAIN, MAIN),
                drift._result("gym_display", drift.OK, "d", MAIN, MAIN)]

    def test_the_same_drift_has_the_same_signature(self):
        self.assertEqual(drift.signature(self._results(drift.DRIFT)),
                         drift.signature(self._results(drift.DRIFT)))

    def test_order_does_not_change_the_signature(self):
        a = self._results(drift.DRIFT)
        self.assertEqual(drift.signature(a), drift.signature(list(reversed(a))))

    def test_a_recovery_changes_the_signature(self):
        self.assertNotEqual(drift.signature(self._results(drift.DRIFT)),
                            drift.signature(self._results(drift.OK, MAIN)))

    def test_a_different_wrong_sha_changes_the_signature(self):
        """Redeploying the wrong commit is new drift, not the same drift."""
        self.assertNotEqual(drift.signature(self._results(drift.DRIFT, OLD)),
                            drift.signature(self._results(drift.DRIFT, "a" * 40)))

    def test_all_clear_only_when_every_check_is_ok(self):
        self.assertTrue(drift.all_clear(self._results(drift.OK, MAIN)))
        self.assertFalse(drift.all_clear(self._results(drift.DRIFT)))
        self.assertFalse(drift.all_clear(self._results(drift.UNKNOWN)))


class TestMessage(unittest.TestCase):
    def test_it_names_each_component_and_explains_unknown(self):
        msg = drift.format_message([
            drift._result("box", drift.DRIFT, "running 8e675df, main is c69a86a", OLD, MAIN),
            drift._result("lambda", drift.OK, "deployed from c69a86a", MAIN, MAIN),
            drift._result("gym_display", drift.UNKNOWN, "HTTPError: 401", None, None)])
        for expected in ("box", "lambda", "gym_display", "8e675df", "could not reach"):
            self.assertIn(expected, msg)

    def test_no_unknown_no_footnote(self):
        msg = drift.format_message([drift._result("box", drift.OK, "on c69a86a", MAIN, MAIN)])
        self.assertNotIn("could not reach", msg)


if __name__ == "__main__":
    unittest.main()
