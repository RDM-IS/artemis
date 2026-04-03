"""PB-001 v2: Demo Access Notification — process Lucint demo-gate emails.

Scans for emails from demo@rdm.is with subject "Lucint demo accessed",
extracts visitor info, writes CRM entities via crm_write_guard, creates
a follow-up commitment, and posts to Mattermost.

Trigger: sender = demo@rdm.is, subject contains "Lucint demo accessed"
"""

import logging
import re
from datetime import date
from email.utils import parseaddr

from artemis import config
from artemis.commitments import add_commitment
from artemis.crm_write_guard import crm_write_guard
from artemis.utils import next_business_day
from knowledge.db import execute_one, execute_write

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Processed-message tracking (same pattern as PB-007 in billing.py)
# ---------------------------------------------------------------------------

_PROCESSED_TABLE = "acos.processed_billing"  # shared processed-message table


def is_processed(message_id: str) -> bool:
    """Check if a demo intake message has already been processed."""
    row = execute_one(
        "SELECT 1 FROM acos.processed_billing WHERE message_id = %s",
        (message_id,),
    )
    return row is not None


def mark_processed(message_id: str) -> None:
    """Mark a demo intake message as processed."""
    execute_write(
        "INSERT INTO acos.processed_billing (message_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (message_id,),
    )


# ---------------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------------

_DEMO_SUBJECT_RE = re.compile(r"lucint demo accessed", re.IGNORECASE)


def matches_trigger(msg: dict) -> bool:
    """Return True if this Gmail message matches the PB-001 demo trigger.

    Expected msg dict keys (from get_recent_messages):
        from_email, subject, id
    """
    from_email = msg.get("from_email", "").lower()
    subject = msg.get("subject", "")
    return "demo@rdm.is" in from_email and bool(_DEMO_SUBJECT_RE.search(subject))


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

_FIELD_RE = re.compile(
    r"^(Name|Email|Company|Time)\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE
)
_NO_COMPANY = {"not provided", "no company", "n/a", "none", ""}


def extract_demo_fields(body_text: str) -> dict:
    """Extract Name, Email, Company, Time from the demo notification body.

    Returns dict with keys: name, email, company, time, domain.
    Missing fields are set to None.
    """
    fields = {}
    for match in _FIELD_RE.finditer(body_text):
        key = match.group(1).lower()
        value = match.group(2).strip()
        fields[key] = value

    name = fields.get("name")
    email = fields.get("email")
    company = fields.get("company")
    time_str = fields.get("time")

    # Normalize empty/no-company values
    if company and company.strip().lower() in _NO_COMPANY:
        company = None

    # Extract domain from email
    domain = None
    if email and "@" in email:
        domain = email.split("@")[1].lower()

    return {
        "name": name,
        "email": email,
        "company": company,
        "time": time_str,
        "domain": domain,
    }


def _sanitize_label_name(name: str) -> str:
    """Sanitize a company/domain name for use as a Gmail label segment.

    Lowercase, replace spaces/special chars with hyphens.
    """
    sanitized = name.lower().strip()
    sanitized = re.sub(r"[^a-z0-9\-]", "-", sanitized)
    sanitized = re.sub(r"-+", "-", sanitized)
    return sanitized.strip("-")


# ---------------------------------------------------------------------------
# Main processing function
# ---------------------------------------------------------------------------


