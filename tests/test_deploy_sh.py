"""api/deploy.sh runs from any directory: it cd's into api/ before anything that
uses $PWD (CLAUDE.md documents `bash api/deploy.sh` from the repo root)."""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD
import unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "api" / "deploy.sh").read_text()


class DeployCwdTest(unittest.TestCase):
    def test_it_cds_into_api_before_any_cwd_relative_step(self):
        cd = SRC.index('cd "$REPO/api"')
        for step in ("docker run", "rm -rf package", "zip -r", "pip install -r requirements.txt"):
            self.assertLess(cd, SRC.index(step), step)

    def test_the_preflight_prefers_the_venv(self):
        self.assertIn('PY="$REPO/venv/bin/python"', SRC)

    def test_claude_md_documents_the_root_relative_command(self):
        rules = (Path(__file__).resolve().parent.parent / "CLAUDE.md").read_text()
        self.assertIn("`PATH=$PWD/venv/bin:$PATH bash api/deploy.sh`", rules)


if __name__ == "__main__":
    unittest.main()
