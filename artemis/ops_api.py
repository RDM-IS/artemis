"""OPS-2 — Engagement Ops API (ops.rdm.is backend).

A thin HTTP surface over the SAME backend functions the Mattermost verbs use — no
second command path, no new write logic. It runs in the Artemis bot process (a Flask
blueprint on the existing app, port 5001), so it imports artemis natively, shares the
live knowledge.db pool, and can read the live scheduler for a TRUTHFUL health panel.
Exposed to ops.rdm.is via a cloudflared tunnel and gated by Cloudflare Access — every
route requires a verified Access JWT (see artemis.ops_access). No anonymous read of
any panel; no anonymous mutation.

Two levels:
  * Portfolio (GET /api/portfolio)          — one card per engagement + health strip.
  * Engagement (GET /api/engagements/<slug>) — the daily working view (approval queue
    centerpiece, commitments, dossiers, projects, header).

Adjudication reuses vault.approve_proposal_by_id / reject_proposal_by_id (which call
the very same _approve_proposal / _reject_proposal write-through as `approve`/`reject`
in Mattermost) and dossier._approve_item / drop_item for dossier drafts. Every
user-facing result is rendered from re-read rows, never an LLM claim.

No-fabrication + scoping: `context = slug` scopes proposals/commitments to an
engagement; NULL-context items are surfaced in a visible "unscoped" section (never
silently filtered) and on the portfolio pending badge.
"""

import logging
import os
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, request

from artemis import commitments, dossier, utils, vault
from artemis.ops_access import current_actor, require_access
from knowledge.db import execute_one, execute_query, log_audit

logger = logging.getLogger(__name__)

_CT = ZoneInfo("America/Chicago")

bp = Blueprint("ops_api", __name__, url_prefix="/api")

# The SPA origin allowed to call this API with credentials. Same-origin deploys
# (SPA + API behind one Access-protected hostname) don't need CORS at all; this
# supports the two-hostname layout (ops.rdm.is SPA → ops-api.rdm.is, both in the
# same Access application) where the browser sends the Access cookie cross-subdomain.
_ALLOWED_ORIGIN = os.environ.get("OPS_ALLOWED_ORIGIN", "https://ops.rdm.is")


@bp.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = _ALLOWED_ORIGIN
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Vary"] = "Origin"
    return resp


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _ct_today() -> date:
    return datetime.now(_CT).date()


def _iso(v):
    """ISO string for a datetime/date, else the value unchanged (None stays None)."""
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _as_date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v:
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def _days_to(d: date | None) -> int | None:
    return (d - _ct_today()).days if d else None


def _ops_audit(action: str, metadata: dict) -> None:
    """Attribute an ops-surface mutation to the Access-verified human. The underlying
    gate functions also write their own domain audit rows; this row records WHO acted
    through the UI (existing log_audit convention, best-effort)."""
    try:
        log_audit(agent="ops-ui", action=action, domain="engagement-ops",
                  metadata={**metadata, "actor": current_actor()})
    except Exception:
        logger.debug("ops audit write failed (%s)", action, exc_info=True)


# ---------------------------------------------------------------------------
# ACOS health strip — every field truthful or absent (OPS-2 honesty requirement).
# jobs/uptime read the LIVE scheduler + process clock in-process; last_brief reads
# the durable acos.system_state slot the morning-brief job now writes.
# ---------------------------------------------------------------------------

def _live_process_stats() -> tuple[int | None, int | None]:
    try:
        import artemis.main as m
        jobs = len(m._sched.scheduler.get_jobs()) if getattr(m, "_sched", None) else None
        uptime = int(time.time() - m._start_time) if getattr(m, "_start_time", 0) else None
        return jobs, uptime
    except Exception:
        logger.debug("ops health: live process stats unavailable", exc_info=True)
        return None, None


def _health_payload() -> dict:
    try:
        from artemis.version import VERSION
    except Exception:
        VERSION = "unknown"
    jobs, uptime = _live_process_stats()
    last_brief = None
    try:
        from artemis.quiet_hours import get_system_value
        last_brief = get_system_value("last_morning_brief_at")
    except Exception:
        logger.debug("ops health: last_brief lookup failed", exc_info=True)
    return {
        "status": "online",
        "version": VERSION,
        "scheduler_jobs": jobs,          # null (not a fake number) if unavailable
        "uptime_seconds": uptime,        # honest elapsed seconds; no fabricated %
        "last_brief": last_brief,        # ISO string from system_state, or null
        "generated_at": datetime.now(_CT).isoformat(),
    }


