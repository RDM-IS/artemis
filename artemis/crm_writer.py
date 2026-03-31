"""CRM writer — persist extracted contacts to public schema + acos knowledge graph."""

import logging
from dataclasses import dataclass, field

from knowledge.db import (
    create_relationship,
    execute_one,
    execute_query,
    execute_write,
    upsert_entity,
)

logger = logging.getLogger(__name__)


@dataclass
class WriteResult:
    contacts_added: int = 0
    contacts_updated: int = 0
    entities_created: int = 0
    relationships_created: int = 0
    details: list[str] = field(default_factory=list)
    summary: str = ""


def _find_contact_by_email(email: str) -> dict | None:
    """Find a public.contacts row by email (case-insensitive)."""
    return execute_one(
        "SELECT * FROM public.contacts WHERE LOWER(email) = LOWER(%s)",
        (email,),
    )


def _find_contact_by_name_company(name: str, company: str | None) -> dict | None:
    """Find a public.contacts row by name + company (fuzzy)."""
    if company:
        return execute_one(
            """SELECT c.* FROM public.contacts c
               JOIN public.organizations o ON c.org_id = o.id
               WHERE LOWER(c.name) = LOWER(%s) AND LOWER(o.name) = LOWER(%s)""",
            (name, company),
        )
    return execute_one(
        "SELECT * FROM public.contacts WHERE LOWER(name) = LOWER(%s)",
        (name,),
    )


def _find_or_create_org(company_name: str) -> str | None:
    """Find or create a public.organizations row. Returns org UUID."""
    existing = execute_one(
        "SELECT id FROM public.organizations WHERE LOWER(name) = LOWER(%s)",
        (company_name,),
    )
    if existing:
        return str(existing["id"])

    result = execute_write(
        """INSERT INTO public.organizations (name, type, notes)
           VALUES (%s, 'prospect', 'Auto-created by Artemis contact import')
           RETURNING id""",
        (company_name,),
    )
    return str(result["id"]) if result else None


def _find_entity_by_name(name: str, entity_type: str = "Person") -> dict | None:
    """Find an acos.entities row by exact name match."""
    return execute_one(
        "SELECT * FROM acos.entities WHERE LOWER(name) = LOWER(%s) AND entity_type = %s",
        (name, entity_type),
    )


def _find_entity_by_name_fuzzy(name: str) -> dict | None:
    """Fuzzy match: find entity where name contains the search term or vice versa."""
    return execute_one(
        """SELECT * FROM acos.entities
           WHERE LOWER(name) LIKE '%%' || LOWER(%s) || '%%'
              OR LOWER(%s) LIKE '%%' || LOWER(name) || '%%'
           ORDER BY confidence DESC
           LIMIT 1""",
        (name, name),
    )


def write_contacts(
    contacts: list,  # list[ExtractedContact] — avoiding circular import
    pipeline_context: str | None = None,
    ryan_context: str = "",
) -> WriteResult:
    """Write extracted contacts to public.contacts + acos knowledge graph.

    For each contact:
    1. UPSERT public.contacts (match on email or name+company)
    2. CREATE acos.entity (Person, silver layer)
    3. CREATE acos.entity (Organization, if company provided)
    4. CREATE acos.relationship (Person → Organization: Works-at)
    5. CREATE relationships for relationship_to_others tuples

    Returns a WriteResult with counts and human-readable summary.
    """
    result = WriteResult()

    for contact in contacts:
        try:
            _process_one_contact(contact, pipeline_context, ryan_context, result)
        except Exception:
            logger.exception("Failed to write contact: %s", contact.name)
            result.details.append(f"Error writing {contact.name}")

    # Build summary
    parts = []
    if result.contacts_added:
        parts.append(f"Added {result.contacts_added} contact{'s' if result.contacts_added != 1 else ''}")
    if result.contacts_updated:
        parts.append(f"Updated {result.contacts_updated}")
    if result.entities_created:
        parts.append(f"{result.entities_created} entities created")
    if result.relationships_created:
        parts.append(f"{result.relationships_created} relationships mapped")

    if result.details:
        detail_lines = "\n".join(f"  - {d}" for d in result.details[:10])
        result.summary = ". ".join(parts) + f".\n{detail_lines}"
    else:
        result.summary = ". ".join(parts) + "." if parts else "No contacts to import."

    logger.info(
        "CRM write complete: added=%d, updated=%d, entities=%d, rels=%d",
        result.contacts_added, result.contacts_updated,
        result.entities_created, result.relationships_created,
    )
    return result


