"""Interaction logger — extract structured data from debrief messages and persist."""

import hashlib
import json
import logging
import re

import anthropic

from artemis.commitments import log_claude_call
from knowledge.db import execute_one, execute_query, execute_write
from knowledge.secrets import get_anthropic_key

logger = logging.getLogger(__name__)

_EXTRACT_SYSTEM = (
    "Extract interaction details from this message. The user (Ryan) is debriefing "
    "after a call, meeting, or conversation. Return ONLY valid JSON:\n"
    "{\n"
    '  "contact_names": ["list of people mentioned"],\n'
    '  "org_names": ["list of companies mentioned"],\n'
    '  "interaction_type": "call" | "meeting" | "email" | "linkedin" | "text" | "other",\n'
    '  "key_facts": ["list of important facts learned"],\n'
    '  "commitments_made": ["things Ryan committed to do"],\n'
    '  "commitments_received": ["things the other party committed to do"],\n'
    '  "deal_stage_update": null or "new stage name",\n'
    '  "next_action": null or "description of next step",\n'
    '  "next_action_date": null or "YYYY-MM-DD",\n'
    '  "sentiment": "positive" | "neutral" | "negative" | "unknown"\n'
    "}\n"
    "Infer interaction_type from context clues (phone, Zoom, LinkedIn DM, etc). "
    "Be specific about commitments — include deadlines if mentioned. "
    "For deal_stage_update, only set if the message clearly indicates the deal "
    "should move to a new stage (e.g. 'meeting scheduled', 'proposal sent')."
)


def log_interaction(message: str, entities: list[str]) -> str:
    """Extract structured interaction data and persist to CRM.

    1. Call Claude to extract structured data
    2. Upsert interactions for each contact found
    3. Log commitments made
    4. Update deal stage if indicated
    5. Store next action
    6. Store full extraction as satellite

    Returns formatted confirmation string.
    """
    # 1. Call Claude for extraction
    extraction = _extract_interaction(message)
    if not extraction:
        return "\u26a0\ufe0f Couldn't parse that interaction. Try rephrasing with the person's name and what happened."

    # Merge entities from intent router + extraction
    all_contact_names = list(dict.fromkeys(
        entities + extraction.get("contact_names", [])
    ))
    all_org_names = extraction.get("org_names", [])
    interaction_type = extraction.get("interaction_type", "other")
    key_facts = extraction.get("key_facts", [])
    commitments_made = extraction.get("commitments_made", [])
    commitments_received = extraction.get("commitments_received", [])
    deal_stage_update = extraction.get("deal_stage_update")
    next_action = extraction.get("next_action")
    next_action_date = extraction.get("next_action_date")
    sentiment = extraction.get("sentiment", "unknown")

    logged_contacts = []
    logged_commitments = []
    deal_updated = None

    # 2. For each contact: find and log interaction
    for contact_name in all_contact_names:
        contact = execute_one(
            """SELECT c.id, c.name, c.org_id, o.name AS org_name
               FROM public.contacts c
               LEFT JOIN public.organizations o ON c.org_id = o.id
               WHERE LOWER(c.name) LIKE '%%' || LOWER(%s) || '%%'
               LIMIT 1""",
            (contact_name,),
        )
        if not contact:
            logger.debug("Contact not found: %s", contact_name)
            continue

        contact_id = str(contact["id"])
        org_name = contact.get("org_name", "")

        # Find deal for this contact's org
        deal_id = None
        if contact.get("org_id"):
            deal = execute_one(
                "SELECT id, name, gate, stage FROM public.deals WHERE org_id = %s LIMIT 1",
                (str(contact["org_id"]),),
            )
            if deal:
                deal_id = str(deal["id"])

        # Insert interaction
        execute_write(
            """INSERT INTO public.interactions
               (contact_id, deal_id, type, date, summary, logged_by)
               VALUES (%s, %s, %s, NOW(), %s, %s)""",
            (contact_id, deal_id, interaction_type, message[:500], "artemis"),
        )

        # Update last_contacted
        execute_write(
            "UPDATE public.contacts SET last_contacted = NOW() WHERE id = %s",
            (contact_id,),
        )

        logged_contacts.append((contact["name"], org_name))

        # 3. Log commitments
        for commitment in commitments_made:
            execute_write(
                """INSERT INTO public.commitments
                   (contact_id, deal_id, description, due_date, status)
                   VALUES (%s, %s, %s, %s, 'open')""",
                (contact_id, deal_id, commitment,
                 next_action_date if next_action_date else None),
            )
            logged_commitments.append(f"Ryan: {commitment}")

        for commitment in commitments_received:
            execute_write(
                """INSERT INTO public.commitments
                   (contact_id, deal_id, description, status)
                   VALUES (%s, %s, %s, 'open')""",
                (contact_id, deal_id, f"[{contact['name']}] {commitment}"),
            )
            logged_commitments.append(f"{contact['name']}: {commitment}")

        # 4. Deal stage update
        if deal_stage_update and deal_id and not deal_updated:
            execute_write(
                """UPDATE public.deals
                   SET stage = %s, updated_at = NOW()
                   WHERE id = %s""",
                (deal_stage_update, deal_id),
            )
            # Log pipeline event
            try:
                execute_write(
                    """INSERT INTO acos.pipeline_events
                       (deal_id, from_stage, to_stage, note, triggered_by)
                       VALUES (%s, %s, %s, %s, 'artemis')""",
                    (deal_id, deal.get("stage", ""),
                     deal_stage_update, f"Via interaction log: {message[:200]}"),
                )
            except Exception:
                logger.debug("Pipeline event logging failed", exc_info=True)

            deal_updated = f"{org_name or contact['name']} \u2192 {deal_stage_update}"

    # 5. Store next action as satellite
    if next_action and all_contact_names:
        # Find entity for the primary contact
        primary_entity = execute_one(
            """SELECT id FROM acos.entities
               WHERE LOWER(name) LIKE '%%' || LOWER(%s) || '%%'
                 AND entity_type IN ('Person', 'Organization')
               ORDER BY confidence DESC LIMIT 1""",
            (all_contact_names[0],),
        )
        if primary_entity:
            action_content = json.dumps({
                "action": next_action,
                "date": next_action_date,
                "account": all_org_names[0] if all_org_names else None,
                "contact": all_contact_names[0],
            })
            execute_write(
                """INSERT INTO acos.data_vault_satellites
                   (entity_id, satellite_type, content, layer, metadata)
                   VALUES (%s, 'next_action', %s, 'gold', '{}')""",
                (str(primary_entity["id"]), action_content),
            )

    # 6. Store full extraction as satellite
    if all_contact_names:
        primary_entity = execute_one(
            """SELECT id FROM acos.entities
               WHERE LOWER(name) LIKE '%%' || LOWER(%s) || '%%'
                 AND entity_type IN ('Person', 'Organization')
               ORDER BY confidence DESC LIMIT 1""",
            (all_contact_names[0],),
        )
        if primary_entity:
            execute_write(
                """INSERT INTO acos.data_vault_satellites
                   (entity_id, satellite_type, content, layer, metadata)
                   VALUES (%s, 'interaction_extract', %s, 'gold', '{}')""",
                (str(primary_entity["id"]), json.dumps(extraction)),
            )

    # Build confirmation
    return _format_confirmation(
        logged_contacts, interaction_type, key_facts,
        logged_commitments, deal_updated, next_action, next_action_date,
    )


