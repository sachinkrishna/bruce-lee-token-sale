"""Airdrop a new SPL token to every wallet in `holders.csv`.

Reads `holders.csv` (produced by `holder_snapshot.py`), and for each `owner`
sends `amount_ui * MULTIPLIER` of the new token, converted to the new token's
decimals. Uses `transfer_checked` for safety, batches multiple recipients per
transaction, idempotently creates ATAs only when needed, and persists per-owner
progress to `airdrop_state.json` so re-running is safe.

Run:
    python airdrop_from_csv.py            # send for real
    DRY_RUN=true python airdrop_from_csv.py  # simulate only (or flip the flag)
"""

import asyncio
import base64
import csv
import json
import logging
import os
import time
from pathlib import Path

import httpx
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from spl.token.constants import TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID
from spl.token.instructions import (
    TransferCheckedParams,
    create_idempotent_associated_token_account,
    get_associated_token_address,
    transfer_checked,
)

# --- CONFIGURATION ---
HELIUS_API_KEY = "86c830da-67f9-4c06-9d98-ccdcb6b8393a"
RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

HOLDERS_CSV = "holders.csv"
NEW_TOKEN_MINT = "96egraTCizRpzNx4WvMMhPJf7TKS7W5kUHeenmd1XfuN"                  # mint pubkey of the token being airdropped
SENDER_PRIVATE_KEY_B58 = "5idzSgPntFhW4ViFNAcezWMibDfUNHUP4XR961itBN8a9WtDfKPbwUKNyVGPJHvNVtGtZTbkpwdL2G4zVUY3MgwF"          # base58 secret key of the sender wallet
IS_TOKEN_2022 = False                # True if NEW_TOKEN_MINT is Token-2022

MULTIPLIER = 1.0                     # new_ui = original_ui * MULTIPLIER
BATCH_SIZE = 5                       # recipients per transaction
PRIORITY_FEE_MICROLAMPORTS = 50_000
COMPUTE_UNIT_LIMIT = 400_000

STATE_FILE = "airdrop_state.json"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# Skip these owners (e.g. LP pools, the sender itself, known program addresses)
BLACKLIST: set[str] = set()
# ---------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("airdrop")


def load_state(path: str) -> dict:
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text())
    return {"sent": {}}


def save_state(path: str, state: dict) -> None:
    Path(path).write_text(json.dumps(state, indent=2))


def load_holders_csv(path: str) -> list[dict]:
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        return [
            {"owner": row["owner"], "amount_ui": float(row["amount_ui"])}
            for row in reader
            if float(row["amount_ui"]) > 0
        ]


async def rpc(client: httpx.AsyncClient, method: str, params):
    r = await client.post(
        RPC_URL,
        json={"jsonrpc": "2.0", "id": method, "method": method, "params": params},
        timeout=60.0,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"{method} error: {data['error']}")
    return data["result"]


async def get_decimals(client, mint: str) -> int:
    res = await rpc(client, "getTokenSupply", [mint])
    return int(res["value"]["decimals"])


async def get_token_balance_raw(client, ata: str) -> int:
    res = await rpc(client, "getTokenAccountBalance", [ata])
    return int(res["value"]["amount"])


async def existing_atas(client, atas: list[str]) -> set[str]:
    found: set[str] = set()
    for i in range(0, len(atas), 100):
        chunk = atas[i : i + 100]
        res = await rpc(client, "getMultipleAccounts", [chunk, {"encoding": "base64"}])
        for addr, acc in zip(chunk, res["value"]):
            if acc is not None:
                found.add(addr)
    return found


async def send_and_confirm(client, signed_tx_b64: str) -> str:
    sig = await rpc(
        client,
        "sendTransaction",
        [
            signed_tx_b64,
            {
                "encoding": "base64",
                "skipPreflight": False,
                "preflightCommitment": "confirmed",
                "maxRetries": 5,
            },
        ],
    )
    for _ in range(45):
        res = await rpc(
            client, "getSignatureStatuses", [[sig], {"searchTransactionHistory": True}]
        )
        st = res["value"][0]
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            if st.get("err"):
                raise RuntimeError(f"tx {sig} failed: {st['err']}")
            return sig
        await asyncio.sleep(2)
    raise TimeoutError(f"tx {sig} not confirmed in time")


