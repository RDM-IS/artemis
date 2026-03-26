"""Google Drive integration — upload billing attachments.

Reuses the existing OAuth credentials from gmail.py / calendar.py.
Requires the drive.file scope; check_billing_scopes() validates this at startup.
"""

import logging
import os

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

from artemis import config

logger = logging.getLogger(__name__)

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"

# Folder path for expenses — read from env, default to RDMIS/Expenses/2026
_DEFAULT_FOLDER = "RDMIS/Expenses/2026"


def get_drive_service():
    """Build Drive API client using existing OAuth credentials.

    Returns the service object or None if credentials are missing/invalid.
    """
    token_path = config.GMAIL_TOKEN_PATH
    if not token_path.exists():
        logger.error("No OAuth token at %s — cannot init Drive service", token_path)
        return None

    try:
        creds = Credentials.from_authorized_user_file(str(token_path))
        return build("drive", "v3", credentials=creds)
    except Exception:
        logger.exception("Failed to build Drive service")
        return None


def ensure_folder(path: str, service=None) -> str | None:
    """Find or create a nested folder path like 'RDMIS/Expenses/2026'.

    Creates each level if missing. Returns the final folder ID or None on failure.
    """
    svc = service or get_drive_service()
    if not svc:
        return None

    parent_id = "root"
    for folder_name in path.split("/"):
        folder_name = folder_name.strip()
        if not folder_name:
            continue

        # Search for existing folder
        query = (
            f"name = '{folder_name}' and "
            f"mimeType = 'application/vnd.google-apps.folder' and "
            f"'{parent_id}' in parents and "
            f"trashed = false"
        )
        try:
            results = svc.files().list(q=query, fields="files(id, name)").execute()
            files = results.get("files", [])
        except Exception:
            logger.exception("Drive folder search failed for '%s'", folder_name)
            return None

        if files:
            parent_id = files[0]["id"]
        else:
            # Create folder
            try:
                meta = {
                    "name": folder_name,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [parent_id],
                }
                folder = svc.files().create(body=meta, fields="id").execute()
                parent_id = folder["id"]
                logger.info("Created Drive folder: %s (id=%s)", folder_name, parent_id)
            except Exception:
                logger.exception("Failed to create Drive folder '%s'", folder_name)
                return None

    return parent_id


def upload_attachment(
    filename: str,
    content: bytes,
    mime_type: str,
    folder_id: str,
    service=None,
) -> tuple[str, str] | tuple[None, None]:
    """Upload a file to Drive and set sharing to 'anyone with link can view'.

    Returns (file_id, shareable_link) or (None, None) on failure.
    """
    svc = service or get_drive_service()
    if not svc:
        return None, None

    try:
        meta = {"name": filename, "parents": [folder_id]}
        media = MediaInMemoryUpload(content, mimetype=mime_type)
        uploaded = svc.files().create(
            body=meta, media_body=media, fields="id, webViewLink"
        ).execute()

        file_id = uploaded["id"]

        # Set sharing: anyone with link can view
        svc.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
        ).execute()

        link = uploaded.get("webViewLink", f"https://drive.google.com/file/d/{file_id}/view")
        logger.info("Uploaded to Drive: %s (id=%s)", filename, file_id)
        return file_id, link
    except Exception:
        logger.exception("Drive upload failed for %s", filename)
        return None, None


def get_or_create_expense_folder(service=None) -> str | None:
    """Convenience wrapper — finds or creates the expense folder from config."""
    folder_path = os.environ.get("GOOGLE_DRIVE_EXPENSE_FOLDER", _DEFAULT_FOLDER)
    return ensure_folder(folder_path, service=service)
