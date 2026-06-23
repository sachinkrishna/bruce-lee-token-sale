"""
example.py — GlobalPoolFactory SDK usage examples
==================================================
Copy this file, replace the placeholder keys/addresses, and run:

    cd python-scripts
    pip install -r requirements.txt
    python example.py
"""

from sdk import GlobalPoolFactory, run

# ── Configure SDK ──────────────────────────────────────────────────────────────

sdk = GlobalPoolFactory(
    rpc_url="https://api.devnet.solana.com",
    program_id="BnHBsdQddqBHtf72HcgirUBrwAyaJjrDbdibjydaUne7",
    # idl_path = "/custom/path/to/global_pool_factory.json",       # optional override
)

# ── Keys & addresses (replace with real values) ────────────────────────────────

ADMIN_B58 = "35auUKFAr1SBWPo5guMsNHC7hJ9Z9sEramzK8TdNoXfCLVnEPBoW9sweepn2yDtVYwVpvJFpQUP5mkqYsSQKfmEY"
USER_B58 = "YOUR_USER_PRIVATE_KEY_BASE58"
AUTHORITY_B58 = "YOUR_CLAIM_CLOSE_AUTHORITY_PRIVATE_KEY_BASE58"

POOL_ADDRESS = "4wAUVcUMegm3Lf5c7MWTk3bewX7XDRZ32aWqDL1ExYAH"  # from create-pool
USER_PUBKEY = "28ErVkiZ5Jogd16t2cUs8qRFX27ouStLHVGRpxS9ci6t"
TREASURY_PUBKEY = "FtGWZnqKwf5p6vEcWPE8BkiC8KChNqWDwdthihCW5xsr"


# ══════════════════════════════════════════════════════════════════════════════
# 1. Fetch on-chain state (no private key needed)
# ══════════════════════════════════════════════════════════════════════════════

print("── Fetch State ──")
state = run(sdk.fetch_state())

cfg = state["config"]
print(f"Admin:                 {cfg['admin']}")
print(f"Treasury:              {cfg['treasury']}")
print(f"Sync authority:        {cfg['sync_authority']}")
print(f"Claim close authority: {cfg['claim_close_authority']}")
print(
    f"Random fee range:      {cfg['random_fee_min_sol']} – {cfg['random_fee_max_sol']} SOL"
)
print(f"Pool count:            {cfg['pool_count']}")

for pool in state["pools"]:
    print(f"\nPool {pool['pool_id']} ({pool['pda']})")
    print(f"  Vault:          {pool['vault_sol']} SOL")
    print(f"  Total allocated:{pool['total_allocated_sol']} SOL")
    print(f"  Total claimed:  {pool['total_claimed_sol']} SOL")
    print(f"  Allocations:    {pool['allocation_count']}")
    print(f"  Frozen:         {pool['is_frozen']}")

# ── Inspect a specific pool only ──────────────────────────────────────────────

# state = run(sdk.fetch_state(pool_address=POOL_ADDRESS))
# state = run(sdk.fetch_state(pool_id=0))


# ══════════════════════════════════════════════════════════════════════════════
# 2. Create a pool and fund it with 1 SOL
# ══════════════════════════════════════════════════════════════════════════════

print("\n── Create Pool ──")
sig = run(
    sdk.create_pool(
        admin_b58=ADMIN_B58,
        amount_sol=1.0,
    )
)
print(f"Pool created: https://solscan.io/tx/{sig}?cluster=devnet")


# ══════════════════════════════════════════════════════════════════════════════
# 3. Freeze a pool
# ══════════════════════════════════════════════════════════════════════════════

print("\n── Freeze Pool ──")
sig = run(
    sdk.freeze_pool(
        admin_b58=ADMIN_B58,
        pool_address=POOL_ADDRESS,  # or: pool_id=0
    )
)
print(f"Pool frozen: https://solscan.io/tx/{sig}?cluster=devnet")


# ══════════════════════════════════════════════════════════════════════════════
# 4. Unfreeze a pool
# ══════════════════════════════════════════════════════════════════════════════

print("\n── Unfreeze Pool ──")
sig = run(
    sdk.unfreeze_pool(
        admin_b58=ADMIN_B58,
        pool_address=POOL_ADDRESS,
    )
)
print(f"Pool unfrozen: https://solscan.io/tx/{sig}?cluster=devnet")