def _extract_interaction(message: str) -> dict | None:
    """Call Claude to extract structured interaction data."""
    try:
        client = anthropic.Anthropic(api_key=get_anthropic_key())
        prompt_hash = hashlib.sha256(
            (_EXTRACT_SYSTEM + message).encode()
        ).hexdigest()[:16]

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            system=_EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": message}],
        )
        raw = response.content[0].text.strip()
        log_claude_call("claude-haiku-4-5-20251001", prompt_hash, len(raw))

        # Strip markdown fences
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"^```\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        raw = raw.strip()

        return json.loads(raw)
    except Exception:
        logger.exception("Interaction extraction failed")
        return None


def _format_confirmation(
    contacts: list[tuple[str, str]],
    interaction_type: str,
    key_facts: list[str],
    commitments: list[str],
    deal_updated: str | None,
    next_action: str | None,
    next_action_date: str | None,
) -> str:
    """Format the confirmation message for Mattermost."""
    from datetime import date

    if not contacts:
        return "\u26a0\ufe0f Logged the interaction but couldn't match any contacts in the CRM."

    # Header
    names = ", ".join(f"{c[0]} ({c[1]})" if c[1] else c[0] for c in contacts)
    today = date.today().strftime("%Y-%m-%d")
    lines = [
        f"\u2705 **Logged interaction** with {names}",
        f"Type: {interaction_type.title()} \u00b7 {today}",
    ]

    # Key facts
    if key_facts:
        lines.append("")
        lines.append("**Key facts noted:**")
        for fact in key_facts[:5]:
            lines.append(f"  \u00b7 {fact}")

    # Commitments
    if commitments:
        lines.append("")
        lines.append("**Commitments logged:**")
        for c in commitments[:5]:
            lines.append(f"  \u00b7 {c}")

    # Deal update
    if deal_updated:
        lines.append("")
        lines.append(f"**Deal updated:** {deal_updated}")

    # Next action
    if next_action:
        date_str = f" \u00b7 due {next_action_date}" if next_action_date else ""
        lines.append("")
        lines.append(f"**Next action:** {next_action}{date_str}")

    return "\n".join(lines)
