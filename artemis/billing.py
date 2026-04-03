"""PB-007: Billing Intake — process billing-labeled emails into expense tracking.

Scans for Gmail messages with the 'artemis/billing' label, extracts expense
data, uploads attachments to Drive, logs to Sheets, and posts to Mattermost.
"""

import base64
import logging
import re
from datetime import date
from email.utils import parseaddr

from artemis import config
from artemis.google_drive import get_or_create_expense_folder, upload_attachment
from artemis.google_sheets import append_expense_row, get_sheet_url
from knowledge.db import execute_one, execute_write
from knowledge.secrets import get_gmail_token

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Postgres state tracking — processed billing message IDs
# ---------------------------------------------------------------------------


def is_processed(message_id: str) -> bool:
    """Check if a billing message has already been processed (via RDS Postgres)."""
    row = execute_one(
        "SELECT 1 FROM acos.processed_billing WHERE message_id = %s",
        (message_id,),
    )
    return row is not None


def mark_processed(message_id: str) -> None:
    """Mark a billing message as processed (via RDS Postgres)."""
    execute_write(
        "INSERT INTO acos.processed_billing (message_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (message_id,),
    )


# ---------------------------------------------------------------------------
# Amount extraction
# ---------------------------------------------------------------------------

# Matches $1,234.56 or $1234 or 1,234.56 (with two decimal places)
_AMOUNT_RE = re.compile(r"\$[\d,]+\.?\d*|[\d,]+\.\d{2}")


def extract_amounts(text: str) -> list[str]:
    """Extract all dollar amounts from text. Returns list of matched strings."""
    return _AMOUNT_RE.findall(text)


def parse_amount(raw: str) -> float:
    """Parse a raw amount string like '$1,234.56' into a float."""
    cleaned = raw.replace("$", "").replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def best_amount(text: str) -> tuple[str, list[str]]:
    """Extract the best (largest) amount from text.

    Returns (best_amount_str, all_amounts_found).
    If multiple found, best is the largest.
    """
    found = extract_amounts(text)
    if not found:
        return "", []
    if len(found) == 1:
        return found[0], found
    # Pick the largest
    parsed = [(a, parse_amount(a)) for a in found]
    parsed.sort(key=lambda x: x[1], reverse=True)
    return parsed[0][0], found


# ---------------------------------------------------------------------------
# Category classification
# ---------------------------------------------------------------------------

_CATEGORY_MAP = [
    (["aws", "azure", "digitalocean", "hetzner", "vercel", "namecheap", "hostinger"],
     "Infrastructure"),
    (["google", "notion", "github", "anthropic", "openai", "slack", "zoom"],
     "SaaS / Software"),
    (["attorney", "legal", "law", "trademark", "llc"],
     "Legal"),
    (["insurance", "liability", "e&o"],
     "Insurance"),
    (["apple", "dell", "bestbuy", "newegg", "hardware"],
     "Hardware"),
    (["score", "linkedin", "ads", "marketing"],
     "Sales & Outreach"),
]


def classify_category(subject: str, sender: str) -> str:
    """Classify an expense category from subject + sender text (case-insensitive)."""
    combined = f"{subject} {sender}".lower()
    for keywords, category in _CATEGORY_MAP:
        for kw in keywords:
            if kw in combined:
                return category
    return "Misc"


# ---------------------------------------------------------------------------
# Gmail helpers — attachment download
# ---------------------------------------------------------------------------


