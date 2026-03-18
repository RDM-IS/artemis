"""OAuth setup helper — run interactively to generate/refresh tokens.

Usage:
    python setup_oauth.py          # Authenticate both Gmail and Calendar
    python setup_oauth.py gmail    # Gmail only
    python setup_oauth.py calendar # Calendar only

Tokens are saved to the paths configured in .env (GMAIL_TOKEN_PATH,
CALENDAR_TOKEN_PATH).  Delete the existing token file first if you need
to re-authenticate with updated scopes.
"""

import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

# ── Scopes ──────────────────────────────────────────────────────────
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

# ── Defaults (overridden by .env if dotenv is installed) ────────────
GMAIL_CREDENTIALS_PATH = Path("credentials.json")
GMAIL_TOKEN_PATH = Path("token.json")
CALENDAR_CREDENTIALS_PATH = Path("credentials.json")
CALENDAR_TOKEN_PATH = Path("calendar_token.json")

# Try to load .env so paths match the running application
try:
    import os

    from dotenv import load_dotenv

    load_dotenv()
    GMAIL_CREDENTIALS_PATH = Path(os.environ.get("GMAIL_CREDENTIALS_PATH", GMAIL_CREDENTIALS_PATH))
    GMAIL_TOKEN_PATH = Path(os.environ.get("GMAIL_TOKEN_PATH", GMAIL_TOKEN_PATH))
    CALENDAR_CREDENTIALS_PATH = Path(os.environ.get("CALENDAR_CREDENTIALS_PATH", CALENDAR_CREDENTIALS_PATH))
    CALENDAR_TOKEN_PATH = Path(os.environ.get("CALENDAR_TOKEN_PATH", CALENDAR_TOKEN_PATH))
except ImportError:
    pass


def _run_flow(name: str, creds_path: Path, token_path: Path, scopes: list[str]) -> None:
    if not creds_path.exists():
        print(f"ERROR: {creds_path} not found — download it from Google Cloud Console.")
        return

    if token_path.exists():
        print(f"  {token_path} already exists. Delete it first to re-authenticate.")
        resp = input(f"  Delete {token_path} and re-authenticate? [y/N] ").strip().lower()
        if resp != "y":
            print(f"  Skipping {name}.")
            return
        token_path.unlink()

    print(f"  Authenticating {name} with scopes:")
    for s in scopes:
        print(f"    - {s}")

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), scopes)
    creds = flow.run_local_server(port=0)
    token_path.write_text(creds.to_json())
    print(f"  Token saved to {token_path}\n")


def main() -> None:
    target = sys.argv[1].lower() if len(sys.argv) > 1 else "all"

    if target in ("all", "gmail"):
        print("── Gmail ──")
        _run_flow("Gmail", GMAIL_CREDENTIALS_PATH, GMAIL_TOKEN_PATH, GMAIL_SCOPES)

    if target in ("all", "calendar"):
        print("── Calendar ──")
        _run_flow("Calendar", CALENDAR_CREDENTIALS_PATH, CALENDAR_TOKEN_PATH, CALENDAR_SCOPES)

    if target not in ("all", "gmail", "calendar"):
        print(f"Unknown target: {target}")
        print("Usage: python setup_oauth.py [gmail|calendar]")
        sys.exit(1)

    print("Done. Restart Artemis to pick up the new tokens.")


if __name__ == "__main__":
    main()
