import uuid
from datetime import datetime
from sqlalchemy import Column, String, Text, Numeric, Boolean, DateTime, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from .database import Base

def gen_uuid():
    return str(uuid.uuid4())

class Organization(Base):
    __tablename__ = "organizations"
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    name = Column(String(255), nullable=False)
    type = Column(String(100))
    industry = Column(String(100))
    website = Column(String(255))
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

class Contact(Base):
    __tablename__ = "contacts"
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    org_id = Column(UUID(as_uuid=False), ForeignKey("organizations.id"))
    name = Column(String(255), nullable=False)
    title = Column(String(255))
    email = Column(String(255))
    phone = Column(String(50))
    notes = Column(Text)
    last_contacted = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

class Deal(Base):
    __tablename__ = "deals"
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    org_id = Column(UUID(as_uuid=False), ForeignKey("organizations.id"))
    name = Column(String(255), nullable=False)
    gate = Column(Integer, nullable=False)   # 1–5
    stage = Column(String(100))
    value = Column(Numeric(12, 2))
    signed_date = Column(DateTime)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Interaction(Base):
    __tablename__ = "interactions"
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    contact_id = Column(UUID(as_uuid=False), ForeignKey("contacts.id"), nullable=False)
    deal_id = Column(UUID(as_uuid=False), ForeignKey("deals.id"), nullable=True)
    type = Column(String(50))  # call/email/meeting/other
    date = Column(DateTime, nullable=False)
    summary = Column(Text)
    logged_by = Column(String(100))
    created_at = Column(DateTime, default=datetime.utcnow)

class Commitment(Base):
    __tablename__ = "commitments"
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    contact_id = Column(UUID(as_uuid=False), ForeignKey("contacts.id"), nullable=False)
    deal_id = Column(UUID(as_uuid=False), ForeignKey("deals.id"), nullable=True)
    description = Column(Text, nullable=False)
    due_date = Column(DateTime)
    status = Column(String(20), default="open")  # open/resolved
    resolved_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

class Invoice(Base):
    __tablename__ = "invoices"
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    deal_id = Column(UUID(as_uuid=False), ForeignKey("deals.id"), nullable=False)
    zoho_invoice_id = Column(String(255), unique=True, nullable=False)
    type = Column(String(100))
    amount = Column(Numeric(12, 2))
    status = Column(String(20))  # draft/sent/paid
    issued_date = Column(DateTime)
    paid_date = Column(DateTime)
    synced_at = Column(DateTime)

class FounderLoan(Base):
    __tablename__ = "founder_loans"
    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    description = Column(Text, nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    paid_by = Column(String(100))
    date_incurred = Column(DateTime, nullable=False)
    reimbursed = Column(Boolean, default=False)
    reimbursed_date = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
