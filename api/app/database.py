import boto3
import json
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

def get_rds_credentials() -> dict:
    secret_name = os.environ.get("RDS_SECRET_ARN")
    client = boto3.client("secretsmanager", region_name="us-east-1")
    response = client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])

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
