"""Chat-authored declarative inbox rules (feature #1 — the automation on-ramp).

A rule is a match (sender/subject/body substrings) → action (archive/spam/file).
Rules live in acos.playbook_rules and are read at RUNTIME by the triage loop, so
a new rule takes effect without a redeploy. Every match is executed through the
audited disposition primitive, so automation is inspectable via acos.audit_log —
the structural opposite of an LLM claiming "I added a rule."

Authoring is propose-then-confirm: parse_rule_spec() turns a command into a
structured proposal, Ryan confirms, create_rule() writes the row. The LLM never
writes a rule; only create_rule() does, and only after confirmation. Activating a
standing automation is the most gated action there is (Brad Spaits descendant).
"""

import logging
import re

from knowledge.db import execute_one, execute_query, execute_write

logger = logging.getLogger(__name__)

_VALID_ACTIONS = {"archive", "spam", "file"}
# Reserved words can't be labels — stops `file as spam` leaking a category named
# after a verb (the footgun from the live session).
_RESERVED = {"archive", "spam", "file", "delete"}


def _slug(text: str) -> str:
    s = re.sub(r"[^\w\s/-]", "", (text or "").strip().lower())
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s[:40]


class RuleSpecError(ValueError):
    """A `rule add …` command that can't be parsed into a valid rule."""


def parse_rule_spec(text: str) -> dict:
    """Parse `<action> [as <label>] [from:<s>] [subject:"<s>"] [body:"<s>"]` into a
    validated proposal. Deterministic — no LLM. Raises RuleSpecError on anything
    malformed, with a message safe to show the user.

    Examples:
      archive from:cloudflare-workers-and-pages body:"Deploy successful"
      file as founder-loan from:vercel.com subject:receipt
      spam from:etsy.com
    """
    raw = (text or "").strip()
    m = re.match(r"^(archive|spam|file|delete)\b", raw, re.IGNORECASE)
    if not m:
        raise RuleSpecError("Start with an action: `archive`, `spam`, or `file`.")
    action = m.group(1).lower()
    if action == "delete":
        raise RuleSpecError(
            "`delete` isn't allowed as a standing rule — too destructive to "
            "automate. Use `archive` or `spam`."
        )
    rest = raw[m.end():].strip()

    label = None
    if action == "file":
        lm = re.match(r"^as\s+([\w/-]+)", rest, re.IGNORECASE)
        if not lm:
            raise RuleSpecError("`file` needs a label: `file as <label> from:… `")
        label = _slug(lm.group(1))
        if not label:
            raise RuleSpecError("That label is empty after normalization.")
        if label in _RESERVED:
            raise RuleSpecError(f"`{label}` is a reserved word and can't be a label.")
        rest = rest[lm.end():].strip()

    fields: dict[str, str] = {}
    for key in ("from", "subject", "body"):
        fm = re.search(rf'{key}:\s*("([^"]*)"|(\S+))', rest, re.IGNORECASE)
        if fm:
            val = fm.group(2) if fm.group(2) is not None else fm.group(3)
            val = (val or "").strip()
            if val:
                fields[key] = val

    if not fields:
        raise RuleSpecError(
            "A rule needs at least one match: `from:`, `subject:`, or `body:`."
        )

    return {
        "action": action,
        "action_label": label,
        "match_sender": fields.get("from"),
        "match_subject": fields.get("subject"),
        "match_body": fields.get("body"),
    }


def describe_rule(rule: dict) -> str:
    """Human-readable one-liner for a proposal or stored rule — rendered from the
    struct, never the LLM. Used for the confirm echo and the `rules` list."""
    conds = []
    if rule.get("match_sender"):
        conds.append(f'from ~ "{rule["match_sender"]}"')
    if rule.get("match_subject"):
        conds.append(f'subject ~ "{rule["match_subject"]}"')
    if rule.get("match_body"):
        conds.append(f'body ~ "{rule["match_body"]}"')
    act = rule["action"]
    if act == "file":
        act = f'file → @artemis/{rule.get("action_label")}'
    return f'{" AND ".join(conds) or "(no conditions)"}  ⇒  {act}'


def matches(rule: dict, sender: str, subject: str, body: str = "") -> bool:
    """True iff every non-null criterion is a case-insensitive substring of the
    corresponding field. A rule with no criteria never matches (fail-safe)."""
    active = [
        (rule.get("match_sender"), sender or ""),
        (rule.get("match_subject"), subject or ""),
        (rule.get("match_body"), body or ""),
    ]
    active = [(need, hay) for need, hay in active if need]
    if not active:
        return False
    return all(need.lower() in hay.lower() for need, hay in active)


def needs_body(rule: dict) -> bool:
    return bool(rule.get("match_body"))


# --- persistence -----------------------------------------------------------

def list_rules(active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM acos.playbook_rules"
    if active_only:
        sql += " WHERE active = TRUE"
    sql += " ORDER BY id"
    return execute_query(sql) or []


def create_rule(name: str, action: str, action_label: str | None,
                match_sender: str | None, match_subject: str | None,
                match_body: str | None, created_by: str = "ryan") -> dict:
    """Write a rule row. The ONLY path that persists a rule — reached only after
    an explicit human confirmation."""
    if action not in _VALID_ACTIONS:
        raise RuleSpecError(f"Invalid action: {action}")
    if action == "file" and not action_label:
        raise RuleSpecError("`file` rules require a label.")
    if not (match_sender or match_subject or match_body):
        raise RuleSpecError("A rule needs at least one match condition.")
    return execute_one(
        """INSERT INTO acos.playbook_rules
               (name, action, action_label, match_sender, match_subject,
                match_body, created_by)
           VALUES (%(name)s, %(action)s, %(label)s, %(s)s, %(subj)s, %(body)s, %(by)s)
           RETURNING *""",
        {"name": name, "action": action, "label": action_label,
         "s": match_sender, "subj": match_subject, "body": match_body,
         "by": created_by},
    )


def deactivate_rule(rule_id: int) -> bool:
    row = execute_one(
        "UPDATE acos.playbook_rules SET active = FALSE WHERE id = %(id)s RETURNING id",
        {"id": rule_id},
    )
    return bool(row)


def record_fired(rule_id: int) -> None:
    execute_write(
        "UPDATE acos.playbook_rules "
        "SET times_fired = times_fired + 1, last_fired_at = now() WHERE id = %(id)s",
        {"id": rule_id},
    )


def match_message(sender: str, subject: str, body_fetcher=None) -> dict | None:
    """First active rule matching this message, or None. Sender/subject are
    matched cheaply first; body is fetched lazily (via body_fetcher()) only when a
    candidate rule needs it, so body rules don't force a fetch on every email.
    """
    body_cache: str | None = None
    for rule in list_rules(active_only=True):
        if needs_body(rule):
            has_pre = rule.get("match_sender") or rule.get("match_subject")
            if has_pre and not matches(
                {"match_sender": rule.get("match_sender"),
                 "match_subject": rule.get("match_subject"), "match_body": None},
                sender, subject,
            ):
                continue
            if body_cache is None and body_fetcher is not None:
                try:
                    body_cache = body_fetcher() or ""
                except Exception:
                    logger.debug("rule body fetch failed", exc_info=True)
                    body_cache = ""
            if matches(rule, sender, subject, body_cache or ""):
                return rule
        elif matches(rule, sender, subject):
            return rule
    return None