def ensure_billing_label(gmail_client) -> str | None:
    """Ensure the 'artemis/billing' Gmail label exists. Creates it if missing.

    Returns the label ID, or None on failure.
    """
    if not gmail_client.service:
        return None

    try:
        labels = gmail_client.service.users().labels().list(userId="me").execute()
        for lbl in labels.get("labels", []):
            if lbl["name"].lower() == "artemis/billing":
                return lbl["id"]

        # Label doesn't exist — create it
        new_label = gmail_client.service.users().labels().create(
            userId="me",
            body={
                "name": "artemis/billing",
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        ).execute()
        label_id = new_label["id"]
        logger.info("Created Gmail label 'artemis/billing' (id=%s)", label_id)
        return label_id
    except Exception:
        logger.exception("Failed to ensure artemis/billing label")
        return None


def get_billing_messages(gmail_client) -> list[dict]:
    """Fetch messages with the 'artemis/billing' label that haven't been processed."""
    if not gmail_client.service:
        return []

    try:
        # Refresh credentials to avoid stale SSL connections
        gmail_client.authenticate()

        # Find the label ID for 'artemis/billing'
        labels = gmail_client.service.users().labels().list(userId="me").execute()
        label_id = None
        for lbl in labels.get("labels", []):
            if lbl["name"].lower() == "artemis/billing":
                label_id = lbl["id"]
                break

        if not label_id:
            logger.warning("Gmail label 'artemis/billing' not found")
            return []

        # List messages with that label
        results = gmail_client.service.users().messages().list(
            userId="me", labelIds=[label_id], maxResults=20
        ).execute()

        messages = []
        for msg_ref in results.get("messages", []):
            if is_processed(msg_ref["id"]):
                continue
            messages.append(msg_ref["id"])

        return messages
    except Exception:
        logger.exception("Failed to fetch billing messages")
        return []


def get_message_full(gmail_client, message_id: str) -> dict | None:
    """Fetch a full message with all parts for body + attachment extraction."""
    if not gmail_client.service:
        return None
    try:
        return gmail_client.service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
    except Exception:
        logger.exception("Failed to fetch full message %s", message_id)
        return None


def extract_attachments(gmail_client, message: dict) -> list[dict]:
    """Extract all attachments from a full message.

    Returns list of {"filename": str, "mime_type": str, "data": bytes}.
    """
    attachments = []
    msg_id = message.get("id", "")

    def _walk_parts(parts):
        for part in parts:
            filename = part.get("filename", "")
            mime = part.get("mimeType", "")
            body = part.get("body", {})

            if filename and body.get("attachmentId"):
                # Download attachment data
                try:
                    att = gmail_client.service.users().messages().attachments().get(
                        userId="me", messageId=msg_id, id=body["attachmentId"]
                    ).execute()
                    data = base64.urlsafe_b64decode(att["data"])
                    attachments.append({
                        "filename": filename,
                        "mime_type": mime,
                        "data": data,
                    })
                except Exception:
                    logger.exception("Failed to download attachment %s", filename)

            # Recurse into sub-parts
            if part.get("parts"):
                _walk_parts(part["parts"])

    payload = message.get("payload", {})
    if payload.get("parts"):
        _walk_parts(payload["parts"])

    return attachments


def extract_headers(message: dict) -> dict:
    """Extract key headers from a full message."""
    headers = {}
    for h in message.get("payload", {}).get("headers", []):
        headers[h["name"].lower()] = h["value"]
    return headers


# ---------------------------------------------------------------------------
# Forwarded email vendor extraction
# ---------------------------------------------------------------------------

_FORWARDED_FROM_RE = re.compile(
    r"From:\s*([^\n<]+)?<?([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+)",
    re.IGNORECASE,
)

# Subdomains to strip when deriving vendor name from domain
_STRIP_SUBDOMAINS = {"mg", "mail", "email", "e", "noreply", "billing", "notifications", "notify"}


def _vendor_from_domain(domain: str) -> str:
    """Derive a clean vendor name from an email domain.

    'mg.github.com' → 'GitHub', 'billing.anthropic.com' → 'Anthropic'
    """
    parts = domain.lower().split(".")
    # Strip known non-brand subdomains and TLD
    if len(parts) >= 3:
        brand_parts = [p for p in parts[:-1] if p not in _STRIP_SUBDOMAINS]
        if brand_parts:
            return brand_parts[-1].title()
    # Two-part domain: just use the name portion
    if len(parts) >= 2:
        return parts[-2].title()
    return domain.title()


def extract_forwarded_vendor(subject: str, body_text: str, fallback_name: str, fallback_domain: str) -> str:
    """Extract the original vendor from a forwarded email.

    If the subject starts with Fwd:/FW:, searches the body for the original
    'From:' line and extracts the vendor name. Falls back to the envelope sender.
    """
    if not re.match(r"^(fwd?|fw)\s*:", subject, re.IGNORECASE):
        return fallback_name or _vendor_from_domain(fallback_domain)

    m = _FORWARDED_FROM_RE.search(body_text)
    if m:
        display_name = (m.group(1) or "").strip().strip('"')
        email_addr = m.group(2)
        if display_name:
            return display_name
        # No display name — derive from email domain
        if "@" in email_addr:
            domain = email_addr.split("@")[1]
            return _vendor_from_domain(domain)

    # No original From found — fall back to envelope sender
    return fallback_name or _vendor_from_domain(fallback_domain)


# ---------------------------------------------------------------------------
# Main processing function
# ---------------------------------------------------------------------------


def process_billing_message(
    gmail_client,
    message_id: str,
    mm_client=None,
    dry_run: bool = False,
) -> dict:
    """Process a single billing email.

    Returns a dict with all extracted/logged data for confirmation.
    If dry_run=True, skips Drive upload, Sheets append, and Mattermost post.
    """
    result = {
        "message_id": message_id,
        "success": False,
        "error": None,
        "drive_link": "",
        "sheet_url": get_sheet_url(),
    }

    # 1. Fetch full message
    msg = get_message_full(gmail_client, message_id)
    if not msg:
        result["error"] = "Failed to fetch message"
        return result

    headers = extract_headers(msg)
    sender_full = headers.get("from", "")
    sender_name, sender_email = parseaddr(sender_full)
    sender_name = sender_name or sender_email
    sender_domain = sender_email.split("@")[1] if "@" in sender_email else ""
    subject = headers.get("subject", "(no subject)")
    msg_date = headers.get("date", "")

    result.update({
        "sender_name": sender_name,
        "sender_email": sender_email,
        "sender_domain": sender_domain,
        "subject": subject,
        "date": msg_date,
    })

    # 2. Extract body text and amounts
    body_text = gmail_client._extract_body(msg.get("payload", {}))
    combined_text = f"{subject} {body_text}"
    amount_str, all_amounts = best_amount(combined_text)

    # Vendor extraction: handle forwarded emails
    vendor = extract_forwarded_vendor(subject, body_text, sender_name, sender_domain)
    category = classify_category(subject, f"{sender_full} {vendor}")

    # Founder Loan detection
    founder_loan_explicit = bool(re.search(r"founder\s+loan", combined_text, re.IGNORECASE))

    result.update({
        "amount": amount_str,
        "all_amounts": all_amounts,
        "category": category,
        "vendor": vendor,
        "founder_loan_explicit": founder_loan_explicit,
    })

    # 2a. CRM Write Guard — register vendor as company entity
    notes_parts = []
    crm_company_id = None
    try:
        from artemis.crm_write_guard import crm_write_guard
        guard_confidence = "high" if sender_domain else "low"
        guard_result = crm_write_guard(
            entity_type="company",
            data={"name": vendor, "domain": sender_domain, "types": ["Vendor"]},
            confidence=guard_confidence,
            source_pb="PB-007",
            gmail_message_id=message_id,
            gmail_client=gmail_client,
            mm_client=mm_client,
        )
        crm_company_id = guard_result.get("entity_id")
        if guard_result.get("status") == "flagged":
            notes_parts.append(
                f"Vendor unresolved — see pending CRM review "
                f"(id={guard_result.get('pending_id', '?')[:8]})"
            )
        elif guard_result.get("status") == "written":
            notes_parts.append(f"CRM: new vendor '{vendor}' added")

        # Log touch event
        crm_write_guard(
            entity_type="touch_event",
            data={
                "company_id": crm_company_id,
                "type": "Email",
                "direction": "Inbound",
                "subject": subject,
                "summary": f"Billing email: {amount_str or 'no amount'}",
                "gmail_message_id": message_id,
                "playbook": "PB-007",
            },
            confidence="high",
            source_pb="PB-007",
            gmail_message_id=message_id,
        )
    except Exception:
        logger.exception("CRM write guard failed for billing vendor — continuing")

    # 3. Process attachments
    attachments = extract_attachments(gmail_client, msg)
    drive_links = []

    if attachments and not dry_run:
        folder_id = get_or_create_expense_folder()
        if folder_id:
            for att in attachments:
                file_id, link = upload_attachment(
                    att["filename"], att["data"], att["mime_type"], folder_id
                )
                if link:
                    drive_links.append(link)
                else:
                    logger.warning("Drive upload failed for %s — using Gmail link", att["filename"])
        else:
            logger.warning("Could not create expense folder — using Gmail links")
    elif attachments and dry_run:
        for att in attachments:
            drive_links.append(f"[DRY RUN] Would upload: {att['filename']} ({att['mime_type']})")

    # Fallback to Gmail deep link if no attachments or all uploads failed
    gmail_link = f"https://mail.google.com/mail/u/0/#inbox/{message_id}"
    doc_link = drive_links[0] if drive_links else gmail_link

    result.update({
        "attachments": [a["filename"] for a in attachments],
        "drive_links": drive_links,
        "document_link": doc_link,
        "gmail_link": gmail_link,
    })

    # 4. Build notes (notes_parts initialized before CRM write guard above)
    if not amount_str:
        notes_parts.append("No amount detected")
    if len(all_amounts) > 1:
        notes_parts.append(f"Multiple amounts found: {', '.join(all_amounts)}")
    if not attachments:
        notes_parts.append("No attachment — Gmail link used")
    if not drive_links and attachments:
        notes_parts.append("Drive upload failed — Gmail link used")
    if founder_loan_explicit:
        notes_parts.append("Founder Loan flagged in email.")
    if notes_parts:
        notes = "Auto-logged by Artemis. Review required. " + "; ".join(notes_parts)
    else:
        notes = "Auto-logged by Artemis."

    result["notes"] = notes

    # 5. Append to Sheets
    row = {
        "date": date.today().strftime("%m/%d/%Y"),
        "vendor": vendor,
        "description": subject,
        "category": category,
        "amount": amount_str,
        "payment_method": "",
        "founder_loan": "Yes",
        "reimbursed": "No",
        "reimbursed_date": "",
        "document_link": doc_link,
        "notes": notes,
    }

    if not dry_run:
        sheet_ok = append_expense_row(row)
        if not sheet_ok:
            result["error"] = "Sheets append failed"
            logger.error("PB-007: Sheets append failed for %s", message_id)
    else:
        sheet_ok = True
        result["dry_run_row"] = row

    # 6. Mark as processed
    if not dry_run:
        mark_processed(message_id)

    # 7. Post to Mattermost
    if mm_client and not dry_run:
        amount_display = amount_str if amount_str else "\u26a0 None found \u2014 review required"
        attachment_display = doc_link
        if drive_links:
            attachment_display = " | ".join(drive_links)

        mm_msg = (
            f"\U0001f4c4 **Billing intake logged**\n"
            f"**From:** {sender_name} <{sender_email}>\n"
            f"**Subject:** {subject}\n"
            f"**Amount detected:** {amount_display}\n"
            f"**Category:** {category}\n"
            f"**Founder Loan:** Yes \u00b7 **Reimbursed:** No\n"
            f"**Attachment:** {attachment_display}\n"
            f"**Sheet:** {result['sheet_url']}\n\n"
            f"_React with \u2705 if correct or reply to correct any fields._"
        )

        try:
            mm_client.post_message(config.CHANNEL_OPS, mm_msg)
        except Exception:
            logger.exception("Failed to post billing intake to Mattermost")
            # Even if MM fails, the data is in Sheets — don't mark as error

    elif mm_client and not sheet_ok and not dry_run:
        # Sheets failed — post all data so Ryan can manually enter
        mm_msg = (
            f"\u26a0\ufe0f **Billing intake FAILED to log to Sheets**\n"
            f"**From:** {sender_name} <{sender_email}>\n"
            f"**Subject:** {subject}\n"
            f"**Amount:** {amount_str or 'not detected'}\n"
            f"**Category:** {category}\n"
            f"**Gmail link:** {gmail_link}\n\n"
            f"_Please add this manually to the expense sheet._"
        )
        try:
            mm_client.post_message(config.CHANNEL_OPS, mm_msg)
        except Exception:
            logger.exception("Failed to post billing failure alert")

    result["success"] = True
    return result


# ---------------------------------------------------------------------------
# Scope checking
# ---------------------------------------------------------------------------

_BILLING_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
]


