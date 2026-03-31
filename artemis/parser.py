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


def parse_document(
    file_bytes: bytes,
    mime_type: str,
    user_context: str = "",
) -> list[ExtractedContact]:
    """Parse a document and extract contacts via Claude.

    Supports: text/*, application/json, application/pdf,
    image/*, application/vnd.openxmlformats-officedocument.wordprocessingml.document (docx).

    Returns list of ExtractedContact. Returns [] on error.
    """
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
                return []

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
        return contacts

    except Exception:
        logger.exception("Document parsing failed (mime=%s)", mime_type)
        return []
