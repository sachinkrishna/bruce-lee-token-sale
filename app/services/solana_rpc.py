import logging
import uuid
from typing import Optional

import httpx
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import (
    TransferCheckedParams,
    create_idempotent_associated_token_account,
    get_associated_token_address,
    transfer_checked,
)

from app.config import settings

logger = logging.getLogger(__name__)

_http_client: Optional[httpx.AsyncClient] = None

# In-memory balance ledger for test mode (pubkey -> lamports)
_test_balances: dict = {}

# In-memory memo ledger for test mode: memo -> signature
_test_memos: dict = {}

# In-memory SPL balance ledger for test mode:
# (owner_pubkey, mint_pubkey) -> raw_amount
_test_spl_balances: dict = {}

# SPL Memo Program v2 (the standard memo program)
MEMO_PROGRAM_ID = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30.0)
    return _http_client


async def init_http_client() -> None:
    global _http_client
    _http_client = httpx.AsyncClient(timeout=30.0)


async def close_http_client() -> None:
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


def test_set_balance(pubkey: str, lamports: int) -> None:
    _test_balances[pubkey] = lamports
    logger.info(f"[TEST] Set balance for {pubkey}: {lamports} lamports ({lamports / 1e9:.6f} SOL)")


def test_get_balances() -> dict:
    return dict(_test_balances)


async def rpc_request(method: str, params: list) -> dict:
    client = get_http_client()
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    resp = await client.post(settings.quicknode_rpc_url, json=payload)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise Exception(f"RPC error: {data['error']}")
    return data["result"]


async def get_balance(pubkey: str, commitment: str = "confirmed") -> int:
    if settings.test_mode:
        return _test_balances.get(pubkey, 0)

    result = await rpc_request("getBalance", [pubkey, {"commitment": commitment}])
    balance = result["value"]
    if balance > 0:
        logger.info(f"Balance for {pubkey[:12]}...: {balance} lamports ({balance / 1e9:.6f} SOL)")
    return balance


async def get_balance_stable(
    pubkey: str,
    *,
    commitment: str = "confirmed",
    attempts: int = 8,
    delay_s: float = 1.25,
    lamport_tolerance: int = 10_000,
) -> int:
    """Poll until two consecutive balance reads agree (mitigates post-tx RPC / load-balancer lag)."""
    import asyncio

    prev = await get_balance(pubkey, commitment=commitment)
    for _ in range(attempts - 1):
        await asyncio.sleep(delay_s)
        cur = await get_balance(pubkey, commitment=commitment)
        if abs(cur - prev) <= lamport_tolerance:
            return cur
        prev = cur
    return prev


async def get_latest_blockhash() -> str:
    if settings.test_mode:
        return "0" * 44

    result = await rpc_request("getLatestBlockhash", [{"commitment": "finalized"}])
    return result["value"]["blockhash"]


