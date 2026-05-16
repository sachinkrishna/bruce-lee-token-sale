"""
x-fee-staking SDK — importable functions, no CLI required.

Usage in your backend:

    from staking_sdk import stake_with_purchase_id, check_purchase_id

    # Stake
    result = stake_with_purchase_id(
        admin_private_key_b58 = "your_base58_private_key",
        pool_address          = "5JLYzh59Ksuru8qBvFQreBMUeiruCwNZEgcYpeU61WHt",
        user_address          = "FtGWZnqKwf5p6vEcWPE8BkiC8KChNqWDwdthihCW5xsr",
        amount                = 2,
        purchase_id           = "order-12345",
    )
    print(result["signature"])  # tx signature
    print(result["already_staked"])  # True if purchase_id was already used

    # Check only (no transaction)
    info = check_purchase_id(
        pool_address  = "5JLYzh59Ksuru8qBvFQreBMUeiruCwNZEgcYpeU61WHt",
        purchase_id   = "order-12345",
    )
    print(info["staked"])   # True / False
    print(info["user"])     # wallet if staked, else None
    print(info["amount"])   # amount staked, else None

Requirements:
    pip install solders solana base58
"""

import base58
import hashlib
import os
import struct
import time
from datetime import datetime, timezone
from typing import Optional

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.transaction import Transaction
from solders.message import Message
from solana.rpc.api import Client
from solana.rpc.types import MemcmpOpts, TxOpts
from solana.rpc.commitment import Confirmed

# ── Constants ─────────────────────────────────────────────────────────────────
PROGRAM_ID = Pubkey.from_string("EX7YLYMv9pjarwgFF8JN5kwSohuhgVVmTDfD31ETekBC")
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
DEFAULT_RPC = os.getenv(
    "ANCHOR_PROVIDER_URL", "https://rpc.shyft.to?api_key=VQMa7xeiPCjxEUjI"
)

STAKE_SEED = b"stake"
PURCHASE_SEED = b"purchase"

# PurchaseRecord account data from RPC is 121 bytes total:
# bytes 0–7 Anchor discriminator; bytes 8–120 struct (purchase_id, user, pool, amount, staked_at, bump).
# purchase_id [u8;32]  → offset 8
# user        Pubkey   → offset 40
# pool        Pubkey   → offset 72
# amount      u64      → offset 104
# staked_at   i64      → offset 112
# bump        u8       → offset 120
PURCHASE_RECORD_LEN = 121
PURCHASE_ACCOUNT_DATA_LEN = PURCHASE_RECORD_LEN
POOL_PUBKEY_OFFSET_IN_PURCHASE_ACCOUNT = 72


# ── Key loading ───────────────────────────────────────────────────────────────


def keypair_from_base58(private_key_b58: str) -> Keypair:
    """Load a Keypair from a base58-encoded private key string."""
    raw = base58.b58decode(private_key_b58)
    return Keypair.from_bytes(raw)


# ── Purchase ID conversion ─────────────────────────────────────────────────────


def to_purchase_id_bytes(purchase_id) -> bytes:
    """
    Convert a purchase ID to 32 bytes:
      - bytes/bytearray of length 32  → used directly
      - 64-char hex string            → decoded as 32 bytes
      - 32-char hex string (UUID)     → decoded as 16 bytes, zero-padded to 32
      - int / float                   → written as little-endian u64 in first 8 bytes
      - any other string              → SHA256 hashed to 32 bytes
    """
    if isinstance(purchase_id, (bytes, bytearray)):
        if len(purchase_id) != 32:
            raise ValueError(
                f"bytes purchase_id must be exactly 32 bytes, got {len(purchase_id)}"
            )
        return bytes(purchase_id)

    if isinstance(purchase_id, (int, float)):
        result = bytearray(32)
        struct.pack_into("<Q", result, 0, int(purchase_id))
        return bytes(result)

    s = str(purchase_id)
    clean = s.replace("-", "")
    is_hex = all(c in "0123456789abcdefABCDEF" for c in clean)

    if is_hex and len(clean) == 64:
        return bytes.fromhex(clean)
    if is_hex and len(clean) == 32:
        result = bytearray(32)
        result[:16] = bytes.fromhex(clean)
        return bytes(result)

    return hashlib.sha256(s.encode()).digest()


# ── PDA derivation ────────────────────────────────────────────────────────────


def _get_stake_pda(pool: Pubkey, user: Pubkey) -> Pubkey:
    pda, _ = Pubkey.find_program_address(
        [STAKE_SEED, bytes(pool), bytes(user)], PROGRAM_ID
    )
    return pda