def process_demo_message(
    gmail_client,
    message_id: str,
    mm_client=None,
    dry_run: bool = False,
) -> dict:
    """Process a single demo notification email.

    Returns a dict with all extracted/logged data for confirmation.
    If dry_run=True, skips all writes (CRM, commitment, labels, processed mark).
    """
    result = {
        "message_id": message_id,
        "success": False,
        "error": None,
    }

    try:
        # 1. Fetch full message body
        msg = _get_message_full(gmail_client, message_id)
        if not msg:
            result["error"] = "Failed to fetch message"
            return result

        headers = _extract_headers(msg)
        subject = headers.get("subject", "(no subject)")
        result["subject"] = subject

        body_text = gmail_client._extract_body(msg.get("payload", {}))
        fields = extract_demo_fields(body_text)

        result.update(fields)

        # Validate required fields
        if not fields["name"] or not fields["email"]:
            result["error"] = "Could not extract name or email from demo notification"
            # Apply needs-review label and notify
            if not dry_run:
                if gmail_client:
                    gmail_client.apply_gmail_label(message_id, "@artemis/needs-review")
                if mm_client:
                    mm_client.post_message(
                        config.CHANNEL_OPS,
                        f"\u26a0\ufe0f **PB-001 extraction failed** — could not parse "
                        f"name/email from [{subject}]\n"
                        f"Raw body preview: {body_text[:300]}\n"
                        f"Label `@artemis/needs-review` applied. Handle manually.",
                    )
            return result

        visitor_name = fields["name"]
        visitor_email = fields["email"]
        company_name = fields["company"]
        domain = fields["domain"]

        if dry_run:
            result["dry_run"] = True
            result["success"] = True
            result["actions"] = "Dry run — no writes performed"
            return result

        # Track which entities were flagged
        flagged_entities = []

        # --- Action 1: Apply Gmail labels ---
        for label in [
            "@artemis",
            "@artemis/pipeline",
            "@artemis/pipeline/demo-request",
        ]:
            gmail_client.apply_gmail_label(message_id, label)

        # --- Action 2: CRM — company ---
        company_display = company_name or domain or "Unknown"
        company_result = crm_write_guard(
            entity_type="company",
            data={
                "name": company_name or domain,
                "domain": domain,
                "types": ["Prospect"],
            },
            confidence="high" if company_name else "low",
            source_pb="PB-001",
            gmail_message_id=message_id,
            gmail_client=gmail_client,
            mm_client=mm_client,
        )
        company_id = company_result.get("entity_id")
        if company_result["status"] == "flagged":
            flagged_entities.append(f"company ({company_result.get('flag_reason', '?')})")

        # --- Action 3: CRM — person ---
        person_result = crm_write_guard(
            entity_type="person",
            data={
                "name": visitor_name,
                "email_primary": visitor_email,
                "emails": [visitor_email],
                "source": "lucint-demo",
                "source_detail": "Demo gate \u2014 magic link confirmed",
                "company_domain": domain,
            },
            confidence="high",
            source_pb="PB-001",
            gmail_message_id=message_id,
            gmail_client=gmail_client,
            mm_client=mm_client,
        )
        person_id = person_result.get("entity_id")
        if person_result["status"] == "flagged":
            flagged_entities.append(f"person ({person_result.get('flag_reason', '?')})")

        # --- Action 4: CRM — relationship ---
        if person_id and company_id:
            rel_result = crm_write_guard(
                entity_type="relationship",
                data={
                    "person_id": person_id,
                    "company_id": company_id,
                    "role": "Contact",
                    "is_primary": True,
                    "source": "lucint-demo",
                },
                confidence="high",
                source_pb="PB-001",
                gmail_message_id=message_id,
                gmail_client=gmail_client,
                mm_client=mm_client,
            )

        # --- Action 5: CRM — engagement ---
        if company_id:
            eng_result = crm_write_guard(
                entity_type="engagement",
                data={
                    "company_id": company_id,
                    "type": "Pilot",
                    "gate": 0,
                    "status": "Active",
                },
                confidence="high",
                source_pb="PB-001",
                gmail_message_id=message_id,
                gmail_client=gmail_client,
                mm_client=mm_client,
            )

        # --- Action 6: CRM — touch event ---
        crm_write_guard(
            entity_type="touch_event",
            data={
                "person_id": person_id,
                "company_id": company_id,
                "type": "Email",
                "direction": "Inbound",
                "subject": subject,
                "summary": f"Demo gate confirmed \u2014 {visitor_name} ({company_display})",
                "gmail_message_id": message_id,
                "playbook": "PB-001",
            },
            confidence="high",
            source_pb="PB-001",
            gmail_message_id=message_id,
        )

        # --- Action 7: Dynamic company pipeline label ---
        label_segment = _sanitize_label_name(company_name or domain or "unknown")
        pipeline_label = f"@artemis/pipeline/{label_segment}"
        gmail_client.ensure_gmail_label(pipeline_label)
        gmail_client.apply_gmail_label(message_id, pipeline_label)

        # --- Action 8: Create commitment ---
        nbd = next_business_day()
        commitment_title = f"Follow up with {visitor_name} re: Lucint demo"
        try:
            add_commitment(
                title=commitment_title,
                due_date=nbd.isoformat(),
                effort_days=1,
                client=company_display,
            )
        except Exception:
            logger.warning("PB-001: Commitment creation failed for %s", visitor_name)

        # --- Action 9: Post to Mattermost ---
        flagged_note = ""
        if flagged_entities:
            flagged_note = "\n\u26a0\ufe0f Flagged: " + ", ".join(flagged_entities)

        mm_msg = (
            f"\U0001f3af **New demo lead:** {visitor_name} ({company_display})\n"
            f"**Gate:** 0 \u2014 Prospect\n"
            f"**Email:** {visitor_email}\n"
            f"**CRM:** company {company_result['status']}, "
            f"person {person_result['status']}\n"
            f"**Follow-up:** {nbd.isoformat()}"
            f"{flagged_note}"
        )
        if mm_client:
            try:
                mm_client.post_message(config.CHANNEL_OPS, mm_msg)
            except Exception:
                logger.exception("PB-001: Failed to post to Mattermost")

        # --- Action 10: Mark as processed (AFTER Mattermost post) ---
        mark_processed(message_id)

        result["success"] = True
        result["company_status"] = company_result["status"]
        result["person_status"] = person_result["status"]
        result["follow_up_date"] = nbd.isoformat()
        logger.info(
            "PB-001: Processed demo lead — %s (%s), company=%s, person=%s",
            visitor_name, company_display,
            company_result["status"], person_result["status"],
        )

    except Exception:
        logger.exception("PB-001: Fatal error processing demo message %s", message_id)
        # Never silently drop a demo lead — post raw content to Mattermost
        if mm_client:
            try:
                mm_client.post_message(
                    config.CHANNEL_OPS,
                    f"\u26a0\ufe0f **PB-001 FAILED** on message `{message_id[:12]}...`\n"
                    f"Check logs. Demo lead NOT fully processed.",
                )
            except Exception:
                pass
        result["error"] = "Fatal error — check logs"

    return result


