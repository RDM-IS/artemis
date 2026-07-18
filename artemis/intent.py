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
    "financial_summary",
    "log_interaction",
    "log_morning_state",
    "log_workout_debrief",
    "trainer_override",
    "general_reply",
}

_ROUTER_SYSTEM = (
    "You are the intent router for Artemis, an AI Chief of Staff.\n"
    "The user has sent a message to @artemis in Mattermost.\n"
    "Determine what they want done. Return ONLY valid JSON matching "
    "this schema, no other text:\n"
    "{\n"
    '  "primary_action": one of ["add_contacts", "query_crm", "add_note", '
    '"schedule", "pipeline_update", "financial_summary", "log_interaction", '
    '"general_reply"],\n'
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
    "5. FINANCIAL SUMMARY:\n"
    '   Keywords: "cash position", "financial summary", "how much have I spent", '
    '"budget", "what\'s my burn", "burn rate", "expenses", "financials", '
    '"spending", "runway", "founder loans"\n'
    "   -> primary_action: financial_summary\n\n"
    "6. PIPELINE MANAGEMENT:\n"
    '   Keywords: "update pipeline", "move to gate", "deal status", '
    '"advance deal", "pipeline update", "change stage"\n'
    "   -> primary_action: pipeline_update (as primary only, no secondary)\n\n"
    "7. INTERACTION LOGGING (debriefs, call summaries):\n"
    '   Keywords: "just got off the phone with", "just met with", '
    '"had a call with", "spoke with", "talked to", "call with", '
    '"meeting with", "just finished", "debrief", "follow up from", '
    '"spoke to", "met with"\n'
    "   -> primary_action: log_interaction\n\n"
    "8. NOTE TAKING:\n"
    '   Keywords: "remember", "note", "keep track", "jot down"\n'
    "   -> primary_action: add_note\n\n"
    "9. MORNING CHECK-IN (training):\n"
    '   Triggers: message starts with "morning", "checkin", '
    '"@artemis morning", or contains "slept" / "sleep"\n'
    '   Examples: "slept 6.5 energy 3", "morning. RHR 58, legs sore"\n'
    "   -> primary_action: log_morning_state\n\n"
    "10. WORKOUT DEBRIEF (training):\n"
    '   Triggers: "done", "debrief", "workout done", "@artemis done", '
    'or contains "RPE" + exercise names\n'
    '   Examples: "done. squats 10 @ 35 RPE 7", '
    '"burpees 15 reps RPE 10 HR peak 159"\n'
    "   -> primary_action: log_workout_debrief\n\n"
    "11. TRAINER OVERRIDE (bike indoor/outdoor for next cardio day):\n"
    '   Triggers: "trainer set indoor", "trainer set outdoor"\n'
    "   -> primary_action: trainer_override\n\n"
    "12. Everything else -> primary_action: general_reply\n\n"
    "SECONDARY ACTIONS: Include secondary_actions when the message implies "
    "multiple things should happen. Examples:\n"
    '  "Add Greg Weddle as a lead for Dover" -> primary: add_contacts, '
    "secondary: [pipeline_update]\n"
    '  "Note that Brian called and schedule a follow-up" -> primary: add_note, '
    "secondary: [schedule]\n"
    "If only one thing is needed, secondary_actions should be [].\n"
)


# ---------------------------------------------------------------------------
# PB-010 dossier: deterministic short-circuit, evaluated BEFORE the LLM
# classifier and unoverridable on a positive match (same principle as the
# HEALTH-1 fix — a positive deterministic detection must not be re-routed by the
# probabilistic router). This is pure regex, no LLM. main.py wires the returned
# tag to the dossier handlers ahead of route_intent(), so the classifier never
# even runs on a dossier phrase.
# ---------------------------------------------------------------------------

# Ordered (first match wins). Each entry: (route_tag, compiled_pattern).
_DOSSIER_PATTERNS = [
    # review-context commands (main.py gates these on a pending review so a bare
    # "drop 4" outside a review still falls through to the LLM).
    ("approve", re.compile(r"^approve\b", re.IGNORECASE)),
    ("drop", re.compile(r"^drop\s+\d", re.IGNORECASE)),
    ("edit", re.compile(r"^edit\s+\d+\s*:", re.IGNORECASE)),
    # dossier subcommands: dossier review/show/new/set/…
    ("dossier", re.compile(r"^dossier\b", re.IGNORECASE)),
    # capture a meeting
    ("capture", re.compile(r"^met\s+with\b", re.IGNORECASE)),
    # pre-brief / meeting package
    ("brief", re.compile(
        r"(?:\bprepare\s+(?:a\s+)?meeting\s+package\b|^brief\b|^i'?m\s+meeting\s+with\b)",
        re.IGNORECASE)),
    # direct commitment. `^remind me` plus the mid-sentence `remind me to …` form
    # (§3.5: "I emailed X about Y, remind me to follow up <when>").
    ("remind", re.compile(r"^remind\s+me\b|\bremind\s+me\s+to\b", re.IGNORECASE)),
    # PB-010c org chart (read-only). `^org <arg>` plus natural forms; bare `org`
    # still matches (→ usage hint in the handler, never LLM fallthrough). `\borg\b`
    # so "organize"/"organic" never trip it. Placed after the dossier prefixes,
    # before todos.
    ("org", re.compile(r"^org\b", re.IGNORECASE)),
    ("org", re.compile(
        r"\bwhere\s+does\s+\S.*\bfit\b"
        r"|\bwho\s+does\s+\S.*\breports?\s+to\b"
        r"|\bwho\s+reports?\s+to\s+\S",
        re.IGNORECASE)),
    # to-do queries (read-only). Broadened (STAB-1 B4) so date questions never
    # fall to the LLM router: bare "todos", any "what's/what are … to-do(s)", and
    # any to-do(s) token paired with a timeframe (today/tomorrow/this week/next
    # week). The token allows "todo(s)", "to-do(s)", and "to dos" (space only when
    # PLURAL) — so ordinary prose "to do the thing" / "what to do about X" (space +
    # singular verb) never misfires.
    ("todos", re.compile(
        r"^(?:todos?|to-dos?|to\s+dos)\b"
        r"|what(?:'?s| are| is)\b[^\n]*\b(?:todos?|to-dos?|to\s+dos)\b"
        r"|\b(?:todos?|to-dos?|to\s+dos)\b[^\n]*\b(?:today|tomorrow|this\s+week|next\s+week)\b"
        r"|\b(?:today|tomorrow|this\s+week|next\s+week)(?:'s)?\s+(?:todos?|to-dos?|to\s+dos)\b",
        re.IGNORECASE)),
]


def detect_dossier_intent(text: str) -> str | None:
    """Return a PB-010 route tag for `text`, or None if no dossier trigger fires.

    Deterministic and side-effect-free. A positive result is authoritative — the
    caller must NOT hand the message to route_intent() (the LLM classifier) after
    a hit, which is what makes the routing unoverridable.
    """
    t = (text or "").strip()
    if not t:
        return None
    for tag, pattern in _DOSSIER_PATTERNS:
        if pattern.search(t):
            return tag
    return None


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
