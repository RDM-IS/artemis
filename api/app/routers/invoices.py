from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from ..database import get_db
from ..models import Invoice

router = APIRouter()

class InvoiceOut(BaseModel):
    id: str
    deal_id: str
    zoho_invoice_id: str
    type: Optional[str]
    amount: Optional[float]
    status: Optional[str]
    issued_date: Optional[datetime]
    paid_date: Optional[datetime]
    synced_at: Optional[datetime]

    class Config:
        from_attributes = True

@router.get("/", response_model=list[InvoiceOut])
def list_invoices(db: Session = Depends(get_db)):
    return db.query(Invoice).order_by(Invoice.issued_date.desc()).all()

@router.get("/{invoice_id}", response_model=InvoiceOut)
def get_invoice(invoice_id: str, db: Session = Depends(get_db)):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice
