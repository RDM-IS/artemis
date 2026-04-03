"""CRM Write Guard — single entry point for all CRM writes with fuzzy dedup.

Matches incoming entities against existing records using exact (email, domain)
and fuzzy (Levenshtein distance ≤ 2) matching.  High-confidence matches are
auto-merged; low-confidence or ambiguous matches are flagged for human review
via Mattermost and Gmail labels.

Tables written:
    public.persons, public.companies, public.relationships,
    public.engagements, public.touch_events, acos.pending_crm_writes
"""

import json
import logging

from Levenshtein import distance as lev_distance

from artemis import config
from knowledge.db import execute_one, execute_query, execute_write

logger = logging.getLogger(__name__)

_FUZZY_THRESHOLD = 2  # Levenshtein distance ≤ 2 = fuzzy match


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def crm_write_guard(
    entity_type: str,
    data: dict,
    confidence: str,
    source_pb: str,
    gmail_message_id: str = None,
    gmail_client=None,
    mm_client=None,
) -> dict:
    """Single entry point for all CRM writes.

    Args:
        entity_type: "person"|"company"|"relationship"|"engagement"|"touch_event"
        data: Entity fields to write.
        confidence: "high"|"low" — controls auto-merge vs flag behavior.
        source_pb: Playbook identifier (e.g. "PB-007").
        gmail_message_id: Optional Gmail message ID for label application.
        gmail_client: Optional GmailClient instance for labelling.
        mm_client: Optional MattermostClient instance for notifications.

    Returns:
        {"status": "written"|"exists"|"flagged",
         "entity_id": UUID|None,
         "flag_reason": str|None}
    """
    try:
        handlers = {
            "company": _guard_company,
            "person": _guard_person,
            "relationship": _guard_relationship,
            "engagement": _guard_engagement,
            "touch_event": _guard_touch_event,
        }
        handler = handlers.get(entity_type)
        if not handler:
            return {"status": "flagged", "entity_id": None,
                    "flag_reason": f"Unknown entity_type: {entity_type}"}

        result = handler(data, confidence, source_pb, gmail_message_id,
                         gmail_client, mm_client)

        # Post confirmation to #artemis-ryan for all successful writes
        if mm_client and result["status"] == "written":
            _post_write_confirmation(mm_client, entity_type, data, result, source_pb)

        return result
    except Exception:
        logger.exception("CRM write guard failed for %s (source=%s)", entity_type, source_pb)
        return {"status": "flagged", "entity_id": None,
                "flag_reason": "Internal error — check logs"}


# ---------------------------------------------------------------------------
# Company guard
# ---------------------------------------------------------------------------


def _guard_company(data, confidence, source_pb, gmail_message_id,
                   gmail_client, mm_client):
    name = data.get("name", "").strip()
    domain = data.get("domain", "").strip().lower() if data.get("domain") else None

    # Step 1: Exact domain match
    if domain:
        existing = execute_one(
            "SELECT * FROM public.companies WHERE LOWER(domain) = LOWER(%s)",
            (domain,),
        )
        if existing:
            return {"status": "exists", "entity_id": str(existing["company_id"]),
                    "flag_reason": None}

    # Step 2: Fuzzy name match
    if name:
        candidates = _fuzzy_match_companies(name)
        if candidates:
            best = candidates[0]
            if confidence == "high":
                # Auto-merge: update name_variants if needed
                _merge_company_variants(best["company_id"], name)
                return {"status": "exists", "entity_id": str(best["company_id"]),
                        "flag_reason": None}
            else:
                return _flag_for_review(
                    "company", data, candidates, source_pb,
                    gmail_message_id, gmail_client, mm_client,
                    f"Fuzzy name match (low confidence): '{name}' ≈ '{best['name']}'",
                )

    # Step 3: No match — create new company
    row = execute_write(
        """INSERT INTO public.companies
           (name, domain, types, industry, hq_location, website, linkedin_url, notes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING company_id""",
        (
            name,
            domain,
            data.get("types"),
            data.get("industry"),
            data.get("hq_location"),
            data.get("website"),
            data.get("linkedin_url"),
            data.get("notes"),
        ),
    )
    entity_id = str(row["company_id"]) if row else None
    logger.info("CRM write guard: created company '%s' (id=%s, source=%s)",
                name, entity_id, source_pb)
    return {"status": "written", "entity_id": entity_id, "flag_reason": None}


