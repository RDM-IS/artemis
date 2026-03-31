"""Rich CRM queries — deep contact and account lookups across all data sources."""

import logging

from knowledge.db import execute_one, execute_query

logger = logging.getLogger(__name__)


def query_contact(entities: list[str]) -> str:
    """Deep CRM lookup for person names.

    For each entity, queries contacts, acos entities, relationships,
    data_vault_satellites, interactions, deals, and commitments.
    Returns formatted plain-text summary.
    """
    parts = []

    for entity_name in entities:
        section = _query_one_contact(entity_name)
        if section:
            parts.append(section)

    if not parts:
        names = ", ".join(entities)
        return (
            f"\U0001f50d No contacts found for: {names}\n\n"
            f"_This person isn't in the CRM yet. "
            f"To add them, say: `@artemis add contact {entities[0] if entities else 'Name'}`_"
        )

    return "\n\n---\n\n".join(parts)


def _query_one_contact(name: str) -> str | None:
    """Build a rich profile for a single contact name."""
    lines = []

    # 1. public.contacts + organization
    contact = execute_one(
        """SELECT c.id, c.name, c.title, c.email, c.phone, c.notes,
                  c.last_contacted, o.name AS org_name, o.id AS org_id
           FROM public.contacts c
           LEFT JOIN public.organizations o ON c.org_id = o.id
           WHERE LOWER(c.name) LIKE '%%' || LOWER(%s) || '%%'
           LIMIT 1""",
        (name,),
    )

    # 2. acos.entities
    entity = execute_one(
        """SELECT id, name, entity_type, content, layer, tags, confidence
           FROM acos.entities
           WHERE LOWER(name) LIKE '%%' || LOWER(%s) || '%%'
             AND entity_type = 'Person'
           ORDER BY confidence DESC
           LIMIT 1""",
        (name,),
    )

    if not contact and not entity:
        return None

    # Header
    display_name = (contact["name"] if contact else entity["name"]).upper()
    org_name = contact.get("org_name", "") if contact else ""
    org_line = f" \u2014 {org_name}" if org_name else ""
    lines.append(f"\U0001f464 **{display_name}**{org_line}")

    if contact:
        if contact.get("title"):
            lines.append(f"Title: {contact['title']}")
        if contact.get("email"):
            lines.append(f"Email: {contact['email']}")
        if contact.get("phone"):
            lines.append(f"Phone: {contact['phone']}")
        last = contact.get("last_contacted")
        lines.append(f"Last contact: {last.strftime('%Y-%m-%d') if last else 'never'}")

    contact_id = str(contact["id"]) if contact else None
    entity_id = str(entity["id"]) if entity else None
    org_id = str(contact["org_id"]) if contact and contact.get("org_id") else None

    # 3. Deals
    if org_id:
        deals = execute_query(
            """SELECT d.name, d.gate, d.stage, d.value, d.notes
               FROM public.deals d
               WHERE d.org_id = %s
               ORDER BY d.updated_at DESC NULLS LAST
               LIMIT 3""",
            (org_id,),
        )
        if deals:
            lines.append("")
            lines.append("**PIPELINE:**")
            for d in deals:
                val = f" \u00b7 ${d['value']:,.0f}" if d.get("value") else ""
                lines.append(f"  {d['name']} \u00b7 Gate {d['gate']} \u00b7 {d['stage'] or 'N/A'}{val}")
                if d.get("notes"):
                    lines.append(f"  _{d['notes'][:200]}_")

    # 4. Relationships
    search_id = entity_id
    if not search_id and contact:
        # Try to find entity by crm_contact_id
        ent = execute_one(
            "SELECT id FROM acos.entities WHERE crm_contact_id = %s LIMIT 1",
            (contact_id,),
        )
        if ent:
            search_id = str(ent["id"])

    if search_id:
        rels = execute_query(
            """SELECT r.relationship_type, r.relationship_context,
                      s.name AS source_name, t.name AS target_name
               FROM acos.relationships r
               JOIN acos.entities s ON r.source_entity_id = s.id
               JOIN acos.entities t ON r.target_entity_id = t.id
               WHERE r.source_entity_id = %s OR r.target_entity_id = %s
               LIMIT 10""",
            (search_id, search_id),
        )
        if rels:
            lines.append("")
            lines.append("**RELATIONSHIPS:**")
            for r in rels:
                lines.append(f"  \u2192 {r['source_name']} **{r['relationship_type']}** {r['target_name']}")

    # 5. Sales context from data_vault_satellites
    if search_id:
        satellites = execute_query(
            """SELECT satellite_type, content, created_at
               FROM acos.data_vault_satellites
               WHERE entity_id = %s
                 AND satellite_type IN ('sales_context', 'business_context')
               ORDER BY created_at DESC
               LIMIT 5""",
            (search_id,),
        )
        if satellites:
            lines.append("")
            lines.append("**SALES CONTEXT:**")
            for s in satellites:
                content = s["content"][:300] if s.get("content") else ""
                lines.append(f"  \u00b7 {content}")

    # Also check org entity satellites if we have an org
    if org_id:
        org_ent = execute_one(
            """SELECT id FROM acos.entities
               WHERE LOWER(name) = LOWER(%s) AND entity_type = 'Organization'
               LIMIT 1""",
            (org_name,),
        ) if org_name else None
        if org_ent:
            org_sats = execute_query(
                """SELECT content FROM acos.data_vault_satellites
                   WHERE entity_id = %s AND satellite_type = 'sales_context'
                   ORDER BY created_at DESC LIMIT 5""",
                (str(org_ent["id"]),),
            )
            if org_sats:
                if not any("SALES CONTEXT" in l for l in lines):
                    lines.append("")
                    lines.append("**SALES CONTEXT:**")
                for s in org_sats:
                    lines.append(f"  \u00b7 {s['content'][:300]}")

    # 6. Recent interactions
    if contact_id:
        interactions = execute_query(
            """SELECT type, date, summary
               FROM public.interactions
               WHERE contact_id = %s
               ORDER BY date DESC
               LIMIT 3""",
            (contact_id,),
        )
        if interactions:
            lines.append("")
            lines.append("**RECENT INTERACTIONS:**")
            for ix in interactions:
                dt = ix["date"].strftime("%m/%d") if ix.get("date") else "?"
                summary = ix.get("summary", "")[:120]
                lines.append(f"  {dt} [{ix.get('type', '?')}] {summary}")

    # 7. Open commitments
    if contact_id:
        commitments = execute_query(
            """SELECT description, due_date, status
               FROM public.commitments
               WHERE contact_id = %s AND status = 'open'
               ORDER BY due_date ASC NULLS LAST
               LIMIT 5""",
            (contact_id,),
        )
        if commitments:
            lines.append("")
            lines.append("**OPEN COMMITMENTS:**")
            for c in commitments:
                due = f" (due {c['due_date'].strftime('%m/%d')})" if c.get("due_date") else ""
                lines.append(f"  \u00b7 {c['description'][:150]}{due}")

    # 8. Next action from satellites
    if search_id:
        next_action = execute_one(
            """SELECT content FROM acos.data_vault_satellites
               WHERE entity_id = %s AND satellite_type = 'next_action'
               ORDER BY created_at DESC LIMIT 1""",
            (search_id,),
        )
        lines.append("")
        if next_action:
            lines.append(f"**NEXT ACTION:** {next_action['content'][:200]}")
        else:
            lines.append("**NEXT ACTION:** None set")

    return "\n".join(lines)


def query_account(org_name: str) -> str:
    """Deep CRM lookup by organization name.

    Queries org, all contacts, deals, acos entities, satellites,
    and recent interactions across all contacts at the org.
    """
    # 1. Find organization
    org = execute_one(
        """SELECT id, name, type, industry, notes
           FROM public.organizations
           WHERE LOWER(name) LIKE '%%' || LOWER(%s) || '%%'
           LIMIT 1""",
        (org_name,),
    )
    if not org:
        return (
            f"\U0001f3e2 No organization found matching \"{org_name}\"\n\n"
            f"_Not in the CRM yet. To add: `@artemis add contact [Name] at {org_name}`_"
        )

    org_id = str(org["id"])
    lines = [f"\U0001f3e2 **{org['name'].upper()}**"]

    # 2. Deal record
    deals = execute_query(
        """SELECT id, name, gate, stage, value, notes, updated_at
           FROM public.deals
           WHERE org_id = %s
           ORDER BY updated_at DESC NULLS LAST""",
        (org_id,),
    )
    if deals:
        d = deals[0]
        tier_info = ""
        if d.get("notes"):
            for token in d["notes"].split("."):
                t = token.strip().lower()
                if "tier" in t:
                    tier_info = token.strip() + " \u00b7 "
                    break
        val = f" \u00b7 ${d['value']:,.0f}" if d.get("value") else ""
        lines.append(f"{tier_info}Gate {d['gate']} \u00b7 {d['stage'] or 'N/A'}{val}")
    if org.get("industry"):
        lines.append(f"Industry: {org['industry']}")

    # 3. All contacts at org
    contacts = execute_query(
        """SELECT id, name, title, email, last_contacted
           FROM public.contacts
           WHERE org_id = %s
           ORDER BY name""",
        (org_id,),
    )
    if contacts:
        lines.append("")
        lines.append(f"**CONTACTS ({len(contacts)}):**")
        for c in contacts:
            title = f" \u2014 {c['title']}" if c.get("title") else ""
            lines.append(f"  \u00b7 {c['name']}{title}")

    # 4. Org entity + sales context satellites
    org_entity = execute_one(
        """SELECT id FROM acos.entities
           WHERE LOWER(name) LIKE '%%' || LOWER(%s) || '%%'
             AND entity_type = 'Organization'
           ORDER BY confidence DESC LIMIT 1""",
        (org_name,),
    )
    if org_entity:
        sats = execute_query(
            """SELECT content, created_at FROM acos.data_vault_satellites
               WHERE entity_id = %s AND satellite_type = 'sales_context'
               ORDER BY created_at DESC LIMIT 10""",
            (str(org_entity["id"]),),
        )
        if sats:
            lines.append("")
            lines.append("**SALES CONTEXT:**")
            for s in sats:
                lines.append(f"  \u00b7 {s['content'][:300]}")

        # Next action
        next_action = execute_one(
            """SELECT content FROM acos.data_vault_satellites
               WHERE entity_id = %s AND satellite_type = 'next_action'
               ORDER BY created_at DESC LIMIT 1""",
            (str(org_entity["id"]),),
        )
        if next_action:
            lines.append("")
            lines.append(f"**NEXT ACTION:** {next_action['content'][:200]}")

    # 5. Recent interactions across all contacts at org
    if contacts:
        contact_ids = [str(c["id"]) for c in contacts]
        placeholders = ", ".join(["%s"] * len(contact_ids))
        interactions = execute_query(
            f"""SELECT i.type, i.date, i.summary, c.name AS contact_name
               FROM public.interactions i
               JOIN public.contacts c ON i.contact_id = c.id
               WHERE i.contact_id IN ({placeholders})
               ORDER BY i.date DESC
               LIMIT 5""",
            tuple(contact_ids),
        )
        if interactions:
            lines.append("")
            lines.append("**RECENT INTERACTIONS:**")
            for ix in interactions:
                dt = ix["date"].strftime("%m/%d") if ix.get("date") else "?"
                lines.append(
                    f"  {dt} [{ix.get('type', '?')}] {ix.get('contact_name', '?')}: "
                    f"{ix.get('summary', '')[:100]}"
                )

    # 6. Open commitments across all contacts
    if contacts:
        contact_ids = [str(c["id"]) for c in contacts]
        placeholders = ", ".join(["%s"] * len(contact_ids))
        commitments = execute_query(
            f"""SELECT c.description, c.due_date, ct.name AS contact_name
               FROM public.commitments c
               JOIN public.contacts ct ON c.contact_id = ct.id
               WHERE c.contact_id IN ({placeholders})
                 AND c.status = 'open'
               ORDER BY c.due_date ASC NULLS LAST
               LIMIT 5""",
            tuple(contact_ids),
        )
        if commitments:
            lines.append("")
            lines.append("**OPEN COMMITMENTS:**")
            for cm in commitments:
                due = f" (due {cm['due_date'].strftime('%m/%d')})" if cm.get("due_date") else ""
                lines.append(f"  \u00b7 {cm['contact_name']}: {cm['description'][:150]}{due}")

    return "\n".join(lines)
