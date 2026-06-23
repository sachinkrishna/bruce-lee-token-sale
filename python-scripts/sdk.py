"""
GlobalPoolFactory Python SDK
============================
Depends only on  solders  +  httpx  +  base58.
No anchorpy / solana-py required.

All methods are async. Use the module-level ``run()`` helper for sync calls:

    from sdk import GlobalPoolFactory, run
    sdk  = GlobalPoolFactory()
    info = run(sdk.fetch_state())

Private keys are base58-encoded 64-byte keypair strings (same format
exported by Phantom, Backpack, or `solana-keygen`).
"""

import asyncio
import base64
import struct
from typing import Optional, Union

import base58 as b58
import httpx
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

# ── Constants ─────────────────────────────────────────────────────────────────

LAMPORTS_PER_SOL   = 1_000_000_000
DEFAULT_RPC        = "https://api.devnet.solana.com"
DEFAULT_PROGRAM_ID = "BnHBsdQddqBHtf72HcgirUBrwAyaJjrDbdibjydaUne7"
SYS_PROGRAM_ID     = Pubkey.from_string("11111111111111111111111111111111")

# ── Instruction discriminators (from compiled IDL) ────────────────────────────
# Each is the first 8 bytes of SHA-256("global:<instruction_name>")

_DISC = {
    "admin_batch_close_allocations": bytes([8,   214, 59,  6,   70,  193, 36,  74 ]),
    "admin_close_user_allocation":   bytes([231, 205, 106, 230, 38,  92,  223, 91 ]),
    "claim":                         bytes([62,  198, 214, 193, 213, 159, 108, 210]),
    "close_claim_record":            bytes([250, 193, 24,  86,  13,  34,  66,  240]),
    "create_pool":                   bytes([233, 146, 209, 142, 207, 104, 64,  188]),
    "emergency_withdraw":            bytes([239, 45,  203, 64,  150, 73,  218, 92 ]),
    "freeze_pool":                   bytes([211, 216, 1,   216, 54,  191, 102, 150]),
    "sync_pool_state":               bytes([36,  124, 113, 69,  198, 89,  113, 199]),
    "unfreeze_pool":                 bytes([236, 22,  34,  179, 44,  68,  15,  108]),
    "write_allocation":              bytes([109, 110, 161, 99,  79,  161, 98,  228]),
}

# UserAllocation on-chain layout
_UA_SIZE        = 74
_UA_POOL_OFFSET = 8
_UA_CLAIMED_OFF = 56


# ── Tiny helpers ───────────────────────────────────────────────────────────────

def _kp(b58_key: str) -> Keypair:
    """Keypair from base58-encoded 64-byte secret key."""
    return Keypair.from_bytes(b58.b58decode(b58_key))


def _pk(s: str) -> Pubkey:
    return Pubkey.from_string(s)


def _pda(seeds: list[bytes], program_id: Pubkey) -> Pubkey:
    pubkey, _ = Pubkey.find_program_address(seeds, program_id)
    return pubkey


def _u64(v: int) -> bytes:
    return struct.pack("<Q", v)


def _sol(lamports: int) -> float:
    return lamports / LAMPORTS_PER_SOL


def _opt_pubkey(pk: Optional[Pubkey]) -> bytes:
    """Borsh Option<Pubkey>: 0x00 = None, 0x01 + 32 bytes = Some."""
    return b"\x00" if pk is None else b"\x01" + bytes(pk)


def _opt_u64(v: Optional[int]) -> bytes:
    """Borsh Option<u64>: 0x00 = None, 0x01 + 8 bytes = Some."""
    return b"\x00" if v is None else b"\x01" + _u64(v)


# ── RPC helpers ────────────────────────────────────────────────────────────────

async def _rpc(rpc_url: str, method: str, params: list) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
        })
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"RPC error ({method}): {body['error']}")
    return body["result"]


async def _get_account(rpc_url: str, pubkey: Pubkey) -> Optional[bytes]:
    result = await _rpc(rpc_url, "getAccountInfo", [
        str(pubkey), {"encoding": "base64"},
    ])
    val = result.get("value")
    if not val:
        return None
    return base64.b64decode(val["data"][0])


