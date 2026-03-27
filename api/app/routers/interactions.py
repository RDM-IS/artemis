from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from ..database import get_db
from ..models import Interaction

router = APIRouter()

class InteractionCreate(BaseModel):
    contact_id: str
    deal_id: Optional[str] = None
    type: Optional[str] = None
    date: datetime
    summary: Optional[str] = None
    logged_by: Optional[str] = None

class InteractionUpdate(BaseModel):
    contact_id: Optional[str] = None
    deal_id: Optional[str] = None
    type: Optional[str] = None
    date: Optional[datetime] = None
    summary: Optional[str] = None
    logged_by: Optional[str] = None

class InteractionOut(BaseModel):
    id: str
    contact_id: str
    deal_id: Optional[str]
    type: Optional[str]
    date: datetime
    summary: Optional[str]
    logged_by: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True

@router.get("/", response_model=list[InteractionOut])
def list_interactions(db: Session = Depends(get_db)):
    return db.query(Interaction).order_by(Interaction.date.desc()).all()

@router.get("/{interaction_id}", response_model=InteractionOut)
def get_interaction(interaction_id: str, db: Session = Depends(get_db)):
    interaction = db.query(Interaction).filter(Interaction.id == interaction_id).first()
    if not interaction:
        raise HTTPException(status_code=404, detail="Interaction not found")
    return interaction

@router.post("/", response_model=InteractionOut, status_code=201)
def create_interaction(payload: InteractionCreate, db: Session = Depends(get_db)):
    interaction = Interaction(**payload.model_dump())
    db.add(interaction)
    db.commit()
    db.refresh(interaction)
    return interaction

@router.patch("/{interaction_id}", response_model=InteractionOut)
def update_interaction(interaction_id: str, payload: InteractionUpdate, db: Session = Depends(get_db)):
    interaction = db.query(Interaction).filter(Interaction.id == interaction_id).first()
    if not interaction:
        raise HTTPException(status_code=404, detail="Interaction not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(interaction, field, value)
    db.commit()
    db.refresh(interaction)
    return interaction

@router.delete("/{interaction_id}", status_code=204)
def delete_interaction(interaction_id: str, db: Session = Depends(get_db)):
    interaction = db.query(Interaction).filter(Interaction.id == interaction_id).first()
    if not interaction:
        raise HTTPException(status_code=404, detail="Interaction not found")
    db.delete(interaction)
    db.commit()
