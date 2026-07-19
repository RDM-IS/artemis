"""PB-011 — Vault / Second Brain Ingest (v1).

Ryan's Obsidian vault (RDM-IS/vault, private) is the canonical human-authored
knowledge store. This module ingests it into Postgres (schema `vault`), runs one
extraction pass per new note, and surfaces everything as proposals through the
existing approval gates. The vault FILE is canon; Postgres is a rebuildable
projection.

THE WALL (statistics vs semantics). Artemis parses, counts, links, detects, and
PROPOSES. Ryan approves, names, blesses. Nothing extraction produces auto-writes to
any system-of-record table — every user-facing confirmation renders from written
rows, never from an LLM claim (the no-fabrication gate). On approval only, a
proposal is written through the EXISTING creation paths (commitment creation,
dossier draft-approval helpers) so all prior guardrails still hold.

v1 non-goals (NOT built here): embeddings / pgvector / semantic links (schema
reserves room); any write to the vault git repo (strictly read-only — zero commits,
zero pushes); rendering to generated/; a second approval surface (adjudication
reuses the E3 `approve 1-3` / `reject 2` / `&` range syntax).

Data layer touches RDS via knowledge.db with %s params only. Secrets via
knowledge.secrets. Git token is used at fetch time only — never on disk, never in
stored git config, never in argv.
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from artemis import config
from knowledge.db import execute_one, execute_query, execute_write, log_audit
from knowledge.secrets import get_vault_repo

logger = logging.getLogger(__name__)

_CT = ZoneInfo("America/Chicago")

# Sources that get an extraction pass. journal feeds the morning diff but is never
# extracted; legacy-* is study/archival material — extracting to-dos from it is noise.
_EXTRACT_SOURCES = ("meeting", "dictation", "thought")

# Backfill-flood guard: at most this many notes extracted per run, oldest-first;
# the remainder queues for the next run.
_EXTRACT_THROTTLE = 20

# Digest / brief render caps.
_DIGEST_CAP = 10
_PROPOSAL_EXPIRY_DAYS = 7

# candidate-list key on the LLM JSON  ->  extraction_type stored on the row.
_CANDIDATE_TYPES = {
    "action_items": "action_item",
    "commitments": "commitment",
    "dossier_entries": "dossier_entry",
    "org_facts": "org_fact",
    "decision_candidates": "decision_candidate",
    "questions": "question",
}

# Digest ordering: decisions first, then commitments/action items, then questions,
# then anything else.
_TYPE_RANK = {
    "decision_candidate": 0, "commitment": 1, "action_item": 2,
    "dossier_entry": 3, "org_fact": 4, "question": 5,
}
_TYPE_LABEL = {
    "decision_candidate": "decision", "commitment": "commitment",
    "action_item": "action item", "dossier_entry": "dossier", "org_fact": "org fact",
    "question": "question",
}


def _ct_today() -> date:
    return datetime.now(_CT).date()


def _audit(action: str, metadata: dict | None = None) -> None:
    try:
        log_audit(agent="vault", action=action, domain="vault", metadata=metadata or {})
    except Exception:
        logger.debug("vault audit write failed (%s)", action, exc_info=True)


# ---------------------------------------------------------------------------
# ingest_state KV
# ---------------------------------------------------------------------------

def _state_get(key: str, default=None):
    row = execute_one("SELECT value FROM vault.ingest_state WHERE key = %s", (key,))
    return row["value"] if row else default


def _state_set(key: str, value) -> None:
    execute_write(
        "INSERT INTO vault.ingest_state (key, value, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key, json.dumps(value)),
    )


# ---------------------------------------------------------------------------
# Git mirror — shallow clone, fetch+reset to origin/main. READ-ONLY.
# ---------------------------------------------------------------------------

def _mirror_dir() -> str:
    """Local mirror path — a sibling of the app dir by convention, overridable via
    VAULT_MIRROR_DIR. Deliberately NOT Ryan's working vault (…/vault): a dedicated
    mirror we reset --hard, never his editing copy."""
    override = os.environ.get("VAULT_MIRROR_DIR")
    if override:
        return override
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(os.path.dirname(repo_root), "artemis-vault-mirror")


@contextmanager
def _git_env(token: str):
    """Yield an env that feeds the PAT to git via GIT_ASKPASS — the token lives in a
    subprocess env var read by an ephemeral askpass script, never on argv and never
    written into git config. The script file carries no secret (reads it from env)
    and is 0700 + removed on exit."""
    fd, path = tempfile.mkstemp(prefix="vault-askpass-", suffix=".sh")
    try:
        with os.fdopen(fd, "w") as f:
            f.write('#!/bin/sh\nexec printf "%s" "$VAULT_GIT_TOKEN"\n')
        os.chmod(path, 0o700)
        env = dict(os.environ)
        env["GIT_ASKPASS"] = path
        env["VAULT_GIT_TOKEN"] = token
        env["GIT_TERMINAL_PROMPT"] = "0"
        yield env
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _run_git(args: list[str], cwd: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=cwd, env=env, check=True,
        capture_output=True, text=True, timeout=180,
    )


def _sync_mirror() -> str:
    """Ensure the mirror is at origin/main and return its HEAD sha. Shallow clone on
    first run; fetch + reset --hard + clean thereafter. Never commits, never pushes."""
    repo = get_vault_repo()
    token = repo["token"]
    # Inject the username (NOT the token) into the URL so git prompts only for the
    # password, which the askpass supplies. The token never enters the stored URL.
    auth_url = repo["clone_url"].replace("https://", "https://x-access-token@", 1)
    mirror = _mirror_dir()
    parent = os.path.dirname(mirror) or "."
    with _git_env(token) as env:
        if not os.path.isdir(os.path.join(mirror, ".git")):
            os.makedirs(parent, exist_ok=True)
            _run_git(["clone", "--depth", "1", auth_url, mirror], cwd=parent, env=env)
        else:
            _run_git(["remote", "set-url", "origin", auth_url], cwd=mirror, env=env)
            _run_git(["fetch", "--depth", "1", "origin", "main"], cwd=mirror, env=env)
            _run_git(["reset", "--hard", "origin/main"], cwd=mirror, env=env)
            _run_git(["clean", "-fd"], cwd=mirror, env=env)
        return _run_git(["rev-parse", "HEAD"], cwd=mirror, env=env).stdout.strip()


# ---------------------------------------------------------------------------
# PAT expiry watch (OPS-1) — proactive rotation warning
# ---------------------------------------------------------------------------

_PAT_WARN_DAYS = 14


def _check_pat_expiry() -> None:
    """Best-effort: read the fine-grained PAT's expiration from GitHub's
    `github-authentication-token-expiration` response header (one lightweight
    authenticated GET to the repo endpoint) and store it in ingest_state under
    `pat_expiry`. Header absent → store 'unknown' and never guess a date. Only a
    network/HTTP failure leaves the value untouched (so it retries next sync)."""
    repo = get_vault_repo()
    token = repo.get("token")
    if not token:
        return
    r = requests.get(
        "https://api.github.com/repos/RDM-IS/vault",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=10,
    )
    expiry = (r.headers.get("github-authentication-token-expiration") or "").strip()
    _state_set("pat_expiry", expiry or "unknown")


def _maybe_check_pat_expiry() -> None:
    """Throttle the expiry check to once per CT day — `digest` inline-syncs often, and
    the header doesn't change intra-day. Never raises into the sync pipeline."""
    today = _ct_today().isoformat()
    if _state_get("pat_expiry_checked_date") == today:
        return
    try:
        _check_pat_expiry()
        _state_set("pat_expiry_checked_date", today)  # only after a real check
    except Exception:
        logger.debug("vault: PAT expiry check failed", exc_info=True)


