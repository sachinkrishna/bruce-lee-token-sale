import asyncio
import logging
import random
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from solders.pubkey import Pubkey

from app.config import settings, staking_signer_private_key
from app.database import allocs_col, purchases_col, users_col, relationship_tree_col, transactions_col, purchase_wallets_col
from app.models.user import SetUserLevelRequest
from app.services.indexer import reindex_full
from app.services.purchase_flow import process_completed_purchase
from app.services.solana_rpc import confirm_transaction, get_balance, transfer_sol
from app.services.staking_sdk import (
    check_purchase_id,
    list_purchase_records_for_pool,
    POOL_PUBKEY_OFFSET_IN_PURCHASE_ACCOUNT,
    PROGRAM_ID,
    PURCHASE_ACCOUNT_DATA_LEN,
    to_purchase_id_bytes,
)
from app.services.wallet_pool import ensure_wallet_pool
from app.tasks.poller import poll_purchase_wallet
from app.utils.economics import POWER_STAKE_MULTIPLIER, calculate_purchase_power_amount
from app.utils.level import MAX_COMMISSION_LEVEL
from app.utils.tranche import tranche_deduction_usd

logger = logging.getLogger(__name__)


def _expected_power_for_purchase(p: dict) -> int:
    return calculate_purchase_power_amount(p, settings.power_delayed_stake_bonus_multiplier)


def _purchase_stake_audit_brief(p: dict) -> dict:
    out = {
        "purchase_id": str(p["_id"]),
        "user_wallet": p.get("user_wallet"),
        "xfee_amount": p.get("xfee_amount"),
        "status": p.get("status"),
        "token_dispatch_tx": p.get("token_dispatch_tx"),
        "power_distribution_status": p.get("power_distribution_status"),
        "power_distribution_bonus_eligible": p.get("power_distribution_bonus_eligible", False),
        "expected_power_units": _expected_power_for_purchase(p),
    }
    ca = p.get("confirmed_at")
    out["confirmed_at"] = ca.isoformat() if ca else None
    cr = p.get("created_at")
    out["created_at"] = cr.isoformat() if cr else None
    return out


async def verify_admin_key(x_admin_key: str = Header(..., alias="X-Admin-Key")):
    if not settings.admin_api_key:
        raise HTTPException(status_code=500, detail="Admin API key not configured on server")
    if x_admin_key != settings.admin_api_key:
        raise HTTPException(status_code=401, detail="Invalid admin API key")


router = APIRouter(
    prefix="/api/v1/admin",
    tags=["admin"],
    dependencies=[Depends(verify_admin_key)],
)

# Separate router for endpoints that handle their own auth (e.g. signature-based)
public_admin_router = APIRouter(
    prefix="/api/v1/admin",
    tags=["admin"],
)


@router.post("/pool/replenish")
async def replenish_pool():
    await ensure_wallet_pool()
    return {"success": True, "message": "Wallet pool replenished"}


@router.get("/staking/check-purchase")
async def admin_check_purchase_stake(
    purchase_id: str = Query(
        ...,
        description="Purchase id as encoded on-chain (same as stake call, e.g. Mongo ObjectId string)",
    ),
    pool_address: str | None = Query(
        None, description="Pool PDA; defaults to POOL_ADDRESS from settings"
    ),
    rpc_url: str | None = Query(
        None, description="Solana RPC URL; defaults to QUICKNODE_RPC_URL from settings"
    ),
):
    """Read-only: returns whether this purchase_id has a stake record in the pool (no transaction)."""
    pool = pool_address or settings.pool_address
    rpc = rpc_url or settings.quicknode_rpc_url
    if not pool:
        raise HTTPException(status_code=400, detail="pool_address not configured and not provided")
    if not rpc:
        raise HTTPException(status_code=400, detail="rpc_url not configured and not provided")
    try:
        Pubkey.from_string(pool)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid pool_address")

    info = await asyncio.to_thread(
        check_purchase_id,
        pool_address=pool,
        purchase_id=purchase_id,
        rpc_url=rpc,
    )
    return info


@router.get("/staking/scan-purchase-records")
async def admin_scan_purchase_records(
    pool_address: str | None = Query(
        None, description="Pool PDA; defaults to POOL_ADDRESS from settings"
    ),
    rpc_url: str | None = Query(
        None, description="Solana RPC URL; defaults to QUICKNODE_RPC_URL from settings"
    ),
    include_records: bool = Query(
        True,
        description="If false, omit the records array (summary and parse_errors only). RPC still scans all accounts.",
    ),
):
    """
    Per-pool scan: getProgramAccounts on the staking program with dataSize + pool memcmp,
    then decode each PurchaseRecord. Use for full on-chain inventory vs Mongo-only audits.

    Filters use account data length 121 bytes (8-byte Anchor discriminator + 113-byte body)
    and memcmp on the pool pubkey at byte offset 72.
    """
    pool = pool_address or settings.pool_address
    rpc = rpc_url or settings.quicknode_rpc_url
    if not pool:
        raise HTTPException(status_code=400, detail="pool_address not configured and not provided")
    if not rpc:
        raise HTTPException(status_code=400, detail="rpc_url not configured and not provided")
    try:
        Pubkey.from_string(pool)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid pool_address")

    raw = await asyncio.to_thread(list_purchase_records_for_pool, pool, rpc)
    if raw.get("rpc_error"):
        raise HTTPException(status_code=502, detail=f"RPC getProgramAccounts failed: {raw['rpc_error']}")

    records: list = raw["records"]
    parse_errors: list = raw["parse_errors"]
    accounts_from_rpc = len(records) + len(parse_errors)
    summary = {
        "accounts_from_rpc": accounts_from_rpc,
        "parsed_ok": len(records),
        "parse_error_count": len(parse_errors),
        "sum_amount": sum(int(r["amount"]) for r in records),
        "unique_users": len({r["user"] for r in records}),
    }
    out = {
        "program_id": str(PROGRAM_ID),
        "pool_address": pool,
        "filters": {
            "data_size": PURCHASE_ACCOUNT_DATA_LEN,
            "pool_pubkey_offset": POOL_PUBKEY_OFFSET_IN_PURCHASE_ACCOUNT,
            "note": (
                f"memcmp compares pool Pubkey bytes at offset "
                f"{POOL_PUBKEY_OFFSET_IN_PURCHASE_ACCOUNT}"
            ),
        },
        "summary": summary,
        "parse_errors": parse_errors,
    }
    if include_records:
        out["records"] = records
    else:
        out["records_omitted"] = True
        out["records_count"] = len(records)
    return out