def _fuzzy_match_companies(name: str) -> list[dict]:
    """Find companies within Levenshtein distance ≤ 2 of the given name."""
    rows = execute_query(
        "SELECT company_id, name, name_variants, domain FROM public.companies "
        "ORDER BY created_at DESC LIMIT 500"
    )
    matches = []
    name_lower = name.lower()
    for row in rows:
        dist = lev_distance(name_lower, row["name"].lower())
        if dist <= _FUZZY_THRESHOLD:
            matches.append(dict(row) | {"_distance": dist})
            continue
        # Also check name_variants
        for variant in (row.get("name_variants") or []):
            if lev_distance(name_lower, variant.lower()) <= _FUZZY_THRESHOLD:
                matches.append(dict(row) | {"_distance": dist})
                break
    matches.sort(key=lambda m: m["_distance"])
    return matches


def _merge_company_variants(company_id, new_name: str):
    """Add a name to company's name_variants if not already present."""
    existing = execute_one(
        "SELECT name, name_variants FROM public.companies WHERE company_id = %s",
        (company_id,),
    )
    if not existing:
        return
    current_name = existing["name"]
    variants = existing.get("name_variants") or []
    if new_name.lower() != current_name.lower() and new_name not in variants:
        execute_write(
            "UPDATE public.companies SET name_variants = array_append(name_variants, %s), "
            "updated_at = now() WHERE company_id = %s",
            (new_name, company_id),
        )


# ---------------------------------------------------------------------------
# Person guard
# ---------------------------------------------------------------------------


def _guard_person(data, confidence, source_pb, gmail_message_id,
                  gmail_client, mm_client):
    name = data.get("name", "").strip()
    email_primary = data.get("email_primary", "").strip().lower() if data.get("email_primary") else None
    emails = data.get("emails") or []

    # Step 1: Exact email match (email_primary or any in emails[])
    if email_primary:
        existing = execute_one(
            "SELECT * FROM public.persons WHERE LOWER(email_primary) = LOWER(%s)",
            (email_primary,),
        )
        if existing:
            return {"status": "exists", "entity_id": str(existing["person_id"]),
                    "flag_reason": None}

    for email in emails:
        existing = execute_one(
            "SELECT * FROM public.persons WHERE LOWER(email_primary) = LOWER(%s) "
            "OR %s = ANY(emails)",
            (email.lower(), email.lower()),
        )
        if existing:
            return {"status": "exists", "entity_id": str(existing["person_id"]),
                    "flag_reason": None}

    # Step 2: Fuzzy name match
    if name:
        candidates = _fuzzy_match_persons(name)
        if candidates:
            best = candidates[0]
            # Check if same company domain
            same_company = _is_same_company(best["person_id"], data.get("company_domain"))
            if same_company:
                if confidence == "high":
                    _merge_person_variants(best["person_id"], name)
                    return {"status": "exists", "entity_id": str(best["person_id"]),
                            "flag_reason": None}
                else:
                    return _flag_for_review(
                        "person", data, candidates, source_pb,
                        gmail_message_id, gmail_client, mm_client,
                        f"Fuzzy name match, same company (low confidence): "
                        f"'{name}' ≈ '{best['name']}'",
                    )
            else:
                # Different company — ALWAYS flag (potential org change)
                return _flag_for_review(
                    "person", data, candidates, source_pb,
                    gmail_message_id, gmail_client, mm_client,
                    f"Fuzzy name match, DIFFERENT company — potential org change: "
                    f"'{name}' ≈ '{best['name']}'",
                )

    # Step 3: No match — create new person
    row = execute_write(
        """INSERT INTO public.persons
           (name, email_primary, emails, phone, linkedin_url,
            location, timezone, source, source_detail, notes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING person_id""",
        (
            name,
            email_primary,
            emails or None,
            data.get("phone"),
            data.get("linkedin_url"),
            data.get("location"),
            data.get("timezone"),
            data.get("source"),
            data.get("source_detail"),
            data.get("notes"),
        ),
    )
    entity_id = str(row["person_id"]) if row else None
    logger.info("CRM write guard: created person '%s' (id=%s, source=%s)",
                name, entity_id, source_pb)
    return {"status": "written", "entity_id": entity_id, "flag_reason": None}


