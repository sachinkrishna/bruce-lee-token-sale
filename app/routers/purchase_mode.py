"""Read/write endpoints for the system-wide purchase mode toggle.

- Public GET so the frontend can render the right instructions without an
  admin key.
- Admin POST (protected by the standard admin key dependency) so operators
  can flip the mode via a curl call or the admin UI.

Flipping the mode is instantaneous: it only affects NEW `/purchase/initiate`
calls. Purchases already in flight snapshot the mode they were initiated
under and continue to their original terms.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.routers.admin import verify_admin_key
from app.services.purchase_mode import (
    VALID_MODES,
    get_mode_history,
    get_purchase_mode,
    set_purchase_mode,
)

logger = logging.getLogger(__name__)

public_router = APIRouter(prefix="/api/v1", tags=["purchase-mode"])
admin_router = APIRouter(
    prefix="/api/v1/admin",
    tags=["purchase-mode"],
    dependencies=[Depends(verify_admin_key)],
)


@public_router.get("/purchase-mode")
async def read_purchase_mode() -> dict:
    doc = await get_purchase_mode()
    return {
        "mode": doc["mode"],
        "updated_at": doc.get("updated_at"),
        "valid_modes": list(VALID_MODES),
    }


@admin_router.post("/purchase-mode")
async def write_purchase_mode(
    mode: str = Body(..., embed=True, description="'SOL' or 'XFEE'"),
    reason: Optional[str] = Body(None, embed=True),
    changed_by: str = Body("admin", embed=True),
) -> dict:
    try:
        result = await set_purchase_mode(mode, changed_by=changed_by, reason=reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result


@admin_router.get("/purchase-mode/history")
async def read_purchase_mode_history(limit: int = Query(50, ge=1, le=500)) -> dict:
    history = await get_mode_history(limit=limit)
    return {"history": history, "count": len(history)}