def get_financial_summary() -> str:
    """Build a plain-text financial summary for the current month.

    Queries:
      1. v_budget_vs_actual — current month planned vs actual
      2. v_founder_loan_balance — outstanding Ryan loans
      3. processed_billing for current month — recent actuals
      4. monthly_financials for last 3 months — trend
    """
    from datetime import datetime
    from knowledge.db import execute_query

    now = datetime.now()
    month_label = now.strftime("%B %Y")
    month_start = now.strftime("%Y-%m-01")

    lines = [f"\U0001f4b0 **RDMIS Financial Position — {month_label}**\n"]

    # 1. Budget vs actual
    try:
        rows = execute_query("SELECT * FROM public.v_budget_vs_actual ORDER BY category")
        if rows:
            lines.append("**MONTHLY BUDGET vs ACTUAL (MTD):**")
            total_planned = 0.0
            total_actual = 0.0
            for r in rows:
                planned = float(r.get("planned_monthly") or 0)
                actual = float(r.get("actual_mtd") or 0)
                variance = float(r.get("variance") or 0)
                total_planned += planned
                total_actual += actual
                icon = "\u2705" if variance >= 0 else "\u26a0\ufe0f"
                sign = "-" if variance >= 0 else "+"
                lines.append(
                    f"  {r['category']:<20s} Planned ${planned:>8.2f}  "
                    f"Actual ${actual:>8.2f}  {icon} {sign}${abs(variance):.2f}"
                )
            total_var = total_planned - total_actual
            icon = "\u2705" if total_var >= 0 else "\u26a0\ufe0f"
            sign = "-" if total_var >= 0 else "+"
            lines.append(
                f"  {'TOTAL':<20s} Planned ${total_planned:>8.2f}  "
                f"Actual ${total_actual:>8.2f}  {icon} {sign}${abs(total_var):.2f}"
            )
        else:
            lines.append("_No budget data for this month._")
    except Exception:
        logger.debug("Budget vs actual query failed", exc_info=True)
        lines.append("_Budget vs actual unavailable._")

    lines.append("")

    # 2. Founder loan balance
    try:
        loan = execute_query("SELECT * FROM public.v_founder_loan_balance")
        if loan and loan[0].get("loan_count"):
            r = loan[0]
            lines.append("**FOUNDER LOAN BALANCE:**")
            lines.append(f"  Total loaned: ${float(r['total_loaned'] or 0):,.2f} across {r['loan_count']} transaction(s)")
            lines.append(f"  Repaid: ${float(r['total_repaid'] or 0):,.2f}")
            lines.append(f"  Outstanding: ${float(r['outstanding_balance'] or 0):,.2f}")
        else:
            lines.append("**FOUNDER LOAN BALANCE:** _No loans recorded._")
    except Exception:
        logger.debug("Founder loan query failed", exc_info=True)
        lines.append("_Founder loan data unavailable._")

    lines.append("")

    # 3. Recent transactions
    try:
        recent = execute_query(
            """SELECT transaction_date, description, amount, category
               FROM public.processed_billing
               WHERE transaction_date >= %s
               ORDER BY transaction_date DESC
               LIMIT 5""",
            (month_start,),
        )
        if recent:
            lines.append("**RECENT TRANSACTIONS (last 5):**")
            for r in recent:
                dt = r["transaction_date"].strftime("%m/%d") if r.get("transaction_date") else "?"
                lines.append(f"  {dt}  {r['description'][:40]:<40s}  ${float(r.get('amount') or 0):>8.2f}  [{r.get('category', '?')}]")
        else:
            lines.append("_No transactions recorded this month._")
    except Exception:
        logger.debug("Recent transactions query failed", exc_info=True)
        lines.append("_Recent transactions unavailable._")

    lines.append("")

    # 4. Three-month trend
    try:
        trend = execute_query(
            """SELECT month, revenue_received, expenses_actual, closing_balance
               FROM public.monthly_financials
               ORDER BY month DESC
               LIMIT 3"""
        )
        if trend:
            lines.append("**3-MONTH TREND:**")
            for r in sorted(trend, key=lambda x: x["month"]):
                m = r["month"].strftime("%b")
                rev = float(r.get("revenue_received") or 0)
                exp = float(r.get("expenses_actual") or 0)
                net = rev - exp
                lines.append(f"  {m}: Revenue ${rev:,.2f} | Expenses ${exp:,.2f} | Net ${net:+,.2f}")
        else:
            lines.append("_No monthly financial history yet._")
    except Exception:
        logger.debug("Monthly financials query failed", exc_info=True)
        lines.append("_Monthly trend unavailable._")

    return "\n".join(lines)