def _parse_pat_expiry(raw) -> date | None:
    """Parse the stored PAT expiry (GitHub sends e.g. `2026-08-01 23:59:59 UTC` or
    ISO 8601) to a date, or None for 'unknown'/missing/unparseable — never guess."""
    if not raw:
        return None
    s = str(raw).strip()
    if s.lower() in ("unknown", "none", ""):
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _pat_expiry_warning() -> str | None:
    """A morning-brief warning when the PAT expires within _PAT_WARN_DAYS — reuses the
    vault-pat-auth runbook's remediation block. Silent when expiry is unknown."""
    exp = _parse_pat_expiry(_state_get("pat_expiry"))
    if exp is None:
        return None
    days = (exp - _ct_today()).days
    if days > _PAT_WARN_DAYS:
        return None
    from artemis import opsdiag
    rb = opsdiag.runbook_for("vault-pat-auth")
    when = "expired" if days < 0 else f"expires in {days} day{'s' if days != 1 else ''}"
    head = f"⚠️ **Vault PAT {when}** ({exp.isoformat()}) — rotate now:"
    return f"{head}\n{rb.remediation}" if rb else head


def _enumerate_notes(mirror: str) -> list[str]:
    """All *.md under authored/ (repo-relative paths). Skips generated/, templates/,
    .obsidian/, and any dotfile/dotdir."""
    root = os.path.join(mirror, "authored")
    out: list[str] = []
    if not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.startswith(".") or not fn.lower().endswith(".md"):
                continue
            out.append(os.path.relpath(os.path.join(dirpath, fn), mirror))
    return sorted(out)


# ---------------------------------------------------------------------------
# Frontmatter parsing (dependency-free, tolerant) + content hashing
# ---------------------------------------------------------------------------

