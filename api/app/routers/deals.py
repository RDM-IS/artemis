from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from ..database import get_db
from ..models import Deal

router = APIRouter()

class DealCreate(BaseModel):
    org_id: str
    name: str
    gate: int
    stage: Optional[str] = None
    value: Optional[float] = None
    notes: Optional[str] = None

class DealUpdate(BaseModel):
    name: Optional[str] = None
    gate: Optional[int] = None
    stage: Optional[str] = None
    value: Optional[float] = None
    signed_date: Optional[datetime] = None
    notes: Optional[str] = None

class DealOut(BaseModel):
    id: str
    org_id: str
    name: str
    gate: int
    stage: Optional[str]
    value: Optional[float]
    signed_date: Optional[datetime]
    notes: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

@router.get("/", response_model=list[DealOut])
def list_deals(db: Session = Depends(get_db)):
    return db.query(Deal).order_by(Deal.updated_at.desc()).all()

@router.get("/{deal_id}", response_model=DealOut)
def get_deal(deal_id: str, db: Session = Depends(get_db)):
    deal = db.query(Deal).filter(Deal.id == deal_id).first()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    return deal

@router.post("/", response_model=DealOut, status_code=201)
def create_deal(payload: DealCreate, db: Session = Depends(get_db)):
    deal = Deal(**payload.model_dump())
    db.add(deal)
    db.commit()
    db.refresh(deal)
    return deal

@router.patch("/{deal_id}", response_model=DealOut)
def update_deal(deal_id: str, payload: DealUpdate, db: Session = Depends(get_db)):
    deal = db.query(Deal).filter(Deal.id == deal_id).first()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(deal, field, value)
    db.commit()
    db.refresh(deal)
    return deal

@router.delete("/{deal_id}", status_code=204)
def delete_deal(deal_id: str, db: Session = Depends(get_db)):
    deal = db.query(Deal).filter(Deal.id == deal_id).first()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")
    db.delete(deal)
    db.commit()
