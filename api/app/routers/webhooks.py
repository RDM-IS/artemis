import hashlib
import hmac
from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import Invoice, Deal
from knowledge.secrets import get_zoho_webhook_secret

router = APIRouter()

def verify_zoho_signature(payload: bytes, signature: str) -> bool:
    secret = get_zoho_webhook_secret()
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)

@router.post("/zoho")
async def zoho_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    signature = request.headers.get("X-Zoho-Signature", "")

    if not verify_zoho_signature(payload, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    data = await request.json()
    zoho_invoice_id = data.get("invoice_id")
    new_status = data.get("status")

    if not zoho_invoice_id:
        raise HTTPException(status_code=400, detail="Missing invoice_id")

    invoice = db.query(Invoice).filter(Invoice.zoho_invoice_id == zoho_invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found in CRM")

    invoice.status = new_status
    invoice.synced_at = __import__("datetime").datetime.utcnow()

    if new_status == "paid":
        invoice.paid_date = __import__("datetime").datetime.utcnow()
        # Advance deal stage on payment
        deal = db.query(Deal).filter(Deal.id == invoice.deal_id).first()
        if deal:
            deal.stage = "deposit_received"
            # TODO: push Artemis notification to #artemis-ryan via Mattermost webhook

    db.commit()
    return {"status": "ok"}
