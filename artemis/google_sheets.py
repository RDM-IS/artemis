"""Google Sheets integration — append expense rows for billing intake.

Reuses the existing OAuth credentials. Requires the spreadsheets scope;
check_billing_scopes() validates this at startup.
"""

import logging
import os
from datetime import date

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from artemis import config

logger = logging.getLogger(__name__)

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


def get_sheets_service():
    """Build Sheets API client using existing OAuth credentials.

    Returns the service object or None if credentials are missing/invalid.
    """
    token_path = config.GMAIL_TOKEN_PATH
    if not token_path.exists():
        logger.error("No OAuth token at %s — cannot init Sheets service", token_path)
        return None

    try:
        creds = Credentials.from_authorized_user_file(str(token_path))
        return build("sheets", "v4", credentials=creds)
    except Exception:
        logger.exception("Failed to build Sheets service")
        return None


def append_expense_row(row: dict, service=None) -> bool:
    """Append one row to the expense tracking sheet.

    Row schema (dict keys):
        date, vendor, description, category, amount, payment_method,
        founder_loan, reimbursed, reimbursed_date, document_link, notes

    Returns True on success, False on failure.
    """
    sheet_id = os.environ.get("GOOGLE_SHEETS_EXPENSE_ID", "")
    if not sheet_id:
        logger.error("GOOGLE_SHEETS_EXPENSE_ID not set — cannot append expense row")
        return False

    svc = service or get_sheets_service()
    if not svc:
        return False

    values = [[
        row.get("date", date.today().strftime("%m/%d/%Y")),
        row.get("vendor", ""),
        row.get("description", ""),
        row.get("category", "Misc"),
        row.get("amount", ""),
        row.get("payment_method", ""),
        row.get("founder_loan", "Yes"),
        row.get("reimbursed", "No"),
        row.get("reimbursed_date", ""),
        row.get("document_link", ""),
        row.get("notes", ""),
    ]]

    try:
        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range="Sheet1!A:K",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        ).execute()
        logger.info("Appended expense row: %s — %s", row.get("vendor"), row.get("amount"))
        return True
    except Exception:
        logger.exception("Failed to append expense row to sheet %s", sheet_id)
        return False


def get_sheet_url() -> str:
    """Return a direct link to the expense tracking sheet."""
    sheet_id = os.environ.get("GOOGLE_SHEETS_EXPENSE_ID", "")
    if sheet_id:
        return f"https://docs.google.com/spreadsheets/d/{sheet_id}"
    return ""
