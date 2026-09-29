"""One fence-stripper for the whole box, and truncation named as truncation.

The bug this exists for: email triage logged `Failed to parse triage response:
```json` 20 times in five days, which reads like a fencing bug. The fence strip
was already correct. Correlating each failure against `response_length` in
acos.audit_log settled it -- the failures were the LONG responses (median 2820
chars vs 511 for the ones that parsed), cut off mid-value by max_tokens=1000.

A truncated response is not a malformed one. Reporting it as a parse failure is
what kept the real cause hidden, so these tests care as much about WHICH error is
reported as about whether parsing succeeds.
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD
import pathlib
import re
import unittest

from artemis.llm_json import (
    LlmJsonError,
    looks_truncated,
    parse,
    stop_reason_of,
    strip_fences,
)

BODY = '[{"subject": "synthetic", "urgency": "low"}]'
PARSED = [{"subject": "synthetic", "urgency": "low"}]


class TestFences(unittest.TestCase):
    def test_a_fenced_json_response(self):
        self.assertEqual(parse(f"```json\n{BODY}\n```"), PARSED)

    def test_a_bare_fence(self):
        self.assertEqual(parse(f"```\n{BODY}\n```"), PARSED)

    def test_no_fence_at_all(self):
        self.assertEqual(parse(BODY), PARSED)

    def test_a_fence_with_whitespace_around_it(self):
        self.assertEqual(parse(f"   ```json   \n{BODY}\n   ```   \n"), PARSED)

    def test_any_language_tag(self):
        for tag in ("json", "JSON", "json5", "javascript", ""):
            with self.subTest(tag):
                self.assertEqual(parse(f"```{tag}\n{BODY}\n```"), PARSED)

    def test_prose_after_the_closing_fence(self):
        """The old regex was `\\s*```$` -- anchored to the end of the string, so
        one cheerful sentence after the fence made json.loads say "Extra data"."""
        self.assertEqual(parse(f"```json\n{BODY}\n```\nHope that helps!"), PARSED)

    def test_an_object_not_an_array(self):
        self.assertEqual(parse('```json\n{"items": []}\n```'), {"items": []})

    def test_strip_fences_is_idempotent_on_unfenced_text(self):
        self.assertEqual(strip_fences(BODY), BODY)
        self.assertEqual(strip_fences(strip_fences(f"```json\n{BODY}\n```")), BODY)


class TestTruncationIsNamedAsTruncation(unittest.TestCase):
    #: What the box actually received: a pretty-printed array, cut off.
    CUT_OFF = '```json\n[\n  {\n    "subject": "synthetic",\n    "urgency": "lo'

    def test_the_api_stop_reason_is_believed(self):
        with self.assertRaises(LlmJsonError) as ctx:
            parse(self.CUT_OFF, stop_reason="max_tokens")
        self.assertTrue(ctx.exception.truncated)
        self.assertIn("cut off", str(ctx.exception))
        self.assertIn("max_tokens", str(ctx.exception))

    def test_it_is_detected_without_a_stop_reason_too(self):
        """The three copies in parser.py never had the response object to hand."""
        with self.assertRaises(LlmJsonError) as ctx:
            parse(self.CUT_OFF)
        self.assertTrue(ctx.exception.truncated)

    def test_an_unclosed_fence_is_truncation(self):
        self.assertTrue(looks_truncated(f"```json\n{BODY}"))

    def test_an_unclosed_array_is_truncation(self):
        self.assertTrue(looks_truncated('[{"a": 1'))

    def test_a_refusal_is_NOT_truncation(self):
        """"I cannot do that." also fails to end in `}`. Calling that a
        truncation would send the next reader looking for the wrong fix."""
        self.assertFalse(looks_truncated("I cannot do that."))
        with self.assertRaises(LlmJsonError) as ctx:
            parse("I cannot do that.")
        self.assertFalse(ctx.exception.truncated)

    def test_prose_is_not_truncation(self):
        with self.assertRaises(LlmJsonError) as ctx:
            parse("Here are the emails: none of them are urgent.")
        self.assertFalse(ctx.exception.truncated)

    def test_a_complete_response_is_not_truncation(self):
        self.assertFalse(looks_truncated(f"```json\n{BODY}\n```"))
        self.assertFalse(looks_truncated(BODY))

    def test_stop_reason_max_tokens_on_text_that_happens_to_parse(self):
        """If it parses, it parses -- a stop_reason is not a reason to reject
        usable JSON."""
        self.assertEqual(parse(BODY, stop_reason="max_tokens"), PARSED)


class TestGarbageAndEmpty(unittest.TestCase):
    def test_empty_raises(self):
        for raw in ("", "   ", "\n"):
            with self.subTest(repr(raw)), self.assertRaises(LlmJsonError):
                parse(raw)

    def test_the_message_says_where_it_gave_up(self):
        with self.assertRaises(LlmJsonError) as ctx:
            parse('{"a": 1,,}')
        self.assertIn("char", str(ctx.exception))

    def test_the_raw_text_is_carried_for_logging(self):
        with self.assertRaises(LlmJsonError) as ctx:
            parse("nope")
        self.assertEqual(ctx.exception.raw, "nope")

    def test_it_is_a_ValueError(self):
        """dossier and interaction_logger catch broad Exception; intent and
        scheduling catch around their own block. Being a ValueError keeps every
        existing handler working."""
        self.assertTrue(issubclass(LlmJsonError, ValueError))


class TestStopReasonOf(unittest.TestCase):
    def test_reads_it_when_present(self):
        self.assertEqual(stop_reason_of(type("R", (), {"stop_reason": "end_turn"})()), "end_turn")

    def test_absent_is_none_not_an_error(self):
        self.assertIsNone(stop_reason_of(object()))


class TestThereIsOnlyOneCopyLeft(unittest.TestCase):
    """Eleven modules had the same three-regex fence strip. Every one of them
    shared both blind spots, and fixing one would have left ten."""

    def test_no_module_strips_fences_by_hand(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        pat = re.compile(r"""re\.sub\(\s*r?['"]\^`{3}""")
        offenders = [str(f.relative_to(root))
                     for f in list((root / "artemis").rglob("*.py")) + list((root / "scripts").rglob("*.py"))
                     if f.name != "llm_json.py" and pat.search(f.read_text())]
        self.assertEqual(offenders, [])

    def test_triage_asks_for_enough_tokens(self):
        """The actual fix. 1000 truncated a 2909-char response; anything at or
        below that ceiling reintroduces the bug the parser cannot see."""
        src = (pathlib.Path(__file__).resolve().parent.parent / "artemis" / "briefs.py").read_text()
        block = src.split("def triage_emails", 1)[1].split("def ", 1)[0]
        found = [int(m) for m in re.findall(r"max_tokens=(\d+)", block)]
        self.assertTrue(found, "triage_emails no longer names max_tokens")
        self.assertGreaterEqual(min(found), 3000)


if __name__ == "__main__":
    unittest.main()