def _fuzzy_match_persons(name: str) -> list[dict]:
    """Find persons within Levenshtein distance ≤ 2 of the given name."""
    rows = execute_query(
        "SELECT person_id, name, name_variants, email_primary FROM public.persons "
        "ORDER BY created_at DESC LIMIT 500"
    )
    matches = []
    name_lower = name.lower()
    for row in rows:
        dist = lev_distance(name_lower, row["name"].lower())
        if dist <= _FUZZY_THRESHOLD:
            matches.append(dict(row) | {"_distance": dist})
            continue
        for variant in (row.get("name_variants") or []):
            if lev_distance(name_lower, variant.lower()) <= _FUZZY_THRESHOLD:
                matches.append(dict(row) | {"_distance": dist})
                break
    matches.sort(key=lambda m: m["_distance"])
    return matches


def _is_same_company(person_id, company_domain: str | None) -> bool:
    """Check if a person has an active relationship with a company matching the domain."""
    if not company_domain:
        return False
    row = execute_one(
        """SELECT 1 FROM public.relationships r
           JOIN public.companies c ON r.company_id = c.company_id
           WHERE r.person_id = %s AND r.status = 'Active'
             AND LOWER(c.domain) = LOWER(%s)""",
        (person_id, company_domain),
    )
    return row is not None


def _merge_person_variants(person_id, new_name: str):
    """Add a name to person's name_variants if not already present."""
    existing = execute_one(
        "SELECT name, name_variants FROM public.persons WHERE person_id = %s",
        (person_id,),
    )
    if not existing:
        return
    current_name = existing["name"]
    variants = existing.get("name_variants") or []
    if new_name.lower() != current_name.lower() and new_name not in variants:
        execute_write(
            "UPDATE public.persons SET name_variants = array_append(name_variants, %s), "
            "updated_at = now() WHERE person_id = %s",
            (new_name, person_id),
        )


# ---------------------------------------------------------------------------
# Relationship guard
# ---------------------------------------------------------------------------


def _guard_relationship(data, confidence, source_pb, gmail_message_id,
                        gmail_client, mm_client):
    person_id = data.get("person_id")
    company_id = data.get("company_id")
    role = data.get("role")

    # Step 1: Find active relationship for person_id + company_id
    existing = execute_one(
        """SELECT * FROM public.relationships
           WHERE person_id = %s AND company_id = %s AND status = 'Active'""",
        (person_id, company_id),
    )

    if existing:
        if existing.get("role") == role:
            # Same role — no write needed
            return {"status": "exists",
                    "entity_id": str(existing["relationship_id"]),
                    "flag_reason": None}
        else:
            # Different role — end old record, create new
            execute_write(
                """UPDATE public.relationships
                   SET status = 'Ended', end_date = CURRENT_DATE, updated_at = now()
                   WHERE relationship_id = %s""",
                (existing["relationship_id"],),
            )

    # Create new relationship
    row = execute_write(
        """INSERT INTO public.relationships
           (person_id, company_id, role, title, status, is_primary,
            start_date, source, notes)
           VALUES (%s, %s, %s, %s, 'Active', %s, CURRENT_DATE, %s, %s)
           RETURNING relationship_id""",
        (
            person_id,
            company_id,
            role,
            data.get("title"),
            data.get("is_primary", False),
            data.get("source"),
            data.get("notes"),
        ),
    )
    entity_id = str(row["relationship_id"]) if row else None
    logger.info("CRM write guard: created relationship (person=%s, company=%s, source=%s)",
                person_id, company_id, source_pb)
    return {"status": "written", "entity_id": entity_id, "flag_reason": None}


# ---------------------------------------------------------------------------
# Engagement guard
# ---------------------------------------------------------------------------


