"""Pre-meeting brief generator using Claude API."""

import hashlib
import json
import logging

import anthropic

from artemis import config
from artemis.commitments import get_commitments_for_client, log_claude_call
from artemis.prompts import (
    BRIEF_SYSTEM,
    BRIEF_USER,
    MENTION_SYSTEM,
    MENTION_USER,
    MORNING_BRIEF_SYSTEM,
    MORNING_BRIEF_USER,
    TRIAGE_SYSTEM,
    TRIAGE_USER,
)

logger = logging.getLogger(__name__)


def _call_claude(
    system: str,
    user_message: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 1000,
) -> str:
    """Make a Claude API call with audit logging."""
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    prompt_hash = hashlib.sha256(
        (system + user_message).encode()
    ).hexdigest()[:16]

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
        text = response.content[0].text
        log_claude_call(model, prompt_hash, len(text))
        return text
    except Exception:
        logger.exception("Claude API call failed (model=%s)", model)
        return ""


def triage_emails(emails_text: str) -> list[dict]:
    """Classify emails by urgency and sender type."""
    result = _call_claude(
        TRIAGE_SYSTEM,
        TRIAGE_USER.format(emails=emails_text),
        max_tokens=1000,
    )
    if not result:
        return []
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        logger.error("Failed to parse triage response: %s", result[:200])
        return []


def generate_meeting_brief(
    meeting_title: str,
    meeting_time: str,
    attendees: list[str],
    email_context: str,
    commitment_context: str,
) -> str:
    """Generate a pre-meeting brief using claude-opus-4-6."""
    return _call_claude(
        BRIEF_SYSTEM,
        BRIEF_USER.format(
            meeting_title=meeting_title,
            meeting_time=meeting_time,
            attendees=", ".join(attendees),
            email_context=email_context,
            commitment_context=commitment_context,
        ),
        model="claude-opus-4-6",
        max_tokens=2000,
    )


def generate_morning_brief(
    meetings: str,
    commitments: str,
    inbox_items: str,
    monitor_alerts: str,
) -> str:
    """Generate the daily morning brief."""
    return _call_claude(
        MORNING_BRIEF_SYSTEM,
        MORNING_BRIEF_USER.format(
            meetings=meetings,
            commitments=commitments,
            inbox_items=inbox_items,
            monitor_alerts=monitor_alerts,
        ),
        max_tokens=1000,
    )


def handle_mention(
    question: str,
    thread_context: str,
    data_context: str,
) -> str:
    """Handle an @artemis mention with context-aware response."""
    return _call_claude(
        MENTION_SYSTEM,
        MENTION_USER.format(
            question=question,
            thread_context=thread_context,
            data_context=data_context,
        ),
        max_tokens=1000,
    )