async def airdrop():
    if not NEW_TOKEN_MINT or not SENDER_PRIVATE_KEY_B58:
        raise SystemExit("Set NEW_TOKEN_MINT and SENDER_PRIVATE_KEY_B58 at the top.")
    if not Path(HOLDERS_CSV).exists():
        raise SystemExit(f"{HOLDERS_CSV} not found. Run holder_snapshot.py first.")

    state = load_state(STATE_FILE)
    holders = load_holders_csv(HOLDERS_CSV)
    log.info(f"Loaded {len(holders)} rows from {HOLDERS_CSV}")

    sender_kp = Keypair.from_base58_string(SENDER_PRIVATE_KEY_B58)
    sender_pub = sender_kp.pubkey()
    new_mint_pub = Pubkey.from_string(NEW_TOKEN_MINT)
    token_program = TOKEN_2022_PROGRAM_ID if IS_TOKEN_2022 else TOKEN_PROGRAM_ID

    sender_ata = get_associated_token_address(
        sender_pub, new_mint_pub, token_program_id=token_program
    )

    async with httpx.AsyncClient(timeout=60.0) as client:
        new_decimals = await get_decimals(client, NEW_TOKEN_MINT)
        scale = 10 ** new_decimals
        log.info(f"New token decimals = {new_decimals} | sender ATA = {sender_ata}")

        # Build worklist (skip already-paid + blacklist + sender itself)
        skip = set(BLACKLIST) | {str(sender_pub)} | set(state["sent"].keys())
        work = []
        for h in holders:
            if h["owner"] in skip:
                continue
            ui = h["amount_ui"] * MULTIPLIER
            amount_raw = int(round(ui * scale))
            if amount_raw <= 0:
                continue
            owner_pub = Pubkey.from_string(h["owner"])
            ata = get_associated_token_address(
                owner_pub, new_mint_pub, token_program_id=token_program
            )
            work.append(
                {
                    "owner": h["owner"],
                    "owner_pub": owner_pub,
                    "ata": ata,
                    "amount_raw": amount_raw,
                    "amount_ui": ui,
                }
            )

        if not work:
            log.info("Nothing to do.")
            return

        log.info(f"{len(work)} recipients queued (skipped {len(skip)})")

        # Determine which ATAs need to be created
        ata_strs = [str(w["ata"]) for w in work]
        existing = await existing_atas(client, ata_strs)
        to_create = sum(1 for s in ata_strs if s not in existing)
        log.info(f"ATAs to create: {to_create} / {len(work)}")

        # Pre-flight: token balance + SOL balance
        sender_bal_raw = await get_token_balance_raw(client, str(sender_ata))
        total_needed = sum(w["amount_raw"] for w in work)
        log.info(
            f"Sender token balance: {sender_bal_raw / scale} | "
            f"required: {total_needed / scale}"
        )
        if sender_bal_raw < total_needed:
            raise SystemExit(
                f"Insufficient new-token balance: have {sender_bal_raw}, need {total_needed} (raw)"
            )

        sol_balance = (
            await rpc(client, "getBalance", [str(sender_pub), {"commitment": "confirmed"}])
        )["value"]
        rent_per_ata = 2_039_280
        num_txs = (len(work) + BATCH_SIZE - 1) // BATCH_SIZE
        priority_per_tx = (PRIORITY_FEE_MICROLAMPORTS * COMPUTE_UNIT_LIMIT) // 1_000_000
        est_fee = num_txs * (5000 + priority_per_tx) + to_create * rent_per_ata
        log.info(
            f"SOL balance: {sol_balance / 1e9:.6f} | "
            f"estimated cost: {est_fee / 1e9:.6f} ({num_txs} txs, {to_create} ATA rents)"
        )
        if sol_balance < int(est_fee * 1.1):
            raise SystemExit("Sender SOL balance too low to cover fees + ATA rent.")

        # Send batches
        for batch_idx in range(0, len(work), BATCH_SIZE):
            batch = work[batch_idx : batch_idx + BATCH_SIZE]
            ixs = [
                set_compute_unit_limit(COMPUTE_UNIT_LIMIT),
                set_compute_unit_price(PRIORITY_FEE_MICROLAMPORTS),
            ]
            for w in batch:
                if str(w["ata"]) not in existing:
                    ixs.append(
                        create_idempotent_associated_token_account(
                            payer=sender_pub,
                            owner=w["owner_pub"],
                            mint=new_mint_pub,
                            token_program_id=token_program,
                        )
                    )
                ixs.append(
                    transfer_checked(
                        TransferCheckedParams(
                            program_id=token_program,
                            source=sender_ata,
                            mint=new_mint_pub,
                            dest=w["ata"],
                            owner=sender_pub,
                            amount=w["amount_raw"],
                            decimals=new_decimals,
                            signers=[],
                        )
                    )
                )

            blockhash_str = (
                await rpc(client, "getLatestBlockhash", [{"commitment": "finalized"}])
            )["value"]["blockhash"]
            blockhash = Hash.from_string(blockhash_str)
            msg = Message.new_with_blockhash(ixs, sender_pub, blockhash)
            tx = Transaction.new_unsigned(msg)
            tx.sign([sender_kp], blockhash)
            tx_b64 = base64.b64encode(bytes(tx)).decode()

            label = f"batch {batch_idx // BATCH_SIZE + 1}/{num_txs} ({len(batch)} recipients)"

            if DRY_RUN:
                sim = await rpc(
                    client,
                    "simulateTransaction",
                    [
                        tx_b64,
                        {"encoding": "base64", "commitment": "confirmed", "sigVerify": False},
                    ],
                )
                log.info(f"[DRY RUN] {label} simulate err={sim['value'].get('err')}")
                continue

            try:
                sig = await send_and_confirm(client, tx_b64)
                log.info(f"{label} confirmed: {sig}")
                ts = int(time.time())
                for w in batch:
                    state["sent"][w["owner"]] = {
                        "sig": sig,
                        "amount_raw": w["amount_raw"],
                        "amount_ui": w["amount_ui"],
                        "ts": ts,
                    }
                    existing.add(str(w["ata"]))
                save_state(STATE_FILE, state)
            except Exception as e:
                log.exception(f"{label} failed: {e}. State preserved; rerun to retry.")
                continue

    log.info(f"Done. Total owners marked paid: {len(state['sent'])}")


if __name__ == "__main__":
    asyncio.run(airdrop())
