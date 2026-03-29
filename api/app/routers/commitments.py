from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from ..database import get_db
from ..models import Commitment

router = APIRouter()

class CommitmentCreate(BaseModel):
    contact_id: str
    deal_id: Optional[str] = None
    description: str
    due_date: Optional[datetime] = None
    status: Optional[str] = "open"

class CommitmentUpdate(BaseModel):
    contact_id: Optional[str] = None
    deal_id: Optional[str] = None
    description: Optional[str] = None
    due_date: Optional[datetime] = None
    status: Optional[str] = None

class CommitmentOut(BaseModel):
    id: str
    contact_id: str
    deal_id: Optional[str]
    description: str
    due_date: Optional[datetime]
    status: Optional[str]
    resolved_at: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True

@router.get("/", response_model=list[CommitmentOut])
def list_commitments(db: Session = Depends(get_db)):
    return db.query(Commitment).order_by(Commitment.created_at.desc()).all()

@router.get("/{commitment_id}", response_model=CommitmentOut)
def get_commitment(commitment_id: str, db: Session = Depends(get_db)):
    commitment = db.query(Commitment).filter(Commitment.id == commitment_id).first()
    if not commitment:
        raise HTTPException(status_code=404, detail="Commitment not found")
    return commitment

@router.post("/", response_model=CommitmentOut, status_code=201)
def create_commitment(payload: CommitmentCreate, db: Session = Depends(get_db)):
    commitment = Commitment(**payload.model_dump())
    db.add(commitment)
    db.commit()
    db.refresh(commitment)
    return commitment

@router.patch("/{commitment_id}", response_model=CommitmentOut)
def update_commitment(commitment_id: str, payload: CommitmentUpdate, db: Session = Depends(get_db)):
    commitment = db.query(Commitment).filter(Commitment.id == commitment_id).first()
    if not commitment:
        raise HTTPException(status_code=404, detail="Commitment not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(commitment, field, value)
    db.commit()
    db.refresh(commitment)
    return commitment

@router.patch("/{commitment_id}/resolve", response_model=CommitmentOut)
def resolve_commitment(commitment_id: str, db: Session = Depends(get_db)):
    commitment = db.query(Commitment).filter(Commitment.id == commitment_id).first()
    if not commitment:
        raise HTTPException(status_code=404, detail="Commitment not found")
    commitment.status = "resolved"
    commitment.resolved_at = datetime.utcnow()
    db.commit()
    db.refresh(commitment)
    return commitment

@router.delete("/{commitment_id}", status_code=204)
def delete_commitment(commitment_id: str, db: Session = Depends(get_db)):
    commitment = db.query(Commitment).filter(Commitment.id == commitment_id).first()
    if not commitment:
        raise HTTPException(status_code=404, detail="Commitment not found")
    db.delete(commitment)
    db.commit()
