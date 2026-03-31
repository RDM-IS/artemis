"""Document parser — extract contacts from PDFs, images, docx, and text."""

import base64
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field

import anthropic

from artemis.commitments import log_claude_call
from knowledge.secrets import get_anthropic_key

logger = logging.getLogger(__name__)

_EXTRACTION_SYSTEM = (
    "Extract all people and their details from this document.\n"
    "Return ONLY a JSON array of contact objects. Each object:\n"
    "{\n"
    '  "name": "string",\n'
    '  "title": "string or null",\n'
    '  "company": "string or null",\n'
    '  "email": "string or null",\n'
    '  "phone": "string or null",\n'
    '  "linkedin": "string or null",\n'
    '  "notes": "string or null",\n'
    '  "relationship_to_others": [["rel_type", "other_name"], ...],\n'
    '  "source_description": "string"\n'
    "}\n"
    "If a field is unknown use null. Extract ALL people mentioned, "
    "not just the primary subject. Infer relationships from context "
    "— if someone 'introduced' another, capture that. If someone "
    "'reports to' another, capture that."
)


@dataclass
class ExtractedContact:
    name: str
    title: str | None = None
    company: str | None = None
    email: str | None = None
    phone: str | None = None
    linkedin: str | None = None
    notes: str | None = None
    relationship_to_others: list[tuple[str, str]] = field(default_factory=list)
    source_description: str = ""


@dataclass
class SalesPlanContext:
    account_name: str
    tier: str | None = None
    gate: int | None = None
    entry_point: str | None = None
    warm_path: str | None = None
    pitch_framing: str | None = None
    never_say: list[str] = field(default_factory=list)
    always_say: list[str] = field(default_factory=list)
    objections: list[dict] = field(default_factory=list)
    gate_sequence: list[dict] = field(default_factory=list)
    next_actions: list[dict] = field(default_factory=list)
    raw_contacts: list[str] = field(default_factory=list)
    source_file: str = ""


def _extract_text_from_pdf(file_bytes: bytes) -> str:
    """Extract text from PDF bytes using PyMuPDF."""
    import fitz  # PyMuPDF

    doc = fitz.open(stream=file_bytes, filetype="pdf")
    pages = []
    for page in doc:
        pages.append(page.get_text())
    doc.close()
    return "\n\n".join(pages)


def _extract_text_from_docx(file_bytes: bytes) -> str:
    """Extract text from DOCX bytes using python-docx."""
    import io
    from docx import Document

    doc = Document(io.BytesIO(file_bytes))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n".join(paragraphs)


def _call_claude_text(text: str, user_context: str) -> list[dict]:
    """Send extracted text to Claude for contact extraction."""
    client = anthropic.Anthropic(api_key=get_anthropic_key())
    user_msg = f"Document content:\n{text[:15000]}\n\nUser context: {user_context}"
    prompt_hash = hashlib.sha256(
        (_EXTRACTION_SYSTEM + user_msg).encode()
    ).hexdigest()[:16]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=_EXTRACTION_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = response.content[0].text.strip()
    log_claude_call("claude-sonnet-4-6", prompt_hash, len(raw))

    # Strip markdown fences
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"^```\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    raw = raw.strip()

    return json.loads(raw)


def _call_claude_vision(image_bytes: bytes, mime_type: str, user_context: str) -> list[dict]:
    """Send image to Claude vision for contact extraction."""
    client = anthropic.Anthropic(api_key=get_anthropic_key())
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    # Map mime types to supported media types
    media_type = mime_type
    if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        media_type = "image/png"  # safe default

    user_content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": b64,
            },
        },
        {
            "type": "text",
            "text": f"Extract all contacts from this image.\n\nUser context: {user_context}",
        },
    ]

    prompt_hash = hashlib.sha256(
        (_EXTRACTION_SYSTEM + user_context).encode()
    ).hexdigest()[:16]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=_EXTRACTION_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
    )
    raw = response.content[0].text.strip()
    log_claude_call("claude-sonnet-4-6", prompt_hash, len(raw))

    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"^```\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    raw = raw.strip()

    return json.loads(raw)


_DOCTYPE_SYSTEM = (
    "Classify this document. Return ONLY one of these labels, nothing else:\n"
    "- sales_plan (account strategy, sales playbook, deal plan)\n"
    "- contact_list (list of people with contact info)\n"
    "- email (forwarded email or email thread)\n"
    "- linkedin_profile (LinkedIn profile or export)\n"
    "- general (anything else)"
)

_SALES_PLAN_SYSTEM = (
    "Extract the sales plan structure from this document. "
    "Return ONLY valid JSON:\n"
    "{\n"
    '  "account_name": "string",\n'
    '  "tier": "Tier 1" or "Tier 2" or "Tier 3" or null,\n'
    '  "gate": integer 0-5 or null,\n'
    '  "entry_point": "person name" or null,\n'
    '  "warm_path": "description" or null,\n'
    '  "pitch_framing": "core message/value prop" or null,\n'
    '  "never_say": ["phrases to avoid"],\n'
    '  "always_say": ["preferred framing phrases"],\n'
    '  "objections": [{"objection": "string", "response": "string"}],\n'
    '  "gate_sequence": [{"gate": int, "stage": "string", "objective": "string"}],\n'
    '  "next_actions": [{"date": "string", "action": "string", "owner": "string"}],\n'
    '  "raw_contacts": ["all person names mentioned"]\n'
    "}"
)


