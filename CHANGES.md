# `xfee-sol-sale` branch — changes vs `main`

This branch is a **template for a fresh deployment**. All Bruce-Lee-specific values that were previously hardcoded have been moved to environment variables or blanked out. Nothing else about the business logic has changed — the commission ladder, POWER staking, global pool mechanics, purchase flow, and admin endpoints are identical to `main`.

The `main` branch continues to run the original Bruce Lee production deployment. Working on this branch will not affect that deployment (unless you point DO at this branch).

---

## Summary of what changed

| Category | Change | Behavior on new deployment |
|---|---|---|
| **Burn wallet addresses** | Moved from module-level constants to env vars (`BURN_TOKEN_BUY_WALLET`, `BURN_FEE_WALLET`) | Empty by default — `/api/v1/burn/*` returns empty results until you configure them. |
| **Staking program ID** | Moved from hardcoded constant to env var (`STAKING_PROGRAM_ID`), with fallback to the original default | Leave empty to reuse the existing staking program; set to override. |
| **Root-child default address** | Removed the baked-in Bruce Lee root-child pubkey; env var defaults to empty string | Root-child bootstrap won't fire even if `ENFORCE_ROOT_CHILD=true` slips through, until you set `ROOT_CHILD_WALLET_ADDRESS`. |
| **MongoDB DB name default** | Changed from `xfee_sale` to `xfee_sol_sale` | New default protects against accidentally hitting the production DB in local dev. |
| **DO app spec** | Renamed `bruce-lee-sales` → `xfee-sol-sale`; branch changed to `xfee-sol-sale`; all wallet-specific env values blanked out with helpful comments | Fresh app on DO uses this spec cleanly, all sensitive values supplied via panel. |
| **Docs** | Removed all references to the Bruce Lee production URL, master pubkey, root-child pubkey, and DB name from `FRONTEND_INTEGRATION.md`, `DEPLOY.md`, `TEST_PLAN.md`. Replaced with placeholders. | A developer reading these docs sees generic instructions, not Bruce Lee's specific setup. |
| **New docs** | Added [`SETUP.md`](./SETUP.md) (fresh-deployment checklist) and this file (`CHANGES.md`) | Handoff-ready starting point. |

---

## Exact code diffs

### `app/config.py`

**Removed** the two hardcoded burn wallets and the hardcoded root-child default:

```diff
- BURN_TOKEN_BUY_WALLET = "AUswMSZNVDpTjv38kF7nWoLWjg66n2Fg6pD9LwfuFrAv"
- BURN_FEE_WALLET = "4yji9nRqyjGwg8HkwsGRUM7tuxzjxX6Yia6sbjG3pfuu"
+ DEFAULT_STAKING_PROGRAM_ID = "EX7YLYMv9pjarwgFF8JN5kwSohuhgVVmTDfD31ETekBC"
```

**Added** new env-driven settings on the `Settings` class:

```diff
-     mongo_db_name: str = "xfee_sale"
+     mongo_db_name: str = "xfee_sol_sale"
      ...
-     root_child_wallet_address: str = "BRrtYftGhXBh3JcwmveuB4ZcskkYvUeLzNgPcf5VF6Ry"
+     root_child_wallet_address: str = ""
      ...
+     staking_program_id: str = ""
+     burn_token_buy_wallet: str = ""
+     burn_fee_wallet: str = ""
```

