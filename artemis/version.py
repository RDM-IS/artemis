"""Version tracking for Artemis."""

import logging
import subprocess

import requests

logger = logging.getLogger(__name__)

VERSION = "1.1.0"
BUILD_DATE = "2026-03-17"
COMMIT_HASH = None  # populated at runtime from git

_REPO_API = "https://api.github.com/repos/RDM-IS/artemis/commits/main"
_ARTEMIS_DIR = "/mnt/d/Artemis"


def get_version() -> str:
    """Get current version with git commit hash if available."""
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_ARTEMIS_DIR,
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return f"{VERSION} ({commit})"
    except Exception:
        return VERSION


def get_commit_hash() -> str:
    """Get the current short commit hash, or empty string."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_ARTEMIS_DIR,
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return ""


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
    """Format a full version status message for @mention responses."""
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
