"""Pre-create Associated Token Accounts (ATAs) for every wallet in a snapshot
CSV, for a new SPL mint.

Reads the CSV at `INPUT_CSV` (must contain an `owner` column), iterates each
owner, checks on chain whether the owner already has an ATA for `NEW_TOKEN_MINT`,
and submits batched `create_idempotent_associated_token_account` instructions
for the missing ones. The sender wallet pays ~0.00204 SOL of rent per ATA.

Idempotent and resumable:
- Pre-existing ATAs are detected via getMultipleAccounts and skipped.
- Per-owner progress is persisted to `ata_create_state.json` so re-running picks
  up where it left off.
- The instruction itself is the *idempotent* variant: if another tx creates the
  ATA in between, our tx still succeeds.

Run:
    python create_atas.py
    DRY_RUN=true python create_atas.py
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
    create_idempotent_associated_token_account,
    get_associated_token_address,
)

# --- CONFIGURATION ---
HELIUS_API_KEY = "86c830da-67f9-4c06-9d98-ccdcb6b8393a"
RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

INPUT_CSV = "snapshot_Bg64WKiN_20260508_143936.csv"  # any CSV with an `owner` column
NEW_TOKEN_MINT = "96egraTCizRpzNx4WvMMhPJf7TKS7W5kUHeenmd1XfuN"                  # mint pubkey of the new token
SENDER_PRIVATE_KEY_B58 = "5idzSgPntFhW4ViFNAcezWMibDfUNHUP4XR961itBN8a9WtDfKPbwUKNyVGPJHvNVtGtZTbkpwdL2G4zVUY3MgwF"          # base58 secret key of the wallet paying rent
IS_TOKEN_2022 = False

BATCH_SIZE = 12                      # ATAs per transaction
PRIORITY_FEE_MICROLAMPORTS = 50_000
# Each fresh create_idempotent_associated_token_account consumes ~22k CU.
# 12 creations ~ 264k CU; 600k gives comfortable headroom + buffer for the 2
# compute-budget ixs and any per-tx overhead.
COMPUTE_UNIT_LIMIT = 600_000

STATE_FILE = "ata_create_state.json"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# Skip these owners (e.g. LP pools, sender wallet, known programs)
BLACKLIST: set[str] = set()
# ---------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("create_atas")


def load_state(path: str) -> dict:
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text())
    return {"created": {}}


def save_state(path: str, state: dict) -> None:
    Path(path).write_text(json.dumps(state, indent=2))


def load_owners(path: str) -> list[str]:
    seen: set[str] = set()
    owners: list[str] = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if "owner" not in (reader.fieldnames or []):
            raise SystemExit(f"{path} is missing an 'owner' column.")
        for row in reader:
            o = (row.get("owner") or "").strip()
            if o and o not in seen:
                seen.add(o)
                owners.append(o)
    return owners


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


async def main():
    if not NEW_TOKEN_MINT or not SENDER_PRIVATE_KEY_B58:
        raise SystemExit("Set NEW_TOKEN_MINT and SENDER_PRIVATE_KEY_B58 at the top.")
    if not Path(INPUT_CSV).exists():
        raise SystemExit(f"{INPUT_CSV} not found.")

    state = load_state(STATE_FILE)
    owners_raw = load_owners(INPUT_CSV)
    log.info(f"Loaded {len(owners_raw)} unique owners from {INPUT_CSV}")

    sender_kp = Keypair.from_base58_string(SENDER_PRIVATE_KEY_B58)
    sender_pub = sender_kp.pubkey()
    new_mint_pub = Pubkey.from_string(NEW_TOKEN_MINT)
    token_program = TOKEN_2022_PROGRAM_ID if IS_TOKEN_2022 else TOKEN_PROGRAM_ID

    skip = set(BLACKLIST) | {str(sender_pub)} | set(state["created"].keys())

    work = []
    for owner_str in owners_raw:
        if owner_str in skip:
            continue
        try:
            owner_pub = Pubkey.from_string(owner_str)
        except Exception:
            log.warning(f"Skipping invalid pubkey: {owner_str}")
            continue
        ata = get_associated_token_address(
            owner_pub, new_mint_pub, token_program_id=token_program
        )
        work.append({"owner": owner_str, "owner_pub": owner_pub, "ata": ata})

    log.info(f"{len(work)} owners to check ({len(skip)} skipped via state/blacklist)")

    if not work:
        log.info("Nothing to do.")
        return

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Filter out ATAs that already exist on chain
        ata_strs = [str(w["ata"]) for w in work]
        existing = await existing_atas(client, ata_strs)
        to_create = [w for w in work if str(w["ata"]) not in existing]
        log.info(
            f"On-chain check: {len(existing)} ATAs already exist | "
            f"{len(to_create)} need to be created"
        )

        # Mark already-existing ones as done so we skip them next run
        if existing:
            ts = int(time.time())
            for w in work:
                if str(w["ata"]) in existing and w["owner"] not in state["created"]:
                    state["created"][w["owner"]] = {
                        "ata": str(w["ata"]),
                        "sig": "pre_existing",
                        "ts": ts,
                    }
            save_state(STATE_FILE, state)

        if not to_create:
            log.info("All ATAs already exist. Done.")
            return

        # Pre-flight: SOL balance check
        rent_per_ata = 2_039_280
        num_txs = (len(to_create) + BATCH_SIZE - 1) // BATCH_SIZE
        priority_per_tx = (PRIORITY_FEE_MICROLAMPORTS * COMPUTE_UNIT_LIMIT) // 1_000_000
        est_fee = num_txs * (5000 + priority_per_tx) + len(to_create) * rent_per_ata
        sol_balance = (
            await rpc(client, "getBalance", [str(sender_pub), {"commitment": "confirmed"}])
        )["value"]
        log.info(
            f"Sender SOL: {sol_balance / 1e9:.6f} | est cost: {est_fee / 1e9:.6f} "
            f"({num_txs} txs, {len(to_create)} ATA rents)"
        )
        if sol_balance < int(est_fee * 1.1):
            raise SystemExit("Sender SOL too low for ATA rent + fees.")

        # Send batches
        for batch_idx in range(0, len(to_create), BATCH_SIZE):
            batch = to_create[batch_idx : batch_idx + BATCH_SIZE]
            ixs = [
                set_compute_unit_limit(COMPUTE_UNIT_LIMIT),
                set_compute_unit_price(PRIORITY_FEE_MICROLAMPORTS),
            ]
            for w in batch:
                ixs.append(
                    create_idempotent_associated_token_account(
                        payer=sender_pub,
                        owner=w["owner_pub"],
                        mint=new_mint_pub,
                        token_program_id=token_program,
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

            label = (
                f"batch {batch_idx // BATCH_SIZE + 1}/{num_txs} "
                f"({len(batch)} ATAs)"
            )

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
                    state["created"][w["owner"]] = {
                        "ata": str(w["ata"]),
                        "sig": sig,
                        "ts": ts,
                    }
                save_state(STATE_FILE, state)
            except Exception as e:
                log.exception(f"{label} failed: {e}. State preserved; rerun to retry.")
                continue

    log.info(f"Done. Total owners with ATAs tracked: {len(state['created'])}")


if __name__ == "__main__":
    asyncio.run(main())
