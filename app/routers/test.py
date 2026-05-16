import logging

from bson import ObjectId
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.database import purchases_col
from app.services.solana_rpc import test_set_balance

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/test", tags=["test"])


class SimulateDepositRequest(BaseModel):
    purchase_id: str
    sol_amount: float


@router.post("/deposit")
async def simulate_deposit(req: SimulateDepositRequest):
    """
    Simulate SOL arriving in a purchase wallet.
    Sets the in-memory balance so the poller picks it up on its next cycle.
    Only works in TEST_MODE.
    """
    if not settings.test_mode:
        raise HTTPException(status_code=403, detail="Only available in test mode")

    try:
        pid = ObjectId(req.purchase_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid purchase ID")

    purchase = await purchases_col().find_one({"_id": pid})
    if not purchase:
        raise HTTPException(status_code=404, detail="Purchase not found")

    if purchase["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Purchase is not pending (status={purchase['status']})")

    pubkey = purchase["purchase_wallet_pubkey"]
    lamports = int(req.sol_amount * 1e9)
    test_set_balance(pubkey, lamports)

    logger.info(f"[TEST] Simulated deposit of {req.sol_amount} SOL to {pubkey} for purchase {req.purchase_id}")

    return {
        "success": True,
        "purchase_id": req.purchase_id,
        "purchase_wallet": pubkey,
        "sol_deposited": req.sol_amount,
        "message": "Balance set. Poller will detect it within ~5 seconds.",
    }
