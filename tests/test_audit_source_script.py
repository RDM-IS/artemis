"""Every audit_log write under scripts/ marks itself source='script'.

COGNITION-1 `manual_gap` rule 2 keys on that marker. The rule it replaced --
"a script writing an audit row for something no scheduled job does" -- was not
computable: nothing joins an action string to an inventory of what the jobs do.
The marker is computable, and it is only as good as its coverage, so this test
is the coverage. A new script that writes an audit row without it makes the gap
list silently incomplete, which is the failure mode worth a test rather than a
comment.

    python3.11 -m unittest tests.test_audit_source_script
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

#: Find each INSERT into acos.audit_log and the statement text that follows it,
#: up to the end of the SQL string. Deliberately crude: it looks at source text,
#: because the point is to catch a write nobody remembered to mark.
_INSERT = re.compile(r"INSERT\s+INTO\s+acos\.audit_log(.{0,600}?)(?:\"\"\"|'''|\)\))",
                     re.S | re.I)


def _writes():
    for path in sorted(SCRIPTS.glob("*.py")):
        text = path.read_text()
        for m in _INSERT.finditer(text):
            yield path.name, m.group(1)


class TestScriptAuditSource(unittest.TestCase):
    def test_there_are_some_to_check(self):
        """A regex that silently matches nothing would pass every assertion."""
        self.assertGreaterEqual(len(list(_writes())), 5)

    def test_every_script_audit_write_marks_itself(self):
        for name, stmt in _writes():
            with self.subTest(script=name):
                self.assertIn("source", stmt,
                              f"{name}: an audit_log write with no source column")
                self.assertIn("'script'", stmt,
                              f"{name}: source is not set to 'script'")

    def test_the_column_and_the_value_line_up(self):
        """A column list naming `source` with no matching value shifts every
        later value one place left — silently, because they are all text."""
        for name, stmt in _writes():
            with self.subTest(script=name):
                cols = _items(_group(stmt))
                vals = _items(_group(stmt[stmt.upper().index("VALUES"):]))
                self.assertEqual(len(cols), len(vals),
                                 f"{name}: {len(cols)} columns, {len(vals)} values "
                                 f"({cols} vs {vals})")
                self.assertEqual(cols.index("source"), vals.index("'script'"),
                                 f"{name}: source is not in the position 'script' fills")


def _group(text: str) -> str:
    """The first parenthesised group, honouring nesting."""
    start = text.index("(")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
    raise AssertionError(f"unbalanced parentheses in {text[:80]!r}")


def _items(group: str) -> list[str]:
    """Top-level comma-separated items — `CAST(x AS jsonb)` stays one item."""
    out, depth, cur = [], 0, ""
    for ch in group:
        if ch == "," and depth == 0:
            out.append(" ".join(cur.split()))
            cur = ""
            continue
        depth += (ch == "(") - (ch == ")")
        cur += ch
    if cur.strip():
        out.append(" ".join(cur.split()))
    return out


if __name__ == "__main__":
    unittest.main()
