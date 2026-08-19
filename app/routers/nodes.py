"""Sequential-node lookup endpoints.

Every user with at least one completed purchase gets a `node_number`,
assigned once in chronological order of their first completed purchase.
See `app/services/node_number.py` for the assignment rules.
"""
from fastapi import APIRouter, HTTPException

from app.database import users_col

router = APIRouter(prefix="/api/v1/nodes", tags=["nodes"])


@router.get("/{node_number}")
async def get_user_by_node_number(node_number: int):
    """Return the user assigned to `node_number`. 404 if no such node."""
    if node_number < 1:
        raise HTTPException(status_code=400, detail="node_number must be >= 1")

    user = await users_col().find_one(
        {"node_number": node_number},
        projection={
            "_id": 0,
            "wallet_address": 1,
            "node_number": 1,
            "node_number_assigned_at": 1,
            "level": 1,
            "founder": 1,
            "founder_since": 1,
            "joined_at": 1,
            "is_valid_referrer": 1,
        },
    )
    if not user:
        raise HTTPException(status_code=404, detail="No user assigned to this node")

    return {
        "node_number": int(user["node_number"]),
        "wallet_address": user["wallet_address"],
        "assigned_at": user.get("node_number_assigned_at"),
        "level": int(user.get("level", 1)),
        "founder": bool(user.get("founder", False)),
        "is_founder": bool(user.get("founder", False)),
        "founder_since": user.get("founder_since"),
        "joined_at": user.get("joined_at"),
        "is_valid_referrer": bool(user.get("is_valid_referrer", False)),
    }