# ---------------------------------------------------------------------------
# Serializers (render from written rows)
# ---------------------------------------------------------------------------

def _proposal_json(p: dict) -> dict:
    """Shape a vault.extraction_proposal row for the approval queue. `effective` is
    what approval will write (payload_final when edited, else payload); `payload` is
    the untouched original so the UI can show proposed-vs-edited."""
    payload = p.get("payload") or {}
    final = p.get("payload_final")
    effective = final or payload
    today = _ct_today()
    due = effective.get("due_date")
    return {
        "id": p["id"],
        "type": p["extraction_type"],
        "type_label": vault._TYPE_LABEL.get(p["extraction_type"], p["extraction_type"]),
        "context": p.get("context"),
        "payload": payload,
        "payload_final": final,
        "effective": effective,
        "text": str(effective.get("text", "")).strip(),
        "evidence": str(effective.get("evidence", payload.get("evidence", "")) or "").strip(),
        "due_date": due,
        "due_label": utils.describe_due(_as_date(due), today) if due else None,
        "direction": effective.get("direction"),
        "person": effective.get("person"),
        "org": effective.get("org"),
        "edited": final is not None,
        "capture_id": p.get("capture_id"),
        "note": os.path.basename(p["path"]) if p.get("path") else None,
        "note_path": p.get("path"),
        "source": p.get("source"),
        "created_at": _iso(p.get("created_at")),
    }


def _commitment_json(c: dict, today: date) -> dict:
    due = _as_date(c.get("due_date"))
    return {
        "id": c["id"],
        "title": c.get("title"),
        "status": c.get("status"),
        "client": c.get("client") or "",
        "context": c.get("context"),
        "due_date": _iso(c.get("due_date")),
        "due_label": utils.describe_due(due, today),
        "effort_days": c.get("effort_days"),
        "created_at": _iso(c.get("created_at")),
    }


def _dossier_card(d: dict) -> dict:
    """A person card with current title/org resolved from the assignment ledger."""
    title = org = reports_to = None
    try:
        a = dossier.current_assignment(d["dossier_id"])
        if a:
            title, org, reports_to = a.get("title"), a.get("org"), a.get("reports_to")
    except Exception:
        logger.debug("ops: assignment lookup failed for %s", d.get("dossier_id"), exc_info=True)
    return {
        "dossier_id": d["dossier_id"],
        "slug": d.get("slug"),
        "full_name": d.get("full_name"),
        "active": d.get("active"),
        "title": title,
        "org": org,
        "reports_to": reports_to,
        "updated_at": _iso(d.get("updated_at")),
    }


# ---------------------------------------------------------------------------
# Panel builders
# ---------------------------------------------------------------------------

def _engagement(slug: str) -> dict | None:
    return execute_one("SELECT * FROM acos.engagement WHERE slug = %s", (slug,))


def _engagement_header(e: dict) -> dict:
    d = _as_date(e.get("next_hard_date"))
    return {
        "slug": e["slug"],
        "display_name": e["display_name"],
        "next_hard_date": _iso(e.get("next_hard_date")),
        "next_hard_date_label": e.get("next_hard_date_label"),
        "days_to_hard_date": _days_to(d),
        "active": e.get("active"),
        "archived": e.get("archived"),
    }


def _scoped_commitments(slug: str, today: date) -> list[dict]:
    rows = execute_query(
        "SELECT * FROM acos.commitments WHERE status = 'active' AND context = %s "
        "ORDER BY due_date NULLS LAST, id",
        (slug,),
    )
    return [_commitment_json(c, today) for c in rows]


def _recent_people(limit: int = 12) -> list[dict]:
    rows = execute_query(
        "SELECT * FROM acos.dossier WHERE active = true "
        "ORDER BY updated_at DESC NULLS LAST, dossier_id DESC LIMIT %s",
        (limit,),
    )
    return [_dossier_card(d) for d in rows]