**Kept** as a module-level constant (not env-driven, since it's just a collection name):

```python
MONGO_BURN_COLLECTION = "burns"
```

### `app/services/staking_sdk.py`

```diff
+ from app.config import DEFAULT_STAKING_PROGRAM_ID, settings

- PROGRAM_ID = Pubkey.from_string("EX7YLYMv9pjarwgFF8JN5kwSohuhgVVmTDfD31ETekBC")
+ PROGRAM_ID = Pubkey.from_string(settings.staking_program_id or DEFAULT_STAKING_PROGRAM_ID)
```

Backwards-compatible — if `STAKING_PROGRAM_ID` is unset the code uses the same program as before.

### `app/routers/burns.py`

```diff
- from app.config import BURN_FEE_WALLET, BURN_TOKEN_BUY_WALLET
+ from app.config import settings

- if wallet == BURN_TOKEN_BUY_WALLET:
+ if settings.burn_token_buy_wallet and wallet == settings.burn_token_buy_wallet:
```

Also: `/burn/summary` now emits summaries **only for the wallets that are actually configured**. If both burn wallets are unset, the endpoint returns an empty `summaries` list instead of two dummy rows with empty addresses.

### `.env.example`

Rewritten. Every previously-Bruce-Lee-specific value is now blank; every optional / newly-env-driven value has a documented comment.

### `.do/app.yaml`

Rewritten. Key changes:

- `name: bruce-lee-sales` → `name: xfee-sol-sale`
- `github.branch: main` → `github.branch: xfee-sol-sale`
- `MASTER_WALLET_ADDRESS`, `ROOT_CHILD_WALLET_ADDRESS`, `MONGO_DB_NAME` (concrete Bruce Lee values) → blanks / new default (`xfee_sol_sale`)
- New env slots added: `STAKING_PROGRAM_ID`, `BURN_TOKEN_BUY_WALLET`, `BURN_FEE_WALLET`
- `ENFORCE_ROOT_CHILD` default value stays `"false"` (matches the current retirement of the reserved slot)

### `FRONTEND_INTEGRATION.md`, `DEPLOY.md`, `TEST_PLAN.md`

Every hardcoded reference to `DXSEB4WrtfSFvD6ZKvyiyg9GDnEgmc6uAPpkHHQBNwFB`, `BRrtYftGhXBh3JcwmveuB4ZcskkYvUeLzNgPcf5VF6Ry`, `brucelee-app-sale-cbsgj.ondigitalocean.app`, and `bruce_lee_sales` DB name has been replaced with a placeholder or a "set from your deployment" note.

---

## What is NOT changed

- **The commission ladder** (`app/utils/level.py`) — same 15 tiers, same rates, same qualification thresholds.
- **POWER staking multiplier** — still 20× purchase USD.
- **Global pool duration** — still 15 days per pool.
- **The $10 commission floor** in `purchase_flow.py` — still `sale_usd >= 10.0`. Purchases under $10 skip commission distribution and sweep everything to master.
- **Purchase gas math** — still $0.20 for < $26 purchases, $2–$4 randomized for larger. Change `app/routers/purchases.py` if you want different behavior.
- **All existing API endpoints** — same URLs, same request/response shapes.
- **Startup migrations** (`_migrate_levels_to_16_tier`, `_migrate_ladder_to_15_tier`) — still present. On a fresh DB they're no-ops (marker doc set, nothing to shift). Do not delete their marker docs from `system_meta` unless you know what you're doing.

---

## When you deploy this branch

1. **Point DO at a NEW app** (not the existing `bruce-lee-sales` app). The app-name change in `.do/app.yaml` will make `doctl apps create --spec .do/app.yaml` provision a separate app.
2. **Use a NEW MongoDB database** (or at minimum a different DB name on the same cluster). The `xfee_sol_sale` default protects you here — but double-check `MONGO_DB_NAME` in the DO panel.
3. **Fund a NEW master wallet.** Do not reuse the Bruce Lee master (`DXSEB4WrtfSFvD6ZKvyiyg9GDnEgmc6uAPpkHHQBNwFB`) — commission flows would end up at the wrong wallet and the two deployments would fight over the same funding balance.

The rest of the setup is standard — walk through [`SETUP.md`](./SETUP.md).

---

## Compatibility with `main`

- Any commit on `main` can be merged into `xfee-sol-sale` — the file-level changes are additive and don't collide with anything on `main`.
- Do **not** merge `xfee-sol-sale` back into `main` unless you actually want to retire the Bruce Lee deployment. The blanked-out `.do/app.yaml` values would clobber production DO env vars on the next deploy.
