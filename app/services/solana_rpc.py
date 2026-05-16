import logging
import uuid
from typing import Optional

import httpx
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction

from app.config import settings

logger = logging.getLogger(__name__)

_http_client: Optional[httpx.AsyncClient] = None

# In-memory balance ledger for test mode (pubkey -> lamports)
_test_balances: dict = {}


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
