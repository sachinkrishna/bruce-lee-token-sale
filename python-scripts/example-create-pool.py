"""
example-create-pool.py
======================
Creates a new pool and funds its vault.

Set environment variables before running:

    export ADMIN_B58="<your-admin-private-key-base58>"
    export AMOUNT_SOL="1"                              # SOL to deposit (default: 1)
    export RPC_URL="https://api.devnet.solana.com"     # optional

Then run:
    python3 example-create-pool.py
"""

import os
from sdk import GlobalPoolFactory, run
import dotenv

dotenv.load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

ADMIN_B58 = os.environ.get("ADMIN_B58", "")
AMOUNT_SOL = float(os.environ.get("AMOUNT_SOL", "1"))
RPC_URL = os.environ.get("RPC_URL", "https://api.devnet.solana.com")

if not ADMIN_B58:
    raise SystemExit(
        "Error: ADMIN_B58 environment variable is required.\n"
        "  export ADMIN_B58='<your-private-key-base58>'"
    )

# ── Run ───────────────────────────────────────────────────────────────────────

sdk = GlobalPoolFactory(rpc_url=RPC_URL)

# 1. Read config BEFORE creating — pool_count is the new pool's ID
print("Fetching current state...")
state = run(sdk.fetch_state())
cfg = state["config"]
new_pid = cfg["pool_count"]  # new pool will get this ID
before = new_pid

print(f"Config PDA   : {cfg['pda']}")
print(f"Pool count   : {before}")
print(f"New pool ID  : {new_pid}")
print(f"Funding with : {AMOUNT_SOL} SOL\n")

# 2. Compute PDAs deterministically — no RPC needed
pool_pda = sdk.pool_pda(new_pid)
vault_pda = sdk.pool_vault_pda(new_pid)

print(f"Pool PDA  : {pool_pda}")
print(f"Vault PDA : {vault_pda}\n")

# 3. Create the pool
sig = run(
    sdk.create_pool(
        admin_b58=ADMIN_B58,
        amount_sol=AMOUNT_SOL,
    )
)

# 4. Print results (PDAs are known without re-fetching)
print(f"✔ Pool created!")
print(f"  Pool ID   : {new_pid}")
print(f"  Pool PDA  : {pool_pda}")
print(f"  Vault PDA : {vault_pda}")
print(f"  Vault SOL : {AMOUNT_SOL} SOL")
print(f"  Tx        : https://solscan.io/tx/{sig}?cluster=devnet")
print()
print("Use POOL_ADDRESS below for write-allocation / claim commands:")
print(f"  POOL_ADDRESS={pool_pda}")