def _projects(slug: str) -> list[dict]:
    """Thin projects panel: vault notes under authored/projects/, each with a cheap
    link to open commitments (context = engagement AND title mentions the note stem).
    No new project schema in v1 — this reads vault.notes only."""
    notes = execute_query(
        "SELECT capture_id, path, frontmatter, created_at FROM vault.notes "
        "WHERE path LIKE 'authored/projects/%%' AND deleted_at IS NULL "
        "ORDER BY created_at DESC NULLS LAST, capture_id LIMIT 30"
    )
    out: list[dict] = []
    for n in notes:
        fm = n.get("frontmatter") or {}
        stem = os.path.splitext(os.path.basename(n["path"]))[0]
        title = str(fm.get("title") or stem).strip()
        links = execute_query(
            "SELECT id, title, due_date, status FROM acos.commitments "
            "WHERE status = 'active' AND context = %s AND title ILIKE %s "
            "ORDER BY due_date NULLS LAST, id",
            (slug, f"%{stem}%"),
        )
        out.append({
            "capture_id": n["capture_id"],
            "title": title,
            "path": n["path"],
            "created_at": _iso(n.get("created_at")),
            "linked_commitments": [
                {"id": c["id"], "title": c["title"], "due_date": _iso(c["due_date"]),
                 "status": c["status"]}
                for c in links
            ],
        })
    return out


def _dossier_drafts() -> list[dict]:
    """Pending dossier drafts (extracted entries/to-dos/notes) awaiting review — the
    SAME rows `dossier review` adjudicates. Not engagement-scoped in v1 (dossiers carry
    no context tag); shown as their own approval-queue group.

    Provenance caveat (stated in the PR): evidence spans for most draft types live in
    an in-memory map in the bot process and may be absent here; the draft rows and
    their text/provenance labels are durable and shown."""
    try:
        items = dossier.pending_items()
    except Exception:
        logger.exception("ops: dossier pending_items failed")
        return []
    return [
        {
            "draft_type": it.get("type"),
            "id": it.get("id"),
            "dossier_id": it.get("dossier_id"),
            "dossier_name": it.get("dossier_name"),
            "text": it.get("text"),
            "provenance": it.get("prov"),
            "evidence": it.get("evidence"),
            "label": it.get("label"),
        }
        for it in items
    ]


# ---------------------------------------------------------------------------
# Routes — portfolio level
# ---------------------------------------------------------------------------

@bp.get("/health")
@require_access
def health():
    return jsonify(_health_payload())


@bp.get("/portfolio")
@require_access
def portfolio():
    engagements = execute_query(
        "SELECT * FROM acos.engagement WHERE archived = false ORDER BY active DESC, slug"
    )
    cards = []
    scoped_pending_total = 0
    for e in engagements:
        slug = e["slug"]
        pending = execute_one(
            "SELECT count(*) c FROM vault.extraction_proposal "
            "WHERE status = 'pending' AND context = %s", (slug,))["c"]
        open_commits = execute_one(
            "SELECT count(*) c FROM acos.commitments "
            "WHERE status = 'active' AND context = %s", (slug,))["c"]
        last_cap = execute_one(
            "SELECT max(n.first_ingested_at) ts FROM vault.notes n "
            "WHERE n.capture_id IN "
            "(SELECT capture_id FROM vault.extraction_proposal WHERE context = %s)",
            (slug,))
        last_ts = last_cap["ts"] if last_cap else None
        scoped_pending_total += pending
        cards.append({
            **_engagement_header(e),
            "pending_proposals": pending,
            "open_commitments": open_commits,
            "last_capture": _iso(last_ts),
            "last_capture_staleness_days": (_ct_today() - last_ts.date()).days
            if isinstance(last_ts, datetime) else None,
        })

    unscoped_pending = execute_one(
        "SELECT count(*) c FROM vault.extraction_proposal "
        "WHERE status = 'pending' AND context IS NULL")["c"]
    unscoped_commits = execute_one(
        "SELECT count(*) c FROM acos.commitments "
        "WHERE status = 'active' AND context IS NULL")["c"]

    return jsonify({
        "engagements": cards,
        "unscoped": {"pending_proposals": unscoped_pending, "open_commitments": unscoped_commits},
        "pending_total": scoped_pending_total + unscoped_pending,
        "health": _health_payload(),
        "generated_at": datetime.now(_CT).isoformat(),
    })


# ---------------------------------------------------------------------------
# Routes — engagement level
# ---------------------------------------------------------------------------

@bp.get("/engagements/<slug>")
@require_access
def engagement_detail(slug):
    e = _engagement(slug)
    if not e:
        return jsonify({"error": "not_found", "detail": f"no engagement '{slug}'"}), 404
    today = _ct_today()
    scoped = [_proposal_json(p) for p in vault.proposals_for_context(slug)]
    unscoped = [_proposal_json(p) for p in vault.proposals_for_context(None, unscoped=True)]
    drafts = _dossier_drafts()
    commits = _scoped_commitments(slug, today)
    return jsonify({
        "engagement": _engagement_header(e),
        "approval_queue": {
            "scoped": scoped,
            "unscoped": unscoped,
            "dossier_drafts": drafts,
            "pending_count": len(scoped) + len(unscoped) + len(drafts),
        },
        "commitments": commits,
        "dossiers": _recent_people(),
        "projects": _projects(slug),
        "health": _health_payload(),
        "generated_at": datetime.now(_CT).isoformat(),
    })