def _process_one_contact(contact, pipeline_context, ryan_context, result):
    """Process a single ExtractedContact into CRM + knowledge graph."""

    # ── Step 1: UPSERT public.contacts ──
    existing = None
    if contact.email:
        existing = _find_contact_by_email(contact.email)
    if not existing:
        existing = _find_contact_by_name_company(contact.name, contact.company)

    org_id = None
    if contact.company:
        org_id = _find_or_create_org(contact.company)

    if existing:
        # Update fields that are currently null
        updates = {}
        if not existing.get("title") and contact.title:
            updates["title"] = contact.title
        if not existing.get("phone") and contact.phone:
            updates["phone"] = contact.phone
        if not existing.get("email") and contact.email:
            updates["email"] = contact.email
        if not existing.get("org_id") and org_id:
            updates["org_id"] = org_id
        if not existing.get("notes") and contact.notes:
            updates["notes"] = contact.notes

        if updates:
            set_clause = ", ".join(f"{k} = %s" for k in updates)
            vals = list(updates.values()) + [existing["id"]]
            execute_write(
                f"UPDATE public.contacts SET {set_clause} WHERE id = %s",
                tuple(vals),
            )
            result.contacts_updated += 1
            result.details.append(f"{contact.name} updated ({', '.join(updates.keys())})")

        contact_id = str(existing["id"])
    else:
        row = execute_write(
            """INSERT INTO public.contacts (name, title, email, phone, org_id, notes)
               VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (contact.name, contact.title, contact.email,
             contact.phone, org_id, contact.notes),
        )
        contact_id = str(row["id"]) if row else None
        result.contacts_added += 1
        result.details.append(
            f"{contact.name} added"
            + (f" ({contact.title} at {contact.company})" if contact.title and contact.company else "")
        )

    # ── Step 2: CREATE acos.entity (Person) ──
    person_entity = _find_entity_by_name(contact.name, "Person")
    if not person_entity:
        content_parts = []
        if contact.title and contact.company:
            content_parts.append(f"{contact.title} at {contact.company}")
        if contact.notes:
            content_parts.append(contact.notes)

        person_entity_id = upsert_entity(
            entity_type="Person",
            name=contact.name,
            domain="rdmis",
            content=". ".join(content_parts) if content_parts else None,
            confidence=0.8,
            layer="silver",
            tags=["imported"],
            crm_contact_id=contact_id,
        )
        result.entities_created += 1
    else:
        person_entity_id = str(person_entity["id"])

    # ── Step 3: FIND OR CREATE org entity in acos.entities ──
    org_entity_id = None
    if contact.company:
        org_entity = _find_entity_by_name(contact.company, "Organization")
        if not org_entity:
            org_entity_id = upsert_entity(
                entity_type="Organization",
                name=contact.company,
                domain="rdmis",
                confidence=0.8,
                layer="silver",
            )
            result.entities_created += 1
        else:
            org_entity_id = str(org_entity["id"])

    # ── Step 4: CREATE relationship Person → Organization ──
    if person_entity_id and org_entity_id:
        try:
            create_relationship(
                source_id=person_entity_id,
                target_id=org_entity_id,
                rel_type="Works-at",
                context=f"{contact.name} is {contact.title or 'employee'} at {contact.company}",
                confidence=0.8,
                layer="silver",
            )
            result.relationships_created += 1
            result.details.append(f"{contact.name} \u2192 {contact.company} (Works-at)")
        except Exception:
            logger.debug("Relationship %s->%s may already exist", contact.name, contact.company)

    # ── Step 5: Process relationship_to_others ──
    for rel_type, other_name in contact.relationship_to_others:
        other_entity = _find_entity_by_name(other_name) or _find_entity_by_name_fuzzy(other_name)
        if other_entity and person_entity_id:
            try:
                create_relationship(
                    source_id=person_entity_id,
                    target_id=str(other_entity["id"]),
                    rel_type=rel_type,
                    context=f"{contact.name} {rel_type.lower()} {other_name} (from {contact.source_description})",
                    confidence=0.7,
                    layer="silver",
                )
                result.relationships_created += 1
                result.details.append(f"{contact.name} \u2192 {other_name} ({rel_type})")
            except Exception:
                logger.debug("Relationship %s->%s failed", contact.name, other_name)
        else:
            logger.debug(
                "Could not find entity for relationship target: %s", other_name
            )

    # ── Step 6: Pipeline context — log interaction if deal found ──
    if pipeline_context and contact.company:
        deal = execute_one(
            """SELECT d.id FROM public.deals d
               JOIN public.organizations o ON d.org_id = o.id
               WHERE LOWER(o.name) = LOWER(%s)
               LIMIT 1""",
            (contact.company,),
        )
        if deal and contact_id:
            execute_write(
                """INSERT INTO public.interactions
                   (contact_id, deal_id, type, date, summary, logged_by)
                   VALUES (%s, %s, %s, now(), %s, %s)""",
                (
                    contact_id,
                    str(deal["id"]),
                    "contact_import",
                    f"Contact imported: {contact.name}. Context: {ryan_context[:300]}",
                    "artemis",
                ),
            )
            result.details.append(
                f"{contact.name} linked to {contact.company} pipeline"
            )