@router.get("/staking/reconcile-purchase-stakes")
async def admin_reconcile_purchase_stakes(
    since_unix: int = Query(
        1778346247,
        description="Only completed purchases with confirmed_at on/after this instant (UTC).",
    ),
    purchase_limit: int = Query(
        5000,
        ge=1,
        le=20000,
        description="Max completed purchases to load from Mongo (confirmed_at desc).",
    ),
    pool_address: str | None = Query(
        None, description="Pool PDA; defaults to POOL_ADDRESS from settings"
    ),
    rpc_url: str | None = Query(
        None, description="Solana RPC URL; defaults to QUICKNODE_RPC_URL from settings"
    ),
    include_details: bool = Query(
        True,
        description="If false, return summary counts only (no unmatched lists).",
    ),
    include_matched_clean: bool = Query(
        False,
        description="If true, include matched_clean: rows that match on-chain with no user/amount warnings.",
    ),
):
    """
    Join on-chain PurchaseRecords (per-pool GPA scan) to Mongo completed purchases.

    **Response focuses on problems**: completed purchases missing stake, on-chain rows with no DB row
    in this query, and rows where on-chain user/amount differs from the DB. Match key:
    ``to_purchase_id_bytes(str(_id)).hex()`` === ``purchase_id_hex`` (same as stake tx).
    """
    pool = pool_address or settings.pool_address
    rpc = rpc_url or settings.quicknode_rpc_url
    if not pool:
        raise HTTPException(status_code=400, detail="pool_address not configured and not provided")
    if not rpc:
        raise HTTPException(status_code=400, detail="rpc_url not configured and not provided")
    try:
        Pubkey.from_string(pool)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid pool_address")

    raw = await asyncio.to_thread(list_purchase_records_for_pool, pool, rpc)
    if raw.get("rpc_error"):
        raise HTTPException(status_code=502, detail=f"RPC getProgramAccounts failed: {raw['rpc_error']}")

    by_hex: dict[str, dict] = {}
    duplicate_chain_hex: list[str] = []
    for r in raw["records"]:
        h = r["purchase_id_hex"]
        if h in by_hex:
            duplicate_chain_hex.append(h)
            continue
        by_hex[h] = r

    cutoff = datetime.fromtimestamp(since_unix, tz=timezone.utc)
    purchase_match = {
        "status": "completed",
        "confirmed_at": {"$ne": None, "$gte": cutoff},
    }
    purchases = await (
        purchases_col()
        .find(purchase_match)
        .sort("confirmed_at", -1)
        .limit(purchase_limit)
        .to_list(length=purchase_limit)
    )

    matched_clean: list[dict] = []
    stake_discrepancies: list[dict] = []
    missing_on_chain: list[dict] = []
    matched_hex: set[str] = set()
    encode_errors: list[dict] = []

    for idx, p in enumerate(purchases, start=1):
        pid = str(p["_id"])
        try:
            expected_hex = to_purchase_id_bytes(pid).hex()
        except Exception as e:
            encode_errors.append(
                {
                    "line": idx,
                    "issue": "purchase_id_encode_failed",
                    "purchase": _purchase_stake_audit_brief(p),
                    "error": str(e),
                }
            )
            continue
        rec = by_hex.get(expected_hex)
        brief = _purchase_stake_audit_brief(p)
        if rec:
            warnings: list[str] = []
            if rec.get("user") != p.get("user_wallet"):
                warnings.append("user_mismatch")
            exp_power = _expected_power_for_purchase(p)
            if int(rec.get("amount", -1)) != exp_power:
                warnings.append("amount_mismatch")
            base = {
                "purchase": brief,
                "expected_purchase_id_hex": expected_hex,
                "on_chain": rec,
                "warnings": warnings,
            }
            if warnings:
                stake_discrepancies.append(
                    {
                        **base,
                        "issue": "on_chain_record_does_not_match_db_user_or_amount",
                    }
                )
            else:
                matched_clean.append({**base, "issue": None})
            matched_hex.add(expected_hex)
        else:
            missing_on_chain.append(
                {
                    "issue": "completed_purchase_no_on_chain_record",
                    "expected_purchase_id_hex": expected_hex,
                    "purchase": brief,
                }
            )

    for i, row in enumerate(missing_on_chain, start=1):
        row["line"] = i
    for i, row in enumerate(stake_discrepancies, start=1):
        row["line"] = i

    on_chain_only_sorted = sorted(
        (rec for h, rec in by_hex.items() if h not in matched_hex),
        key=lambda x: (-int(x.get("amount", 0)), x.get("pda", "")),
    )
    on_chain_only: list[dict] = []
    for i, rec in enumerate(on_chain_only_sorted, start=1):
        row = dict(rec)
        row["line"] = i
        row["issue"] = (
            "on_chain_stake_not_matched_to_any_purchase_in_db_query "
            "(wrong DB window, different pool encoding, or legacy / manual stake)"
        )
        on_chain_only.append(row)

    db_expected_power_sum = sum(_expected_power_for_purchase(p) for p in purchases)
    matched_on_chain_sum = sum(int(by_hex[h]["amount"]) for h in matched_hex if h in by_hex)

    summary = {
        "since_unix": since_unix,
        "since_iso_utc": cutoff.isoformat(),
        "db_purchases_scanned": len(purchases),
        "db_expected_power_sum": db_expected_power_sum,
        "on_chain_parsed_records": len(raw["records"]),
        "on_chain_duplicate_purchase_id_hex": len(duplicate_chain_hex),
        "on_chain_parse_errors": len(raw.get("parse_errors", [])),
        "matched_clean_count": len(matched_clean),
        "stake_field_mismatch_count": len(stake_discrepancies),
        "completed_without_on_chain_record_count": len(missing_on_chain),
        "on_chain_without_db_match_count": len(on_chain_only),
        "matched_on_chain_amount_sum": matched_on_chain_sum,
        "purchase_id_encode_error_count": len(encode_errors),
    }

    out: dict = {
        "program_id": str(PROGRAM_ID),
        "pool_address": pool,
        "summary": summary,
        "duplicate_chain_purchase_id_hex": duplicate_chain_hex if duplicate_chain_hex else [],
        "scan_parse_errors": raw.get("parse_errors", []),
        "purchase_id_encode_errors": encode_errors,
    }
    if include_details:
        out["unmatched"] = {
            "completed_without_stake": missing_on_chain,
            "on_chain_without_db_purchase": on_chain_only,
            "stake_does_not_match_db": stake_discrepancies,
        }
    else:
        out["details_omitted"] = True
    if include_matched_clean:
        for i, row in enumerate(matched_clean, start=1):
            row["line"] = i
        out["matched_clean"] = matched_clean
    return out