# ---------------------------------------------------------------------------
# Routes — proposal adjudication (reuses vault write-through)
# ---------------------------------------------------------------------------

def _edited_payload_from_request() -> dict | None:
    """An edit-then-approve body carries {"payload_final": {...}} (or a flat set of
    edited fields). Return the edited payload dict, or None for a plain approve."""
    body = request.get_json(silent=True) or {}
    edited = body.get("payload_final")
    if isinstance(edited, dict):
        return edited
    # Convenience: accept flat edited fields too.
    flat = {k: body[k] for k in ("text", "due_date", "direction", "person", "org", "evidence")
            if k in body}
    return flat or None


@bp.post("/proposals/<int:pid>/approve")
@require_access
def approve_proposal(pid):
    edited = _edited_payload_from_request()
    ok, target = vault.approve_proposal_by_id(pid, edited)
    if not ok:
        return jsonify({"ok": False, "id": pid,
                        "detail": "not pending or approval write failed"}), 409
    _ops_audit("approve_proposal", {"id": pid, "target_ref": target, "edited": edited is not None})
    return jsonify({"ok": True, "id": pid, "target_ref": target, "edited": edited is not None})


@bp.post("/proposals/<int:pid>/reject")
@require_access
def reject_proposal(pid):
    ok = vault.reject_proposal_by_id(pid)
    if not ok:
        return jsonify({"ok": False, "id": pid, "detail": "not pending or reject failed"}), 409
    _ops_audit("reject_proposal", {"id": pid})
    return jsonify({"ok": True, "id": pid})


@bp.post("/proposals/batch")
@require_access
def batch_proposals():
    body = request.get_json(silent=True) or {}
    action = str(body.get("action", "")).lower()
    ids = body.get("ids") or []
    if action not in ("approve", "reject") or not isinstance(ids, list):
        return jsonify({"error": "bad_request",
                        "detail": "body: {action: approve|reject, ids: [int]}"}), 400
    results = []
    for raw in ids:
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            results.append({"id": raw, "ok": False, "detail": "not an int"})
            continue
        if action == "approve":
            ok, target = vault.approve_proposal_by_id(pid)
            results.append({"id": pid, "ok": ok, "target_ref": target})
        else:
            ok = vault.reject_proposal_by_id(pid)
            results.append({"id": pid, "ok": ok})
    _ops_audit(f"batch_{action}_proposals",
               {"ids": ids, "ok_count": sum(1 for r in results if r["ok"])})
    return jsonify({"action": action, "results": results})


# ---------------------------------------------------------------------------
# Routes — dossier-draft adjudication (approve/reject only in v1)
# ---------------------------------------------------------------------------

def _find_draft(draft_type: str, draft_id: int) -> dict | None:
    for it in dossier.pending_items():
        if it.get("type") == draft_type and it.get("id") == draft_id:
            return it
    return None


@bp.post("/dossier-drafts/approve")
@require_access
def approve_dossier_draft():
    body = request.get_json(silent=True) or {}
    dtype, did = body.get("type"), body.get("id")
    it = _find_draft(dtype, did) if dtype and isinstance(did, int) else None
    if not it:
        return jsonify({"ok": False, "detail": "draft not found or already handled"}), 409
    ok = dossier._approve_item(it)
    if not ok:
        return jsonify({"ok": False, "detail": "approval write failed"}), 409
    _ops_audit("approve_dossier_draft", {"type": dtype, "id": did})
    return jsonify({"ok": True, "type": dtype, "id": did})


@bp.post("/dossier-drafts/reject")
@require_access
def reject_dossier_draft():
    body = request.get_json(silent=True) or {}
    dtype, did = body.get("type"), body.get("id")
    it = _find_draft(dtype, did) if dtype and isinstance(did, int) else None
    if not it:
        return jsonify({"ok": False, "detail": "draft not found or already handled"}), 409
    # Reuse the exact reject path (`drop`) the Mattermost review uses.
    msg = dossier.drop_item(1, {1: it})
    _ops_audit("reject_dossier_draft", {"type": dtype, "id": did})
    return jsonify({"ok": True, "type": dtype, "id": did, "detail": msg})


