"""Scheduling assistant — detect meeting requests, draft responses (Learning mode)."""

import json
import logging
import re

import anthropic

from artemis import config
from knowledge.secrets import get_anthropic_key, get_booking_links

logger = logging.getLogger(__name__)

_DETECTION_SYSTEM = (
    "You classify whether an email contains a request to schedule a meeting or call. "
    "Reply with JSON only — no markdown fences, no explanation.\n"
    "Schema: {\"detected\": bool, \"duration_minutes\": 30|60|90|null, "
    "\"confidence\": 0.0-1.0, \"relevant_text\": string|null}"
)


def detect_scheduling_request(email_body: str, sender: str) -> dict | None:
    """Detect if an email contains a scheduling request.

    Returns None if no request or confidence <= 0.7.
    Returns dict with type, sender, suggested_duration_minutes,
    raw_request, and confidence if detected.
    """
    client = anthropic.Anthropic(api_key=get_anthropic_key())

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=_DETECTION_SYSTEM,
            messages=[{"role": "user", "content": f"From: {sender}\n\n{email_body[:3000]}"}],
        )
        text = response.content[0].text.strip()
        # Strip markdown fences if present
        text = re.sub(r'^```json\s*', '', text)
        text = re.sub(r'^```\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        text = text.strip()

        result = json.loads(text)
    except Exception:
        logger.debug("Scheduling detection failed for email from %s", sender)
        return None

    if not result.get("detected") or result.get("confidence", 0) <= 0.7:
        return None

    duration = result.get("duration_minutes")
    if duration not in (30, 60, 90):
        duration = 30  # default

    return {
        "type": "scheduling_request",
        "sender": sender,
        "suggested_duration_minutes": duration,
        "raw_request": result.get("relevant_text", ""),
        "confidence": result["confidence"],
    }


def draft_scheduling_response(
    sender_name: str,
    sender_email: str,
    duration_minutes: int,
    free_blocks: list[dict],
    original_subject: str,
) -> dict:
    """Draft a scheduling reply with free blocks and booking link.

    Returns a dict ready for Mattermost approval posting.
    """
    # Pick the right booking link
    try:
        links = get_booking_links()
    except Exception:
        links = {}
    duration_key = f"{duration_minutes}min"
    booking_link = links.get(duration_key, links.get("30min", ""))

    # Format time slots
    slot_lines = []
    for block in free_blocks:
        slot_lines.append(f"  - {block['date_label']} at {block['time_label']}")
    slots_text = "\n".join(slot_lines)

    first_name = sender_name.split()[0] if sender_name else "there"
    booking_line = f"\n\nOr grab a time directly: {booking_link}" if booking_link else ""

    body = (
        f"Hi {first_name}, happy to connect! "
        f"Here are a few times that work for a {duration_minutes}-minute call:\n\n"
        f"{slots_text}"
        f"{booking_line}\n\n"
        f"Looking forward to it,\nRyan"
    )

    subject = original_subject
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    preview = f"Reply to {sender_name} with {len(free_blocks)} time slots ({duration_minutes}min)"

    return {
        "approval_type": "send_email",
        "to": sender_email,
        "subject": subject,
        "body": body,
        "preview": preview,
    }