# ══════════════════════════════════════════════════════════════════════════════
# 5. Emergency withdraw all SOL from pool vault
# ══════════════════════════════════════════════════════════════════════════════

print("\n── Emergency Withdraw ──")
sig = run(
    sdk.emergency_withdraw(
        admin_b58=ADMIN_B58,
        target_wallet=TREASURY_PUBKEY,
        pool_address=POOL_ADDRESS,
    )
)
print(f"Vault drained: https://solscan.io/tx/{sig}?cluster=devnet")


# ══════════════════════════════════════════════════════════════════════════════
# 6. Write an allocation for a user
# ══════════════════════════════════════════════════════════════════════════════

print("\n── Write Allocation ──")
sig = run(
    sdk.write_allocation(
        admin_b58=ADMIN_B58,
        target_user=USER_PUBKEY,
        amount_sol=0.1,
        pool_address=POOL_ADDRESS,
    )
)
print(f"Allocation written: https://solscan.io/tx/{sig}?cluster=devnet")


# ══════════════════════════════════════════════════════════════════════════════
# 7. User claims their allocation
# ══════════════════════════════════════════════════════════════════════════════

print("\n── Claim ──")
sig = run(
    sdk.claim(
        user_b58=USER_B58,
        pool_address=POOL_ADDRESS,
    )
)
print(f"Claimed: https://solscan.io/tx/{sig}?cluster=devnet")

# After claiming, a ClaimRecord PDA holds the random fee (0.005–0.006 SOL + rent).
record_pda = sdk.claim_record_pda(pool_id=0, user=USER_PUBKEY)
print(f"ClaimRecord PDA: {record_pda}")


# ══════════════════════════════════════════════════════════════════════════════
# 8. Admin closes a single UserAllocation PDA (recover rent)
# ══════════════════════════════════════════════════════════════════════════════

print("\n── Admin Close Single Allocation ──")
sig = run(
    sdk.admin_close_allocation(
        admin_b58=ADMIN_B58,
        target_user=USER_PUBKEY,
        target_wallet=TREASURY_PUBKEY,
        pool_address=POOL_ADDRESS,
    )
)
print(f"Allocation closed: https://solscan.io/tx/{sig}?cluster=devnet")


# ══════════════════════════════════════════════════════════════════════════════
# 9. Admin batch-closes ALL allocation PDAs for a pool
# ══════════════════════════════════════════════════════════════════════════════

print("\n── Admin Batch Close All Allocations ──")
sigs = run(
    sdk.admin_batch_close(
        admin_b58=ADMIN_B58,
        target_wallet=TREASURY_PUBKEY,
        pool_address=POOL_ADDRESS,
        batch_size=20,
    )
)
print(f"Closed {len(sigs)} batch(es).")
for s in sigs:
    print(f"  https://solscan.io/tx/{s}?cluster=devnet")


# ══════════════════════════════════════════════════════════════════════════════
# 10. Close a ClaimRecord PDA and sweep fee to any wallet
# ══════════════════════════════════════════════════════════════════════════════

print("\n── Close Claim Record ──")
sig = run(
    sdk.close_claim_record(
        authority_b58=AUTHORITY_B58,  # admin OR claim_close_authority key
        claim_record_user=USER_PUBKEY,
        target_wallet=TREASURY_PUBKEY,  # any wallet
        pool_address=POOL_ADDRESS,
    )
)
print(f"ClaimRecord closed: https://solscan.io/tx/{sig}?cluster=devnet")


# ══════════════════════════════════════════════════════════════════════════════
# PDA address helpers (no RPC call needed)
# ══════════════════════════════════════════════════════════════════════════════

print("\n── PDA Addresses ──")
print("Config PDA:         ", sdk.config_pda())
print("Pool PDA (id=0):    ", sdk.pool_pda(0))
print("Pool Vault (id=0):  ", sdk.pool_vault_pda(0))
print("UserAllocation PDA: ", sdk.user_allocation_pda(0, USER_PUBKEY))
print("ClaimRecord PDA:    ", sdk.claim_record_pda(0, USER_PUBKEY))