def check_billing_scopes() -> tuple[bool, list[str]]:
    """Check if current OAuth token has Drive and Sheets scopes.

    Reads the token from Secrets Manager (not a local file).
    Returns (all_present, list_of_missing_scopes).
    """
    try:
        token_dict = get_gmail_token()
        granted = set(token_dict.get("scopes", []))
        missing = [s for s in _BILLING_SCOPES if s not in granted]
        return len(missing) == 0, missing
    except Exception:
        logger.debug("Could not read Gmail token from Secrets Manager for scope check")
        return False, _BILLING_SCOPES[:]


def print_scope_migration_instructions(missing: list[str]) -> None:
    """Print instructions for re-authorizing with new scopes."""
    print("\n" + "=" * 60)
    print("PB-007 BILLING INTAKE: OAuth scope update required")
    print("=" * 60)
    print(f"\nMissing scopes: {', '.join(missing)}")
    print("\nTo fix:")
    print("  1. Delete token.json:")
    print(f"     rm {config.GMAIL_TOKEN_PATH}")
    print("  2. Re-run setup_oauth.py to authorize with all scopes:")
    print("     python setup_oauth.py")
    print("  3. Approve ALL permission prompts in the browser")
    print("  4. Restart Artemis")
    print("\nArtemis will continue running without billing intake until")
    print("the token is updated.\n")
