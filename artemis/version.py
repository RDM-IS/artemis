"""Version tracking for Artemis.

OPS-1 version truth: the running commit is resolved ONCE at import from the repo
git HEAD (WorkingDirectory is the repo), so `VERSION` is deploy-accurate — it can
never drift into an LLM guess. Base version + short SHA: `1.4.0+<sha7>`, or
`1.4.0+unknown` when git is unavailable.
"""

import logging
import os
import subprocess

import requests

logger = logging.getLogger(__name__)

BASE_VERSION = "1.4.0"
BUILD_DATE = "2026-07-18"

# The repo root — this file is artemis/version.py, so two dirs up. Deliberately not
# a hardcoded path (the old /mnt/d/Artemis constant was stale): git runs where the
# code lives, matching the service's WorkingDirectory.
_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_REPO_API = "https://api.github.com/repos/RDM-IS/artemis/commits/main"


def _git(args: list[str]) -> str:
    """Run a read-only git command in the repo with a tight timeout; '' on failure."""
    return subprocess.check_output(
        ["git", *args], cwd=_REPO_DIR, stderr=subprocess.DEVNULL, timeout=5,
    ).decode().strip()


def _resolve_sha() -> str:
    """Short HEAD sha (7 chars), or 'unknown' when git can't be reached."""
    try:
        return _git(["rev-parse", "--short=7", "HEAD"])[:7] or "unknown"
    except Exception:
        return "unknown"


def get_commit_subject() -> str:
    """Subject line of the running commit (git log -1 --format=%s), or ''."""
    try:
        return _git(["log", "-1", "--format=%s"])
    except Exception:
        return ""


# Resolved once at import and cached as module constants.
_SHA = _resolve_sha()
VERSION = f"{BASE_VERSION}+{_SHA}"
COMMIT_HASH = None if _SHA == "unknown" else _SHA


def get_version() -> str:
    """Deploy-accurate version string, e.g. `1.4.0+abc1234`."""
    return VERSION


def get_commit_hash() -> str:
    """Short commit hash, or '' when git was unavailable at startup."""
    return "" if _SHA == "unknown" else _SHA


def get_latest_github_version() -> tuple[str | None, str | None]:
    """Check latest commit on main branch from GitHub.

    Returns (short_hash, date_string) or (None, None) on failure.
    """
    try:
        r = requests.get(_REPO_API, timeout=5)
        if r.status_code == 200:
            data = r.json()
            latest_commit = data["sha"][:7]
            commit_date = data["commit"]["committer"]["date"][:10]
            return latest_commit, commit_date
        return None, None
    except Exception:
        return None, None


def format_version_status() -> str:
    """Format a full version status message for @mention responses (compares the
    running commit against origin/main on GitHub)."""
    current = get_version()
    local_hash = get_commit_hash()
    latest_hash, latest_date = get_latest_github_version()

    if not latest_hash:
        return f"Running {current} — could not reach GitHub to check for updates."

    if local_hash and latest_hash.startswith(local_hash):
        return f"Running {current} — up to date."

    if local_hash:
        return (
            f"Running {current} — latest on main is {latest_hash} "
            f"({latest_date}). Run `git pull` to update."
        )

    return f"Running {current} — unable to determine local commit."