def _get_purchase_pda(pool: Pubkey, purchase_id_bytes: bytes) -> Pubkey:
    pda, _ = Pubkey.find_program_address(
        [PURCHASE_SEED, bytes(pool), purchase_id_bytes], PROGRAM_ID
    )
    return pda


def parse_purchase_record_account_data(data: bytes, pool_address: Optional[str] = None) -> Optional[dict]:
    """
    Decode raw PurchaseRecord account data (Anchor account: 8-byte discriminator + body).
    If pool_address is set, returns None when the pool field does not match.
    """
    if len(data) < PURCHASE_ACCOUNT_DATA_LEN:
        return None
    purchase_id_bytes = data[8:40]
    user = str(Pubkey.from_bytes(data[40:72]))
    pool_str = str(Pubkey.from_bytes(data[72:104]))
    if pool_address is not None and pool_str != pool_address:
        return None
    amount = struct.unpack_from("<Q", data, 104)[0]
    staked_at = struct.unpack_from("<q", data, 112)[0]
    bump = int(data[120])
    staked_at_dt = datetime.fromtimestamp(staked_at, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    return {
        "purchase_id_hex": purchase_id_bytes.hex(),
        "user": user,
        "pool": pool_str,
        "amount": amount,
        "staked_at": staked_at,
        "staked_at_dt": staked_at_dt,
        "bump": bump,
    }


def list_purchase_records_for_pool(
    pool_address: str,
    rpc_url: str = DEFAULT_RPC,
) -> dict:
    """
    All PurchaseRecord accounts for this pool via getProgramAccounts (dataSize + pool memcmp).
    """
    pool_pubkey = Pubkey.from_string(pool_address)
    client = Client(rpc_url, commitment=Confirmed)
    filters = [
        PURCHASE_ACCOUNT_DATA_LEN,
        MemcmpOpts(
            offset=POOL_PUBKEY_OFFSET_IN_PURCHASE_ACCOUNT,
            bytes=str(pool_pubkey),
        ),
    ]
    out: dict = {
        "program_id": str(PROGRAM_ID),
        "pool_address": str(pool_pubkey),
        "records": [],
        "parse_errors": [],
        "rpc_error": None,
    }
    try:
        resp = client.get_program_accounts(
            PROGRAM_ID,
            commitment=Confirmed,
            encoding="base64",
            filters=filters,
        )
    except Exception as e:
        out["rpc_error"] = str(e)
        return out

    records: list[dict] = []
    parse_errors: list[dict] = []
    pool_b58 = str(pool_pubkey)
    for ka in resp.value:
        pk = str(ka.pubkey)
        raw = bytes(ka.account.data)
        parsed = parse_purchase_record_account_data(raw, pool_b58)
        if not parsed:
            parse_errors.append({"pubkey": pk, "data_len": len(raw)})
            continue
        row = dict(parsed)
        row["pda"] = pk
        row["lamports"] = int(ka.account.lamports)
        records.append(row)
    out["records"] = records
    out["parse_errors"] = parse_errors
    return out


# ── Instruction builder ───────────────────────────────────────────────────────


def _ix_discriminator(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


def _build_stake_ix(
    authority: Pubkey,
    pool: Pubkey,
    user: Pubkey,
    amount: int,
    purchase_id_bytes: bytes,
) -> tuple[Instruction, Pubkey, Pubkey]:
    stake_pda = _get_stake_pda(pool, user)
    purchase_pda = _get_purchase_pda(pool, purchase_id_bytes)

    data = (
        _ix_discriminator("stake_with_purchase_id")
        + bytes(user)
        + struct.pack("<Q", amount)
        + purchase_id_bytes
    )

    accounts = [
        AccountMeta(pubkey=authority, is_signer=True, is_writable=True),
        AccountMeta(pubkey=pool, is_signer=False, is_writable=True),
        AccountMeta(pubkey=stake_pda, is_signer=False, is_writable=True),
        AccountMeta(pubkey=purchase_pda, is_signer=False, is_writable=True),
        AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
    ]

    return (
        Instruction(program_id=PROGRAM_ID, accounts=accounts, data=data),
        stake_pda,
        purchase_pda,
    )


# ── Public API ────────────────────────────────────────────────────────────────


def stake_with_purchase_id(
    admin_private_key_b58: str,
    pool_address: str,
    user_address: str,
    amount: int,
    purchase_id,
    rpc_url: str = DEFAULT_RPC,
    wait_for_confirmation: bool = True,
) -> dict:
    """
    Stake for a user with a unique purchase ID.

    Parameters:
        admin_private_key_b58   Base58-encoded private key of the pool authority wallet
        pool_address            Pool PDA address (base58 string)
        user_address            User wallet address to stake for (base58 string)
        amount                  Amount to stake (integer)
        purchase_id             Unique purchase identifier. Accepts:
                                  - str  (UUID, hex, or any string)
                                  - int  (numeric order ID)
                                  - bytes (raw 32 bytes)
        rpc_url                 Solana RPC endpoint (default: mainnet-beta)
        wait_for_confirmation   Block until the transaction is confirmed (default: True)

    Returns a dict:
        {
          "success":        bool,
          "signature":      str | None,
          "already_staked": bool,   # True if purchase_id was already used
          "stake_pda":      str,
          "purchase_pda":   str,
          "error":          str | None,
        }
    """
    result = {
        "success": False,
        "signature": None,
        "already_staked": False,
        "stake_pda": None,
        "purchase_pda": None,
        "error": None,
    }

    try:
        keypair = keypair_from_base58(admin_private_key_b58)
        pool_pubkey = Pubkey.from_string(pool_address)
        user_pubkey = Pubkey.from_string(user_address)
        purchase_id_bytes = to_purchase_id_bytes(purchase_id)

        ix, stake_pda, purchase_pda = _build_stake_ix(
            keypair.pubkey(), pool_pubkey, user_pubkey, amount, purchase_id_bytes
        )
        result["stake_pda"] = str(stake_pda)
        result["purchase_pda"] = str(purchase_pda)

        client = Client(rpc_url, commitment=Confirmed)
        blockhash = client.get_latest_blockhash().value.blockhash
        msg = Message.new_with_blockhash([ix], keypair.pubkey(), blockhash)
        tx = Transaction([keypair], msg, blockhash)

        resp = client.send_transaction(
            tx, opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
        )
        sig = resp.value
        result["signature"] = str(sig)

        if wait_for_confirmation:
            for _ in range(30):
                time.sleep(2)
                status = client.get_signature_statuses([sig]).value[0]
                if status is not None:
                    if status.err:
                        raise RuntimeError(f"Transaction failed on-chain: {status.err}")
                    if str(status.confirmation_status).lower() in (
                        "confirmed",
                        "finalized",
                    ):
                        break

        result["success"] = True

    except Exception as e:
        msg = str(e)
        # Detect duplicate purchase_id (account already in use)
        if "already in use" in msg.lower() or "0x0" in msg:
            result["already_staked"] = True
            result["error"] = "This purchase ID has already been staked in this pool."
        else:
            result["error"] = msg

    return result


def check_purchase_id(
    pool_address: str,
    purchase_id,
    rpc_url: str = DEFAULT_RPC,
) -> dict:
    """
    Check if a purchase ID has already been staked in a pool.
    No transaction — pure read.

    Parameters:
        pool_address   Pool PDA address (base58 string)
        purchase_id    Purchase identifier (str, int, or bytes)
        rpc_url        Solana RPC endpoint

    Returns a dict:
        {
          "staked":      bool,
          "pda":         str,         # purchase record PDA address
          "user":        str | None,  # wallet address if staked
          "amount":      int | None,
          "staked_at":   int | None,  # unix timestamp
          "staked_at_dt": str | None, # human-readable UTC
        }
    """
    pool_pubkey = Pubkey.from_string(pool_address)
    purchase_id_bytes = to_purchase_id_bytes(purchase_id)
    pda = _get_purchase_pda(pool_pubkey, purchase_id_bytes)

    client = Client(rpc_url, commitment=Confirmed)
    resp = client.get_account_info(pda)

    base = {
        "staked": False,
        "pda": str(pda),
        "user": None,
        "amount": None,
        "staked_at": None,
        "staked_at_dt": None,
    }

    if resp.value is None:
        return base

    data = bytes(resp.value.data)
    if len(data) < PURCHASE_RECORD_LEN:
        return base

    user = str(Pubkey.from_bytes(data[40:72]))
    amount = struct.unpack_from("<Q", data, 104)[0]
    staked_at = struct.unpack_from("<q", data, 112)[0]
    staked_at_dt = datetime.fromtimestamp(staked_at, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

    return {
        "staked": True,
        "pda": str(pda),
        "user": user,
        "amount": amount,
        "staked_at": staked_at,
        "staked_at_dt": staked_at_dt,
    }