"""Intent router — classify @mention messages into actionable intents via Claude."""

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field

import anthropic

from artemis.commitments import log_claude_call
from knowledge.secrets import get_anthropic_key

logger = logging.getLogger(__name__)

VALID_ACTIONS = {
    "add_contacts",
    "query_crm",
    "add_note",
    "schedule",
    "pipeline_update",
    "general_reply",
}

_ROUTER_SYSTEM = (
    "You are the intent router for Artemis, an AI Chief of Staff.\n"
    "The user has sent a message to @artemis in Mattermost.\n"
    "Determine what they want done. Return ONLY valid JSON matching "
    "this schema, no other text:\n"
    "{\n"
    '  "action": one of ["add_contacts", "query_crm", "add_note", '
    '"schedule", "pipeline_update", "general_reply"],\n'
    '  "entities": [list of person/org names mentioned],\n'
    '  "context": "one sentence explaining intent",\n'
    '  "attachments_needed": true or false,\n'
    '  "confidence": 0.0 to 1.0\n'
    "}\n\n"
    "Rules:\n"
    "- If message mentions adding, saving, importing people or contacts -> add_contacts\n"
    "- If message asks what you know about someone or company, or asks for status -> query_crm\n"
    "- If message references a meeting, scheduling, calendar -> schedule\n"
    "- If message mentions pipeline, gate, deal, prospect -> pipeline_update\n"
    "- If message asks to remember, note, or log something -> add_note\n"
    "- Everything else -> general_reply\n"
    "- If an attachment is present and action is ambiguous, bias toward add_contacts"
)


@dataclass
class IntentResult:
    action: str = "general_reply"
    entities: list[str] = field(default_factory=list)
    context: str = "fallback"
    attachments_needed: bool = False
    confidence: float = 0.0


def route_intent(
    message: str,
    has_attachment: bool = False,
    attachment_mime: str | None = None,
) -> IntentResult:
    """Classify a user message into an actionable intent via Claude.

    Returns an IntentResult. On any error, returns a fallback
    general_reply with confidence 0.0.
    """
    client = anthropic.Anthropic(api_key=get_anthropic_key())
    user_msg = (
        f"Message: {message}\n"
        f"Has attachment: {has_attachment}\n"
        f"Attachment type: {attachment_mime or 'none'}"
    )
    prompt_hash = hashlib.sha256(
        (_ROUTER_SYSTEM + user_msg).encode()
    ).hexdigest()[:16]

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=_ROUTER_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = response.content[0].text.strip()
        log_claude_call("claude-haiku-4-5-20251001", prompt_hash, len(text))

        # Strip markdown fences if present
        text = re.sub(r"^```json\s*", "", text)
        text = re.sub(r"^```\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

        data = json.loads(text)

        action = data.get("action", "general_reply")
        if action not in VALID_ACTIONS:
            action = "general_reply"

        return IntentResult(
            action=action,
            entities=data.get("entities", []),
            context=data.get("context", ""),
            attachments_needed=bool(data.get("attachments_needed", False)),
            confidence=float(data.get("confidence", 0.5)),
        )
    except Exception:
        logger.debug("Intent routing failed, falling back to general_reply", exc_info=True)
        return IntentResult()