async def _get_account_lamports(rpc_url: str, pubkey: Pubkey) -> int:
    result = await _rpc(rpc_url, "getAccountInfo", [str(pubkey), {"encoding": "base64"}])
    val = result.get("value")
    return val["lamports"] if val else 0


async def _get_program_accounts(
    rpc_url: str,
    program_id: Pubkey,
    data_size: int,
    pool_id_filter_offset: int,
    pool_id: int,
    extra_filters: Optional[list] = None,
) -> list[dict]:
    pool_id_b58 = b58.b58encode(_u64(pool_id)).decode()
    filters = [
        {"dataSize": data_size},
        {"memcmp": {"offset": pool_id_filter_offset, "bytes": pool_id_b58}},
    ]
    if extra_filters:
        filters += extra_filters
    result = await _rpc(rpc_url, "getProgramAccounts", [
        str(program_id),
        {"encoding": "base64", "filters": filters},
    ])
    return result  # list of {pubkey, account: {data, lamports, ...}}


async def _latest_blockhash(rpc_url: str) -> Hash:
    result = await _rpc(rpc_url, "getLatestBlockhash", [{"commitment": "confirmed"}])
    return Hash.from_string(result["value"]["blockhash"])


async def _send_and_confirm(rpc_url: str, tx: VersionedTransaction) -> str:
    tx_b64 = base64.b64encode(bytes(tx)).decode()
    sig = await _rpc(rpc_url, "sendTransaction", [
        tx_b64, {"encoding": "base64", "skipPreflight": False,
                 "preflightCommitment": "confirmed"},
    ])
    # poll for confirmation
    for _ in range(40):
        await asyncio.sleep(2)
        statuses = await _rpc(rpc_url, "getSignatureStatuses",
                              [[sig], {"searchTransactionHistory": True}])
        st = statuses["value"][0]
        if st is None:
            continue
        if st.get("err"):
            raise RuntimeError(f"Transaction failed: {st['err']}")
        if st.get("confirmationStatus") in ("confirmed", "finalized"):
            return sig
    raise RuntimeError(f"Transaction {sig} not confirmed after timeout.")


async def _send_ix(
    rpc_url: str,
    kp: Keypair,
    ix: Instruction,
    extra_signers: Optional[list[Keypair]] = None,
) -> str:
    bh     = await _latest_blockhash(rpc_url)
    msg    = Message.new_with_blockhash([ix], kp.pubkey(), bh)
    signers = [kp] + (extra_signers or [])
    tx     = VersionedTransaction(msg, signers)
    return await _send_and_confirm(rpc_url, tx)


# ── Account decoders ───────────────────────────────────────────────────────────

def _decode_config(data: bytes) -> dict:
    d = data[8:]   # skip discriminator
    return {
        "admin":                 str(Pubkey.from_bytes(d[0:32])),
        "treasury":              str(Pubkey.from_bytes(d[32:64])),
        "sync_authority":        str(Pubkey.from_bytes(d[64:96])),
        "claim_close_authority": str(Pubkey.from_bytes(d[96:128])),
        "claim_fee":             struct.unpack_from("<Q", d, 128)[0],
        "random_claim_fee_min":  struct.unpack_from("<Q", d, 136)[0],
        "random_claim_fee_max":  struct.unpack_from("<Q", d, 144)[0],
        "pool_count":            struct.unpack_from("<Q", d, 152)[0],
        "bump":                  d[160],
    }


def _decode_pool(data: bytes) -> dict:
    d = data[8:]
    return {
        "pool_id":          struct.unpack_from("<Q", d, 0)[0],
        "total_allocated":  struct.unpack_from("<Q", d, 8)[0],
        "total_claimed":    struct.unpack_from("<Q", d, 16)[0],
        "allocation_count": struct.unpack_from("<Q", d, 24)[0],
        "is_frozen":        bool(d[32]),
        "bump":             d[33],
        "vault_bump":       d[34],
    }


def _decode_ua(data: bytes) -> dict:
    d = data[8:]
    return {
        "pool_id": struct.unpack_from("<Q", d, 0)[0],
        "user":    str(Pubkey.from_bytes(d[8:40])),
        "amount":  struct.unpack_from("<Q", d, 40)[0],
        "claimed": bool(d[48]),
        "bump":    d[49],
    }