async def transfer_sol(
    from_keypair: Keypair, to_pubkey: Pubkey, lamports: int
) -> str:
    if settings.test_mode:
        from_pub = str(from_keypair.pubkey())
        to_pub = str(to_pubkey)
        current = _test_balances.get(from_pub, 0)
        _test_balances[from_pub] = max(0, current - lamports - 5000)
        _test_balances[to_pub] = _test_balances.get(to_pub, 0) + lamports
        sig = f"test_tx_{uuid.uuid4().hex[:16]}"
        logger.info(f"[TEST] Transfer {lamports} lamports: {from_pub[:8]}.. -> {to_pub[:8]}.. tx={sig}")
        return sig

    blockhash_str = await get_latest_blockhash()
    blockhash = Hash.from_string(blockhash_str)

    ix = transfer(TransferParams(from_pubkey=from_keypair.pubkey(), to_pubkey=to_pubkey, lamports=lamports))
    msg = Message.new_with_blockhash([ix], from_keypair.pubkey(), blockhash)
    tx = Transaction.new_unsigned(msg)
    tx.sign([from_keypair], blockhash)

    tx_bytes = bytes(tx)
    import base64

    encoded = base64.b64encode(tx_bytes).decode("utf-8")

    result = await rpc_request(
        "sendTransaction",
        [encoded, {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed"}],
    )
    signature = result
    logger.info(f"SOL transfer tx: {signature}")
    return signature


async def transfer_sol_with_memo(
    from_keypair: Keypair,
    to_pubkey: Pubkey,
    lamports: int,
    memo: str,
) -> str:
    """SOL transfer with an attached SPL memo for on-chain idempotency lookup."""
    if settings.test_mode:
        from_pub = str(from_keypair.pubkey())
        to_pub = str(to_pubkey)
        current = _test_balances.get(from_pub, 0)
        _test_balances[from_pub] = max(0, current - lamports - 5000)
        _test_balances[to_pub] = _test_balances.get(to_pub, 0) + lamports
        sig = f"test_tx_{uuid.uuid4().hex[:16]}"
        _test_memos[memo] = sig
        logger.info(
            "[TEST] Transfer %s lamports w/ memo %s: %s.. -> %s.. tx=%s",
            lamports, memo, from_pub[:8], to_pub[:8], sig,
        )
        return sig

    blockhash_str = await get_latest_blockhash()
    blockhash = Hash.from_string(blockhash_str)

    memo_ix = Instruction(
        program_id=MEMO_PROGRAM_ID,
        accounts=[],
        data=memo.encode("utf-8"),
    )
    transfer_ix = transfer(
        TransferParams(
            from_pubkey=from_keypair.pubkey(),
            to_pubkey=to_pubkey,
            lamports=lamports,
        )
    )
    msg = Message.new_with_blockhash([memo_ix, transfer_ix], from_keypair.pubkey(), blockhash)
    tx = Transaction.new_unsigned(msg)
    tx.sign([from_keypair], blockhash)

    import base64

    encoded = base64.b64encode(bytes(tx)).decode("utf-8")
    result = await rpc_request(
        "sendTransaction",
        [encoded, {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed"}],
    )
    logger.info("SOL transfer w/ memo tx: %s (memo=%s)", result, memo)
    return result


async def find_signature_by_memo(
    funding_pubkey: str,
    memo: str,
    *,
    limit: int = 1000,
) -> Optional[str]:
    """Scan recent signatures of `funding_pubkey` for one whose memo contains `memo`.

    `getSignaturesForAddress` returns a `memo` field for each entry when memo instructions
    were present in the tx, so this is a single RPC call per lookup.
    """
    if settings.test_mode:
        return _test_memos.get(memo)

    page_limit = min(limit, 1000)
    result = await rpc_request(
        "getSignaturesForAddress",
        [funding_pubkey, {"limit": page_limit}],
    )
    for entry in result or []:
        entry_memo = entry.get("memo") or ""
        if memo in entry_memo and entry.get("err") is None:
            return entry.get("signature")
    return None


async def confirm_transaction(signature: str, max_retries: int = 30) -> bool:
    if settings.test_mode:
        return True

    import asyncio

    for _ in range(max_retries):
        result = await rpc_request(
            "getSignatureStatuses", [[signature], {"searchTransactionHistory": True}]
        )
        statuses = result["value"]
        if statuses and statuses[0]:
            status = statuses[0]
            if status.get("confirmationStatus") in ("confirmed", "finalized"):
                if status.get("err") is None:
                    return True
                logger.error(f"Transaction {signature} failed: {status['err']}")
                return False
        await asyncio.sleep(2)
    logger.warning(f"Transaction {signature} confirmation timed out")
    return False


async def get_token_account_balance(token_account: str) -> float:
    if settings.test_mode:
        return 400_000.0

    result = await rpc_request("getTokenAccountBalance", [token_account])
    return float(result["value"]["uiAmount"] or 0)


# ─────────────────────────────────────────────────────────────────────────────
# SPL Token helpers (used by XFEE-mode purchases + XFEE-side pool settlement)
# ─────────────────────────────────────────────────────────────────────────────


def derive_ata(owner_pubkey: str, mint_pubkey: str) -> str:
    """Compute the Associated Token Account address for an (owner, mint) pair."""
    return str(
        get_associated_token_address(
            Pubkey.from_string(owner_pubkey),
            Pubkey.from_string(mint_pubkey),
        )
    )


def test_set_spl_balance(owner_pubkey: str, mint_pubkey: str, raw_amount: int) -> None:
    _test_spl_balances[(owner_pubkey, mint_pubkey)] = raw_amount
    logger.info(
        "[TEST] Set SPL balance for owner=%s mint=%s: %s raw",
        owner_pubkey, mint_pubkey, raw_amount,
    )


async def get_spl_balance_raw(owner_pubkey: str, mint_pubkey: str) -> int:
    """Return the raw (integer, lamport-equivalent) SPL balance for owner+mint.

    Returns 0 when the ATA does not exist yet — matches the "account not found"
    case reported by getTokenAccountBalance.
    """
    if settings.test_mode:
        return int(_test_spl_balances.get((owner_pubkey, mint_pubkey), 0))

    ata = derive_ata(owner_pubkey, mint_pubkey)
    try:
        result = await rpc_request("getTokenAccountBalance", [ata])
        amount = result["value"].get("amount")
        return int(amount or 0)
    except Exception as exc:
        # Missing / uninitialized ATA is common — surface as zero.
        msg = str(exc)
        if "could not find account" in msg.lower() or "invalid param" in msg.lower():
            return 0
        raise


async def get_spl_balance_stable(
    owner_pubkey: str,
    mint_pubkey: str,
    *,
    attempts: int = 8,
    delay_s: float = 1.25,
    tolerance: int = 0,
) -> int:
    """Poll SPL balance until two consecutive reads agree.

    Mirrors `get_balance_stable` for SOL — protects against RPC load-balancer
    lag right after a transfer confirms.
    """
    import asyncio

    prev = await get_spl_balance_raw(owner_pubkey, mint_pubkey)
    for _ in range(attempts - 1):
        await asyncio.sleep(delay_s)
        cur = await get_spl_balance_raw(owner_pubkey, mint_pubkey)
        if abs(cur - prev) <= tolerance:
            return cur
        prev = cur
    return prev


async def _send_signed_tx(tx: Transaction) -> str:
    import base64

    encoded = base64.b64encode(bytes(tx)).decode("utf-8")
    result = await rpc_request(
        "sendTransaction",
        [encoded, {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed"}],
    )
    return result


async def transfer_spl_token(
    from_keypair: Keypair,
    to_owner_pubkey: Pubkey,
    mint_pubkey: Pubkey,
    amount_raw: int,
    decimals: int,
    *,
    fee_payer_keypair: Optional[Keypair] = None,
    memo: Optional[str] = None,
) -> str:
    """Transfer an SPL token from `from_keypair`'s ATA to `to_owner_pubkey`'s ATA.

    - Always prepends an idempotent `CreateAssociatedTokenAccount` for the
      recipient so the transfer succeeds even if the recipient has never held
      this token before.
    - `fee_payer_keypair` (optional): when provided, this keypair pays SOL fees
      AND rent for the ATA creation. When `None`, the sender pays both. Useful
      for commission dispatch: buyer's ephemeral wallet is the SPL sender, but
      master pays the ATA rent so we don't blow through the buyer's gas buffer.
    - `memo` (optional): attaches an SPL memo instruction — needed for the
      global-pool XFEE-side settlement idempotency scan.
    """
    if settings.test_mode:
        from_owner = str(from_keypair.pubkey())
        to_owner = str(to_owner_pubkey)
        mint_str = str(mint_pubkey)
        current = _test_spl_balances.get((from_owner, mint_str), 0)
        _test_spl_balances[(from_owner, mint_str)] = max(0, current - amount_raw)
        _test_spl_balances[(to_owner, mint_str)] = (
            _test_spl_balances.get((to_owner, mint_str), 0) + amount_raw
        )
        sig = f"test_spl_{uuid.uuid4().hex[:16]}"
        if memo:
            _test_memos[memo] = sig
        logger.info(
            "[TEST] SPL transfer %s raw mint=%s: %s.. -> %s.. tx=%s (memo=%s)",
            amount_raw, mint_str[:8], from_owner[:8], to_owner[:8], sig, memo,
        )
        return sig

    fee_payer_kp = fee_payer_keypair or from_keypair
    fee_payer = fee_payer_kp.pubkey()
    sender = from_keypair.pubkey()

    source_ata = get_associated_token_address(sender, mint_pubkey)
    dest_ata = get_associated_token_address(to_owner_pubkey, mint_pubkey)

    ixs: list[Instruction] = []
    if memo:
        ixs.append(
            Instruction(
                program_id=MEMO_PROGRAM_ID,
                accounts=[],
                data=memo.encode("utf-8"),
            )
        )
    ixs.append(
        create_idempotent_associated_token_account(
            payer=fee_payer,
            owner=to_owner_pubkey,
            mint=mint_pubkey,
        )
    )
    ixs.append(
        transfer_checked(
            TransferCheckedParams(
                program_id=TOKEN_PROGRAM_ID,
                source=source_ata,
                mint=mint_pubkey,
                dest=dest_ata,
                owner=sender,
                amount=amount_raw,
                decimals=decimals,
                signers=[],
            )
        )
    )

    blockhash_str = await get_latest_blockhash()
    blockhash = Hash.from_string(blockhash_str)
    msg = Message.new_with_blockhash(ixs, fee_payer, blockhash)
    tx = Transaction.new_unsigned(msg)

    # Sign with both keypairs when fee_payer != sender. `Transaction.sign` de-dups.
    signers = [fee_payer_kp]
    if bytes(fee_payer_kp) != bytes(from_keypair):
        signers.append(from_keypair)
    tx.sign(signers, blockhash)

    sig = await _send_signed_tx(tx)
    logger.info(
        "SPL transfer tx: %s (amount=%s raw, mint=%s, memo=%s)",
        sig, amount_raw, str(mint_pubkey)[:8], memo,
    )
    return sig


async def find_spl_signature_by_memo(
    owner_pubkey: str,
    memo: str,
    *,
    limit: int = 1000,
) -> Optional[str]:
    """Scan recent signatures of an owner wallet for one whose memo contains `memo`.

    Because the SPL memo appears on the wrapping transaction — regardless of
    whether it carried a SOL transfer or an SPL transfer — the same helper
    covers both currencies. We scan the owner wallet (not the ATA) because
    `getSignaturesForAddress` on the owner reliably returns transactions that
    include SPL transfers signed by that owner.
    """
    if settings.test_mode:
        return _test_memos.get(memo)

    page_limit = min(limit, 1000)
    result = await rpc_request(
        "getSignaturesForAddress",
        [owner_pubkey, {"limit": page_limit}],
    )
    for entry in result or []:
        entry_memo = entry.get("memo") or ""
        if memo in entry_memo and entry.get("err") is None:
            return entry.get("signature")
    return None