def _coerce_scalar(val: str):
    """Coerce a frontmatter scalar: strip quotes, inline `[a, b]` → list, else str."""
    v = val.strip()
    if not v:
        return ""
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [x.strip().strip("'\"") for x in inner.split(",") if x.strip()]
    if (v[0] == v[-1]) and v[0] in ("'", '"') and len(v) >= 2:
        return v[1:-1]
    return v


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body). Tolerant: a missing or unterminated
    frontmatter fence yields ({}, text). Only the leading `---` fenced block is
    parsed — a stray `key:` line in the body is never read as frontmatter. Malformed
    lines inside the fence are skipped, never fatal. The body is the byte-faithful
    remainder after the closing fence."""
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text  # unterminated fence → treat as no frontmatter
    fm: dict = {}
    for ln in lines[1:end]:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        m = re.match(r"^\s*([A-Za-z0-9_-]+)\s*:\s*(.*)$", ln)
        if not m:
            continue  # tolerant: skip a malformed frontmatter line
        fm[m.group(1).strip()] = _coerce_scalar(m.group(2))
    body = "\n".join(lines[end + 1:])
    return fm, body


def _normalize_body(body: str) -> str:
    return body.replace("\r\n", "\n").replace("\r", "\n").strip()


def _content_hash(body: str) -> str:
    return hashlib.sha256(_normalize_body(body).encode("utf-8")).hexdigest()


def _parse_ts(val) -> str | None:
    """Validate a frontmatter `created` value into an ISO string PG accepts, or None
    (so a malformed timestamp never aborts the note's insert transaction)."""
    if not val:
        return None
    s = str(val).strip()
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        return s
    except ValueError:
        try:
            date.fromisoformat(s[:10])
            return s[:10]
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# Sync pipeline (one job, two triggers — 04:00 CT cron + `vault sync`)
# ---------------------------------------------------------------------------

def _upsert_note(rel_path: str, mirror: str, counts: dict, changed: list[str]) -> None:
    """Parse one file and upsert vault.notes keyed on capture_id. Anti-clobber: a
    path already owned by a DIFFERENT capture_id, or a capture_id already stored at a
    DIFFERENT path, is logged and skipped — never overwrite one note with another."""
    abspath = os.path.join(mirror, rel_path)
    try:
        with open(abspath, "r", encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        logger.warning("vault: could not read %s — skipped", rel_path)
        counts["skipped"] += 1
        return

    fm, body = parse_frontmatter(text)
    capture_id = str(fm.get("capture_id") or "").strip()
    source = str(fm.get("source") or "").strip()
    if not capture_id or not source:
        logger.info("vault: %s has no capture_id/source — skipped", rel_path)
        counts["skipped"] += 1
        return

    chash = _content_hash(body)
    by_path = execute_one("SELECT capture_id FROM vault.notes WHERE path = %s", (rel_path,))
    if by_path and by_path["capture_id"] != capture_id:
        logger.warning("vault: path %s owned by %s, not %s — skipped (no clobber)",
                       rel_path, by_path["capture_id"], capture_id)
        counts["skipped"] += 1
        return
    by_cap = execute_one("SELECT path, content_hash, deleted_at FROM vault.notes WHERE capture_id = %s", (capture_id,))
    if by_cap and by_cap["path"] != rel_path:
        logger.warning("vault: capture_id %s already at %s, seen at %s — skipped (no clobber)",
                       capture_id, by_cap["path"], rel_path)
        counts["skipped"] += 1
        return

    status = str(fm.get("status") or "bronze").strip() or "bronze"
    created = _parse_ts(fm.get("created"))
    wc = len(_normalize_body(body).split())

    if by_cap is None:
        execute_write(
            "INSERT INTO vault.notes "
            "(capture_id, path, source, status, created_at, frontmatter, raw_text, "
            " content_hash, word_count) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (capture_id, rel_path, source, status, created, json.dumps(fm), body, chash, wc),
        )
        counts["new"] += 1
        changed.append(capture_id)
        return

    reappeared = by_cap["deleted_at"] is not None
    if by_cap["content_hash"] == chash and not reappeared:
        execute_write("UPDATE vault.notes SET last_ingested_at = now() WHERE capture_id = %s", (capture_id,))
        counts["unchanged"] += 1
        return

    execute_write(
        "UPDATE vault.notes SET source = %s, status = %s, created_at = %s, frontmatter = %s, "
        "raw_text = %s, content_hash = %s, word_count = %s, last_ingested_at = now(), "
        "deleted_at = NULL WHERE capture_id = %s",
        (source, status, created, json.dumps(fm), body, chash, wc, capture_id),
    )
    if reappeared:
        counts["reappeared"] += 1
    if by_cap["content_hash"] != chash:
        counts["changed"] += 1
        changed.append(capture_id)


def _detect_deletions(seen_paths: set, counts: dict) -> None:
    """Mark rows whose file no longer exists as deleted (retained, never hard-
    deleted). Reappearance is handled in _upsert_note (deleted_at cleared)."""
    live = execute_query("SELECT capture_id, path FROM vault.notes WHERE deleted_at IS NULL")
    for r in live:
        if r["path"] not in seen_paths:
            execute_write("UPDATE vault.notes SET deleted_at = now() WHERE capture_id = %s", (r["capture_id"],))
            counts["deleted"] += 1


def _wikilink_targets(raw: str) -> list[tuple[str, str]]:
    """Parse [[wikilink]] targets from note body. Returns ordered, deduped
    (target_raw, resolve_key) pairs — resolve_key strips an `|alias` and a
    `#heading` and is what resolves against a note's filename stem."""
    out: list[tuple[str, str]] = []
    seen: set = set()
    for m in re.finditer(r"\[\[([^\]]+)\]\]", raw or ""):
        target_raw = m.group(1).strip()
        key = target_raw.split("|", 1)[0].split("#", 1)[0].strip()
        if not key or key.lower() in seen:
            continue
        seen.add(key.lower())
        out.append((target_raw, key))
    return out


def _recompute_links(changed: list[str]) -> None:
    """Recompute [[wikilink]] edges for changed notes. Targets resolve against note
    filename stems (case-insensitive); unresolved targets stay dangling (NULL)."""
    if not changed:
        return
    stem_map: dict[str, str] = {}
    for r in execute_query("SELECT capture_id, path FROM vault.notes WHERE deleted_at IS NULL"):
        stem = os.path.splitext(os.path.basename(r["path"]))[0].lower()
        stem_map.setdefault(stem, r["capture_id"])
    for cap in changed:
        note = execute_one("SELECT raw_text FROM vault.notes WHERE capture_id = %s", (cap,))
        if not note:
            continue
        execute_write("DELETE FROM vault.note_links WHERE source_capture_id = %s AND link_type = 'wikilink'", (cap,))
        for target_raw, key in _wikilink_targets(note["raw_text"] or ""):
            target_cap = stem_map.get(key.lower())
            execute_write(
                "INSERT INTO vault.note_links (source_capture_id, target_raw, target_capture_id, link_type) "
                "VALUES (%s, %s, %s, 'wikilink') ON CONFLICT DO NOTHING",
                (cap, target_raw, target_cap),
            )


def sync_vault() -> dict:
    """Fetch → upsert → link → extract → record. Returns a summary rendered ONLY
    from written rows. Identical code path for the 04:00 cron and `vault sync`."""
    counts = {"new": 0, "changed": 0, "unchanged": 0, "skipped": 0,
              "deleted": 0, "reappeared": 0, "proposals": 0, "extracted": 0}
    sha = _sync_mirror()
    _maybe_check_pat_expiry()  # mirror sync proved the token works — read its expiry
    last_sha = _state_get("last_sha")

    if sha == last_sha:
        _state_set("last_run", datetime.now(_CT).isoformat())
        _state_set("last_run_counts", counts)
        _audit("sync_no_change", {"sha": sha})
        return {"sha": sha, "no_change": True, **counts}

    mirror = _mirror_dir()
    changed: list[str] = []
    seen_paths: set = set()
    for rel_path in _enumerate_notes(mirror):
        seen_paths.add(rel_path)
        try:
            _upsert_note(rel_path, mirror, counts, changed)
        except Exception:
            logger.exception("vault: upsert failed for %s", rel_path)
            counts["skipped"] += 1
    _detect_deletions(seen_paths, counts)
    _recompute_links(changed)

    expire_stale_proposals()
    ex_counts = _run_extraction()
    counts["extracted"] = ex_counts["extracted"]
    counts["proposals"] = ex_counts["proposals"]

    _state_set("last_sha", sha)
    _state_set("last_run", datetime.now(_CT).isoformat())
    _state_set("last_run_counts", counts)
    _audit("sync", {"sha": sha, **counts})
    return {"sha": sha, "no_change": False, **counts}


# ---------------------------------------------------------------------------
# Extraction pass (LLM, one call per eligible note, throttled)
# ---------------------------------------------------------------------------

def _detect_context(raw_text: str, fm: dict) -> str | None:
    """Deterministic context detection — an `fca:` prefix or an `fca` tag/hashtag
    maps to context='fca'. Deterministic wins over any LLM suggestion."""
    tags = fm.get("tags")
    tag_list = tags if isinstance(tags, list) else ([tags] if tags else [])
    tag_str = " ".join(str(t).lower() for t in tag_list)
    if "fca" in re.split(r"[\s/#]+", tag_str):
        return "fca"
    if re.search(r"(?im)^\s*fca\s*[:\-]", raw_text or ""):
        return "fca"
    if re.search(r"(?i)#fca\b", raw_text or ""):
        return "fca"
    return None


def _proposal_core(etype: str, cand: dict) -> str:
    """Canonical core string for a candidate — the idempotency key. Re-extracting
    the same content yields the same hash, so ON CONFLICT DO NOTHING never
    re-proposes adjudicated content."""
    text = re.sub(r"\s+", " ", str(cand.get("text", "")).strip().lower())
    if etype == "commitment":
        return f"commitment|{str(cand.get('direction', '')).lower()}|{text}"
    if etype == "dossier_entry":
        return f"dossier_entry|{str(cand.get('person', '')).lower()}|{text}"
    if etype == "org_fact":
        return f"org_fact|{str(cand.get('org', '')).lower()}|{text}"
    return f"{etype}|{text}"


def _extract_one(note: dict) -> dict:
    """One LLM pass over a single note → counts. Stores cleaned_text + summary on the
    note, deterministic frontmatter tags + inferential summary in note_metadata, and
    candidates as pending proposals (idempotent). Proposals never touch target
    tables. cleaned_text is always set at the end so the note is not re-queued."""
    from artemis.briefs import _call_claude, _strip_fences
    from artemis.prompts import VAULT_EXTRACT_SYSTEM, VAULT_EXTRACT_USER

    cap = note["capture_id"]
    fm = note.get("frontmatter") or {}
    raw = note.get("raw_text") or ""
    out = {"extracted": 0, "proposals": 0}

    user = VAULT_EXTRACT_USER.format(
        source=note.get("source", ""), created=note.get("created_at", ""),
        today=_ct_today(), raw_text=raw[:12000],
    )
    resp = _call_claude(VAULT_EXTRACT_SYSTEM, user, model=config.EXTRACT_MODEL, max_tokens=2500)
    data = None
    if resp:
        try:
            parsed = json.loads(_strip_fences(resp))
            if isinstance(parsed, dict):
                data = parsed
        except json.JSONDecodeError:
            logger.error("vault: extraction JSON parse failed for %s", cap)

    if data is None:
        # Mark extracted (cleaned_text = normalized raw) so a permanently-malformed
        # note is not retried forever; no proposals written on a failed parse.
        execute_write("UPDATE vault.notes SET cleaned_text = %s WHERE capture_id = %s",
                      (_normalize_body(raw), cap))
        out["extracted"] = 1
        return out

    cleaned = str(data.get("cleaned_text") or "").strip() or _normalize_body(raw)
    execute_write("UPDATE vault.notes SET cleaned_text = %s WHERE capture_id = %s", (cleaned, cap))
    out["extracted"] = 1

    # note_metadata: deterministic frontmatter tags + inferential one-line summary.
    tags = fm.get("tags")
    if tags:
        _upsert_metadata(cap, "tags", tags if isinstance(tags, list) else [tags], "deterministic")
    summary = str(data.get("summary") or "").strip()
    if summary:
        _upsert_metadata(cap, "summary", summary, "inferential",
                         confidence=0.5, model=config.EXTRACT_MODEL)

    context = _detect_context(raw, fm) or (str(data.get("context")).strip() if data.get("context") else None)
    if context and context.lower() in ("null", "none", ""):
        context = None

    for key, etype in _CANDIDATE_TYPES.items():
        for cand in data.get(key) or []:
            if not isinstance(cand, dict):
                continue
            text = str(cand.get("text", "")).strip()
            evidence = str(cand.get("evidence", "")).strip()
            if not text or not evidence:
                continue  # every candidate must carry a verbatim evidence span
            payload = {k: cand.get(k) for k in ("text", "evidence", "due_date", "direction", "person", "org")
                       if cand.get(k) is not None}
            chash = hashlib.sha256(_proposal_core(etype, cand).encode("utf-8")).hexdigest()
            row = execute_write(
                "INSERT INTO vault.extraction_proposal "
                "(capture_id, extraction_type, payload, context, content_hash) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (capture_id, extraction_type, content_hash) DO NOTHING RETURNING id",
                (cap, etype, json.dumps(payload), context, chash),
            )
            if row:
                out["proposals"] += 1
    _audit("extract", {"capture_id": cap, **out})
    return out


def _upsert_metadata(cap: str, key: str, value, provenance: str,
                     confidence: float | None = None, model: str | None = None) -> None:
    execute_write(
        "INSERT INTO vault.note_metadata (capture_id, key, value, provenance, confidence, model) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (capture_id, key, provenance) "
        "DO UPDATE SET value = EXCLUDED.value, confidence = EXCLUDED.confidence, "
        "model = EXCLUDED.model, created_at = now()",
        (cap, key, json.dumps(value), provenance, confidence, model),
    )


def _run_extraction() -> dict:
    """Extract eligible, not-yet-extracted notes (source in meeting/dictation/thought,
    cleaned_text IS NULL), oldest-first, throttled to _EXTRACT_THROTTLE per run. The
    remainder queues for the next run."""
    totals = {"extracted": 0, "proposals": 0}
    rows = execute_query(
        "SELECT * FROM vault.notes "
        "WHERE deleted_at IS NULL AND source = ANY(%s) AND cleaned_text IS NULL "
        "ORDER BY created_at NULLS LAST, capture_id LIMIT %s",
        (list(_EXTRACT_SOURCES), _EXTRACT_THROTTLE),
    )
    for note in rows:
        try:
            c = _extract_one(note)
            totals["extracted"] += c["extracted"]
            totals["proposals"] += c["proposals"]
        except Exception:
            logger.exception("vault: extraction failed for %s", note.get("capture_id"))
    return totals


# ---------------------------------------------------------------------------
# Proposals — expiry, listing, digest, adjudication
# ---------------------------------------------------------------------------

def expire_stale_proposals() -> int:
    """Auto-expire pending proposals older than 7 days (recoverable via
    `proposals expired`). Returns the count expired."""
    rows = execute_query(
        "UPDATE vault.extraction_proposal SET status = 'expired', adjudicated_at = now() "
        "WHERE status = 'pending' AND created_at < now() - %s * interval '1 day' RETURNING id",
        (_PROPOSAL_EXPIRY_DAYS,),
    )
    if rows:
        _audit("expire_proposals", {"count": len(rows)})
    return len(rows)


def _pending_proposals(today_only: bool) -> list[dict]:
    sql = (
        "SELECT p.id, p.capture_id, p.extraction_type, p.payload, p.context, p.created_at, "
        "n.path, n.source "
        "FROM vault.extraction_proposal p JOIN vault.notes n ON n.capture_id = p.capture_id "
        "WHERE p.status = 'pending' "
    )
    params: tuple = ()
    if today_only:
        sql += "AND (p.created_at AT TIME ZONE 'America/Chicago')::date = %s "
        params = (_ct_today(),)
    rows = execute_query(sql + "ORDER BY p.created_at, p.id", params)
    rows.sort(key=lambda r: (_TYPE_RANK.get(r["extraction_type"], 9), r["created_at"] or datetime.min, r["id"]))
    return rows


def _proposal_line(num: int, p: dict) -> str:
    payload = p["payload"] or {}
    text = str(payload.get("text", "")).strip()
    label = _TYPE_LABEL.get(p["extraction_type"], p["extraction_type"])
    ctx = f" · _{p['context']}_" if p.get("context") else ""
    note = os.path.basename(p["path"])
    line = f"  {num}. [{label}] {text}{ctx}\n       ↳ note: _{note}_"
    ev = str(payload.get("evidence", "")).strip()
    if ev:
        line += f'\n       ↳ _"{ev[:160]}"_'
    return line


def render_digest(today_only: bool, header: str) -> tuple[str, dict]:
    """Render a numbered proposal digest and return (reply, {num: proposal}). Grouped
    by type (decisions → commitments/actions → questions), numbered globally, capped
    at _DIGEST_CAP with the overflow stated explicitly. Everything renders from the
    written rows."""
    rows = _pending_proposals(today_only)
    if not rows:
        scope = "today" if today_only else ""
        return (f"✅ No pending proposals{(' for ' + str(_ct_today())) if today_only else ''}. "
                f"`vault sync` to pull new notes.", {})
    shown = rows[:_DIGEST_CAP]
    mapping: dict[int, dict] = {}
    lines = [f"\U0001f4dd **{header}** ({len(rows)} pending):"]
    current_group = None
    for n, p in enumerate(shown, 1):
        mapping[n] = p
        grp = _TYPE_LABEL.get(p["extraction_type"], p["extraction_type"])
        if grp != current_group:
            current_group = grp
            lines.append(f"\n**{grp.title()}s**")
        lines.append(_proposal_line(n, p))
    if len(rows) > _DIGEST_CAP:
        lines.append(f"\n_+{len(rows) - _DIGEST_CAP} more not shown — approve/reject these first._")
    lines.append("\nApprove: `approve 1-3` · `approve 1 & 4` · `approve all` · reject: `reject 2`")
    return "\n".join(lines), mapping


# ── Approval writers — write through EXISTING creation paths only ──

def _approve_commitment(payload: dict, context: str | None) -> str:
    from artemis import commitments
    cid = commitments.add_commitment(
        title=str(payload.get("text", "")).strip(),
        due_date=payload.get("due_date"), effort_days=1, client="",
        status="active", context=context,
    )
    return f"commitment:{cid}"


def _approve_dossier_entry(payload: dict, note_created) -> str:
    from artemis import dossier
    person = str(payload.get("person", "")).strip() or "Unknown"
    kind, d = dossier.resolve_attendee(person)
    if kind == "unknown":
        d = dossier.create_stub(person, active=False)
    elif kind == "ambiguous":
        d = d[0]  # approval must resolve — take the first (Ryan can correct on the dossier)
    entry_date = note_created.date() if isinstance(note_created, datetime) else (note_created or _ct_today())
    row = execute_one(
        "INSERT INTO acos.dossier_entry (dossier_id, entry_date, entry_text, status, approved_at) "
        "VALUES (%s, %s, %s, 'approved', now()) RETURNING entry_id",
        (d["dossier_id"], entry_date, str(payload.get("text", "")).strip()),
    )
    return f"dossier_entry:{row['entry_id']}"


def _approve_org_fact(payload: dict, note_created) -> str:
    from artemis import dossier
    org = str(payload.get("org", "")).strip().lower() or "unknown"
    dossier._ensure_org_profile(org)
    note_date = note_created.date() if isinstance(note_created, datetime) else (note_created or _ct_today())
    row = execute_one(
        "INSERT INTO acos.org_note (org, note_text, status, approved_at, note_date) "
        "VALUES (%s, %s, 'approved', now(), %s) RETURNING note_id",
        (org, str(payload.get("text", "")).strip(), note_date),
    )
    return f"org_note:{row['note_id']}"


def _approve_proposal(p: dict) -> tuple[bool, str]:
    """Approve one proposal by writing through the existing creation path, then flip
    the row to approved and record the target ref. Returns (ok, target_ref), ok read
    back from the re-read row (no-fabrication)."""
    etype = p["extraction_type"]
    payload = p["payload"] or {}
    note = execute_one("SELECT created_at FROM vault.notes WHERE capture_id = %s", (p["capture_id"],))
    note_created = note["created_at"] if note else None
    try:
        if etype in ("action_item", "commitment"):
            # OPS-1 action-item routing decision (deliberate v1 mapping): an
            # `action_item` proposal is approved through the SAME commitment creation
            # path as a `commitment`, landing in acos.commitments with target_ref
            # `commitment:N`. Investigated acos.action_items — its only creation paths
            # are inline raw SQL in the scheduler (scheduling_request items with a
            # bespoke metadata/due_at shape) and the reminder loop; there is NO
            # sanctioned general-purpose create_action_item() helper, and building one
            # is out of scope here. So both types route to add_commitment. Reporting
            # over vault approvals must therefore account for action_item proposals
            # appearing in acos.commitments, not acos.action_items.
            target = _approve_commitment(payload, p.get("context"))
        elif etype == "dossier_entry":
            target = _approve_dossier_entry(payload, note_created)
        elif etype == "org_fact":
            target = _approve_org_fact(payload, note_created)
        else:
            # decision_candidate / question: no system-of-record table in v1.
            # Approving acknowledges + retires it from the digest (the note stays canon).
            target = "acknowledged"
    except Exception:
        logger.exception("vault: approval write failed for proposal %s (%s)", p["id"], etype)
        return False, ""

    execute_write(
        "UPDATE vault.extraction_proposal SET status = 'approved', adjudicated_at = now(), "
        "target_ref = %s WHERE id = %s AND status = 'pending'",
        (target, p["id"]),
    )
    row = execute_one("SELECT status, target_ref FROM vault.extraction_proposal WHERE id = %s", (p["id"],))
    ok = bool(row and row["status"] == "approved")
    if ok:
        _audit("approve_proposal", {"id": p["id"], "type": etype, "target_ref": target})
    return ok, target


def _reject_proposal(p: dict) -> bool:
    execute_write(
        "UPDATE vault.extraction_proposal SET status = 'rejected', adjudicated_at = now() "
        "WHERE id = %s AND status = 'pending'",
        (p["id"],),
    )
    row = execute_one("SELECT status FROM vault.extraction_proposal WHERE id = %s", (p["id"],))
    ok = bool(row and row["status"] == "rejected")
    if ok:
        _audit("reject_proposal", {"id": p["id"], "type": p["extraction_type"]})
    return ok


def adjudicate(action: str, nums: list[int], mapping: dict) -> str:
    """Approve/reject the numbered proposals against the active digest mapping.
    Mutates `mapping` (retires handled numbers). Renders from re-read rows."""
    if not mapping:
        return "No digest is open — say `digest` first."
    lines: list[str] = []
    for n in nums:
        p = mapping.get(n)
        if not p:
            lines.append(f"\U0001f6ab #{n} — not in the current digest.")
            continue
        label = _TYPE_LABEL.get(p["extraction_type"], p["extraction_type"])
        if action == "approve":
            ok, target = _approve_proposal(p)
            if ok:
                extra = "" if target == "acknowledged" else f" → `{target}`"
                lines.append(f"✅ Approved #{n} [{label}]{extra}")
                mapping.pop(n, None)
            else:
                lines.append(f"⚠️ #{n} — could not approve (already handled?).")
        else:  # reject
            if _reject_proposal(p):
                lines.append(f"\U0001f5d1️ Rejected #{n} [{label}]")
                mapping.pop(n, None)
            else:
                lines.append(f"⚠️ #{n} — could not reject (already handled?).")
    return "\n".join(lines) if lines else "Nothing to do."


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_sync() -> str:
    """`vault sync` — run the pipeline now, reply rendered from ingest_state + the
    row counts actually written."""
    try:
        s = sync_vault()
    except Exception as exc:
        logger.exception("vault sync failed")
        # OPS-1: deterministic self-diagnosis — a known failure class renders its
        # runbook (what failed + literal error + exact fix) and writes an audit row;
        # an unknown one surfaces the raw error labeled unclassified. Nothing was
        # written from a failed run (sync_vault records last_sha only on success).
        from artemis import opsdiag
        return opsdiag.report_failure(exc, {"stage": "vault sync"}, agent="vault")
    sha7 = (s["sha"] or "")[:7]
    if s.get("no_change"):
        return f"Vault already synced to `{sha7}` — no changes."
    bits = []
    if s["new"]:
        bits.append(f"{s['new']} new note{'s' if s['new'] != 1 else ''}")
    if s["changed"]:
        bits.append(f"{s['changed']} changed")
    if s["reappeared"]:
        bits.append(f"{s['reappeared']} reappeared")
    if s["deleted"]:
        bits.append(f"{s['deleted']} deleted")
    if s["skipped"]:
        bits.append(f"{s['skipped']} skipped")
    if s["proposals"]:
        bits.append(f"{s['proposals']} proposal{'s' if s['proposals'] != 1 else ''} created")
    body = ", ".join(bits) if bits else "no changes to notes"
    tail = "  `digest` to review." if s["proposals"] else ""
    return f"✅ Synced to `{sha7}`: {body}.{tail}"


def cmd_status() -> str:
    """`vault status` — last sha/run, note counts by source, pending/expired proposal
    counts, extraction queue depth. All rendered from written rows."""
    sha = _state_get("last_sha")
    last_run = _state_get("last_run")
    by_source = execute_query(
        "SELECT source, count(*) c FROM vault.notes WHERE deleted_at IS NULL GROUP BY source ORDER BY source"
    )
    deleted = execute_one("SELECT count(*) c FROM vault.notes WHERE deleted_at IS NOT NULL")["c"]
    pending = execute_one("SELECT count(*) c FROM vault.extraction_proposal WHERE status = 'pending'")["c"]
    expired = execute_one("SELECT count(*) c FROM vault.extraction_proposal WHERE status = 'expired'")["c"]
    queue = execute_one(
        "SELECT count(*) c FROM vault.notes WHERE deleted_at IS NULL AND source = ANY(%s) "
        "AND cleaned_text IS NULL", (list(_EXTRACT_SOURCES),)
    )["c"]

    lines = ["\U0001f5c4️ **Vault status**"]
    lines.append(f"- Synced to: `{(sha or 'never')[:7] if sha else 'never'}`"
                 + (f" (last run {last_run})" if last_run else ""))
    total = sum(r["c"] for r in by_source)
    lines.append(f"- Notes: {total} live" + (f", {deleted} deleted (retained)" if deleted else ""))
    for r in by_source:
        lines.append(f"    · {r['source']}: {r['c']}")
    lines.append(f"- Proposals: {pending} pending, {expired} expired")
    lines.append(f"- Extraction queue: {queue} note{'s' if queue != 1 else ''} awaiting a pass")

    exp = _parse_pat_expiry(_state_get("pat_expiry"))
    if exp is not None:
        days = (exp - _ct_today()).days
        lines.append(f"- Vault PAT: expires {exp.isoformat()} ({days}d)")

    from artemis.version import VERSION
    lines.append(f"\n_Artemis {VERSION}_")
    return "\n".join(lines)


def cmd_digest() -> tuple[str, dict]:
    """`digest` — sync inline, expire stale, render today's pending proposals."""
    try:
        sync_vault()
    except Exception:
        logger.exception("vault digest: inline sync failed (rendering existing proposals)")
    expire_stale_proposals()
    return render_digest(today_only=True, header="Today's digest")


def cmd_proposals(expired: bool) -> tuple[str, dict]:
    """`proposals` (pending, all dates) / `proposals expired`. Pending returns a
    numbered mapping so approve/reject work; expired is a read-only listing."""
    expire_stale_proposals()
    if expired:
        rows = execute_query(
            "SELECT p.id, p.extraction_type, p.payload, p.created_at, n.path "
            "FROM vault.extraction_proposal p JOIN vault.notes n ON n.capture_id = p.capture_id "
            "WHERE p.status = 'expired' ORDER BY p.created_at DESC, p.id DESC LIMIT 50"
        )
        if not rows:
            return "No expired proposals.", {}
        lines = [f"\U0001f570️ **Expired proposals** ({len(rows)}):"]
        for p in rows:
            payload = p["payload"] or {}
            label = _TYPE_LABEL.get(p["extraction_type"], p["extraction_type"])
            lines.append(f"  • [{label}] {str(payload.get('text', '')).strip()} "
                         f"_({os.path.basename(p['path'])})_")
        return "\n".join(lines), {}
    return render_digest(today_only=False, header="Pending proposals")


# ---------------------------------------------------------------------------
# Morning brief additions
# ---------------------------------------------------------------------------

def _journal_diff_section(mirror: str, day: date) -> str:
    """Journal diff for `day`: compare Ryan's journal note vs that day's extracted
    decision/commitment proposals. Surfaces gaps both ways; writes nothing."""
    journal_path = os.path.join(mirror, "authored", "journal", f"{day.isoformat()}.md")
    if not os.path.isfile(journal_path):
        return f"**Journal diff** — No journal entry for {day.isoformat()}."
    try:
        with open(journal_path, "r", encoding="utf-8") as f:
            _, journal_body = parse_frontmatter(f.read())
    except (OSError, UnicodeDecodeError):
        return f"**Journal diff** — No journal entry for {day.isoformat()}."

    props = execute_query(
        "SELECT p.extraction_type, p.payload FROM vault.extraction_proposal p "
        "JOIN vault.notes n ON n.capture_id = p.capture_id "
        "WHERE p.extraction_type IN ('decision_candidate', 'commitment') "
        "AND (n.created_at AT TIME ZONE 'America/Chicago')::date = %s",
        (day,),
    )
    extracted = "\n".join(
        f"- ({p['extraction_type']}) {str((p['payload'] or {}).get('text', '')).strip()}" for p in props
    ) or "(none extracted)"

    from artemis.briefs import _call_claude, _strip_fences
    from artemis.prompts import (VAULT_JOURNAL_DIFF_SYSTEM, VAULT_JOURNAL_DIFF_USER)
    from artemis.prompts import UNTRUSTED_PREFIX

    resp = _call_claude(
        VAULT_JOURNAL_DIFF_SYSTEM,
        VAULT_JOURNAL_DIFF_USER.format(
            date=day.isoformat(),
            journal_text=UNTRUSTED_PREFIX + journal_body[:6000],
            extracted_text=extracted,
        ),
        model=config.EXTRACT_MODEL, max_tokens=800,
    )
    try:
        data = json.loads(_strip_fences(resp)) if resp else {}
    except json.JSONDecodeError:
        data = {}
    lines = [f"**Journal diff** — {day.isoformat()}"]
    en = data.get("extracted_not_journaled") or []
    jn = data.get("journaled_not_extracted") or []
    if en:
        lines.append("  _Extracted but not journaled (real, or noise?):_")
        lines += [f"    • {x}" for x in en[:5]]
    if jn:
        lines.append("  _Journaled but not extracted (undictated meeting, or after-hours decision?):_")
        lines += [f"    • {x}" for x in jn[:5]]
    if not en and not jn:
        lines.append("  _No gaps — journal and extractions line up._")
    return "\n".join(lines)


def morning_brief_section(mirror: str | None = None) -> str:
    """The PB-011 block appended to the morning brief: pending-proposals digest
    (read-only, capped at 10), yesterday's journal diff, and yesterday's coverage
    line. Read-only — writes nothing. Best-effort; never raises into the brief."""
    parts: list[str] = ["\U0001f5c4️ **Vault**"]
    try:
        warn = _pat_expiry_warning()
        if warn:
            parts.append(warn)
    except Exception:
        logger.exception("vault: PAT expiry warning failed")
    try:
        digest, _ = render_digest(today_only=False, header="Pending proposals")
        parts.append(digest)
        parts.append("_Say `digest` to approve/reject these._")
    except Exception:
        logger.exception("vault: morning digest section failed")

    yesterday = _ct_today() - timedelta(days=1)
    try:
        parts.append(_journal_diff_section(mirror or _mirror_dir(), yesterday))
    except Exception:
        logger.exception("vault: morning journal diff failed")

    cov = _state_get(f"coverage:{yesterday.isoformat()}")
    if cov:
        parts.append(f"**Coverage ({yesterday.isoformat()})** — {cov}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Coverage monitor (weekday 16:30 CT)
# ---------------------------------------------------------------------------

def _real_meeting(e: dict) -> bool:
    """A calendar event that should have been dictated: timed (not all-day), not
    declined by Ryan, and >= 15 minutes."""
    start, end = e.get("start", ""), e.get("end", "")
    if "T" not in start:  # all-day
        return False
    for a in e.get("attendees", []):
        if a.get("self") and a.get("response") == "declined":
            return False
    try:
        s = datetime.fromisoformat(start)
        en = datetime.fromisoformat(end)
        if (en - s) < timedelta(minutes=15):
            return False
    except ValueError:
        pass
    return True


def run_coverage_monitor(calendar, mm) -> str | None:
    """Compare today's real meetings to today's ingested meeting/dictation notes.
    Stores the result for the morning brief; posts ONE nudge per day to CHANNEL_OPS
    when meetings outnumber captures. Returns the posted text, or None."""
    today = _ct_today()
    try:
        events = calendar.get_events_in_range(today, today) if calendar else []
    except Exception:
        logger.exception("vault coverage: calendar fetch failed")
        events = []
    meetings = [e for e in events if _real_meeting(e)]

    captures = execute_one(
        "SELECT count(*) c FROM vault.notes "
        "WHERE deleted_at IS NULL AND source IN ('meeting', 'dictation') "
        "AND (first_ingested_at AT TIME ZONE 'America/Chicago')::date = %s",
        (today,),
    )["c"]

    result = f"{len(meetings)} meeting{'s' if len(meetings) != 1 else ''}, {captures} captured"
    _state_set(f"coverage:{today.isoformat()}", result)

    if len(meetings) <= captures:
        return None
    if _state_get(f"coverage_posted:{today.isoformat()}"):
        return None  # one post per day — no nagging repeats

    names = ", ".join(e.get("summary", "(untitled)") for e in meetings) or "(none named)"
    msg = (
        f"\U0001f5c2️ **Capture check** — {result} today.\n"
        f"Today's meetings: {names}\n"
        f"Some went undictated — dictate now or let them go?"
    )
    if mm:
        try:
            mm.post_message(config.CHANNEL_OPS, msg)
            _state_set(f"coverage_posted:{today.isoformat()}", True)
        except Exception:
            logger.exception("vault coverage: post failed")
            return None
    _audit("coverage_nudge", {"meetings": len(meetings), "captures": captures})
    return msg