# ══════════════════════════════════════════════════════════════════════════════
# SDK class
# ══════════════════════════════════════════════════════════════════════════════

class GlobalPoolFactory:
    """
    Python SDK for the GlobalPoolFactory Anchor program.

    Parameters
    ----------
    rpc_url    : Solana RPC endpoint. Defaults to devnet.
    program_id : Deployed program address (base58 string).
    """

    def __init__(
        self,
        rpc_url:    str = DEFAULT_RPC,
        program_id: str = DEFAULT_PROGRAM_ID,
    ):
        self.rpc_url    = rpc_url
        self.program_id = _pk(program_id)

    # ── PDA helpers ────────────────────────────────────────────────────────────

    def config_pda(self) -> Pubkey:
        return _pda([b"config"], self.program_id)

    def pool_pda(self, pool_id: int) -> Pubkey:
        return _pda([b"pool", _u64(pool_id)], self.program_id)

    def pool_vault_pda(self, pool_id: int) -> Pubkey:
        return _pda([b"pool_vault", _u64(pool_id)], self.program_id)

    def user_allocation_pda(self, pool_id: int, user: Union[str, Pubkey]) -> Pubkey:
        user_pk = _pk(user) if isinstance(user, str) else user
        return _pda([b"user_allocation", _u64(pool_id), bytes(user_pk)], self.program_id)

    def claim_record_pda(self, pool_id: int, user: Union[str, Pubkey]) -> Pubkey:
        user_pk = _pk(user) if isinstance(user, str) else user
        return _pda([b"claim_record", _u64(pool_id), bytes(user_pk)], self.program_id)

    # ── Internal ───────────────────────────────────────────────────────────────

    async def _resolve_pool(
        self,
        pool_address: Optional[str],
        pool_id: Optional[int],
    ) -> tuple[int, Pubkey, Pubkey]:
        """Return (pool_id, pool_pda, pool_vault_pda)."""
        if pool_address:
            ppda  = _pk(pool_address)
            data  = await _get_account(self.rpc_url, ppda)
            if data is None:
                raise ValueError(f"Pool not found at {pool_address}")
            pid = _decode_pool(data)["pool_id"]
        else:
            pid  = pool_id if pool_id is not None else 0
            ppda = self.pool_pda(pid)
        return pid, ppda, self.pool_vault_pda(pid)

    def _ix(self, name: str, args: bytes, accounts: list[AccountMeta]) -> Instruction:
        return Instruction(
            program_id=self.program_id,
            data=_DISC[name] + args,
            accounts=accounts,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # 1. fetch_state
    # ══════════════════════════════════════════════════════════════════════════

    async def fetch_state(
        self,
        pool_address: Optional[str] = None,
        pool_id: Optional[int]      = None,
    ) -> dict:
        """
        Read-only. Returns Config + pool(s) + vault balances. No key required.

        Parameters
        ----------
        pool_address : Show only this pool PDA  (preferred).
        pool_id      : Show only this pool by numeric ID.
        (omit both)  : Show all pools.
        """
        cfg_data = await _get_account(self.rpc_url, self.config_pda())
        if cfg_data is None:
            raise RuntimeError("Config PDA not found — has the program been initialized?")

        cfg        = _decode_config(cfg_data)
        pool_count = cfg["pool_count"]

        result = {
            "config": {
                "pda":                   str(self.config_pda()),
                "admin":                 cfg["admin"],
                "treasury":              cfg["treasury"],
                "sync_authority":        cfg["sync_authority"],
                "claim_close_authority": cfg["claim_close_authority"],
                "claim_fee_sol":         _sol(cfg["claim_fee"]),
                "random_fee_min_sol":    _sol(cfg["random_claim_fee_min"]),
                "random_fee_max_sol":    _sol(cfg["random_claim_fee_max"]),
                "pool_count":            pool_count,
            },
            "pools": [],
        }

        if pool_count == 0:
            return result

        if pool_address:
            ppda  = _pk(pool_address)
            data  = await _get_account(self.rpc_url, ppda)
            ids   = [_decode_pool(data)["pool_id"]] if data else []
        elif pool_id is not None:
            ids   = [pool_id]
        else:
            ids   = list(range(pool_count))

        for pid in ids:
            ppda  = self.pool_pda(pid)
            vpda  = self.pool_vault_pda(pid)
            pdata = await _get_account(self.rpc_url, ppda)
            if pdata is None:
                continue
            pool  = _decode_pool(pdata)
            vlamp = await _get_account_lamports(self.rpc_url, vpda)
            result["pools"].append({
                "pool_id":             pid,
                "pda":                 str(ppda),
                "vault_pda":           str(vpda),
                "vault_sol":           _sol(vlamp),
                "total_allocated_sol": _sol(pool["total_allocated"]),
                "total_claimed_sol":   _sol(pool["total_claimed"]),
                "allocation_count":    pool["allocation_count"],
                "is_frozen":           pool["is_frozen"],
            })

        return result

    # ══════════════════════════════════════════════════════════════════════════
    # 2. create_pool
    # ══════════════════════════════════════════════════════════════════════════

    async def create_pool(self, admin_b58: str, amount_sol: float) -> str:
        """
        Admin creates a new pool and funds its vault.

        Parameters
        ----------
        admin_b58  : Admin wallet private key (base58).
        amount_sol : SOL to deposit into the pool vault.

        Returns tx signature string.
        """
        lamports  = int(amount_sol * LAMPORTS_PER_SOL)
        kp        = _kp(admin_b58)
        cfg_data  = await _get_account(self.rpc_url, self.config_pda())
        pid       = _decode_config(cfg_data)["pool_count"]

        ix = self._ix("create_pool", _u64(lamports), [
            AccountMeta(kp.pubkey(),           is_signer=True,  is_writable=True),
            AccountMeta(self.config_pda(),     is_signer=False, is_writable=True),
            AccountMeta(self.pool_pda(pid),    is_signer=False, is_writable=True),
            AccountMeta(self.pool_vault_pda(pid), is_signer=False, is_writable=True),
            AccountMeta(SYS_PROGRAM_ID,        is_signer=False, is_writable=False),
        ])
        return await _send_ix(self.rpc_url, kp, ix)

    # ══════════════════════════════════════════════════════════════════════════
    # 3. freeze_pool
    # ══════════════════════════════════════════════════════════════════════════

    async def freeze_pool(
        self,
        admin_b58:    str,
        pool_address: Optional[str] = None,
        pool_id:      Optional[int] = None,
    ) -> str:
        """Admin freezes a pool (blocks new allocations and claims)."""
        kp            = _kp(admin_b58)
        pid, ppda, _  = await self._resolve_pool(pool_address, pool_id)

        ix = self._ix("freeze_pool", _u64(pid), [
            AccountMeta(kp.pubkey(),       is_signer=True,  is_writable=False),
            AccountMeta(self.config_pda(), is_signer=False, is_writable=False),
            AccountMeta(ppda,              is_signer=False, is_writable=True),
        ])
        return await _send_ix(self.rpc_url, kp, ix)

    # ══════════════════════════════════════════════════════════════════════════
    # 4. unfreeze_pool
    # ══════════════════════════════════════════════════════════════════════════

    async def unfreeze_pool(
        self,
        admin_b58:    str,
        pool_address: Optional[str] = None,
        pool_id:      Optional[int] = None,
    ) -> str:
        """Admin unfreezes a previously frozen pool."""
        kp            = _kp(admin_b58)
        pid, ppda, _  = await self._resolve_pool(pool_address, pool_id)

        ix = self._ix("unfreeze_pool", _u64(pid), [
            AccountMeta(kp.pubkey(),       is_signer=True,  is_writable=False),
            AccountMeta(self.config_pda(), is_signer=False, is_writable=False),
            AccountMeta(ppda,              is_signer=False, is_writable=True),
        ])
        return await _send_ix(self.rpc_url, kp, ix)

    # ══════════════════════════════════════════════════════════════════════════
    # 5. emergency_withdraw
    # ══════════════════════════════════════════════════════════════════════════

    async def emergency_withdraw(
        self,
        admin_b58:     str,
        target_wallet: str,
        pool_address:  Optional[str] = None,
        pool_id:       Optional[int] = None,
    ) -> str:
        """Admin drains ALL available SOL from a pool vault to target_wallet."""
        kp                 = _kp(admin_b58)
        pid, ppda, vpda    = await self._resolve_pool(pool_address, pool_id)
        target             = _pk(target_wallet)

        ix = self._ix("emergency_withdraw", _u64(pid), [
            AccountMeta(kp.pubkey(),       is_signer=True,  is_writable=False),
            AccountMeta(self.config_pda(), is_signer=False, is_writable=False),
            AccountMeta(ppda,              is_signer=False, is_writable=False),
            AccountMeta(vpda,              is_signer=False, is_writable=True),
            AccountMeta(target,            is_signer=False, is_writable=True),
            AccountMeta(SYS_PROGRAM_ID,    is_signer=False, is_writable=False),
        ])
        return await _send_ix(self.rpc_url, kp, ix)

    # ══════════════════════════════════════════════════════════════════════════
    # 6. write_allocation
    # ══════════════════════════════════════════════════════════════════════════

    async def write_allocation(
        self,
        admin_b58:    str,
        target_user:  str,
        amount_sol:   float,
        pool_address: Optional[str] = None,
        pool_id:      Optional[int] = None,
    ) -> str:
        """Admin writes a SOL allocation for a user from the pool vault."""
        lamports           = int(amount_sol * LAMPORTS_PER_SOL)
        kp                 = _kp(admin_b58)
        pid, ppda, vpda    = await self._resolve_pool(pool_address, pool_id)
        user_pk            = _pk(target_user)
        ua_pda             = self.user_allocation_pda(pid, user_pk)

        ix = self._ix("write_allocation", _u64(pid) + _u64(lamports), [
            AccountMeta(kp.pubkey(),       is_signer=True,  is_writable=True),
            AccountMeta(self.config_pda(), is_signer=False, is_writable=False),
            AccountMeta(ppda,              is_signer=False, is_writable=True),
            AccountMeta(vpda,              is_signer=False, is_writable=True),
            AccountMeta(user_pk,           is_signer=False, is_writable=False),
            AccountMeta(ua_pda,            is_signer=False, is_writable=True),
            AccountMeta(SYS_PROGRAM_ID,    is_signer=False, is_writable=False),
        ])
        return await _send_ix(self.rpc_url, kp, ix)

    # ══════════════════════════════════════════════════════════════════════════
    # 7. claim
    # ══════════════════════════════════════════════════════════════════════════

    async def claim(
        self,
        user_b58:     str,
        pool_address: Optional[str] = None,
        pool_id:      Optional[int] = None,
    ) -> str:
        """
        User claims their full allocation.
        A random fee (0.005–0.006 SOL) is paid from the user's wallet into a
        new ClaimRecord PDA. The full allocated amount is sent back to the user.
        """
        kp            = _kp(user_b58)
        pid, ppda, _  = await self._resolve_pool(pool_address, pool_id)
        ua_pda        = self.user_allocation_pda(pid, kp.pubkey())
        cr_pda        = self.claim_record_pda(pid, kp.pubkey())

        ix = self._ix("claim", _u64(pid), [
            AccountMeta(kp.pubkey(),       is_signer=True,  is_writable=True),
            AccountMeta(self.config_pda(), is_signer=False, is_writable=False),
            AccountMeta(ppda,              is_signer=False, is_writable=True),
            AccountMeta(ua_pda,            is_signer=False, is_writable=True),
            AccountMeta(cr_pda,            is_signer=False, is_writable=True),
            AccountMeta(SYS_PROGRAM_ID,    is_signer=False, is_writable=False),
        ])
        return await _send_ix(self.rpc_url, kp, ix)

    # ══════════════════════════════════════════════════════════════════════════
    # 8. admin_close_allocation
    # ══════════════════════════════════════════════════════════════════════════

    async def admin_close_allocation(
        self,
        admin_b58:     str,
        target_user:   str,
        target_wallet: str,
        pool_address:  Optional[str] = None,
        pool_id:       Optional[int] = None,
    ) -> str:
        """Admin closes a single UserAllocation PDA and recovers all lamports."""
        kp         = _kp(admin_b58)
        pid, _, _  = await self._resolve_pool(pool_address, pool_id)
        user_pk    = _pk(target_user)
        ua_pda     = self.user_allocation_pda(pid, user_pk)
        target_pk  = _pk(target_wallet)

        ix = self._ix("admin_close_user_allocation", _u64(pid), [
            AccountMeta(kp.pubkey(),       is_signer=True,  is_writable=False),
            AccountMeta(self.config_pda(), is_signer=False, is_writable=False),
            AccountMeta(user_pk,           is_signer=False, is_writable=False),
            AccountMeta(ua_pda,            is_signer=False, is_writable=True),
            AccountMeta(target_pk,         is_signer=False, is_writable=True),
            AccountMeta(SYS_PROGRAM_ID,    is_signer=False, is_writable=False),
        ])
        return await _send_ix(self.rpc_url, kp, ix)

    # ══════════════════════════════════════════════════════════════════════════
    # 9. admin_batch_close
    # ══════════════════════════════════════════════════════════════════════════

    async def admin_batch_close(
        self,
        admin_b58:     str,
        target_wallet: str,
        pool_address:  Optional[str] = None,
        pool_id:       Optional[int] = None,
        batch_size:    int           = 20,
    ) -> list[str]:
        """
        Admin closes ALL UserAllocation PDAs for a pool in batches.
        Returns list of tx signatures (one per batch).
        """
        kp        = _kp(admin_b58)
        pid, _, _ = await self._resolve_pool(pool_address, pool_id)
        target_pk = _pk(target_wallet)

        accounts = await _get_program_accounts(
            self.rpc_url, self.program_id,
            data_size=_UA_SIZE, pool_id_filter_offset=_UA_POOL_OFFSET, pool_id=pid,
        )
        if not accounts:
            print("No allocation PDAs found.")
            return []

        print(f"Found {len(accounts)} allocation PDAs.")
        sigs = []

        for i in range(0, len(accounts), batch_size):
            batch = accounts[i : i + batch_size]

            base_accounts = [
                AccountMeta(kp.pubkey(),       is_signer=True,  is_writable=False),
                AccountMeta(self.config_pda(), is_signer=False, is_writable=False),
                AccountMeta(target_pk,         is_signer=False, is_writable=True),
            ]
            remaining = [
                AccountMeta(_pk(acc["pubkey"]), is_signer=False, is_writable=True)
                for acc in batch
            ]

            ix = self._ix("admin_batch_close_allocations", _u64(pid),
                          base_accounts + remaining)
            sig = await _send_ix(self.rpc_url, kp, ix)
            sigs.append(sig)
            n = i // batch_size + 1
            t = (len(accounts) + batch_size - 1) // batch_size
            print(f"Batch {n}/{t}: closed {len(batch)} PDAs → {sig}")

        return sigs

    # ══════════════════════════════════════════════════════════════════════════
    # 10. close_claim_record
    # ══════════════════════════════════════════════════════════════════════════

    async def close_claim_record(
        self,
        authority_b58:     str,
        claim_record_user: str,
        target_wallet:     str,
        pool_address:      Optional[str] = None,
        pool_id:           Optional[int] = None,
    ) -> str:
        """
        Admin OR claim_close_authority closes a ClaimRecord PDA and sweeps
        all lamports (random_fee + rent) to any target_wallet.
        """
        kp         = _kp(authority_b58)
        pid, _, _  = await self._resolve_pool(pool_address, pool_id)
        user_pk    = _pk(claim_record_user)
        cr_pda     = self.claim_record_pda(pid, user_pk)
        target_pk  = _pk(target_wallet)

        ix = self._ix("close_claim_record", _u64(pid), [
            AccountMeta(kp.pubkey(),       is_signer=True,  is_writable=False),
            AccountMeta(self.config_pda(), is_signer=False, is_writable=False),
            AccountMeta(cr_pda,            is_signer=False, is_writable=True),
            AccountMeta(target_pk,         is_signer=False, is_writable=True),
        ])
        return await _send_ix(self.rpc_url, kp, ix)


# ── Sync convenience wrapper ───────────────────────────────────────────────────

def run(coro):
    """
    Run any async SDK method synchronously.

    Example
    -------
    >>> from sdk import GlobalPoolFactory, run
    >>> sdk = GlobalPoolFactory()
    >>> sig = run(sdk.create_pool(admin_b58="...", amount_sol=1.0))
    """
    return asyncio.run(coro)