def _guard_engagement(data, confidence, source_pb, gmail_message_id,
                      gmail_client, mm_client):
    company_id = data.get("company_id")
    eng_type = data.get("type")

    # Step 1: Find active engagement for company_id + type
    existing = execute_one(
        """SELECT * FROM public.engagements
           WHERE company_id = %s AND type = %s AND status = 'Active'""",
        (company_id, eng_type),
    )

    if existing:
        # Update gate/status only
        updates = {}
        if data.get("gate") is not None:
            updates["gate"] = data["gate"]
        if data.get("status"):
            updates["status"] = data["status"]
        if updates:
            set_clause = ", ".join(f"{k} = %s" for k in updates)
            vals = list(updates.values()) + [existing["engagement_id"]]
            execute_write(
                f"UPDATE public.engagements SET {set_clause}, updated_at = now() "
                f"WHERE engagement_id = %s",
                tuple(vals),
            )
        return {"status": "exists",
                "entity_id": str(existing["engagement_id"]),
                "flag_reason": None}

    # Create new engagement
    row = execute_write(
        """INSERT INTO public.engagements
           (company_id, type, gate, status, pilot_start, pilot_end,
            msa_signed, arr, notes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING engagement_id""",
        (
            company_id,
            eng_type,
            data.get("gate"),
            data.get("status", "Active"),
            data.get("pilot_start"),
            data.get("pilot_end"),
            data.get("msa_signed"),
            data.get("arr"),
            data.get("notes"),
        ),
    )
    entity_id = str(row["engagement_id"]) if row else None
    logger.info("CRM write guard: created engagement (company=%s, type=%s, source=%s)",
                company_id, eng_type, source_pb)
    return {"status": "written", "entity_id": entity_id, "flag_reason": None}


# ---------------------------------------------------------------------------
# Touch event guard — always write, no dedup
# ---------------------------------------------------------------------------


def _guard_touch_event(data, confidence, source_pb, gmail_message_id,
                       gmail_client, mm_client):
    row = execute_write(
        """INSERT INTO public.touch_events
           (person_id, company_id, type, direction, subject, summary,
            gmail_message_id, playbook)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING touch_id""",
        (
            data.get("person_id"),
            data.get("company_id"),
            data.get("type"),
            data.get("direction"),
            data.get("subject"),
            data.get("summary"),
            data.get("gmail_message_id"),
            data.get("playbook"),
        ),
    )
    entity_id = str(row["touch_id"]) if row else None
    return {"status": "written", "entity_id": entity_id, "flag_reason": None}


# ---------------------------------------------------------------------------
# Flag routing
# ---------------------------------------------------------------------------


def _flag_for_review(entity_type, data, candidates, source_pb,
                     gmail_message_id, gmail_client, mm_client, reason):
    """Route an ambiguous match to the review queue.

    1. Write to acos.pending_crm_writes
    2. Apply Gmail label @artemis/needs-review (if message_id provided)
    3. Post to Mattermost #artemis-ryan with confirm/reject instructions
    """
    # Serialize candidates for storage (strip internal fields)
    clean_candidates = []
    for c in (candidates or []):
        clean = {k: str(v) if not isinstance(v, (str, int, float, bool, type(None), list)) else v
                 for k, v in c.items() if not k.startswith("_")}
        clean_candidates.append(clean)

    row = execute_write(
        """INSERT INTO acos.pending_crm_writes
           (entity_type, data, candidates, source_pb, gmail_message_id)
           VALUES (%s, %s::jsonb, %s::jsonb, %s, %s)
           RETURNING id""",
        (
            entity_type,
            json.dumps(data),
            json.dumps(clean_candidates),
            source_pb,
            gmail_message_id,
        ),
    )
    pending_id = str(row["id"]) if row else None

    # Apply Gmail label
    if gmail_client and gmail_message_id:
        try:
            gmail_client.apply_gmail_label(gmail_message_id, "@artemis/needs-review")
        except Exception:
            logger.exception("Failed to apply needs-review label to %s", gmail_message_id)

    # Post to Mattermost
    if mm_client and pending_id:
        candidate_summary = ""
        if clean_candidates:
            best = clean_candidates[0]
            candidate_summary = (
                f"\n**Candidate 1:** {best.get('name', '?')} "
                f"(domain={best.get('domain', '?')})"
            )

        new_data_summary = (
            f"\n**New data:** {data.get('name', '?')} "
            f"(domain={data.get('domain', '?')})"
        )

        mm_msg = (
            f"\u26a0\ufe0f **CRM review needed** — {entity_type} from {source_pb}\n"
            f"**Reason:** {reason}"
            f"{candidate_summary}"
            f"{new_data_summary}\n\n"
            f"Reply: `@artemis crm confirm {pending_id}` "
            f"or `@artemis crm reject {pending_id}`"
        )
        try:
            mm_client.post_message(config.CHANNEL_OPS, mm_msg)
        except Exception:
            logger.exception("Failed to post CRM review to Mattermost")

    logger.info("CRM write guard: flagged %s for review (pending_id=%s, reason=%s)",
                entity_type, pending_id, reason)
    return {"status": "flagged", "entity_id": None,
            "flag_reason": reason, "pending_id": pending_id}