@router.get("/staking/audit-purchases")
async def admin_audit_purchase_stakes(
    since_unix: int = Query(
        1778346247,
        description="Only purchases on/after this instant (UTC). Completed uses confirmed_at; failed uses created_at.",
    ),
    completed_limit: int = Query(500, ge=1, le=5000, description="Max completed purchases to scan (newest first)"),
    failed_limit: int = Query(500, ge=1, le=5000, description="Max failed purchases to scan (newest first)"),
    concurrency: int = Query(8, ge=1, le=32, description="Parallel RPC checks"),
    pool_address: str | None = Query(
        None, description="Pool PDA; defaults to POOL_ADDRESS from settings"
    ),
    rpc_url: str | None = Query(
        None, description="Solana RPC URL; defaults to QUICKNODE_RPC_URL from settings"
    ),
):
    """
    For completed and failed purchases (each up to a limit), call check_purchase_id on-chain.
    Returns completed rows with no stake as missing_stakes, and all scanned failed rows with stake_check.
    """
    pool = pool_address or settings.pool_address
    rpc = rpc_url or settings.quicknode_rpc_url
    if not pool:
        raise HTTPException(status_code=400, detail="pool_address not configured and not provided")
    if not rpc:
        raise HTTPException(status_code=400, detail="rpc_url not configured and not provided")
    try:
        Pubkey.from_string(pool)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid pool_address")

    cutoff = datetime.fromtimestamp(since_unix, tz=timezone.utc)
    completed_match = {
        "status": "completed",
        "confirmed_at": {"$ne": None, "$gte": cutoff},
    }
    failed_match = {"status": "failed", "created_at": {"$gte": cutoff}}

    completed_docs = await (
        purchases_col()
        .find(completed_match)
        .sort("confirmed_at", -1)
        .limit(completed_limit)
        .to_list(length=completed_limit)
    )
    failed_docs = await (
        purchases_col()
        .find(failed_match)
        .sort("created_at", -1)
        .limit(failed_limit)
        .to_list(length=failed_limit)
    )

    async def _agg_expected_power(match: dict) -> tuple[int, int]:
        pipeline = [
            {"$match": match},
            {
                "$group": {
                    "_id": None,
                    "count": {"$sum": 1},
                    "expected_power": {
                        "$sum": {
                            "$multiply": [
                                {"$ifNull": ["$xfee_amount", 0]},
                                POWER_STAKE_MULTIPLIER,
                                {
                                    "$cond": [
                                        {"$eq": ["$power_distribution_bonus_eligible", True]},
                                        settings.power_delayed_stake_bonus_multiplier,
                                        1,
                                    ]
                                },
                            ]
                        }
                    },
                }
            },
        ]
        n, pwr = 0, 0
        async for doc in purchases_col().aggregate(pipeline):
            n = int(doc.get("count", 0))
            pwr = int(doc.get("expected_power", 0) or 0)
        return n, pwr

    db_completed_total_n, db_completed_total_power = await _agg_expected_power(completed_match)
    db_failed_total_n, db_failed_total_power = await _agg_expected_power(failed_match)

    sem = asyncio.Semaphore(concurrency)

    async def _check(p: dict):
        async with sem:
            info = await asyncio.to_thread(
                check_purchase_id,
                pool_address=pool,
                purchase_id=str(p["_id"]),
                rpc_url=rpc,
            )
        return p, info

    completed_results = await asyncio.gather(*[_check(p) for p in completed_docs])
    failed_results = await asyncio.gather(*[_check(p) for p in failed_docs])

    missing_stakes = []
    scan_on_chain_amount_sum = 0
    scan_on_chain_staked_count = 0
    scan_on_chain_completed_staked_amount = 0
    scan_on_chain_failed_staked_amount = 0

    for idx, (p, info) in enumerate(
        ((p, i) for p, i in completed_results if not i.get("staked")),
        start=1,
    ):
        missing_stakes.append(
            {
                "line": idx,
                "purchase": _purchase_stake_audit_brief(p),
                "stake_check": info,
            }
        )

    for p, info in completed_results:
        if info.get("staked") and info.get("amount") is not None:
            a = int(info["amount"])
            scan_on_chain_amount_sum += a
            scan_on_chain_staked_count += 1
            scan_on_chain_completed_staked_amount += a

    failed_purchases = []
    failed_staked = 0
    failed_not_staked = 0
    for idx, (p, info) in enumerate(failed_results, start=1):
        if info.get("staked"):
            failed_staked += 1
            if info.get("amount") is not None:
                a = int(info["amount"])
                scan_on_chain_amount_sum += a
                scan_on_chain_staked_count += 1
                scan_on_chain_failed_staked_amount += a
        else:
            failed_not_staked += 1
        failed_purchases.append(
            {
                "line": idx,
                "purchase": _purchase_stake_audit_brief(p),
                "stake_check": info,
            }
        )

    db_expected_power_completed_scanned = sum(
        _expected_power_for_purchase(p) for p in completed_docs
    )
    db_expected_power_failed_scanned = sum(
        _expected_power_for_purchase(p) for p in failed_docs
    )

    summary = {
        "since_unix": since_unix,
        "since_iso_utc": cutoff.isoformat(),
        "completed_time_field": "confirmed_at",
        "failed_time_field": "created_at",
        "completed_scanned": len(completed_results),
        "failed_scanned": len(failed_results),
        "completed_missing_on_chain": len(missing_stakes),
        "failed_staked_on_chain": failed_staked,
        "failed_not_staked_on_chain": failed_not_staked,
    }

    totals = {
        "on_chain_from_rpc_scan": {
            "note": "Sum of PurchaseRecord.amount from check_purchase_id for rows in this scan where staked is true.",
            "staked_row_count": scan_on_chain_staked_count,
            "sum_amount": scan_on_chain_amount_sum,
            "sum_amount_completed_rows": scan_on_chain_completed_staked_amount,
            "sum_amount_failed_rows": scan_on_chain_failed_staked_amount,
        },
        "db_scanned_rows_only": {
            "note": (
                f"Expected POWER units = xfee_amount * {POWER_STAKE_MULTIPLIER} "
                "for Mongo rows actually listed in this response scan."
            ),
            "completed_row_count": len(completed_docs),
            "failed_row_count": len(failed_docs),
            "expected_power_sum_completed": db_expected_power_completed_scanned,
            "expected_power_sum_failed": db_expected_power_failed_scanned,
            "expected_power_sum_all_scanned": (
                db_expected_power_completed_scanned + db_expected_power_failed_scanned
            ),
        },
        "db_all_rows_matching_time_filter": {
            "note": "All completed/failed in DB matching since_unix (ignores completed_limit/failed_limit).",
            "completed_count": db_completed_total_n,
            "failed_count": db_failed_total_n,
            "expected_power_sum_completed": db_completed_total_power,
            "expected_power_sum_failed": db_failed_total_power,
            "expected_power_sum_both": db_completed_total_power + db_failed_total_power,
        },
    }

    return {
        "missing_stakes": missing_stakes,
        "failed_purchases": failed_purchases,
        "summary": summary,
        "totals": totals,
    }


