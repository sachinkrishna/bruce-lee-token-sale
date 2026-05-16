"""Snapshot all on-chain holders of an SPL token and write to CSV.

Uses Helius DAS `getTokenAccounts` (paginated, cursor-based) to enumerate every
token account for a given mint, aggregates per owner wallet, and writes
`holders.csv` sorted by balance descending.

Output CSV columns:
    owner,amount_raw,amount_ui,num_token_accounts,frozen_any

Run:
    python holder_snapshot.py
"""

import asyncio
import csv
import sys
from collections import defaultdict

import httpx

# --- CONFIGURATION ---
HELIUS_API_KEY = "86c830da-67f9-4c06-9d98-ccdcb6b8393a"
TOKEN_MINT = "12SX7uuQvfXTFDW9jTykQs23Z1jmPvaCkzHgXAqNxfUN"  # SPL mint pubkey (e.g. the XFEE mint)
PAGE_LIMIT = 1000
OUT_CSV = "holders.csv"
INCLUDE_ZERO = False  # include accounts with 0 raw balance
# ---------------------

RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"


async def rpc_call(client: httpx.AsyncClient, method: str, params):
    payload = {"jsonrpc": "2.0", "id": method, "method": method, "params": params}
    resp = await client.post(RPC_URL, json=payload, timeout=60.0)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"{method} error: {data['error']}")
    return data["result"]


async def get_decimals(client: httpx.AsyncClient, mint: str) -> int:
    res = await rpc_call(client, "getTokenSupply", [mint])
    return int(res["value"]["decimals"])


async def fetch_all_token_accounts(client: httpx.AsyncClient, mint: str):
    cursor = None
    page = 0
    while True:
        page += 1
        params = {"mint": mint, "limit": PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor

        result = await rpc_call(client, "getTokenAccounts", params)
        accounts = (result or {}).get("token_accounts", []) or []
        if not accounts:
            break

        print(f"Page {page}: fetched {len(accounts)} token accounts")
        for acc in accounts:
            yield acc

        cursor = result.get("cursor")
        if not cursor:
            break


async def main():
    if not TOKEN_MINT:
        print("ERROR: set TOKEN_MINT at the top of the script.")
        sys.exit(1)
    if not HELIUS_API_KEY:
        print("ERROR: HELIUS_API_KEY is empty.")
        sys.exit(1)

    async with httpx.AsyncClient() as client:
        decimals = await get_decimals(client, TOKEN_MINT)
        scale = 10 ** decimals
        print(f"Mint {TOKEN_MINT} | decimals={decimals}")

        owners: dict[str, dict] = defaultdict(
            lambda: {"raw": 0, "accounts": [], "frozen_any": False}
        )

        async for acc in fetch_all_token_accounts(client, TOKEN_MINT):
            owner = acc.get("owner")
            if not owner:
                continue
            raw = int(acc.get("amount", 0) or 0)
            if raw == 0 and not INCLUDE_ZERO:
                continue
            entry = owners[owner]
            entry["raw"] += raw
            entry["accounts"].append(acc.get("address"))
            if acc.get("frozen"):
                entry["frozen_any"] = True

    rows = [
        {
            "owner": owner,
            "amount_raw": e["raw"],
            "amount_ui": e["raw"] / scale,
            "num_token_accounts": len(e["accounts"]),
            "frozen_any": e["frozen_any"],
        }
        for owner, e in owners.items()
    ]
    rows.sort(key=lambda r: r["amount_raw"], reverse=True)

    with open(OUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["owner", "amount_raw", "amount_ui", "num_token_accounts", "frozen_any"],
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    total_ui = sum(r["amount_ui"] for r in rows)
    print(f"\nUnique holders: {len(rows)}")
    print(f"Total UI balance: {total_ui}")
    print(f"Wrote {OUT_CSV}")


if __name__ == "__main__":
    asyncio.run(main())
