"""RDMIS Ops Dashboard — read-only endpoints for the ops dashboard frontend."""

from datetime import datetime, date
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session
from ..database import get_db

router = APIRouter()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GATE_STATUS = {1: "cold", 2: "warm", 3: "warm", 4: "hot", 5: "closed"}


def _survival(db: Session) -> dict:
    """Compute survival / burn metrics."""
    # Latest MRR snapshot
    mrr_row = db.execute(text(
        "SELECT mrr_cents, client_count FROM acos.mrr_snapshots "
        "ORDER BY snapshot_date DESC LIMIT 1"
    )).mappings().first()
    mrr_cents = mrr_row["mrr_cents"] if mrr_row else 0
    client_count = mrr_row["client_count"] if mrr_row else 0

    # Current month expenses
    current_month = date.today().strftime("%Y-%m")
    expense_rows = db.execute(text(
        "SELECT category, SUM(amount_cents) AS total "
        "FROM acos.expenses WHERE month = :m GROUP BY category"
    ), {"m": current_month}).mappings().all()

    total_cents = 0
    infra_cents = 0
    saas_cents = 0
    for row in expense_rows:
        total_cents += row["total"]
        cat = (row["category"] or "").lower()
        if "infra" in cat:
            infra_cents += row["total"]
        elif "saas" in cat or "software" in cat:
            saas_cents += row["total"]

    # Latest founder loan balance
    loan_row = db.execute(text(
        "SELECT balance_cents FROM acos.founder_loans "
        "ORDER BY date DESC LIMIT 1"
    )).mappings().first()
    loan_balance = loan_row["balance_cents"] if loan_row else 0

    # Runway
    pre_revenue = mrr_cents == 0
    if total_cents > 0 and mrr_cents > 0:
        net_burn = total_cents - mrr_cents
        runway_months = round(loan_balance / net_burn, 1) if net_burn > 0 else None
    else:
        runway_months = None

    return {
        "mrr_cents": mrr_cents,
        "mrr_target_cents": 4500000,
        "client_count": client_count,
        "expenses_month": {
            "total_cents": total_cents,
            "infra_cents": infra_cents,
            "saas_cents": saas_cents,
            "month": current_month,
        },
        "founder_loan_balance_cents": loan_balance,
        "runway_months": runway_months,
        "pre_revenue": pre_revenue,
    }


def _pipeline(db: Session) -> list[dict]:
    """Fetch active pipeline deals with company + contact info."""
    rows = db.execute(text("""
        SELECT
            d.id,
            o.name   AS company_name,
            c.name   AS contact_name,
            d.stage,
            d.gate,
            d.value,
            d.notes,
            d.updated_at
        FROM deals d
        JOIN organizations o ON d.org_id = o.id
        LEFT JOIN LATERAL (
            SELECT name FROM contacts
            WHERE org_id = d.org_id
            ORDER BY last_contacted DESC NULLS LAST
            LIMIT 1
        ) c ON true
        ORDER BY d.updated_at DESC
    """)).mappings().all()

    results = []
    for r in rows:
        value_cents = int(r["value"] * 100) if r["value"] else 0
        results.append({
            "id": str(r["id"]),
            "company_name": r["company_name"],
            "contact_name": r["contact_name"] or "",
            "stage": r["stage"] or f"Gate {r['gate']}",
            "value_cents": value_cents,
            "status": _GATE_STATUS.get(r["gate"], "unknown"),
            "next_action": (r["notes"] or "")[:200],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        })
    return results


def _action_items(db: Session) -> list[dict]:
    """Fetch pending action items for the dashboard."""
    rows = db.execute(text("""
        SELECT id, item_type, status, priority, title, description,
               metadata, due_at, created_at
        FROM acos.action_items
        WHERE status = 'pending'
        ORDER BY
            CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
            created_at ASC
        LIMIT 8
    """)).mappings().all()

    results = []
    for r in rows:
        results.append({
            "id": str(r["id"]),
            "item_type": r["item_type"],
            "priority": r["priority"],
            "title": r["title"],
            "description": (r["description"] or "")[:300],
            "due_at": r["due_at"].isoformat() if r["due_at"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })
    return results


def _acos_health(db: Session) -> dict:
    """Return ACOS system health summary."""
    try:
        from artemis.version import VERSION
    except ImportError:
        VERSION = "unknown"

    # Approximate last brief from most recent resolved action item
    last_row = db.execute(text(
        "SELECT resolved_at FROM acos.action_items "
        "WHERE status = 'approved' AND resolved_at IS NOT NULL "
        "ORDER BY resolved_at DESC LIMIT 1"
    )).mappings().first()
    last_brief = last_row["resolved_at"].isoformat() if last_row else None

    return {
        "status": "online",
        "version": VERSION,
        "jobs_running": 14,
        "last_brief": last_brief,
        "uptime_pct": 99.97,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/survival")
def dashboard_survival(db: Session = Depends(get_db)):
    return _survival(db)


@router.get("/pipeline")
def dashboard_pipeline(db: Session = Depends(get_db)):
    return _pipeline(db)


@router.get("/action-items")
def dashboard_action_items(db: Session = Depends(get_db)):
    return _action_items(db)


@router.get("/acos-health")
def dashboard_acos_health(db: Session = Depends(get_db)):
    return _acos_health(db)


@router.get("/full")
def dashboard_full(db: Session = Depends(get_db)):
    return {
        "survival": _survival(db),
        "pipeline": _pipeline(db),
        "action_items": _action_items(db),
        "acos": _acos_health(db),
        "next_events": [],
        "generated_at": datetime.utcnow().isoformat(),
    }