@router.post("/staking/stake-purchase/{purchase_id}")
async def admin_stake_purchase(
    purchase_id: str,
    amount: int | None = Query(
        None,
        ge=1,
        description=(
            f"Stake amount (POWER units). Default: int(xfee_amount * {POWER_STAKE_MULTIPLIER}), "
            "same as purchase_flow."
        ),
    ),
    pool_address: str | None = Query(
        None, description="Pool PDA; defaults to POOL_ADDRESS from settings"
    ),
    rpc_url: str | None = Query(
        None, description="Solana RPC URL; defaults to QUICKNODE_RPC_URL from settings"
    ),
    skip_if_staked: bool = Query(
        True,
        description="If true, no transaction when check_purchase_id shows already staked",
    ),
):
    """
    Submit on-chain stake for a Mongo purchase (recovery / manual run).
    Does not run commissions or sweep — only stake_with_purchase_id + DB update on success.
    """
    try:
        pid = ObjectId(purchase_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid purchase ID")

    purchase = await purchases_col().find_one({"_id": pid})
    if not purchase:
        raise HTTPException(status_code=404, detail="Purchase not found")

    if not staking_signer_private_key():
        raise HTTPException(
            status_code=500,
            detail="POOL_AUTHORITY_PRIVATE_KEY (or fallback MASTER_WALLET_PRIVATE_KEY) not configured",
        )

    pool = pool_address or settings.pool_address
    rpc = rpc_url or settings.quicknode_rpc_url
    if not pool:
        raise HTTPException(status_code=400, detail="pool_address not configured and not provided")
    if not rpc:
        raise HTTPException(status_code=400, detail="rpc_url not configured and not provided")
    try:
        Pubkey.from_string(pool)
        Pubkey.from_string(purchase["user_wallet"])
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid pool or user_wallet pubkey")

    xfee_amount = int(purchase.get("xfee_amount", 0))
    if xfee_amount <= 0:
        raise HTTPException(status_code=400, detail="Purchase has invalid xfee_amount")

    from app.services.stake_repair import stake_purchase_from_doc

    out = await stake_purchase_from_doc(
        purchase,
        amount_override=int(amount) if amount is not None else None,
        skip_if_staked=skip_if_staked,
        pool_address=pool,
        rpc_url=rpc,
    )
    return out


@router.post("/staking/run-repair-scan")
async def admin_run_stake_repair_scan():
    """Run one POWER staking catch-up batch immediately."""
    from app.services.stake_repair import run_stake_repair_scan

    return await run_stake_repair_scan()


@router.post("/reindex/{wallet_address}")
async def reindex_wallet(wallet_address: str):
    try:
        Pubkey.from_string(wallet_address)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Solana address")

    await reindex_full(wallet_address)
    return {"success": True, "wallet_address": wallet_address}


@router.post("/reindex-all")
async def reindex_all_users():
    """Full reindex for every user in the system."""
    count = 0
    async for user in users_col().find({}, {"wallet_address": 1}):
        await reindex_full(user["wallet_address"])
        count += 1
    logger.info(f"Reindex-all complete: {count} users reindexed")
    return {"success": True, "users_reindexed": count}


@router.post("/purchase/{purchase_id}/sweep")
async def sweep_purchase(purchase_id: str):
    """Sweep remaining SOL from a completed purchase wallet to master."""
    try:
        pid = ObjectId(purchase_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid purchase ID")

    purchase = await purchases_col().find_one({"_id": pid})
    if not purchase:
        raise HTTPException(status_code=404, detail="Purchase not found")

    if purchase["status"] != "completed":
        raise HTTPException(status_code=400, detail=f"Purchase status is '{purchase['status']}', expected 'completed'")

    pubkey = purchase["purchase_wallet_pubkey"]
    balance_lamports = await get_balance(pubkey)
    balance_sol = balance_lamports / 1e9

    if balance_sol < 0.001:
        return {"success": False, "message": f"No funds to sweep (balance: {balance_sol:.6f} SOL)"}

    pw_doc = await purchase_wallets_col().find_one({"public_key": pubkey})
    if not pw_doc:
        raise HTTPException(status_code=404, detail="Purchase wallet not found")

    from app.utils.keypair import keypair_from_private_key
    pw_keypair = keypair_from_private_key(pw_doc["private_key"])

    purchase_value_sol = purchase.get("purchase_value_sol")
    if purchase_value_sol is None:
        sol_price_at_confirmation = purchase.get("sol_price_at_confirmation", 0.0)
        if sol_price_at_confirmation <= 0:
            raise HTTPException(status_code=400, detail="Purchase is missing sol_price_at_confirmation")
        purchase_value_sol = float(purchase.get("xfee_amount", 0)) / sol_price_at_confirmation

    sale_usd = float(purchase.get("xfee_amount", 0))
    from app.services.sol_price import get_sol_price
    sol_price = await get_sol_price()

    sol_price_confirm = float(purchase.get("sol_price_at_confirmation") or 0.0)
    if sol_price_confirm <= 0:
        sol_price_confirm = float(sol_price)
    deduction_usd = tranche_deduction_usd(sale_usd)
    commissionable_sol = purchase_value_sol - (deduction_usd / sol_price_confirm)

    commission_pipeline = [
        {"$match": {"purchase_id": pid, "alloc_type": "commission", "status": "sent"}},
        {"$group": {"_id": None, "total": {"$sum": "$sol_amount"}}},
    ]
    total_distributed = 0.0
    async for doc in allocs_col().aggregate(commission_pipeline):
        total_distributed = doc.get("total", 0.0)

    gas_buffer_usd = purchase.get("gas_buffer_usd", 2.0)
    reserve_lamports = int((gas_buffer_usd / sol_price) * 1e9)
    rent_lamports = 890_880
    fee_buffer_lamports = 20_000
    target_sweep_lamports = int((commissionable_sol - total_distributed) * 1e9)
    available_lamports = balance_lamports - reserve_lamports - rent_lamports - fee_buffer_lamports
    sweep_lamports = min(target_sweep_lamports, available_lamports)
    if sweep_lamports <= 0:
        return {"success": False, "message": "Amount too small to cover tx fee and rent"}

    sweep_amount_sol = sweep_lamports / 1e9
    sig = await transfer_sol(pw_keypair, Pubkey.from_string(settings.master_wallet_address), sweep_lamports)
    confirmed = await confirm_transaction(sig)

    await allocs_col().insert_one({
        "purchase_id": pid,
        "recipient_wallet": settings.master_wallet_address,
        "sol_amount": sweep_amount_sol,
        "sale_usd": 0.0,
        "alloc_type": "master_sweep",
        "ancestor_level_tier": 0,
        "differential_rate": 0.0,
        "on_chain_tx": sig if confirmed else None,
        "status": "sent" if confirmed else "failed",
        "indexed": True,
        "created_at": datetime.now(timezone.utc),
    })

    await transactions_col().insert_one({
        "purchase_id": pid,
        "tx_type": "master_sweep",
        "from_wallet": pubkey,
        "to_wallet": settings.master_wallet_address,
        "amount_sol": sweep_amount_sol,
        "tx_signature": sig,
        "created_at": datetime.now(timezone.utc),
    })

    logger.info(f"Manual sweep: {sweep_amount_sol:.6f} SOL from {pubkey} to master (confirmed={confirmed})")
    return {
        "success": True,
        "swept_sol": sweep_amount_sol,
        "tx_signature": sig,
        "confirmed": confirmed,
    }


@router.post("/purchase/{purchase_id}/resume-commissions")
async def resume_commissions(purchase_id: str, token_tx_signature: str = None):
    """Resume a failed purchase where tokens were already sent but confirmation timed out."""
    try:
        pid = ObjectId(purchase_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid purchase ID")

    purchase = await purchases_col().find_one({"_id": pid})
    if not purchase:
        raise HTTPException(status_code=404, detail="Purchase not found")

    if purchase["status"] == "completed" and purchase.get("commission_distributed"):
        raise HTTPException(status_code=400, detail="Purchase is already completed and commissions are distributed")

    pubkey = purchase["purchase_wallet_pubkey"]
    balance_lamports = await get_balance(pubkey)
    balance_sol = balance_lamports / 1e9

    if token_tx_signature:
        await purchases_col().update_one({"_id": pid}, {"$set": {"token_dispatch_tx": token_tx_signature}})

    await process_completed_purchase(
        purchase_id, balance_sol, skip_token_dispatch=True, force_process=True
    )
    return {"success": True, "message": "Purchase resumed successfully"}


@router.post("/purchase/{purchase_id}/retry")
async def retry_purchase(purchase_id: str):
    """Manually retry a stuck pending or failed purchase."""
    try:
        pid = ObjectId(purchase_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid purchase ID")

    purchase = await purchases_col().find_one({"_id": pid})
    if not purchase:
        raise HTTPException(status_code=404, detail="Purchase not found")

    if purchase["status"] not in ("pending", "failed"):
        raise HTTPException(
            status_code=400,
            detail=f"Purchase has status '{purchase['status']}', only 'pending' or 'failed' can be retried"
        )

    pubkey = purchase["purchase_wallet_pubkey"]
    balance_lamports = await get_balance(pubkey)
    balance_sol = balance_lamports / 1e9

    if balance_sol < 0.001:
        return {
            "success": False,
            "message": f"No funds detected on {pubkey} (balance: {balance_sol:.6f} SOL)",
            "balance_sol": balance_sol,
        }

    # Reset to pending so process_completed_purchase will accept it
    if purchase["status"] == "failed":
        await purchases_col().update_one(
            {"_id": pid}, {"$set": {"status": "pending"}}
        )

    await process_completed_purchase(purchase_id, balance_sol)
    return {
        "success": True,
        "message": f"Purchase processed with {balance_sol:.6f} SOL",
        "balance_sol": balance_sol,
    }


@router.post("/purchase/{purchase_id}/refund")
async def refund_purchase(purchase_id: str):
    """Refund a purchase back to the buyer, subtracting a random 0.007 to 0.01 SOL penalty."""
    try:
        pid = ObjectId(purchase_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid purchase ID")

    purchase = await purchases_col().find_one({"_id": pid})
    if not purchase:
        raise HTTPException(status_code=404, detail="Purchase not found")

    pubkey = purchase.get("purchase_wallet_pubkey")
    if not pubkey:
        raise HTTPException(status_code=400, detail="Purchase does not have an assigned wallet")

    balance_lamports = await get_balance(pubkey)
    balance_sol = balance_lamports / 1e9

    deduction_sol = round(random.uniform(0.007, 0.01), 6)
    
    if balance_sol <= deduction_sol:
        raise HTTPException(
            status_code=400, 
            detail=f"Balance too low for refund (balance: {balance_sol:.6f} SOL, required penalty: >{deduction_sol:.6f} SOL)"
        )

    refund_amount_sol = balance_sol - deduction_sol
    refund_lamports = int(refund_amount_sol * 1e9)

    pw_doc = await purchase_wallets_col().find_one({"public_key": pubkey})
    if not pw_doc:
        raise HTTPException(status_code=404, detail="Purchase wallet not found in database")

    from app.utils.keypair import keypair_from_private_key
    pw_keypair = keypair_from_private_key(pw_doc["private_key"])

    buyer_pubkey = Pubkey.from_string(purchase["user_wallet"])
    
    sig = await transfer_sol(pw_keypair, buyer_pubkey, refund_lamports)
    confirmed = await confirm_transaction(sig)

    if confirmed:
        # Record the transaction
        await transactions_col().insert_one(
            {
                "purchase_id": pid,
                "tx_type": "refund",
                "from_wallet": pubkey,
                "to_wallet": purchase["user_wallet"],
                "amount_sol": refund_amount_sol,
                "tx_signature": sig,
                "created_at": datetime.now(timezone.utc),
            }
        )
        
        # Update the purchase status
        await purchases_col().update_one(
            {"_id": pid},
            {"$set": {
                "status": "refunded",
                "refund_tx_signature": sig,
                "refunded_at": datetime.now(timezone.utc),
            }}
        )

        # Record remaining balance in the purchase wallet
        final_balance_lamports = await get_balance(pubkey)
        await purchase_wallets_col().update_one(
            {"public_key": pubkey},
            {"$set": {"remaining_balance_sol": final_balance_lamports / 1e9}},
        )
        
        # Mark wallet as used so it isn't returned to pool
        from app.services.wallet_pool import mark_wallet_used
        await mark_wallet_used(pid)

        logger.info(f"Refunded {refund_amount_sol:.6f} SOL to {purchase['user_wallet']} for purchase {purchase_id} (deducted {deduction_sol:.6f} SOL)")
        
        return {
            "success": True,
            "message": "Refund successful",
            "refund_amount_sol": refund_amount_sol,
            "deducted_sol": deduction_sol,
            "tx_signature": sig
        }
    else:
        raise HTTPException(status_code=500, detail="Refund transaction failed to confirm")


@router.post("/purchases/recover-pending")
async def recover_pending_purchases():
    """Re-launch pollers for all pending, non-expired purchases (use after server restart)."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    recovered = 0
    processed = 0

    cursor = purchases_col().find({"status": "pending"})
    async for purchase in cursor:
        pid = str(purchase["_id"])
        pubkey = purchase["purchase_wallet_pubkey"]
        expires_at = purchase["expires_at"]

        if not pubkey:
            continue

        balance_lamports = await get_balance(pubkey)
        balance_sol = balance_lamports / 1e9

        if balance_sol >= 0.001:
            logger.info(f"Recovery: processing {pid} immediately ({balance_sol:.6f} SOL found)")
            await process_completed_purchase(pid, balance_sol)
            processed += 1
        elif expires_at > now:
            logger.info(f"Recovery: re-launching poller for {pid} (expires {expires_at.isoformat()})")
            asyncio.create_task(
                poll_purchase_wallet(
                    purchase_id=pid,
                    pubkey=pubkey,
                    expected_sol=purchase["sol_amount_expected"],
                    expires_at=expires_at,
                )
            )
            recovered += 1
        else:
            logger.info(f"Recovery: marking {pid} as expired")
            await purchases_col().update_one(
                {"_id": purchase["_id"]},
                {"$set": {"status": "expired"}},
            )

    return {
        "success": True,
        "processed_immediately": processed,
        "pollers_relaunched": recovered,
    }


@router.get("/purchase/{purchase_id}/breakdown")
async def purchase_breakdown(purchase_id: str):
    """
    Audit endpoint: verifies commission distribution correctness for a purchase.
    Runs mathematical checks like a test suite and reports pass/fail for each.
    """
    from app.utils.level import get_rate_for_level, RATE_BY_LEVEL

    try:
        pid = ObjectId(purchase_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid purchase ID")

    purchase = await purchases_col().find_one({"_id": pid})
    if not purchase:
        raise HTTPException(status_code=404, detail="Purchase not found")

    sol_received = purchase.get("sol_amount_received", 0.0)
    xfee_amount = purchase.get("xfee_amount", 0)
    # 1 XFEE = 1 USD
    sale_usd = float(xfee_amount)
    buyer_wallet = purchase["user_wallet"]
    max_rate = max(RATE_BY_LEVEL.values())

    # Fetch all allocs for this purchase
    commission_allocs = []
    master_sweep_alloc = None
    async for a in allocs_col().find({"purchase_id": pid}).sort("created_at", 1):
        if a["alloc_type"] == "commission":
            commission_allocs.append(a)
        elif a["alloc_type"] == "master_sweep":
            master_sweep_alloc = a

    sol_price = float(purchase.get("sol_price_at_confirmation") or 1.0)
    purchase_value_sol = purchase.get("purchase_value_sol", sol_received)
    deduction_usd = tranche_deduction_usd(sale_usd)
    commissionable_sol = purchase_value_sol - (deduction_usd / sol_price)

    # Fetch buyer's ancestor tree
    tree_doc = await relationship_tree_col().find_one({"wallet_address": buyer_wallet})
    ancestors = tree_doc.get("ancestors", []) if tree_doc else []

    # Fetch all user docs for recipients
    all_wallets = [a["recipient_wallet"] for a in commission_allocs]
    user_docs = {}
    if all_wallets:
        async for u in users_col().find({"wallet_address": {"$in": all_wallets}}):
            user_docs[u["wallet_address"]] = u

    # --- Run verification checks ---
    checks = []
    all_passed = True

    def check(name, passed, expected=None, actual=None, detail=None):
        nonlocal all_passed
        entry = {"test": name, "passed": passed}
        if expected is not None:
            entry["expected"] = expected
        if actual is not None:
            entry["actual"] = actual
        if detail:
            entry["detail"] = detail
        if not passed:
            all_passed = False
        checks.append(entry)

    # Check 1: Alloc count matches ancestor count
    check(
        "Alloc count matches ancestor count",
        len(commission_allocs) == len(ancestors),
        expected=len(ancestors),
        actual=len(commission_allocs),
    )

    # Check 2: All ancestors have an alloc
    alloc_wallets = {a["recipient_wallet"] for a in commission_allocs}
    missing = [w for w in ancestors if w not in alloc_wallets]
    check(
        "All ancestors have an alloc",
        len(missing) == 0,
        detail=f"Missing: {missing}" if missing else None,
    )

    # Check 3: Allocs are in correct ancestor order
    alloc_order = [a["recipient_wallet"] for a in commission_allocs]
    check(
        "Allocs follow ancestor tree order",
        alloc_order == ancestors[:len(alloc_order)],
    )

    # Check 4: Verify each differential rate
    highest_paid = 0
    total_commission_sol = 0.0
    distributions = []

    for a in commission_allocs:
        wallet = a["recipient_wallet"]
        user = user_docs.get(wallet)
        current_level = user.get("level", 0) if user else 0
        alloc_level = a.get("ancestor_level_tier", 0)
        diff_rate = a.get("differential_rate", 0.0)
        sol_amount = a.get("sol_amount", 0.0)

        if alloc_level > highest_paid:
            expected_diff = get_rate_for_level(alloc_level) - get_rate_for_level(highest_paid)
        else:
            expected_diff = 0.0

        expected_sol = round(commissionable_sol * expected_diff, 6) if expected_diff > 0 else 0.0
        actual_sol = round(sol_amount, 6)

        rate_ok = abs(diff_rate - expected_diff) < 1e-9
        amount_ok = abs(actual_sol - expected_sol) < 0.000002

        check(
            f"Differential rate for {wallet[:8]}.. (L{alloc_level})",
            rate_ok,
            expected=f"{expected_diff * 100:.1f}%",
            actual=f"{diff_rate * 100:.1f}%",
        )

        check(
            f"SOL amount for {wallet[:8]}.. (L{alloc_level})",
            amount_ok,
            expected=expected_sol,
            actual=actual_sol,
        )

        # Check alloc level matches user's current level
        check(
            f"Alloc level for {wallet[:8]}.. matches user level",
            alloc_level == current_level,
            expected=current_level,
            actual=alloc_level,
            detail="Level may have changed after purchase" if alloc_level != current_level else None,
        )

        if alloc_level > highest_paid:
            highest_paid = alloc_level

        total_commission_sol += sol_amount

        distributions.append({
            "recipient_wallet": wallet,
            "user_current_level": current_level,
            "alloc_level_at_purchase": alloc_level,
            "level_rate": f"{get_rate_for_level(alloc_level) * 100:.1f}%",
            "differential_rate": f"{diff_rate * 100:.1f}%",
            "expected_differential": f"{expected_diff * 100:.1f}%",
            "sol_amount": sol_amount,
            "expected_sol": expected_sol,
            "on_chain_tx": a.get("on_chain_tx"),
            "status": a["status"],
        })

    # Check 5: Total differential rates sum correctly
    sum_of_rates = sum(a.get("differential_rate", 0.0) for a in commission_allocs)
    max_alloc_level = max((a.get("ancestor_level_tier", 0) for a in commission_allocs), default=0)
    expected_total_rate = get_rate_for_level(max_alloc_level) if max_alloc_level > 0 else 0.0
    check(
        "Sum of differential rates equals highest ancestor's rate",
        abs(sum_of_rates - expected_total_rate) < 1e-9,
        expected=f"{expected_total_rate * 100:.1f}%",
        actual=f"{sum_of_rates * 100:.1f}%",
    )

    # Check 6: Total commission SOL is correct
    expected_total_commission = round(commissionable_sol * expected_total_rate, 6)
    check(
        "Total commission SOL is correct",
        abs(round(total_commission_sol, 6) - expected_total_commission) < 0.000002,
        expected=expected_total_commission,
        actual=round(total_commission_sol, 6),
    )

    # Check 7: Master sweep exists (for completed purchases)
    if purchase["status"] == "completed":
        check(
            "Master sweep alloc exists",
            master_sweep_alloc is not None,
        )

    # Check 8: All outgoing SOL roughly equals received SOL (minus rent leftover)
    sweep_sol = master_sweep_alloc["sol_amount"] if master_sweep_alloc else 0.0
    leave_sol = settings.leave_in_purchase_wallet_usd / sol_price
    leave_sol += tranche_deduction_usd(sale_usd) / sol_price
        
    total_outgoing = total_commission_sol + sweep_sol + leave_sol
    balance_diff = abs(sol_received - total_outgoing)
    check(
        "SOL accounting: commission + sweep + leftover ≈ received",
        balance_diff < 0.001,
        expected=round(sol_received, 6),
        actual=round(total_outgoing, 6),
        detail=f"Difference: {balance_diff:.6f} SOL",
    )

    # Check 9: No negative commission amounts
    negatives = [a["recipient_wallet"][:8] for a in commission_allocs if a.get("sol_amount", 0) < 0]
    check(
        "No negative commission amounts",
        len(negatives) == 0,
        detail=f"Negative amounts for: {negatives}" if negatives else None,
    )

    # Check 10: sale_usd on allocs is gross (xfee_amount); tranche deduction applies only to commissionable_sol
    alloc_sale_usds = set(a.get("sale_usd", 0) for a in commission_allocs)
    check(
        "All commission allocs have correct sale_usd",
        alloc_sale_usds == {sale_usd} or len(commission_allocs) == 0,
        expected=sale_usd,
        actual=list(alloc_sale_usds),
    )

    passed_count = sum(1 for c in checks if c["passed"])
    failed_count = sum(1 for c in checks if not c["passed"])

    return {
        "purchase_id": purchase_id,
        "status": purchase["status"],
        "buyer_wallet": buyer_wallet,
        "xfee_amount": xfee_amount,
        "sale_usd": sale_usd,
        "sol_received": sol_received,
        "sol_price": purchase.get("sol_price_at_confirmation", 0.0),
        "ancestor_count": len(ancestors),
        "summary": {
            "total_commission_sol": round(total_commission_sol, 6),
            "master_sweep_sol": round(sweep_sol, 6),
            "effective_commission_rate": f"{sum_of_rates * 100:.1f}%",
            "highest_level_in_tree": max_alloc_level,
            "token_dispatch_tx": purchase.get("token_dispatch_tx"),
        },
        "verification": {
            "all_passed": all_passed,
            "passed": passed_count,
            "failed": failed_count,
            "total": len(checks),
            "checks": checks,
        },
        "distributions": distributions,
    }


@router.post("/reset")
async def reset_system():
    """Wipe all data and re-initialize master wallet. Purchase wallets are preserved."""
    from datetime import datetime, timezone

    deleted = {}
    deleted["users"] = (await users_col().delete_many({})).deleted_count
    deleted["purchases"] = (await purchases_col().delete_many({})).deleted_count
    deleted["allocs"] = (await allocs_col().delete_many({})).deleted_count
    deleted["transactions"] = (await transactions_col().delete_many({})).deleted_count
    deleted["relationship_tree"] = (await relationship_tree_col().delete_many({})).deleted_count

    await purchase_wallets_col().update_many(
        {},
        {"$set": {"status": "free", "locked_for_purchase": None}},
    )

    await users_col().insert_one({
        "wallet_address": settings.master_wallet_address,
        "referrer_wallet": "",
        "level": MAX_COMMISSION_LEVEL,
        "is_valid_referrer": True,
        "joined_at": datetime.now(timezone.utc),
        "self_purchase": 0.0,
        "total_sales_usd": 0.0,
        "total_commission_sol": 0.0,
        "self_purchase_tokens": 0,
        "total_tokens_sold": 0,
        "level_sales": {},
        "level_commission": {},
        "direct_sales_sol": 0.0,
        "indirect_sales_sol": 0.0,
        "direct_commission_sol": 0.0,
        "indirect_commission_sol": 0.0,
        "direct_referral_count": 0,
        "network_size": 0,
    })

    logger.info("System reset complete")
    return {
        "success": True,
        "deleted": deleted,
        "master_wallet": settings.master_wallet_address,
    }


def _verify_signature_by_pubkey(message: str, signature_encoded: str, pubkey_b58: str) -> bool:
    """Verify an Ed25519 signature against an arbitrary base58-encoded Solana pubkey.

    Accepts the signature itself as either base58 or base64. Returns True iff the
    bytes verify against `(message, pubkey)`.
    """
    if settings.test_mode:
        return signature_encoded == "test_signature"
    try:
        import base58
        import base64
        from nacl.signing import VerifyKey

        pubkey_bytes = base58.b58decode(pubkey_b58)
        msg_bytes = message.encode("utf-8")

        sig_bytes = None
        for decode_fn, name in [(base58.b58decode, "base58"), (base64.b64decode, "base64")]:
            try:
                decoded = decode_fn(signature_encoded)
                if len(decoded) == 64:
                    sig_bytes = decoded
                    logger.info(f"Signature decoded as {name} ({len(decoded)} bytes)")
                    break
            except Exception:
                continue

        if sig_bytes is None:
            logger.warning("Could not decode signature as base58 or base64")
            return False

        verify_key = VerifyKey(pubkey_bytes)
        verify_key.verify(msg_bytes, sig_bytes)
        return True
    except Exception as e:
        logger.warning("Signature verification failed against pubkey %s: %s", pubkey_b58, e)
        return False


def _verify_master_signature(message: str, signature_encoded: str) -> bool:
    """Backward-compat alias: verify a signature against the master wallet pubkey."""
    return _verify_signature_by_pubkey(message, signature_encoded, settings.master_wallet_address)


@public_admin_router.post("/set-user-level")
async def set_user_level(req: SetUserLevelRequest, x_admin_key: str = Header(None, alias="X-Admin-Key")):
    # Dual auth: admin key OR master wallet signature
    has_admin_key = x_admin_key and settings.admin_api_key and x_admin_key == settings.admin_api_key
    has_signature = bool(req.signature)

    if not has_admin_key and not has_signature:
        raise HTTPException(status_code=401, detail="Provide X-Admin-Key header or signature in body")

    try:
        Pubkey.from_string(req.wallet_address)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid Solana address: {req.wallet_address}")

    if req.level < 1 or req.level > MAX_COMMISSION_LEVEL:
        raise HTTPException(status_code=400, detail=f"Level must be between 1 and {MAX_COMMISSION_LEVEL}")

    if not has_admin_key:
        expected_signer = (
            settings.set_user_level_signer_wallet or settings.master_wallet_address
        )
        expected_message = f"set-user-level:{req.wallet_address}:{req.level}"
        if not _verify_signature_by_pubkey(expected_message, req.signature, expected_signer):
            raise HTTPException(
                status_code=401,
                detail=f"Invalid signature (expected signer: {expected_signer})",
            )

    user = await users_col().find_one({"wallet_address": req.wallet_address})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    current_level = user.get("level", 1)
    if req.level <= current_level:
        raise HTTPException(
            status_code=400,
            detail=f"User is already at level {current_level}. Can only upgrade to a higher level",
        )

    await users_col().update_one(
        {"wallet_address": req.wallet_address},
        {"$set": {"level": req.level}},
    )

    auth_method = "admin_key" if has_admin_key else "signature"
    logger.info(f"Level override ({auth_method}): {req.wallet_address} from L{current_level} to L{req.level}")
    return {
        "success": True,
        "wallet_address": req.wallet_address,
        "previous_level": current_level,
        "new_level": req.level,
    }


@router.post("/backfill-allocs", dependencies=[Depends(verify_admin_key)])
async def backfill_allocs():
    """
    Backfill sale_sol, sale_tokens, and is_direct_sale on existing allocs that are missing them.
    Looks up each alloc's purchase to get purchase_value_sol, xfee_amount, and buyer's referrer.
    Does NOT touch indexed, level_indexed, or dir_indir_indexed flags.
    """
    purchase_cache = {}
    buyer_referrer_cache = {}
    updated = 0
    skipped = 0

    cursor = allocs_col().find({
        "alloc_type": "commission",
        "$or": [
            {"sale_sol": {"$exists": False}},
            {"sale_tokens": {"$exists": False}},
            {"is_direct_sale": {"$exists": False}},
        ],
    })

    async for alloc in cursor:
        pid = alloc.get("purchase_id")
        if not pid:
            skipped += 1
            continue

        if pid not in purchase_cache:
            purchase = await purchases_col().find_one({"_id": pid})
            if purchase:
                buyer_wallet = purchase.get("user_wallet", "")
                purchase_value_sol = purchase.get("purchase_value_sol")
                if purchase_value_sol is None:
                    sol_price_at_confirmation = purchase.get("sol_price_at_confirmation", 0.0)
                    token_cost_usd = float(purchase.get("xfee_amount", 0))
                    purchase_value_sol = token_cost_usd / sol_price_at_confirmation if sol_price_at_confirmation > 0 else 0.0
                purchase_cache[pid] = {
                    "sale_sol": purchase_value_sol,
                    "sale_tokens": purchase.get("xfee_amount", 0),
                    "buyer_wallet": buyer_wallet,
                }
                if buyer_wallet and buyer_wallet not in buyer_referrer_cache:
                    buyer_doc = await users_col().find_one({"wallet_address": buyer_wallet})
                    buyer_referrer_cache[buyer_wallet] = buyer_doc.get("referrer_wallet", "") if buyer_doc else ""
            else:
                purchase_cache[pid] = None

        p_data = purchase_cache[pid]
        if not p_data:
            skipped += 1
            continue

        set_fields = {}
        if "sale_sol" not in alloc:
            set_fields["sale_sol"] = p_data["sale_sol"]
        if "sale_tokens" not in alloc:
            set_fields["sale_tokens"] = p_data["sale_tokens"]
        if "is_direct_sale" not in alloc:
            buyer_referrer = buyer_referrer_cache.get(p_data["buyer_wallet"], "")
            set_fields["is_direct_sale"] = alloc["recipient_wallet"] == buyer_referrer

        if set_fields:
            await allocs_col().update_one({"_id": alloc["_id"]}, {"$set": set_fields})
            updated += 1

    logger.info(f"Backfill complete: {updated} allocs updated, {skipped} skipped")
    return {
        "success": True,
        "allocs_updated": updated,
        "allocs_skipped": skipped,
        "purchases_looked_up": len(purchase_cache),
    }


@router.post("/backfill-purchase-values", dependencies=[Depends(verify_admin_key)])
async def backfill_purchase_values():
    """
    Backfill purchase_value_sol on completed purchases and correct commission alloc sale_sol.
    Does not modify paid commission amounts or any indexed flags.
    """
    purchases_updated = 0
    allocs_updated = 0
    skipped = 0

    cursor = purchases_col().find({"status": "completed"})
    async for purchase in cursor:
        sol_price_at_confirmation = purchase.get("sol_price_at_confirmation", 0.0)
        if sol_price_at_confirmation <= 0:
            skipped += 1
            continue

        token_cost_usd = float(purchase.get("xfee_amount", 0))
        purchase_value_sol = token_cost_usd / sol_price_at_confirmation
        expected_usd = purchase.get("sol_amount_expected", 0.0) * sol_price_at_confirmation
        gas_buffer_usd = max(expected_usd - token_cost_usd, 0.0)

        purchase_update = await purchases_col().update_one(
            {"_id": purchase["_id"]},
            {
                "$set": {
                    "purchase_value_sol": round(purchase_value_sol, 6),
                    "gas_buffer_usd": round(gas_buffer_usd, 2),
                }
            },
        )
        purchases_updated += purchase_update.modified_count

        alloc_update = await allocs_col().update_many(
            {"purchase_id": purchase["_id"], "alloc_type": "commission"},
            {"$set": {"sale_sol": round(purchase_value_sol, 6)}},
        )
        allocs_updated += alloc_update.modified_count

    logger.info(
        f"Purchase value backfill complete: {purchases_updated} purchases updated, "
        f"{allocs_updated} allocs updated, {skipped} skipped"
    )
    return {
        "success": True,
        "purchases_updated": purchases_updated,
        "allocs_updated": allocs_updated,
        "purchases_skipped": skipped,
    }


@router.post("/backfill-wallet-balances", dependencies=[Depends(verify_admin_key)])
async def backfill_wallet_balances():
    """Read on-chain balance for all used purchase wallets and record it."""
    updated = 0
    failed = 0

    cursor = purchase_wallets_col().find({
        "status": "used",
        "remaining_balance_sol": {"$exists": False},
    })

    async for pw in cursor:
        pubkey = pw.get("public_key")
        if not pubkey:
            continue
        try:
            balance_lamports = await get_balance(pubkey)
            await purchase_wallets_col().update_one(
                {"_id": pw["_id"]},
                {"$set": {"remaining_balance_sol": balance_lamports / 1e9}},
            )
            updated += 1
        except Exception:
            logger.exception(f"Failed to read balance for {pubkey}")
            failed += 1

    logger.info(f"Wallet balance backfill: {updated} updated, {failed} failed")
    return {"success": True, "wallets_updated": updated, "wallets_failed": failed}
