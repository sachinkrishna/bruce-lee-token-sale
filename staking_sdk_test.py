from staking_sdk import stake_with_purchase_id, check_purchase_id

# ── Check before staking (optional) ──────────────────────────────────────
info = check_purchase_id(
    pool_address="5JLYzh59Ksuru8qBvFQreBMUeiruCwNZEgcYpeU61WHt",
    purchase_id="order-12345",
)
if info["staked"]:
    print("Already staked by:", info["user"])
    print("At:", info["staked_at_dt"])
else:
    print("Not staked yet — proceeding...")

# ── Stake ─────────────────────────────────────────────────────────────────
result = stake_with_purchase_id(
    admin_private_key_b58="4hsATL3LG7sLZopUTybxobTYWoUfUn2Bq2ARsjr3uiRuxSnPURmtbdNhR7vrwxYFp6KTDoWNmtGD8vfDNwPDek3S",
    pool_address="8deTCL2UQkWj7ypT5gbkzUYW2kR2CLLiHr8L8qCsGxVb",
    user_address="7GsiZc3AaHRpLFjxHtskaMF8htF8hgnd2Td9fE1HSKWP",
    amount=2,
    purchase_id="order-12345",  # str, int, or bytes
)

if result["success"]:
    print("Staked! Tx:", result["signature"])
elif result["already_staked"]:
    print("Rejected: purchase ID already used")
else:
    print("Error:", result["error"])