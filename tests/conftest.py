"""pytest: every collected test runs with the TEST-DB-GUARD flag set.

knowledge.dbguard also refuses under pytest on its own; this makes the
flag explicit for anything a test spawns (subprocesses inherit the env).
"""
import os

os.environ["ARTEMIS_TEST_NO_DB"] = "1"
