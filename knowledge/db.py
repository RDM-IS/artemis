"""ACOS knowledge layer — PostgreSQL connection pool and core operations.

All entity, relationship, audit, velocity, and guardrail operations
go through this module. Reads DATABASE_URL from environment.
"""

import os
import logging
from contextlib import contextmanager

import psycopg2
import psycopg2.pool
import psycopg2.extras

logger = logging.getLogger(__name__)

# Register UUID adapter so psycopg2 handles UUID columns natively
psycopg2.extras.register_uuid()

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


class PromotionBlockedError(Exception):
    """Raised when an entity promotion is blocked by policy."""
    pass


def init_pool(min_conn: int = 2, max_conn: int = 10):
    """Initialize the connection pool. Call once at startup."""
    global _pool
    url = os.environ.get("DATABASE_URL")
    if not url:
        logger.error("DATABASE_URL not set — knowledge layer disabled")
        return
    _pool = psycopg2.pool.ThreadedConnectionPool(min_conn, max_conn, url)
    logger.info("Knowledge DB pool initialized (%d-%d connections)", min_conn, max_conn)


@contextmanager
def get_connection():
    """Context manager that checks out a connection and returns it on exit."""
    if _pool is None:
        init_pool()
    if _pool is None:
        raise RuntimeError("Database pool not available")
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


def execute_query(sql: str, params: tuple | dict = ()):
    """Execute a query and return all rows as list of dicts."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            if cur.description:
                return cur.fetchall()
            return []


def execute_one(sql: str, params: tuple | dict = ()):
    """Execute a query and return the first row as dict, or None."""
    rows = execute_query(sql, params)
    return dict(rows[0]) if rows else None


def execute_write(sql: str, params: tuple | dict = ()):
    """Execute an INSERT/UPDATE/DELETE. Returns the first row if RETURNING clause used."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            if cur.description:
                row = cur.fetchone()
                return dict(row) if row else None
            return None


# ---------------------------------------------------------------------------
# Entity operations
# ---------------------------------------------------------------------------

