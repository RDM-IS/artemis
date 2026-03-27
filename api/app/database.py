import os
import sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

# Ensure repo root is on sys.path so knowledge.secrets is importable
_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from knowledge.secrets import get_rds_credentials


def build_database_url() -> str:
    creds = get_rds_credentials()
    host = os.environ.get("RDS_HOST")
    db = os.environ.get("RDS_DB", "crm")
    return f"postgresql+psycopg2://{creds['username']}:{creds['password']}@{host}:5432/{db}"

_engine = None
_SessionLocal = None

def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(build_database_url(), pool_pre_ping=True)
    return _engine

class Base(DeclarativeBase):
    pass

def get_db():
    SessionLocal = sessionmaker(bind=get_engine(), autocommit=False, autoflush=False)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
