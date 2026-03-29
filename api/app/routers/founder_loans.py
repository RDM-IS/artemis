from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from ..database import get_db
from ..models import FounderLoan

router = APIRouter()

class FounderLoanCreate(BaseModel):
    description: str
    amount: float
    paid_by: Optional[str] = None
    date_incurred: datetime
    reimbursed: Optional[bool] = False
    reimbursed_date: Optional[datetime] = None

class FounderLoanUpdate(BaseModel):
    description: Optional[str] = None
    amount: Optional[float] = None
    paid_by: Optional[str] = None
    date_incurred: Optional[datetime] = None
    reimbursed: Optional[bool] = None
    reimbursed_date: Optional[datetime] = None

class FounderLoanOut(BaseModel):
    id: str
    description: str
    amount: float
    paid_by: Optional[str]
    date_incurred: datetime
    reimbursed: Optional[bool]
    reimbursed_date: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True

@router.get("/", response_model=list[FounderLoanOut])
def list_founder_loans(db: Session = Depends(get_db)):
    return db.query(FounderLoan).order_by(FounderLoan.date_incurred.desc()).all()

@router.get("/{loan_id}", response_model=FounderLoanOut)
def get_founder_loan(loan_id: str, db: Session = Depends(get_db)):
    loan = db.query(FounderLoan).filter(FounderLoan.id == loan_id).first()
    if not loan:
        raise HTTPException(status_code=404, detail="Founder loan not found")
    return loan

@router.post("/", response_model=FounderLoanOut, status_code=201)
def create_founder_loan(payload: FounderLoanCreate, db: Session = Depends(get_db)):
    loan = FounderLoan(**payload.model_dump())
    db.add(loan)
    db.commit()
    db.refresh(loan)
    return loan

@router.patch("/{loan_id}", response_model=FounderLoanOut)
def update_founder_loan(loan_id: str, payload: FounderLoanUpdate, db: Session = Depends(get_db)):
    loan = db.query(FounderLoan).filter(FounderLoan.id == loan_id).first()
    if not loan:
        raise HTTPException(status_code=404, detail="Founder loan not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(loan, field, value)
    db.commit()
    db.refresh(loan)
    return loan

@router.delete("/{loan_id}", status_code=204)
def delete_founder_loan(loan_id: str, db: Session = Depends(get_db)):
    loan = db.query(FounderLoan).filter(FounderLoan.id == loan_id).first()
    if not loan:
        raise HTTPException(status_code=404, detail="Founder loan not found")
    db.delete(loan)
    db.commit()