# ---------------------------------------------------------------------------
# Write confirmation notification
# ---------------------------------------------------------------------------


def _post_write_confirmation(mm_client, entity_type, data, result, source_pb):
    """Post a brief confirmation of a successful CRM write to Mattermost."""
    name = data.get("name", data.get("subject", "?"))
    entity_id = result.get("entity_id", "?")
    msg = (
        f"\u2705 CRM {entity_type} written: **{name}** "
        f"(id=`{str(entity_id)[:8]}`, source={source_pb})"
    )
    try:
        mm_client.post_message(config.CHANNEL_OPS, msg)
    except Exception:
        logger.exception("Failed to post CRM write confirmation")


# ---------------------------------------------------------------------------
# Confirm / reject pending writes
# ---------------------------------------------------------------------------


def confirm_pending_write(pending_id: str) -> dict:
    """Execute a pending CRM write and remove it from the queue."""
    pending = execute_one(
        "SELECT * FROM acos.pending_crm_writes WHERE id = %s",
        (pending_id,),
    )
    if not pending:
        return {"status": "error", "error": "Pending write not found"}

    if pending["expires_at"] and pending["expires_at"].timestamp() < __import__("time").time():
        execute_write("DELETE FROM acos.pending_crm_writes WHERE id = %s", (pending_id,))
        return {"status": "error", "error": "Pending write expired"}

    entity_type = pending["entity_type"]
    data = pending["data"] if isinstance(pending["data"], dict) else json.loads(pending["data"])

    # Execute the write based on entity_type
    handlers = {
        "company": _execute_company_write,
        "person": _execute_person_write,
    }
    handler = handlers.get(entity_type)
    if not handler:
        return {"status": "error", "error": f"Cannot confirm entity_type: {entity_type}"}

    entity_id = handler(data)

    # Remove from pending queue
    execute_write("DELETE FROM acos.pending_crm_writes WHERE id = %s", (pending_id,))

    logger.info("CRM write guard: confirmed pending write %s → %s (id=%s)",
                pending_id, entity_type, entity_id)
    return {"status": "confirmed", "entity_type": entity_type, "entity_id": entity_id}


def _execute_company_write(data: dict) -> str | None:
    """Execute a company INSERT from confirmed pending data."""
    row = execute_write(
        """INSERT INTO public.companies
           (name, domain, types, industry, hq_location, website, linkedin_url, notes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING company_id""",
        (
            data.get("name"),
            data.get("domain"),
            data.get("types"),
            data.get("industry"),
            data.get("hq_location"),
            data.get("website"),
            data.get("linkedin_url"),
            data.get("notes"),
        ),
    )
    return str(row["company_id"]) if row else None


def _execute_person_write(data: dict) -> str | None:
    """Execute a person INSERT from confirmed pending data."""
    row = execute_write(
        """INSERT INTO public.persons
           (name, email_primary, emails, phone, linkedin_url,
            location, timezone, source, source_detail, notes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING person_id""",
        (
            data.get("name"),
            data.get("email_primary"),
            data.get("emails"),
            data.get("phone"),
            data.get("linkedin_url"),
            data.get("location"),
            data.get("timezone"),
            data.get("source"),
            data.get("source_detail"),
            data.get("notes"),
        ),
    )
    return str(row["person_id"]) if row else None


def reject_pending_write(pending_id: str) -> dict:
    """Delete a pending CRM write from the queue."""
    existing = execute_one(
        "SELECT 1 FROM acos.pending_crm_writes WHERE id = %s",
        (pending_id,),
    )
    if not existing:
        return {"status": "error", "error": "Pending write not found"}

    execute_write("DELETE FROM acos.pending_crm_writes WHERE id = %s", (pending_id,))
    logger.info("CRM write guard: rejected pending write %s", pending_id)
    return {"status": "rejected", "pending_id": pending_id}


def list_pending_writes() -> list[dict]:
    """List all unexpired pending CRM writes."""
    return execute_query(
        "SELECT * FROM acos.pending_crm_writes "
        "WHERE expires_at > now() ORDER BY created_at DESC LIMIT 20"
    )
