"""PACKAGE-IDENTITY — shipped code may not import what the package does not ship.

The Lambda package contains api/, knowledge/ and migrations/. It does NOT contain
artemis/, and `api/app/routers/health.py` says so in a comment — which is not a
check. On 2026-09-29 api/app/routers/prep.py imported `knowledge.config` for the
active timezone; config.py actually lives in artemis/, so every stay-dependent
route raised ImportError in production while the one route that needed no date
worked. Nothing caught it but hitting the deployed endpoints.

The rule this enforces: an `artemis` import inside api/ or knowledge/ must sit in
a try/except ImportError with a fallback, the way `dashboard.py` reads the version.
An unguarded one is a route that 500s the moment it is deployed.

It also checks the reverse direction of the same fact: a module the shipped code
imports from `knowledge` has to EXIST in knowledge/, since a plausible-looking
`knowledge.<thing>` that lives elsewhere is exactly how the original bug read.
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: What api/deploy.sh zips. One fact written twice is the PACKAGE-IDENTITY
#: hazard, so if this list and deploy.sh diverge, the divergence is the bug.
SHIPPED = ("api", "knowledge", "migrations")


#: The ONE exemption, named rather than pattern-matched.
#:
#: knowledge/test_google_clients.py is a diagnostic script that imports
#: artemis.gmail and artemis.calendar. Nothing in the Lambda imports it, so it
#: cannot raise there — but it IS shipped, because deploy.sh zips all of
#: knowledge/. That makes it two smaller problems worth stating rather than
#: hiding: a test-only file inside a runtime path, which MOVES THE PACKAGE HASH
#: when edited (the exact noise PACKAGE-IDENTITY exists to prevent — the reason
#: tests/ was removed from the hashed list on 2026-09-26), and dead weight in the
#: deployment. Moving it is a package-identity change with its own verification
#: (prove a test-only commit leaves the hash unchanged, and that a one-line change
#: in each shipped path still moves it), so it is reported, not smuggled into
#: PREP-1. Reported 2026-09-29.
#:
#: The exemption is a FILENAME, not a glob: a new file cannot inherit it.
EXEMPT = ("knowledge/test_google_clients.py",)


def _py_files(*dirs) -> list:
    out = []
    for d in dirs:
        for p in (ROOT / d).rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            out.append(p)
    return out


def _guarded_import_lines(tree: ast.AST) -> set:
    """Line numbers of every import inside a try/except ImportError."""
    safe = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        catches_import = any(
            h.type is not None and (
                (isinstance(h.type, ast.Name) and h.type.id in ("ImportError", "Exception"))
                or (isinstance(h.type, ast.Tuple) and any(
                    isinstance(e, ast.Name) and e.id in ("ImportError", "Exception")
                    for e in h.type.elts)))
            for h in node.handlers)
        if not catches_import:
            continue
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                safe.add(sub.lineno)
    return safe


class TestShippedCodeImports(unittest.TestCase):
    def test_no_unguarded_artemis_import_in_the_lambda_package(self):
        offenders = []
        for path in _py_files("api", "knowledge"):
            if str(path.relative_to(ROOT)) in EXEMPT:
                continue
            tree = ast.parse(path.read_text())
            safe = _guarded_import_lines(tree)
            for node in ast.walk(tree):
                mod = None
                if isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                elif isinstance(node, ast.Import):
                    mod = ",".join(a.name for a in node.names)
                if not mod:
                    continue
                if mod == "artemis" or mod.startswith("artemis."):
                    if node.lineno not in safe:
                        offenders.append(
                            f"{path.relative_to(ROOT)}:{node.lineno} imports {mod}")
        self.assertEqual(offenders, [], "\n".join(
            ["unguarded artemis import in shipped code — it will raise "
             "ImportError in the Lambda:"] + offenders))

    def test_every_knowledge_module_the_package_imports_actually_exists(self):
        missing = []
        for path in _py_files("api", "knowledge"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.ImportFrom) and (node.module or "") == "knowledge":
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("knowledge."):
                    names = [(node.module or "").split(".", 1)[1].split(".")[0]]
                for name in names:
                    if name == "*":
                        continue
                    mod = ROOT / "knowledge" / f"{name}.py"
                    pkg = ROOT / "knowledge" / name / "__init__.py"
                    attr_ok = (ROOT / "knowledge" / "__init__.py").read_text()
                    if not mod.exists() and not pkg.exists() and name not in attr_ok:
                        missing.append(
                            f"{path.relative_to(ROOT)}:{node.lineno} imports "
                            f"knowledge.{name}, which is not in knowledge/")
        self.assertEqual(missing, [], "\n".join(
            ["`knowledge.<x>` that does not exist in knowledge/ — this is how "
             "`knowledge.config` (actually artemis/config.py) reached production:"]
            + missing))

    def test_every_exemption_is_still_needed(self):
        """An exemption that no longer applies must be DELETED, not left standing.

        A stale exemption is a hole waiting for the next file with that name, and
        it quietly misrepresents the package as cleaner than it is."""
        for rel in EXEMPT:
            path = ROOT / rel
            self.assertTrue(path.exists(),
                            f"{rel} is gone — delete its exemption")
            tree = ast.parse(path.read_text())
            safe = _guarded_import_lines(tree)
            unguarded = [
                n.lineno for n in ast.walk(tree)
                if isinstance(n, (ast.Import, ast.ImportFrom))
                and (getattr(n, "module", "") or "").startswith("artemis")
                and n.lineno not in safe]
            self.assertTrue(unguarded,
                            f"{rel} no longer has an unguarded artemis import — "
                            f"delete its exemption")

    def test_the_shipped_path_list_still_matches_deploy_sh(self):
        """SHIPPED above and deploy.sh's zip lines are one fact written twice."""
        deploy = (ROOT / "api" / "deploy.sh").read_text()
        for name in SHIPPED:
            self.assertTrue(
                f"../{name}/" in deploy or f" {name}/" in deploy,
                f"deploy.sh no longer zips {name}/ — update SHIPPED, or the "
                f"guards above are checking the wrong package")
        self.assertNotIn("../artemis/", deploy,
                         "deploy.sh now ships artemis/ — if that is intended, "
                         "the artemis-import guard above is obsolete")


if __name__ == "__main__":
    unittest.main()
