from fastapi import FastAPI, Depends, HTTPException, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader
from mangum import Mangum
from sqlalchemy import text
import os
import subprocess
import sys

# Ensure repo root is on sys.path so knowledge.secrets is importable
_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from knowledge.secrets import get_crm_api_key
from .database import get_db
from .routers import (
    organizations, contacts, deals, interactions,
    commitments, invoices, founder_loans, webhooks,
    dashboard,
)

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=True)

_API_KEY = None

def verify_api_key(api_key: str = Security(API_KEY_HEADER)):
    global _API_KEY
    if _API_KEY is None:
        _API_KEY = get_crm_api_key()
    if api_key != _API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return api_key

app = FastAPI(title="RDMIS CRM API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(organizations.router, prefix="/organizations", tags=["organizations"], dependencies=[Depends(verify_api_key)])
app.include_router(contacts.router, prefix="/contacts", tags=["contacts"], dependencies=[Depends(verify_api_key)])
app.include_router(deals.router, prefix="/deals", tags=["deals"], dependencies=[Depends(verify_api_key)])
app.include_router(interactions.router, prefix="/interactions", tags=["interactions"], dependencies=[Depends(verify_api_key)])
app.include_router(commitments.router, prefix="/commitments", tags=["commitments"], dependencies=[Depends(verify_api_key)])
app.include_router(invoices.router, prefix="/invoices", tags=["invoices"], dependencies=[Depends(verify_api_key)])
app.include_router(founder_loans.router, prefix="/founder-loans", tags=["founder-loans"], dependencies=[Depends(verify_api_key)])
app.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])
app.include_router(dashboard.router, prefix="/dashboard", tags=["dashboard"], dependencies=[Depends(verify_api_key)])

@app.get("/health")
@app.get("/default/rdmis-crm-api/health")
@app.get("/rdmis-crm-api/health")
def health():
    return {"status": "ok"}

@app.get("/admin/debug-schemas")
async def debug_schemas(api_key: str = Depends(verify_api_key)):
    db = next(get_db())
    result = db.execute(text("""
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema NOT IN ('pg_catalog','information_schema')
        ORDER BY table_schema, table_name
    """)).fetchall()
    return {"tables": [{"schema": r[0], "table": r[1]} for r in result]}

@app.get("/admin/debug-import")
async def debug_import(api_key: str = Depends(verify_api_key)):
    try:
        import psycopg2
        return {"status": "ok", "version": psycopg2.__version__, "path": psycopg2.__file__}
    except Exception as e:
        return {"status": "error", "error": str(e), "sys_path": sys.path}

@app.post("/admin/run-migrations")
async def run_migrations(api_key: str = Depends(verify_api_key)):
    env = os.environ.copy()
    env["PYTHONPATH"] = ":".join(sys.path)
    result = subprocess.run(
        [sys.executable, "/var/task/migrations/run_migrations.py"],
        capture_output=True, text=True, env=env
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr
    }
@app.post("/admin/run-tests")
async def run_tests(api_key: str = Depends(verify_api_key)):
    env = os.environ.copy()
    env["PYTHONPATH"] = ":".join(sys.path)
    result = subprocess.run(
        [sys.executable, "/var/task/tests/test_phase1_schema.py"],
        capture_output=True, text=True, env=env
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr
    }
handler = Mangum(app, lifespan="off", api_gateway_base_path="/default/rdmis-crm-api")
