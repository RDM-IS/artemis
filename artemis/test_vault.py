"""Tests for PB-011 — Vault / Second Brain Ingest (v1).

Two tiers (mirrors test_dossier.py):

  * MOCKED unit tests (always run) — pure parsing/hashing/routing logic: the
    tolerant frontmatter parser, content hashing, context detection, proposal
    idempotency key, extraction eligibility, wikilink parsing, the real-meeting
    coverage filter, digest rendering.  No AWS/RDS needed.

  * LIVE integration tests (skipped unless a local Postgres is reachable) —
    migrations 001+006+020+024..028 apply clean, and the real vault.py pipeline runs
    its real SQL against a throwaway DB (mirror + LLM patched): idempotent re-ingest
    (same sha no-op; same notes no dup rows/proposals), deletion + reappearance,
    wikilink resolution incl. dangling, proposal idempotency + 7-day expiry, the
    eligibility filter (journal/legacy never extracted), CT date anchoring, and
    no-fabrication (command replies render only from written rows).

Run:
    python3 -m artemis.test_vault
    python3 artemis/test_vault.py
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

from artemis import vault  # noqa: E402


# ============================================================================
# MOCKED — pure logic (always run)
# ============================================================================

class TestFrontmatterParser(unittest.TestCase):
    def test_valid(self):
        text = (
            "---\ncreated: 2025-12-31T18:18:50\ncapture_id: cap-legacy-20251231-1818\n"
            "status: bronze\nsource: legacy-kl\n---\n# Body\n\nSome text.\n"
        )
        fm, body = vault.parse_frontmatter(text)
        self.assertEqual(fm["capture_id"], "cap-legacy-20251231-1818")
        self.assertEqual(fm["source"], "legacy-kl")
        self.assertEqual(fm["status"], "bronze")
        self.assertEqual(body, "# Body\n\nSome text.\n")

    def test_missing_frontmatter(self):
        text = "# Just a heading\n\nno frontmatter here.\n"
        fm, body = vault.parse_frontmatter(text)
        self.assertEqual(fm, {})
        self.assertEqual(body, text)

    def test_unterminated_fence_is_tolerated(self):
        text = "---\ncapture_id: cap-1\nsource: thought\n# never closes\n"
        fm, body = vault.parse_frontmatter(text)
        self.assertEqual(fm, {})            # treated as no frontmatter, not a crash
        self.assertEqual(body, text)

    def test_malformed_line_skipped(self):
        text = "---\ncapture_id: cap-1\nthis line has no colon\nsource: thought\n---\nbody\n"
        fm, body = vault.parse_frontmatter(text)
        self.assertEqual(fm["capture_id"], "cap-1")
        self.assertEqual(fm["source"], "thought")
        self.assertNotIn("this line has no colon", fm)

    def test_body_byte_faithful(self):
        body_in = "line1\n\n  indented\ttabbed\ntrailing spaces   \n"
        text = f"---\ncapture_id: c\nsource: thought\n---\n{body_in}"
        _, body = vault.parse_frontmatter(text)
        self.assertEqual(body, body_in)

    def test_stray_body_key_not_frontmatter(self):
        # A legacy note has `source: [Pluralsight ...]` in the BODY — must not be read
        # as frontmatter (only the leading fenced block is parsed).
        text = "---\ncapture_id: c\nsource: legacy-notes\n---\nsource: [Pluralsight](http://x)\n"
        fm, body = vault.parse_frontmatter(text)
        self.assertEqual(fm["source"], "legacy-notes")
        self.assertIn("Pluralsight", body)

    def test_inline_list_tags(self):
        text = "---\ncapture_id: c\nsource: meeting\ntags: [fca, odae]\n---\nx\n"
        fm, _ = vault.parse_frontmatter(text)
        self.assertEqual(fm["tags"], ["fca", "odae"])

    def test_empty_list(self):
        text = "---\ncapture_id: c\nsource: meeting\nattendees: []\n---\nx\n"
        fm, _ = vault.parse_frontmatter(text)
        self.assertEqual(fm["attendees"], [])


class TestContentHash(unittest.TestCase):
    def test_crlf_and_trailing_ws_normalized(self):
        self.assertEqual(
            vault._content_hash("a\r\nb\r\n"),
            vault._content_hash("a\nb"),
        )

    def test_different_content_differs(self):
        self.assertNotEqual(vault._content_hash("a"), vault._content_hash("b"))


class TestContextDetection(unittest.TestCase):
    def test_fca_prefix(self):
        self.assertEqual(vault._detect_context("fca: notes about odae", {}), "fca")

    def test_fca_hashtag(self):
        self.assertEqual(vault._detect_context("some notes #fca more", {}), "fca")

    def test_fca_frontmatter_tag(self):
        self.assertEqual(vault._detect_context("body", {"tags": ["fca", "x"]}), "fca")

    def test_no_context(self):
        self.assertIsNone(vault._detect_context("just some ordinary thoughts", {}))

    def test_fca_substring_does_not_falsely_match(self):
        # 'fcast' / 'africa' must not trip the tag test (token split).
        self.assertIsNone(vault._detect_context("body", {"tags": ["forecast"]}))


class TestProposalIdempotencyKey(unittest.TestCase):
    def test_same_candidate_same_core(self):
        c1 = {"text": "Follow up with Dennis", "evidence": "x"}
        c2 = {"text": "  follow   up with dennis  ", "evidence": "y"}
        self.assertEqual(
            vault._proposal_core("action_item", c1),
            vault._proposal_core("action_item", c2),
        )

    def test_direction_in_commitment_core(self):
        a = vault._proposal_core("commitment", {"text": "send deck", "direction": "owed-by-ryan"})
        b = vault._proposal_core("commitment", {"text": "send deck", "direction": "owed-to-ryan"})
        self.assertNotEqual(a, b)

    def test_person_in_dossier_core(self):
        a = vault._proposal_core("dossier_entry", {"text": "met", "person": "Jennifer"})
        b = vault._proposal_core("dossier_entry", {"text": "met", "person": "Dennis"})
        self.assertNotEqual(a, b)


class TestEligibility(unittest.TestCase):
    def test_extract_sources(self):
        self.assertEqual(set(vault._EXTRACT_SOURCES), {"meeting", "dictation", "thought"})

    def test_journal_and_legacy_excluded(self):
        for s in ("journal", "legacy-kl", "legacy-notes"):
            self.assertNotIn(s, vault._EXTRACT_SOURCES)


class TestWikilinkParsing(unittest.TestCase):
    def test_basic(self):
        got = vault._wikilink_targets("see [[Dennis Rowe]] and [[ODAE]]")
        self.assertEqual([k for _, k in got], ["Dennis Rowe", "ODAE"])

    def test_alias_and_heading_stripped(self):
        got = vault._wikilink_targets("[[Dennis Rowe|Dennis]] and [[ODAE#budget]]")
        self.assertEqual([k for _, k in got], ["Dennis Rowe", "ODAE"])

    def test_dedup_case_insensitive(self):
        got = vault._wikilink_targets("[[Dennis]] [[dennis]] [[DENNIS]]")
        self.assertEqual(len(got), 1)


class TestRealMeetingFilter(unittest.TestCase):
    def _ev(self, start, end, self_decl=False):
        atts = [{"self": True, "response": "declined" if self_decl else "accepted"}]
        return {"start": start, "end": end, "attendees": atts, "summary": "m"}

    def test_normal_meeting_included(self):
        self.assertTrue(self._ev("2026-07-20T10:00:00-05:00", "2026-07-20T10:30:00-05:00")
                        and vault._real_meeting(self._ev("2026-07-20T10:00:00-05:00", "2026-07-20T10:30:00-05:00")))

    def test_all_day_excluded(self):
        self.assertFalse(vault._real_meeting(self._ev("2026-07-20", "2026-07-21")))

    def test_short_excluded(self):
        self.assertFalse(vault._real_meeting(self._ev("2026-07-20T10:00:00-05:00", "2026-07-20T10:10:00-05:00")))

    def test_declined_excluded(self):
        self.assertFalse(vault._real_meeting(
            self._ev("2026-07-20T10:00:00-05:00", "2026-07-20T11:00:00-05:00", self_decl=True)))


class TestParseTs(unittest.TestCase):
    def test_datetime(self):
        self.assertEqual(vault._parse_ts("2025-12-31T18:18:50"), "2025-12-31T18:18:50")

    def test_date_only(self):
        self.assertEqual(vault._parse_ts("2026-07-18"), "2026-07-18")

    def test_malformed_is_none(self):
        self.assertIsNone(vault._parse_ts("not-a-date"))
        self.assertIsNone(vault._parse_ts(""))


class TestDigestRender(unittest.TestCase):
    def _p(self, i, etype, text):
        return {"id": i, "capture_id": f"c{i}", "extraction_type": etype,
                "payload": {"text": text, "evidence": "ev"}, "context": None,
                "created_at": datetime(2026, 7, 18), "path": f"authored/x/{i}.md", "source": "meeting"}

    def test_ordering_and_numbering(self):
        rows = [self._p(1, "question", "q"), self._p(2, "decision_candidate", "d"),
                self._p(3, "commitment", "c")]
        with patch.object(vault, "_pending_proposals", return_value=sorted(
                rows, key=lambda r: (vault._TYPE_RANK[r["extraction_type"]], r["id"]))):
            reply, mapping = vault.render_digest(today_only=True, header="Today's digest")
        # decision first (rank 0), then commitment (1), question (5)
        self.assertEqual(mapping[1]["extraction_type"], "decision_candidate")
        self.assertEqual(mapping[2]["extraction_type"], "commitment")
        self.assertEqual(mapping[3]["extraction_type"], "question")
        self.assertIn("approve", reply.lower())

    def test_cap_and_overflow(self):
        rows = [self._p(i, "commitment", f"c{i}") for i in range(1, 15)]
        with patch.object(vault, "_pending_proposals", return_value=rows):
            reply, mapping = vault.render_digest(today_only=False, header="Pending proposals")
        self.assertEqual(len(mapping), vault._DIGEST_CAP)   # capped at 10
        self.assertIn("more not shown", reply)              # overflow stated

    def test_empty(self):
        with patch.object(vault, "_pending_proposals", return_value=[]):
            reply, mapping = vault.render_digest(today_only=True, header="Today's digest")
        self.assertEqual(mapping, {})
        self.assertIn("No pending proposals", reply)


class TestPatExpiryParse(unittest.TestCase):
    def test_github_header_form(self):
        self.assertEqual(vault._parse_pat_expiry("2026-08-01 23:59:59 UTC"),
                         date(2026, 8, 1))

    def test_iso_form(self):
        self.assertEqual(vault._parse_pat_expiry("2026-08-01T23:59:59+00:00"),
                         date(2026, 8, 1))

    def test_unknown_is_none(self):
        self.assertIsNone(vault._parse_pat_expiry("unknown"))

    def test_missing_is_none(self):
        self.assertIsNone(vault._parse_pat_expiry(None))
        self.assertIsNone(vault._parse_pat_expiry(""))

    def test_garbage_is_none(self):
        self.assertIsNone(vault._parse_pat_expiry("not a date"))


class TestPatExpiryWarning(unittest.TestCase):
    def _warn_with_expiry(self, expiry_value):
        def _state_get(key, default=None):
            return expiry_value if key == "pat_expiry" else default
        with patch.object(vault, "_state_get", side_effect=_state_get):
            return vault._pat_expiry_warning()

    def test_warns_within_14_days(self):
        soon = (vault._ct_today() + timedelta(days=10)).isoformat() + " 23:59:59 UTC"
        warn = self._warn_with_expiry(soon)
        self.assertIsNotNone(warn)
        self.assertIn("10 day", warn)
        # reuses the vault-pat-auth runbook remediation block
        self.assertIn("aws secretsmanager put-secret-value", warn)

    def test_silent_beyond_14_days(self):
        far = (vault._ct_today() + timedelta(days=30)).isoformat() + " 23:59:59 UTC"
        self.assertIsNone(self._warn_with_expiry(far))

    def test_expired_warns(self):
        past = (vault._ct_today() - timedelta(days=2)).isoformat() + " 23:59:59 UTC"
        warn = self._warn_with_expiry(past)
        self.assertIsNotNone(warn)
        self.assertIn("expired", warn.lower())

    def test_unknown_is_silent(self):
        self.assertIsNone(self._warn_with_expiry("unknown"))
        self.assertIsNone(self._warn_with_expiry(None))


class TestExtractionPromptTuning(unittest.TestCase):
    """The OPS-1 PB-011 follow-up added explicit dedupe / commitment-direction /
    decision-ownership guidance to the extraction system prompt."""

    def test_dedupe_instruction_present(self):
        from artemis.prompts import VAULT_EXTRACT_SYSTEM
        self.assertIn("DEDUPLICATE", VAULT_EXTRACT_SYSTEM)

    def test_commitment_direction_instruction_present(self):
        from artemis.prompts import VAULT_EXTRACT_SYSTEM
        self.assertIn("COMMITMENT DIRECTION", VAULT_EXTRACT_SYSTEM)
        self.assertIn("waiting-on", VAULT_EXTRACT_SYSTEM)
        self.assertIn("NOT a commitment", VAULT_EXTRACT_SYSTEM)

    def test_decision_ownership_instruction_present(self):
        from artemis.prompts import VAULT_EXTRACT_SYSTEM
        self.assertIn("DECISIONS are choices MADE BY Ryan", VAULT_EXTRACT_SYSTEM)


# ============================================================================
# LIVE Postgres integration (skipped unless a local PG is reachable)
# ============================================================================

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

from artemis import commitments, dossier  # noqa: E402
from artemis import briefs  # noqa: E402

_TEST_DB = "artemis_vault_test"
_LIVE = False
_CONN = None

_MIGS = [(_REPO_ROOT / "migrations" / m).read_text() for m in (
    "001_create_acos_schema.sql", "006_create_action_items.sql", "020_commitments.sql",
    "024_dossier.sql", "025_dossier_approve.sql", "026_org_assignment.sql",
    "027_org_profile.sql", "028_vault.sql",
)]


def _admin_connect():
    return psycopg2.connect(dbname="postgres", connect_timeout=2)


def _q(sql, params=None):
    with _CONN.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()] if cur.description else []


def _one(sql, params=None):
    rows = _q(sql, params)
    return dict(rows[0]) if rows else None


def _w(sql, params=None):
    with _CONN.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        if cur.description:
            r = cur.fetchone()
            return dict(r) if r else None
        return None


def setUpModule():
    global _LIVE, _CONN
    try:
        admin = _admin_connect()
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {_TEST_DB}")
            cur.execute(f"CREATE DATABASE {_TEST_DB}")
        admin.close()
        _CONN = psycopg2.connect(dbname=_TEST_DB)
        _CONN.autocommit = True
        with _CONN.cursor() as cur:
            for mig in _MIGS:
                cur.execute(mig)
        _LIVE = True
    except Exception as e:
        sys.stderr.write(f"[test_vault] live PG unavailable, skipping: {e}\n")
        _LIVE = False


def tearDownModule():
    global _CONN
    if _CONN:
        _CONN.close()
        _CONN = None
    if not _LIVE:
        return
    try:
        admin = _admin_connect()
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {_TEST_DB}")
        admin.close()
    except Exception:
        pass


# Deterministic fake LLM: returns a fixed extraction for any note carrying the
# marker text; empty extraction otherwise. Patched onto briefs._call_claude (which
# vault._extract_one imports).
def _fake_llm(system, user, model=None, max_tokens=None):
    if "EXTRACT AN ACTION" in user:
        return json.dumps({
            "cleaned_text": "cleaned version",
            "summary": "a summary",
            "context": None,
            "action_items": [{"text": "Follow up with Dennis", "due_date": None, "evidence": "follow up with Dennis"}],
            "commitments": [], "dossier_entries": [], "org_facts": [],
            "decision_candidates": [{"text": "Go with option A", "evidence": "we'll go with option A"}],
            "questions": [],
        })
    return json.dumps({"cleaned_text": "clean", "summary": None, "context": None,
                       "action_items": [], "commitments": [], "dossier_entries": [],
                       "org_facts": [], "decision_candidates": [], "questions": []})


class LiveBase(unittest.TestCase):
    def setUp(self):
        if not _LIVE:
            self.skipTest("no local Postgres")
        self._orig = {
            "v_q": vault.execute_query, "v_one": vault.execute_one, "v_w": vault.execute_write,
            "v_audit": vault.log_audit,
            "d_q": dossier.execute_query, "d_one": dossier.execute_one, "d_w": dossier.execute_write,
            "d_audit": dossier.log_audit,
            "c_q": commitments.execute_query, "c_one": commitments.execute_one, "c_w": commitments.execute_write,
            "llm": briefs._call_claude,
        }
        vault.execute_query, vault.execute_one, vault.execute_write = _q, _one, _w
        vault.log_audit = lambda *a, **k: ""
        dossier.execute_query, dossier.execute_one, dossier.execute_write = _q, _one, _w
        dossier.log_audit = lambda *a, **k: ""
        commitments.execute_query, commitments.execute_one, commitments.execute_write = _q, _one, _w
        briefs._call_claude = _fake_llm
        for t in ("extraction_proposal", "note_links", "note_metadata", "ingest_state", "notes"):
            with _CONN.cursor() as cur:
                cur.execute(f"TRUNCATE vault.{t} RESTART IDENTITY CASCADE")
        for t in ("commitments", "dossier_entry", "org_note", "org_profile", "dossier"):
            with _CONN.cursor() as cur:
                cur.execute(f"TRUNCATE acos.{t} RESTART IDENTITY CASCADE")

    def tearDown(self):
        vault.execute_query = self._orig["v_q"]
        vault.execute_one = self._orig["v_one"]
        vault.execute_write = self._orig["v_w"]
        vault.log_audit = self._orig["v_audit"]
        dossier.execute_query = self._orig["d_q"]
        dossier.execute_one = self._orig["d_one"]
        dossier.execute_write = self._orig["d_w"]
        dossier.log_audit = self._orig["d_audit"]
        commitments.execute_query = self._orig["c_q"]
        commitments.execute_one = self._orig["c_one"]
        commitments.execute_write = self._orig["c_w"]
        briefs._call_claude = self._orig["llm"]

    # Build a temp mirror with the given {relpath: text} files and run sync_vault
    # with the git fetch stubbed to return `sha`.
    def _sync_with(self, files: dict, sha: str) -> dict:
        d = tempfile.mkdtemp(prefix="vault-mirror-")
        for rel, text in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
        with patch.object(vault, "_mirror_dir", return_value=d), \
             patch.object(vault, "_sync_mirror", return_value=sha):
            return vault.sync_vault()

    @staticmethod
    def _note(capture_id, source, body, tags=None):
        fm = f"---\ncreated: 2026-07-18T09:00:00\ncapture_id: {capture_id}\nstatus: bronze\nsource: {source}\n"
        if tags:
            fm += f"tags: [{', '.join(tags)}]\n"
        fm += "---\n"
        return fm + body


class TestLiveSchema(LiveBase):
    def test_five_vault_tables(self):
        rows = _q("SELECT table_name FROM information_schema.tables WHERE table_schema='vault'")
        names = {r["table_name"] for r in rows}
        for t in ("notes", "note_links", "note_metadata", "extraction_proposal", "ingest_state"):
            self.assertIn(t, names)

    def test_context_columns_added(self):
        for tbl in ("commitments", "action_items"):
            row = _one(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema='acos' AND table_name=%s AND column_name='context'", (tbl,))
            self.assertIsNotNone(row, tbl)


class TestIngest(LiveBase):
    def test_ingest_and_idempotent_rerun(self):
        files = {"authored/legacy/a.md": self._note("cap-a", "legacy-notes", "body a"),
                 "authored/meetings/b.md": self._note("cap-b", "meeting", "EXTRACT AN ACTION")}
        s1 = self._sync_with(files, "sha1")
        self.assertEqual(s1["new"], 2)
        self.assertEqual(_one("SELECT count(*) c FROM vault.notes")["c"], 2)
        # legacy excluded from extraction; meeting extracted → proposals exist
        self.assertGreaterEqual(s1["proposals"], 1)
        n_props = _one("SELECT count(*) c FROM vault.extraction_proposal")["c"]

        # Same notes, new sha → no dup rows, no dup proposals (idempotent).
        s2 = self._sync_with(files, "sha2")
        self.assertEqual(s2["new"], 0)
        self.assertEqual(_one("SELECT count(*) c FROM vault.notes")["c"], 2)
        self.assertEqual(_one("SELECT count(*) c FROM vault.extraction_proposal")["c"], n_props)

    def test_same_sha_is_noop(self):
        files = {"authored/x/a.md": self._note("cap-a", "thought", "hi")}
        self._sync_with(files, "shaX")
        s = self._sync_with(files, "shaX")   # unchanged sha
        self.assertTrue(s["no_change"])
        self.assertEqual(s["new"], 0)

    def test_legacy_and_journal_never_extracted(self):
        files = {
            "authored/legacy/a.md": self._note("cap-a", "legacy-kl", "EXTRACT AN ACTION"),
            "authored/journal/2026-07-18.md": self._note("cap-j", "journal", "EXTRACT AN ACTION"),
        }
        self._sync_with(files, "sha1")
        self.assertEqual(_one("SELECT count(*) c FROM vault.extraction_proposal")["c"], 0)
        # ingested but not extracted → cleaned_text stays NULL
        self.assertIsNone(_one("SELECT cleaned_text FROM vault.notes WHERE capture_id='cap-a'")["cleaned_text"])

    def test_deletion_and_reappearance(self):
        f_full = {"authored/x/a.md": self._note("cap-a", "thought", "a"),
                  "authored/x/b.md": self._note("cap-b", "thought", "b")}
        self._sync_with(f_full, "s1")
        # b removed → deleted_at set, row retained
        self._sync_with({"authored/x/a.md": self._note("cap-a", "thought", "a")}, "s2")
        self.assertIsNotNone(_one("SELECT deleted_at FROM vault.notes WHERE capture_id='cap-b'")["deleted_at"])
        self.assertEqual(_one("SELECT count(*) c FROM vault.notes")["c"], 2)  # retained
        # b returns → deleted_at cleared
        self._sync_with(f_full, "s3")
        self.assertIsNone(_one("SELECT deleted_at FROM vault.notes WHERE capture_id='cap-b'")["deleted_at"])

    def test_wikilinks_resolve_and_dangle(self):
        files = {
            "authored/x/Dennis Rowe.md": self._note("cap-d", "thought", "profile"),
            "authored/x/note.md": self._note("cap-n", "thought", "see [[Dennis Rowe]] and [[Ghost Person]]"),
        }
        self._sync_with(files, "s1")
        links = _q("SELECT target_raw, target_capture_id FROM vault.note_links "
                   "WHERE source_capture_id='cap-n' ORDER BY target_raw")
        by_raw = {l["target_raw"]: l["target_capture_id"] for l in links}
        self.assertEqual(by_raw["Dennis Rowe"], "cap-d")   # resolved
        self.assertIsNone(by_raw["Ghost Person"])          # dangling


class TestProposals(LiveBase):
    def _seed_meeting(self, sha="s1"):
        files = {"authored/meetings/b.md": self._note("cap-b", "meeting", "EXTRACT AN ACTION")}
        return self._sync_with(files, sha)

    def test_expiry_at_7_days(self):
        self._seed_meeting()
        # Age one proposal past the 7-day window.
        _w("UPDATE vault.extraction_proposal SET created_at = now() - interval '8 days' "
           "WHERE id = (SELECT min(id) FROM vault.extraction_proposal)")
        n = vault.expire_stale_proposals()
        self.assertGreaterEqual(n, 1)
        self.assertEqual(
            _one("SELECT status FROM vault.extraction_proposal WHERE created_at < now() - interval '7 days'")["status"],
            "expired")

    def test_approve_writes_commitment_via_creation_path(self):
        self._seed_meeting()
        p = _one("SELECT * FROM vault.extraction_proposal WHERE extraction_type='action_item'")
        ok, target = vault._approve_proposal(p)
        self.assertTrue(ok)
        self.assertTrue(target.startswith("commitment:"))
        # The row flipped to approved, and a real commitment exists (written through
        # commitments.add_commitment — no fabrication).
        self.assertEqual(_one("SELECT status FROM vault.extraction_proposal WHERE id=%s", (p["id"],))["status"],
                         "approved")
        cid = int(target.split(":")[1])
        row = _one("SELECT status, context FROM acos.commitments WHERE id=%s", (cid,))
        self.assertEqual(row["status"], "active")

    def test_reject_marks_only(self):
        self._seed_meeting()
        p = _one("SELECT * FROM vault.extraction_proposal LIMIT 1")
        before = _one("SELECT count(*) c FROM acos.commitments")["c"]
        self.assertTrue(vault._reject_proposal(p))
        self.assertEqual(_one("SELECT status FROM vault.extraction_proposal WHERE id=%s", (p["id"],))["status"],
                         "rejected")
        self.assertEqual(_one("SELECT count(*) c FROM acos.commitments")["c"], before)  # nothing written

    def test_status_renders_from_rows(self):
        self._seed_meeting()
        reply = vault.cmd_status()
        # counts come from the DB, not a claim
        self.assertIn("meeting: 1", reply)
        self.assertIn("Proposals:", reply)


class TestCoverageAndDigestWindows(LiveBase):
    def test_coverage_counts_today_captures(self):
        self._sync_with({"authored/meetings/b.md": self._note("cap-b", "meeting", "notes")}, "s1")

        class _Cal:
            def get_events_in_range(self, a, b):
                # two real meetings today vs one capture → nudge
                base = f"{date.today().isoformat()}T"
                return [
                    {"start": base + "10:00:00-05:00", "end": base + "11:00:00-05:00",
                     "attendees": [], "summary": "Meeting One"},
                    {"start": base + "13:00:00-05:00", "end": base + "14:00:00-05:00",
                     "attendees": [], "summary": "Meeting Two"},
                ]

        posts = []

        class _MM:
            def post_message(self, ch, msg):
                posts.append(msg)

        msg = vault.run_coverage_monitor(_Cal(), _MM())
        self.assertIsNotNone(msg)
        self.assertIn("Meeting One", msg)
        self.assertEqual(len(posts), 1)
        # Second call same day → no repeat post (one nudge/day).
        vault.run_coverage_monitor(_Cal(), _MM())
        self.assertEqual(len(posts), 1)

    def test_digest_today_only_window(self):
        self._sync_with({"authored/meetings/b.md": self._note("cap-b", "meeting", "EXTRACT AN ACTION")}, "s1")
        # Push the proposal's created_at to yesterday → excluded from today's digest.
        _w("UPDATE vault.extraction_proposal SET created_at = now() - interval '2 days'")
        _, mapping_today = vault.render_digest(today_only=True, header="Today's digest")
        _, mapping_all = vault.render_digest(today_only=False, header="Pending proposals")
        self.assertEqual(mapping_today, {})              # nothing dated today
        self.assertGreaterEqual(len(mapping_all), 1)     # still pending overall


if __name__ == "__main__":
    unittest.main(verbosity=2)
