from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from ..database import get_db
from ..models import Organization

router = APIRouter()

class OrgCreate(BaseModel):
    name: str
    type: Optional[str] = None
    industry: Optional[str] = None
    website: Optional[str] = None
    notes: Optional[str] = None

class OrgUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    industry: Optional[str] = None
    website: Optional[str] = None
    notes: Optional[str] = None

class OrgOut(BaseModel):
    id: str
    name: str
    type: Optional[str]
    industry: Optional[str]
    website: Optional[str]
    notes: Optional[str]
    created_at: datetime
    class Config:
        from_attributes = True

@router.get("/", response_model=list[OrgOut])
def list_orgs(db: Session = Depends(get_db)):
    return db.query(Organization).order_by(Organization.name).all()

@router.get("/{org_id}", response_model=OrgOut)
def get_org(org_id: str, db: Session = Depends(get_db)):
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org

@router.post("/", response_model=OrgOut, status_code=201)
def create_org(payload: OrgCreate, db: Session = Depends(get_db)):
    org = Organization(**payload.model_dump())
    db.add(org)
    db.commit()
    db.refresh(org)
    return org

@router.patch("/{org_id}", response_model=OrgOut)
def update_org(org_id: str, payload: OrgUpdate, db: Session = Depends(get_db)):
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(org, field, value)
    db.commit()
    db.refresh(org)
    return org

@router.delete("/{org_id}", status_code=204)
def delete_org(org_id: str, db: Session = Depends(get_db)):
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    db.delete(org)
    db.commit()