# ---------------------------------------------------------------------------
# Gmail helpers (same pattern as billing.py)
# ---------------------------------------------------------------------------


def _get_message_full(gmail_client, message_id: str) -> dict | None:
    """Fetch a full message for body extraction."""
    if not gmail_client.service:
        return None
    try:
        return gmail_client.service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
    except Exception:
        logger.exception("Failed to fetch full message %s", message_id)
        return None


def _extract_headers(message: dict) -> dict:
    """Extract key headers from a full message."""
    headers = {}
    for h in message.get("payload", {}).get("headers", []):
        headers[h["name"].lower()] = h["value"]
    return headers


# ---------------------------------------------------------------------------
# Scanning: find unprocessed demo emails
# ---------------------------------------------------------------------------


def get_demo_messages(gmail_client) -> list[str]:
    """Find unprocessed demo notification emails via Gmail search.

    Returns list of message IDs that match the PB-001 trigger.
    """
    if not gmail_client.service:
        return []

    try:
        gmail_client.authenticate()

        results = gmail_client.service.users().messages().list(
            userId="me",
            q='from:demo@rdm.is subject:"Lucint demo accessed"',
            maxResults=20,
        ).execute()

        message_ids = []
        for msg_ref in results.get("messages", []):
            if not is_processed(msg_ref["id"]):
                message_ids.append(msg_ref["id"])

        return message_ids
    except Exception:
        logger.exception("Failed to fetch demo notification emails")
        return []


# ---------------------------------------------------------------------------
# CLI dry-run entry point
# ---------------------------------------------------------------------------


def _dry_run():
    """Manual dry-run test — find the most recent demo email and extract fields.

    Usage:
        python -m artemis.demo_intake --dry-run
    """
    import sys
    from artemis.gmail import GmailClient

    print("PB-001 Demo Intake — Dry Run")
    print("=" * 50)

    gmail = GmailClient()
    try:
        gmail.authenticate()
    except Exception as e:
        print(f"Gmail auth failed: {e}")
        sys.exit(1)

    # Search for recent demo emails
    print("Searching for demo notification emails...")
    try:
        results = gmail.service.users().messages().list(
            userId="me",
            q='from:demo@rdm.is subject:"Lucint demo accessed"',
            maxResults=5,
        ).execute()
    except Exception as e:
        print(f"Search failed: {e}")
        sys.exit(1)

    messages = results.get("messages", [])
    if not messages:
        print("No demo notification emails found.")
        sys.exit(0)

    print(f"Found {len(messages)} demo email(s). Processing most recent...\n")

    msg_id = messages[0]["id"]
    result = process_demo_message(gmail, msg_id, mm_client=None, dry_run=True)

    print(f"Message ID: {result.get('message_id', '?')}")
    print(f"Subject:    {result.get('subject', '?')}")
    print(f"Name:       {result.get('name', '?')}")
    print(f"Email:      {result.get('email', '?')}")
    print(f"Company:    {result.get('company', '?')}")
    print(f"Domain:     {result.get('domain', '?')}")
    print(f"Time:       {result.get('time', '?')}")
    print(f"Success:    {result.get('success', False)}")
    if result.get("error"):
        print(f"Error:      {result['error']}")

    already = is_processed(msg_id)
    print(f"\nAlready processed: {already}")
    print("\n[DRY RUN] No writes performed.")


if __name__ == "__main__":
    import sys
    if "--dry-run" in sys.argv:
        _dry_run()
    else:
        print("Usage: python -m artemis.demo_intake --dry-run")
        sys.exit(1)