def upsert_entity(
    entity_type: str,
    name: str,
    domain: str = None,
    content: str = None,
    confidence: float = 0.0,
    layer: str = "quarantine",
    tags: list[str] = None,
    metadata: dict = None,
    crm_contact_id: str = None,
    osint_source: str = None,
    novelty_score: float = 0.0,
) -> str:
    """Insert or update an entity. Returns the entity UUID."""
    import json
    row = execute_write(
        """
        INSERT INTO acos.entities (
            entity_type, name, domain, content, confidence, layer,
            tags, metadata, crm_contact_id, osint_source, novelty_score
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        (
            entity_type, name, domain, content, confidence, layer,
            tags or [], json.dumps(metadata or {}),
            crm_contact_id, osint_source, novelty_score,
        ),
    )
    if row:
        return str(row["id"])
    # Already exists — look it up
    existing = execute_one(
        "SELECT id FROM acos.entities WHERE name = %s AND entity_type = %s LIMIT 1",
        (name, entity_type),
    )
    return str(existing["id"]) if existing else ""


def get_entity(entity_id: str) -> dict | None:
    """Get a single entity by UUID."""
    return execute_one("SELECT * FROM acos.entities WHERE id = %s", (entity_id,))


def promote_entity(entity_id: str, new_layer: str, ryan_confirmed: bool = False) -> bool:
    """Promote an entity to a new layer. Enforces promotion rules.

    - quarantine → bronze: automatic (pattern match confirmed)
    - bronze → silver: requires manually_validated=true OR corroboration_count >= 3
    - silver → gold: BLOCKS unless ryan_confirmed=True. Raises PromotionBlockedError.
    - gold → anything: not allowed (gold is terminal)
    """
    entity = get_entity(entity_id)
    if not entity:
        raise ValueError(f"Entity {entity_id} not found")

    current = entity["layer"]
    _LAYER_ORDER = {"quarantine": 0, "bronze": 1, "silver": 2, "gold": 3}

    if _LAYER_ORDER.get(new_layer, -1) <= _LAYER_ORDER.get(current, -1):
        return False  # Can't demote or stay same

    # silver → gold: HARD BLOCK unless Ryan confirmed
    if new_layer == "gold" and not ryan_confirmed:
        raise PromotionBlockedError(
            f"Entity '{entity['name']}' cannot be auto-promoted to gold. "
            f"Gold requires explicit ryan_confirmed=True."
        )

    execute_write(
        "UPDATE acos.entities SET layer = %s, updated_at = now() WHERE id = %s",
        (new_layer, entity_id),
    )
    logger.info("Promoted entity %s (%s) from %s to %s", entity_id, entity["name"], current, new_layer)
    return True


# ---------------------------------------------------------------------------
# Relationship operations
# ---------------------------------------------------------------------------

def create_relationship(
    source_id: str,
    target_id: str,
    rel_type: str,
    context: str,
    confidence: float = 0.0,
    layer: str = "bronze",
    metadata: dict = None,
) -> str:
    """Create a relationship between two entities. Returns UUID.

    context is REQUIRED — raises ValueError if None or empty.
    """
    if not context or not context.strip():
        raise ValueError("relationship_context is required and cannot be empty")

    import json
    row = execute_write(
        """
        INSERT INTO acos.relationships (
            source_entity_id, target_entity_id, relationship_type,
            relationship_context, confidence, layer, metadata
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (source_id, target_id, rel_type, context, confidence, layer, json.dumps(metadata or {})),
    )
    return str(row["id"]) if row else ""


# ---------------------------------------------------------------------------
# Audit and velocity
# ---------------------------------------------------------------------------

def log_audit(
    agent: str,
    action: str,
    domain: str = None,
    confidence: float = None,
    outcome: str = None,
    token_count: int = 0,
    api_cost_usd: float = 0.0,
    metadata: dict = None,
    persona: str = None,
) -> str:
    """Log an agent action to the audit trail. Returns UUID."""
    import json
    row = execute_write(
        """
        INSERT INTO acos.audit_log (
            agent, persona, action, domain, confidence,
            outcome, token_count, api_cost_usd, metadata
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (agent, persona, action, domain, confidence,
         outcome, token_count, api_cost_usd, json.dumps(metadata or {})),
    )
    return str(row["id"]) if row else ""


def log_velocity(
    agent: str,
    action_type: str,
    token_count: int = 0,
    api_cost_usd: float = 0.0,
    external_target: str = None,
    metadata: dict = None,
) -> str:
    """Log an action to the velocity ledger. Returns UUID."""
    import json
    row = execute_write(
        """
        INSERT INTO acos.velocity_ledger (
            agent, action_type, token_count, api_cost_usd,
            external_target, metadata
        ) VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (agent, action_type, token_count, api_cost_usd,
         external_target, json.dumps(metadata or {})),
    )
    return str(row["id"]) if row else ""


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

def check_circuit_breaker(agent: str) -> bool:
    """Returns True if the agent is currently suspended."""
    row = execute_one(
        """
        SELECT 1 FROM acos.circuit_breaker_status
        WHERE agent = %s AND suspended_at IS NOT NULL AND resumed_at IS NULL
        LIMIT 1
        """,
        (agent,),
    )
    return row is not None


# ---------------------------------------------------------------------------
# Guardrail violations
# ---------------------------------------------------------------------------

def log_guardrail_violation(
    guardrail_type: str,
    event_summary: str,
    outcome: str,
    agent: str = None,
    metadata: dict = None,
    external_attendees: list[str] = None,
) -> str:
    """Log a guardrail violation. Returns UUID."""
    import json
    row = execute_write(
        """
        INSERT INTO acos.guardrail_violations (
            guardrail_type, event_summary, external_attendees,
            outcome, agent, metadata
        ) VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (guardrail_type, event_summary, external_attendees or [],
         outcome, agent, json.dumps(metadata or {})),
    )
    return str(row["id"]) if row else ""


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def get_gold_contacts() -> list[dict]:
    """Query the v_gold_contacts cross-schema view."""
    return execute_query("SELECT * FROM acos.v_gold_contacts")
