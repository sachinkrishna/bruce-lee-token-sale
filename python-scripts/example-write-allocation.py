"""
example-write-allocation.py
============================
Writes a SOL allocation for a user inside a pool.
The funds move from the pool vault → user's allocation PDA.

Set environment variables before running:

    export ADMIN_B58="<your-admin-private-key-base58>"
    export TARGET_USER="<user-wallet-pubkey>"
    export AMOUNT_SOL="0.1"
    export POOL_ADDRESS="<pool-pda-address>"   # from create-pool output
      OR
    export POOL_ID="0"                          # numeric pool ID (default: 0)
    export RPC_URL="https://api.devnet.solana.com"   # optional

Then run:
    python3 example-write-allocation.py
"""

import os
from sdk import GlobalPoolFactory, run
import dotenv

dotenv.load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

ADMIN_B58 = os.environ.get("ADMIN_B58", "")
TARGET_USER = "FtGWZnqKwf5p6vEcWPE8BkiC8KChNqWDwdthihCW5xsr"
AMOUNT_SOL = float(0.0001)
POOL_ADDRESS = "Hi1NjjQ6vg8ug4KNB7rLex4GBDJUTTEcRAw6rCdYi3s9"
POOL_ID = int(os.environ["POOL_ID"]) if os.environ.get("POOL_ID") else None
RPC_URL = os.environ.get("RPC_URL", "https://api.devnet.solana.com")

if not ADMIN_B58:
    raise SystemExit("Error: ADMIN_B58 environment variable is required.")
if not TARGET_USER:
    raise SystemExit(
        "Error: TARGET_USER environment variable is required.\n"
        "  export TARGET_USER='<user-wallet-pubkey>'"
    )
if not POOL_ADDRESS and POOL_ID is None:
    print("No POOL_ADDRESS or POOL_ID set — defaulting to pool ID 0.")

# ── Run ───────────────────────────────────────────────────────────────────────

sdk = GlobalPoolFactory(rpc_url=RPC_URL)

# Show vault balance before writing
state = run(sdk.fetch_state(pool_address=POOL_ADDRESS, pool_id=POOL_ID))
if state["pools"]:
    pool = state["pools"][0]
    pid = pool["pool_id"]
    print(f"Pool ID   : {pid}")
    print(f"Pool PDA  : {pool['pda']}")
    print(f"Vault SOL : {pool['vault_sol']} SOL (before)")
    print(f"Allocated : {pool['total_allocated_sol']} SOL (before)\n")
else:
    pid = POOL_ID or 0
    print(f"Warning: pool {pid} not found on chain.")

# Derive the allocation PDA address for display
alloc_pda = sdk.user_allocation_pda(pid, TARGET_USER)
print(f"Writing allocation:")
print(f"  User           : {TARGET_USER}")
print(f"  Amount         : {AMOUNT_SOL} SOL")
print(f"  Allocation PDA : {alloc_pda}\n")

sig = run(
    sdk.write_allocation(
        admin_b58=ADMIN_B58,
        target_user=TARGET_USER,
        amount_sol=AMOUNT_SOL,
        pool_address=POOL_ADDRESS,
        pool_id=POOL_ID,
    )
)

print(f"✔ Allocation written!")
print(f"  Tx  : https://solscan.io/tx/{sig}?cluster=devnet")

# Show updated vault balance
state = run(sdk.fetch_state(pool_address=POOL_ADDRESS, pool_id=POOL_ID))
if state["pools"]:
    pool = state["pools"][0]
    print(f"  Vault SOL after : {pool['vault_sol']} SOL")
    print(f"  Allocated after : {pool['total_allocated_sol']} SOL")