def detect_document_type(text: str) -> str:
    """Classify a document's type using Claude. Returns label string."""
    try:
        client = anthropic.Anthropic(api_key=get_anthropic_key())
        snippet = text[:500]
        prompt_hash = hashlib.sha256(
            (_DOCTYPE_SYSTEM + snippet).encode()
        ).hexdigest()[:16]

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            system=_DOCTYPE_SYSTEM,
            messages=[{"role": "user", "content": snippet}],
        )
        raw = response.content[0].text.strip().lower()
        log_claude_call("claude-haiku-4-5-20251001", prompt_hash, len(raw))

        valid = {"sales_plan", "contact_list", "email", "linkedin_profile", "general"}
        return raw if raw in valid else "general"
    except Exception:
        logger.debug("Document type detection failed", exc_info=True)
        return "general"


def parse_sales_plan(text: str, filename: str = "") -> SalesPlanContext:
    """Extract sales plan structure from document text via Claude."""
    client = anthropic.Anthropic(api_key=get_anthropic_key())
    user_msg = f"Document content:\n{text[:15000]}"
    prompt_hash = hashlib.sha256(
        (_SALES_PLAN_SYSTEM + user_msg).encode()
    ).hexdigest()[:16]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=_SALES_PLAN_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = response.content[0].text.strip()
    log_claude_call("claude-sonnet-4-6", prompt_hash, len(raw))

    # Strip markdown fences
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"^```\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    raw = raw.strip()

    data = json.loads(raw)
    return SalesPlanContext(
        account_name=data.get("account_name", "Unknown"),
        tier=data.get("tier"),
        gate=data.get("gate"),
        entry_point=data.get("entry_point"),
        warm_path=data.get("warm_path"),
        pitch_framing=data.get("pitch_framing"),
        never_say=data.get("never_say", []),
        always_say=data.get("always_say", []),
        objections=data.get("objections", []),
        gate_sequence=data.get("gate_sequence", []),
        next_actions=data.get("next_actions", []),
        raw_contacts=data.get("raw_contacts", []),
        source_file=filename,
    )


def parse_document(
    file_bytes: bytes,
    mime_type: str,
    user_context: str = "",
) -> tuple[list[ExtractedContact], SalesPlanContext | None]:
    """Parse a document and extract contacts via Claude.

    Supports: text/*, application/json, application/pdf,
    image/*, application/vnd.openxmlformats-officedocument.wordprocessingml.document (docx).

    Returns (list[ExtractedContact], SalesPlanContext | None).
    If the document is a sales plan, the second element contains the parsed plan.
    Returns ([], None) on error.
    """
    sales_plan = None

    try:
        if mime_type.startswith("image/"):
            raw_contacts = _call_claude_vision(file_bytes, mime_type, user_context)
        else:
            # Extract text first
            if mime_type == "application/pdf":
                text = _extract_text_from_pdf(file_bytes)
            elif "wordprocessingml" in mime_type or mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
                text = _extract_text_from_docx(file_bytes)
            elif mime_type.startswith("text/") or mime_type == "application/json":
                text = file_bytes.decode("utf-8", errors="replace")
            else:
                # Try as text
                text = file_bytes.decode("utf-8", errors="replace")

            if not text.strip():
                logger.warning("Empty text extracted from %s document", mime_type)
                return [], None

            # Detect document type and handle sales plans
            doc_type = detect_document_type(text)
            logger.info("Document type detected: %s", doc_type)

            if doc_type == "sales_plan":
                try:
                    sales_plan = parse_sales_plan(text, filename=user_context[:100])
                    logger.info("Sales plan parsed for: %s", sales_plan.account_name)
                except Exception:
                    logger.exception("Sales plan extraction failed, falling back to contact extraction")

            raw_contacts = _call_claude_text(text, user_context)

        # Parse into dataclass list
        contacts = []
        if not isinstance(raw_contacts, list):
            raw_contacts = [raw_contacts]

        for c in raw_contacts:
            if not isinstance(c, dict) or not c.get("name"):
                continue

            rels = []
            for r in c.get("relationship_to_others", []):
                if isinstance(r, (list, tuple)) and len(r) >= 2:
                    rels.append((str(r[0]), str(r[1])))

            contacts.append(ExtractedContact(
                name=c["name"],
                title=c.get("title"),
                company=c.get("company"),
                email=c.get("email"),
                phone=c.get("phone"),
                linkedin=c.get("linkedin"),
                notes=c.get("notes"),
                relationship_to_others=rels,
                source_description=c.get("source_description", ""),
            ))

        logger.info("Parsed %d contacts from %s document", len(contacts), mime_type)
        return contacts, sales_plan

    except Exception:
        logger.exception("Document parsing failed (mime=%s)", mime_type)
        return [], None
