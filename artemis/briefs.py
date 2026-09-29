"""Pre-meeting brief generator using Claude API."""

import hashlib
import logging
import re

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


def _strip_fences(text: str) -> str:
    """Kept as a thin alias: `artemis.llm_json` owns fence stripping now, and
    this spelling existed in four modules with four chances to drift."""
    from artemis.llm_json import strip_fences
    return strip_fences(text)


#: The stop_reason of the most recent _call_claude, so a caller that only gets
#: the text back can still tell "cut off" from "malformed". A one-element list
#: rather than a return-shape change, because four callers read that text.
_LAST_STOP_REASON: list[str | None] = [None]


def _call_claude(
    system: str,
    user_message: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 1000,
) -> str:
    """Make a Claude API call with audit logging."""
    from knowledge.secrets import get_anthropic_key
    client = anthropic.Anthropic(api_key=get_anthropic_key())
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
        from artemis.llm_json import stop_reason_of
        stop = stop_reason_of(response)
        if stop == "max_tokens":
            # Say it HERE, where the number to change is in scope. Twenty triages
            # were lost to this being reported downstream as a parse failure.
            logger.warning("Claude response hit max_tokens=%s (%s chars) — the "
                           "answer is cut off, not malformed", max_tokens, len(text))
        _LAST_STOP_REASON[0] = stop
        return text
    except Exception:
        logger.exception("Claude API call failed (model=%s)", model)
        _LAST_STOP_REASON[0] = None
        return ""


def triage_emails(emails_text: str, playbook_text: str = "") -> list[dict]:
    """Classify emails by urgency and sender type, with playbook matching."""
    system = TRIAGE_SYSTEM.replace("{playbooks}", playbook_text or "")
    # max_tokens was 1000, and that -- not fencing -- is what broke 20 triages in
    # five days: the failures had a median response of 2820 chars against 511 for
    # the ones that parsed, i.e. they were the long ones, cut off mid-value. The
    # fence strip had been correct the whole time. 4000 leaves headroom over the
    # 2909-char longest response actually observed.
    result = _call_claude(
        system,
        TRIAGE_USER.format(emails=emails_text),
        max_tokens=4000,
    )
    if not result:
        return []
    from artemis.llm_json import LlmJsonError, parse as parse_llm_json
    try:
        data = parse_llm_json(result, stop_reason=_LAST_STOP_REASON[0],
                              what="triage response")
    except LlmJsonError as exc:
        # The message distinguishes truncated from malformed. "Failed to parse"
        # for a truncated response is what made this look like a fencing bug.
        logger.error("Triage unusable — %s | starts: %s", exc, result[:120])
        return []
    # Claude may return a bare array or a wrapped object like {"items": [...]}
    if isinstance(data, list):
        return data
    return data.get("items", [data])


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