# ---------------------------------------------------------------------------
# Routes — commitments
# ---------------------------------------------------------------------------

@bp.post("/commitments/<int:cid>/close")
@require_access
def close_commitment(cid):
    result = commitments.close_commitment_by_id(cid)
    if result.get("status") != "closed":
        return jsonify({"ok": False, "id": cid, "detail": "no active commitment with that id"}), 409
    _ops_audit("close_commitment", {"id": cid, "title": result.get("title")})
    return jsonify({"ok": True, "id": cid, "title": result.get("title")})


# ---------------------------------------------------------------------------
# Routes — dossier / org reads (read-only)
# ---------------------------------------------------------------------------

@bp.get("/dossier/search")
@require_access
def dossier_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"people": [], "orgs": []})
    people = [_dossier_card(d) for d in dossier._find_dossiers(q)]
    orgs = execute_query(
        "SELECT org, display_name FROM acos.org_profile "
        "WHERE org ILIKE %s OR display_name ILIKE %s ORDER BY org LIMIT 10",
        (f"%{q}%", f"%{q}%"),
    )
    return jsonify({"people": people, "orgs": [dict(o) for o in orgs]})


@bp.get("/dossier/person/<slug>")
@require_access
def dossier_person(slug):
    d = dossier.get_dossier_by_slug(slug)
    if not d:
        return jsonify({"error": "not_found"}), 404
    card = _dossier_card(d)
    entries = execute_query(
        "SELECT entry_date, entry_text, status FROM acos.dossier_entry "
        "WHERE dossier_id = %s AND status = 'approved' "
        "ORDER BY entry_date DESC, entry_id DESC LIMIT 20",
        (d["dossier_id"],),
    )
    open_commits = execute_query(
        "SELECT id, title, due_date, status FROM acos.commitments "
        "WHERE dossier_id = %s AND status IN ('active', 'draft') ORDER BY due_date NULLS LAST",
        (d["dossier_id"],),
    )
    return jsonify({
        **card,
        "position_terrain": d.get("position_terrain"),
        "needs_from_me": d.get("needs_from_me"),
        "entries": [{"entry_date": _iso(e["entry_date"]), "text": e["entry_text"],
                     "status": e["status"]} for e in entries],
        "commitments": [{"id": c["id"], "title": c["title"], "due_date": _iso(c["due_date"]),
                         "status": c["status"]} for c in open_commits],
    })


@bp.get("/dossier/org/<org>")
@require_access
def dossier_org(org):
    prof = dossier.get_org_profile(org)
    notes = []
    try:
        notes = dossier._approved_notes(org, limit=20)
    except Exception:
        logger.debug("ops: approved notes failed for %s", org, exc_info=True)
    people = execute_query(
        "SELECT a.dossier_id, a.title, a.reports_to, d.slug, d.full_name "
        "FROM acos.org_assignment a JOIN acos.dossier d ON d.dossier_id = a.dossier_id "
        "WHERE lower(a.org) = lower(%s) AND a.valid_to IS NULL AND a.status = 'approved' "
        "ORDER BY a.is_root DESC NULLS LAST, d.full_name",
        (org,),
    )
    return jsonify({
        "org": org,
        "display_name": (prof or {}).get("display_name") if prof else org,
        "overview": (prof or {}).get("overview"),
        "active_work": (prof or {}).get("active_work"),
        "opportunities": (prof or {}).get("opportunities"),
        "exists": prof is not None,
        "notes": [{"text": n["note_text"], "note_date": _iso(n["note_date"])} for n in notes],
        "people": [{"dossier_id": p["dossier_id"], "slug": p["slug"], "full_name": p["full_name"],
                    "title": p["title"], "reports_to": p["reports_to"]} for p in people],
    })


# ---------------------------------------------------------------------------
# Blueprint-level error handler — an ops route must never bring down the bot.
# ---------------------------------------------------------------------------

@bp.errorhandler(Exception)
def _handle_error(exc):
    logger.exception("ops API error on %s", request.path)
    return jsonify({"error": "internal", "detail": str(exc)}), 500


def register(app) -> None:
    """Register the ops API on the bot's Flask app. Called from main after startup so
    the health panel can read the live scheduler. Idempotent-safe (Flask raises on a
    duplicate blueprint name, which we swallow)."""
    try:
        app.register_blueprint(bp)
        logger.info("OPS-2 ops API registered at /api (Cloudflare Access enforced)")
    except Exception:
        logger.exception("Failed to register ops API blueprint")
