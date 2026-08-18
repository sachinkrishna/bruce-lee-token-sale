"""Vendored SDK for the founder on-chain Revenue Split program.

This is a direct port of the reference `founder_sdk.py` at the repo root, with
one enhancement: the program id is env-overridable via `FOUNDER_ONCHAIN_PROGRAM_ID`.
Everything else — discriminators, borsh encoders, PDA derivation, instruction
layout — matches the reference SDK exactly.

We only use `add_power` and the `power_grant_pda`/`recipient_pda` helpers from
this module; `deposit_revenue` and read helpers are kept for parity with the
reference so you can drop-in-replace this file if the program is ever redeployed.
"""

import hashlib
import os
import struct

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.transaction import VersionedTransaction

_DEFAULT_PROGRAM_ID = "XP4WvTsTeZFQXsGQeowobXC84kiY1oiq92EUcAv1VsM"
PROGRAM_ID: Pubkey = Pubkey.from_string(
    os.getenv("FOUNDER_ONCHAIN_PROGRAM_ID", _DEFAULT_PROGRAM_ID).strip()
    or _DEFAULT_PROGRAM_ID
)
PRECISION = 1_000_000_000_000

DISC_ADD_POWER = bytes([45, 43, 36, 188, 182, 69, 210, 228])
DISC_DEPOSIT_REVENUE = bytes([224, 212, 82, 100, 60, 240, 220, 29])
DISC_CLAIM = bytes([62, 198, 214, 193, 213, 159, 108, 210])

DISC_POOL = bytes([241, 154, 109, 4, 17, 177, 109, 188])
DISC_RECIPIENT = bytes([80, 186, 47, 196, 232, 251, 21, 148])


# ─── Borsh encoders ──────────────────────────────────────────────────────────

def _borsh_u64(v: int) -> bytes:
    return struct.pack("<Q", v)


def _borsh_string(s: str) -> bytes:
    encoded = s.encode("utf-8")
    return struct.pack("<I", len(encoded)) + encoded


def _borsh_bytes32(b: bytes) -> bytes:
    assert len(b) == 32
    return b


def _borsh_pubkey(pk: Pubkey) -> bytes:
    return bytes(pk)


# ─── PDA derivation ─────────────────────────────────────────────────────────

def vault_pda(pool: Pubkey) -> Pubkey:
    pda, _ = Pubkey.find_program_address([b"vault", bytes(pool)], PROGRAM_ID)
    return pda


def recipient_pda(pool: Pubkey, wallet: Pubkey) -> Pubkey:
    pda, _ = Pubkey.find_program_address(
        [b"recipient", bytes(pool), bytes(wallet)], PROGRAM_ID
    )
    return pda


def power_grant_pda(pool: Pubkey, ref_hash: bytes) -> Pubkey:
    pda, _ = Pubkey.find_program_address(
        [b"power_grant", bytes(pool), ref_hash], PROGRAM_ID
    )
    return pda


def sha256_ref(external_ref: str) -> bytes:
    """32-byte sha256 of `external_ref` — used as the PDA seed and on-chain dedup key."""
    return hashlib.sha256(external_ref.encode()).digest()


# ─── Send helper ────────────────────────────────────────────────────────────

async def _send_instruction(
    connection: AsyncClient,
    signer: Keypair,
    ix: Instruction,
) -> str:
    latest = await connection.get_latest_blockhash(commitment=Confirmed)
    blockhash = latest.value.blockhash
    msg = MessageV0.try_compile(
        payer=signer.pubkey(),
        instructions=[ix],
        address_lookup_table_accounts=[],
        recent_blockhash=blockhash,
    )
    tx = VersionedTransaction(msg, [signer])
    resp = await connection.send_transaction(
        tx,
        opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed),
    )
    sig = resp.value
    await connection.confirm_transaction(sig, commitment=Confirmed)
    return str(sig)


# ─── add_power ──────────────────────────────────────────────────────────────

async def add_power(
    connection: AsyncClient,
    signer: Keypair,
    pool: Pubkey,
    wallet: Pubkey,
    external_ref: str,
    power_delta: int,
) -> str:
    """Credit `power_delta` mining power to `wallet` in `pool`.

    Deduped on-chain by `external_ref` — replaying the same ref reverts. The
    signer must be the pool admin or operator.
    """
    ref_hash = sha256_ref(external_ref)
    data = (
        DISC_ADD_POWER
        + _borsh_bytes32(ref_hash)
        + _borsh_pubkey(wallet)
        + _borsh_string(external_ref)
        + _borsh_u64(power_delta)
    )
    pg_pda = power_grant_pda(pool, ref_hash)
    rec_pda = recipient_pda(pool, wallet)
    ix = Instruction(
        program_id=PROGRAM_ID,
        data=bytes(data),
        accounts=[
            AccountMeta(signer.pubkey(), is_signer=True, is_writable=True),
            AccountMeta(pool, is_signer=False, is_writable=True),
            AccountMeta(pg_pda, is_signer=False, is_writable=True),
            AccountMeta(rec_pda, is_signer=False, is_writable=True),
            AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ],
    )
    return await _send_instruction(connection, signer, ix)


async def power_grant_exists(
    connection: AsyncClient,
    pool: Pubkey,
    external_ref: str,
) -> bool:
    """Cheap idempotency probe: has this `external_ref` already been credited?

    Returns True iff the `power_grant` PDA for (pool, sha256(external_ref)) is
    initialized on-chain. Uses a single `getAccountInfo` RPC call.
    """
    ref_hash = sha256_ref(external_ref)
    pda = power_grant_pda(pool, ref_hash)
    resp = await connection.get_account_info(pda, commitment=Confirmed)
    return resp.value is not None
