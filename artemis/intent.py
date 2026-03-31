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
    '  "primary_action": one of ["add_contacts", "query_crm", "add_note", '
    '"schedule", "pipeline_update", "general_reply"],\n'
    '  "secondary_actions": [...],\n'
    '  "entities": [list of person/org names mentioned],\n'
    '  "context": "one sentence explaining intent",\n'
    '  "attachments_needed": true or false,\n'
    '  "confidence": 0.0 to 1.0\n'
    "}\n\n"
    "CLASSIFICATION RULES (follow these strictly, in priority order):\n\n"
    "1. ATTACHMENT OVERRIDE: If an attachment is present AND the message mentions "
    "a person or organization name -> add_contacts as primary_action, regardless "
    "of other keywords.\n\n"
    "2. CONTACT / LEAD CREATION:\n"
    '   Keywords: "create lead", "add lead", "new lead", "add to pipeline", '
    '"potential POC", "potential contact", "add contact", "save contact", '
    '"import", "add this person"\n'
    "   -> primary_action: add_contacts, secondary_actions: [pipeline_update]\n\n"
    "3. CRM QUERIES:\n"
    '   Keywords: "what do you know", "tell me about", "find", "look up", '
    '"who is", "what\'s the status", "any info on"\n'
    "   -> primary_action: query_crm\n\n"
    "4. SCHEDULING:\n"
    '   Keywords: "schedule", "meeting", "calendar", "book", "set up a call"\n'
    "   -> primary_action: schedule\n\n"
    "5. PIPELINE MANAGEMENT:\n"
    '   Keywords: "update pipeline", "move to gate", "deal status", '
    '"advance deal", "pipeline update", "change stage"\n'
    "   -> primary_action: pipeline_update (as primary only, no secondary)\n\n"
    "6. NOTE TAKING:\n"
    '   Keywords: "remember", "note", "log", "keep track", "jot down"\n'
    "   -> primary_action: add_note\n\n"
    "7. Everything else -> primary_action: general_reply\n\n"
    "SECONDARY ACTIONS: Include secondary_actions when the message implies "
    "multiple things should happen. Examples:\n"
    '  "Add Greg Weddle as a lead for Dover" -> primary: add_contacts, '
    "secondary: [pipeline_update]\n"
    '  "Note that Brian called and schedule a follow-up" -> primary: add_note, '
    "secondary: [schedule]\n"
    "If only one thing is needed, secondary_actions should be [].\n"
)


@dataclass
class IntentResult:
    primary_action: str = "general_reply"
    secondary_actions: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    context: str = "fallback"
    attachments_needed: bool = False
    confidence: float = 0.0

    @property
    def action(self) -> str:
        """Backward-compatible alias for primary_action."""
        return self.primary_action


def load_intent_examples() -> str:
    """Load learned intent corrections from acos.data_vault_satellites.

    Returns formatted string of past corrections for inclusion in the
    Claude routing prompt, or empty string if none exist.
    """
    try:
        from knowledge.db import execute_query

        rows = execute_query(
            """SELECT content, created_at
               FROM acos.data_vault_satellites
               WHERE satellite_type = 'intent_example'
               ORDER BY created_at DESC
               LIMIT 20"""
        )
        if not rows:
            return ""

        lines = []
        for row in rows:
            try:
                data = json.loads(row["content"]) if isinstance(row["content"], str) else row["content"]
                user_said = data.get("user_said", "?")
                correct = data.get("correct_action", "?")
                rule = data.get("rule", "")
                date_str = row["created_at"].strftime("%Y-%m-%d") if row.get("created_at") else "?"
                lines.append(f'  User said: "{user_said}" -> correct action: {correct} ({date_str})')
                if rule:
                    lines.append(f"    Rule: {rule}")
            except (json.JSONDecodeError, AttributeError):
                continue

        if not lines:
            return ""

        return "Learned corrections from user feedback:\n" + "\n".join(lines) + "\n"

    except Exception:
        logger.debug("Could not load intent examples", exc_info=True)
        return ""


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

    # Build system prompt with learned examples
    examples = load_intent_examples()
    system = _ROUTER_SYSTEM
    if examples:
        system += "\n" + examples

    user_msg = (
        f"Message: {message}\n"
        f"Has attachment: {has_attachment}\n"
        f"Attachment type: {attachment_mime or 'none'}"
    )
    prompt_hash = hashlib.sha256(
        (system + user_msg).encode()
    ).hexdigest()[:16]

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=system,
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

        # Support both old "action" and new "primary_action" keys
        primary = data.get("primary_action") or data.get("action", "general_reply")
        if primary not in VALID_ACTIONS:
            primary = "general_reply"

        secondary = [a for a in data.get("secondary_actions", []) if a in VALID_ACTIONS]

        return IntentResult(
            primary_action=primary,
            secondary_actions=secondary,
            entities=data.get("entities", []),
            context=data.get("context", ""),
            attachments_needed=bool(data.get("attachments_needed", False)),
            confidence=float(data.get("confidence", 0.5)),
        )
    except Exception:
        logger.debug("Intent routing failed, falling back to general_reply", exc_info=True)
        return IntentResult()
