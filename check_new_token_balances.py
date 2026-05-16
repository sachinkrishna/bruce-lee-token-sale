"""For every `owner` in a snapshot CSV, look up every token account that
wallet owns for `NEW_TOKEN_MINT` (ATA or otherwise) and sum the balances.

Uses `getTokenAccountsByOwner` with a mint filter — the correct way to ask
"does this wallet hold any of this token, anywhere?". One RPC call per
wallet, run with bounded concurrency.

Output (`new_token_balances.csv`) columns:
    owner,num_token_accounts,amount_raw,amount_ui

Run:
    python check_new_token_balances.py
"""

from __future__ import annotations

import asyncio
import csv
import logging
from pathlib import Path

import httpx

# --- CONFIGURATION ---
HELIUS_API_KEY = "86c830da-67f9-4c06-9d98-ccdcb6b8393a"
RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

INPUT_CSV = "snapshot_Bg64WKiN_20260508_143936.csv"
NEW_TOKEN_MINT = "96egraTCizRpzNx4WvMMhPJf7TKS7W5kUHeenmd1XfuN"                  # mint pubkey of the token to check
IS_TOKEN_2022 = False                # True if NEW_TOKEN_MINT is a Token-2022 mint

OUT_CSV = "new_token_balances.csv"
ONLY_HOLDERS = False                 # True = write rows with amount_raw > 0 only
CONCURRENCY = 20                     # parallel RPC calls
# ---------------------

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("check_balances")


def load_owners(path: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if "owner" not in (reader.fieldnames or []):
            raise SystemExit(f"{path} has no 'owner' column.")
        for row in reader:
            o = (row.get("owner") or "").strip()
            if o and o not in seen:
                seen.add(o)
                out.append(o)
    return out


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


async def get_decimals(client: httpx.AsyncClient, mint: str) -> int:
    res = await rpc(client, "getTokenSupply", [mint])
    return int(res["value"]["decimals"])


async def get_owner_balance(
    client: httpx.AsyncClient,
    owner: str,
    mint: str,
    sem: asyncio.Semaphore,
    progress: dict,
    total: int,
) -> tuple[int, int]:
    """Return (num_token_accounts, total_amount_raw) for `owner` for `mint`."""
    async with sem:
        try:
            res = await rpc(
                client,
                "getTokenAccountsByOwner",
                [
                    owner,
                    {"mint": mint},
                    {"encoding": "jsonParsed", "commitment": "confirmed"},
                ],
            )
        except Exception as e:
            log.warning(f"RPC failed for {owner}: {e}")
            res = {"value": []}

    accounts = (res or {}).get("value") or []
    total_raw = 0
    for acc in accounts:
        try:
            amount = int(
                acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]
            )
        except (KeyError, TypeError, ValueError):
            amount = 0
        total_raw += amount

    progress["done"] += 1
    if progress["done"] % 50 == 0 or progress["done"] == total:
        log.info(f"Checked {progress['done']}/{total}")

    return len(accounts), total_raw


async def main():
    if not NEW_TOKEN_MINT:
        raise SystemExit("Set NEW_TOKEN_MINT at the top of the script.")
    if not Path(INPUT_CSV).exists():
        raise SystemExit(f"{INPUT_CSV} not found.")

    owners = load_owners(INPUT_CSV)
    log.info(f"Loaded {len(owners)} unique owners from {INPUT_CSV}")

    rows: list[dict] = []

    async with httpx.AsyncClient(timeout=60.0) as client:
        decimals = await get_decimals(client, NEW_TOKEN_MINT)
        scale = 10 ** decimals
        log.info(f"Mint {NEW_TOKEN_MINT} | decimals={decimals}")

        sem = asyncio.Semaphore(CONCURRENCY)
        progress = {"done": 0}
        results = await asyncio.gather(
            *[
                get_owner_balance(client, o, NEW_TOKEN_MINT, sem, progress, len(owners))
                for o in owners
            ]
        )

    for owner, (n_accounts, raw) in zip(owners, results):
        rows.append(
            {
                "owner": owner,
                "num_token_accounts": n_accounts,
                "amount_raw": raw,
                "amount_ui": raw / scale,
            }
        )

    if ONLY_HOLDERS:
        rows = [r for r in rows if r["amount_raw"] > 0]

    rows.sort(key=lambda r: r["amount_raw"], reverse=True)

    with open(OUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["owner", "num_token_accounts", "amount_raw", "amount_ui"],
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    holders = sum(1 for r in rows if r["amount_raw"] > 0)
    no_account = sum(1 for r in rows if r["num_token_accounts"] == 0)
    total_ui = sum(r["amount_ui"] for r in rows)

    print()
    print(f"Wallets checked:          {len(rows)}")
    print(f"With at least 1 account:  {len(rows) - no_account}")
    print(f"Without any account:      {no_account}")
    print(f"With positive balance:    {holders}")
    print(f"Total balance (UI):       {total_ui}")
    print(f"Wrote {OUT_CSV}")


if __name__ == "__main__":
    asyncio.run(main())
