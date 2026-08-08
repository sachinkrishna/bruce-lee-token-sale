# XFEE SOL Sale — New Deployment Setup

End-to-end checklist for standing up a fresh instance of this system on a **new Solana wallet set, new MongoDB, new DigitalOcean app**. Follow the steps in order — each one is required.

For a summary of what changed in this branch vs. the original Bruce Lee deployment, see [`CHANGES.md`](./CHANGES.md).

For day-2 operations (redeploys, rollback, scaling), see [`DEPLOY.md`](./DEPLOY.md).

For frontend integration, see [`FRONTEND_INTEGRATION.md`](./FRONTEND_INTEGRATION.md).

---

## 0. Overview

The system is a Solana-based token pre-sale with:

- 15-tier cumulative-differential commission ladder (rates in `app/utils/level.py`).
- POWER token staking, 20× purchase amount, on-chain via a staking program.
- 15-day "global pool" that reimburses users skipped by the commission cascade, settled directly from a funding wallet (no on-chain contract).
- FastAPI + MongoDB (Motor) + async Solana RPC.
- Deployed as a Docker service on DigitalOcean App Platform.

Nothing about the code assumes a specific wallet, mint, or DB. Every deployment-specific value is env-driven — see the "Environment variables" section below.

---

## 1. Prepare Solana assets

You need the following on Solana mainnet:

### 1.1 Master wallet

Top of the referral tree. Level 15 (100% rate). Receives the commission "sweep" (undistributed portion of every purchase's commissionable amount) and funds the global-pool payouts.

- Generate a fresh keypair. Do not reuse the Bruce Lee master wallet.
- Save the **base58 64-byte secret key** somewhere secure (1Password, GCP Secret Manager, etc.). This is `MASTER_WALLET_PRIVATE_KEY`.
- Pre-fund with SOL:
  - For daily operational headroom (commission + stake tx fees + purchase-wallet rent): **≥ 0.5 SOL**.
  - Plus whatever SOL you want to seed the first global pool with.

### 1.2 Treasury wallet

Holds the entire XFEE token supply. Dispatches XFEE to buyers on each purchase.

> **Note:** The current code path stakes POWER to buyers instead of transferring an SPL token. The treasury wallet is still validated at startup (its XFEE balance is logged), so create the ATA and mint your XFEE supply into it even if you don't intend to send SPL tokens per-purchase.

- Generate a fresh keypair.
- Create the associated token account (ATA) for the XFEE mint.
- Mint the full XFEE supply into the treasury ATA (or transfer it there from the mint authority).

### 1.3 XFEE token mint

The SPL token that represents "one purchase unit".

- Create a new SPL mint (0 decimals is fine — 1 XFEE = $1 USD is the fixed pricing).
- Set mint authority to whatever you want (can be revoked after minting the supply).
- Record the mint address — this is `XFEE_TOKEN_MINT`.

### 1.4 POWER staking pool

The on-chain pool that will hold staked POWER.

- **Option A (recommended):** reuse the deployed staking program `EX7YLYMv9pjarwgFF8JN5kwSohuhgVVmTDfD31ETekBC`. Create a new pool under it — that gives you a new `POOL_ADDRESS`. Leave `STAKING_PROGRAM_ID` empty.
- **Option B:** deploy your own copy of the staking program to a new program ID. Set `STAKING_PROGRAM_ID` to that new program ID and set `POOL_ADDRESS` to your new pool PDA.

The keypair that created the pool is the pool authority. Set `POOL_AUTHORITY_PRIVATE_KEY` to its base58 secret. When left empty, the code falls back to `MASTER_WALLET_PRIVATE_KEY` (works only if the pool was created by master).

### 1.5 Global-pool funding wallet

The wallet that pays global-pool users. In practice this is **the same wallet as master** (the master already receives every purchase's sweep).

- Set `GLOBAL_POOL_FUNDING_WALLET_PRIVATE_KEY = MASTER_WALLET_PRIVATE_KEY`.
- If you want a separate funding wallet, generate a different keypair and set this to its secret.

### 1.6 (Optional) Root-child wallet

Only needed when `ENFORCE_ROOT_CHILD=true`. The default is `false` and this reserved-slot behavior is retired for the new deployment; skip this unless you specifically want it back.

### 1.7 (Optional) Set-user-level signer wallet

If you want manual level upgrades (`POST /api/v1/admin/set-user-level`) to require a signature from a wallet **other than master** (e.g. an ops-only wallet), set `SET_USER_LEVEL_SIGNER_WALLET` to that pubkey. Otherwise leave empty and master signs.

Header-based auth (`X-Admin-Key`) always works regardless of this setting.

---

## 2. Prepare MongoDB

- Create a fresh MongoDB Atlas cluster (M0 free tier is enough for launch).
- Create a database user and get the connection string.
- Whitelist `0.0.0.0/0` initially (narrow to DO's egress IPs after first deploy).
- Choose a database name. The branch default is `xfee_sol_sale`. If you want a different name, set `MONGO_DB_NAME` accordingly.

> **Do not** reuse the Bruce Lee production database. Every collection (`users`, `purchases`, `allocs`, `global_pools`, `pool_points`, `relationship_tree`, `purchase_wallets`, `transactions`, `system_meta`, `burns`) starts empty and is populated at runtime.

The app creates all required indexes on first startup — no manual migration needed.

---

## 3. Prepare Solana RPC

Free public RPC endpoints will rate-limit you within minutes. Get a premium endpoint:

- **QuickNode** (recommended): create a Solana mainnet endpoint and copy the URL. Save as `QUICKNODE_RPC_URL`. Despite the name of the env var, any Solana-JSON-RPC-compatible endpoint works — Helius and Triton URLs go in the same slot.
- **Rate limits**: The app uses this endpoint for every commission transfer, every stake call, every balance read, every settlement transfer, and every poller tick. Size accordingly.

---

## 4. Prepare SOL price feed

The default is CoinGecko's free endpoint (`https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd`), rate-limited to ~10-30 req/min. The app caches for a short window. For a busy sale, get a paid key or replace with a different oracle.

The `SOL_PRICE_API_URL` env var takes the full URL; the app expects the response to include a numeric price accessible via a JSON path the code understands (see `app/services/sol_price.py`).

---

## 5. Fork the DO app spec

The DO app spec is in `.do/app.yaml`. **Before deploying, edit it:**

```yaml
name: xfee-sol-sale                # change to your app name
region: nyc                        # or your preferred region

services:
  - name: api
    github:
      repo: <your-github-username>/<your-repo>
      branch: xfee-sol-sale        # or whichever branch you deploy from
```

All environment values in the spec are placeholders (`value: ""`). The concrete production values go into the DO panel — not into git.

---

## 6. Environment variables

Every env var, grouped by purpose. Set these in **DO Dashboard → your app → Settings → App-Level Environment Variables** (or via `doctl`, but the dashboard is the safer path for secrets).

Values that need a Solana wallet address should be **base58 pubkey** (32 bytes → ~44 chars). Values that need a private key should be a **base58 64-byte secret key** (the full keypair, ~88 chars). Common mistake: pasting only the 32-byte seed — that will fail with cryptic errors.

### 6.1 Data & auth

| Var | Required? | Purpose |
|---|---|---|
| `MONGO_URI` | ✅ | MongoDB connection string. Secret. |
| `MONGO_DB_NAME` | ✅ | DB name (default `xfee_sol_sale`). |
| `ADMIN_API_KEY` | ✅ | Any string; passed as `X-Admin-Key` header for admin endpoints. Generate with `openssl rand -hex 32`. Secret. |

### 6.2 Solana wallets & program

| Var | Required? | Purpose |
|---|---|---|
| `MASTER_WALLET_ADDRESS` | ✅ | Master wallet pubkey (top of tree). |
| `MASTER_WALLET_PRIVATE_KEY` | ✅ | Master's base58 secret. Secret. |
| `TREASURY_WALLET_ADDRESS` | ✅ | Treasury wallet pubkey. |
| `TREASURY_WALLET_PRIVATE_KEY` | ✅ | Treasury's base58 secret. Secret. |
| `XFEE_TOKEN_MINT` | ✅ | XFEE SPL mint address. |
| `POOL_ADDRESS` | ✅ | POWER staking pool PDA. |
| `POOL_AUTHORITY_PRIVATE_KEY` | Recommended | Staking pool authority's base58 secret. Leave empty to fall back to master. |
| `STAKING_PROGRAM_ID` | Optional | On-chain program ID. Empty = use built-in default `EX7YLYMv9pjarwgFF8JN5kwSohuhgVVmTDfD31ETekBC`. |
| `QUICKNODE_RPC_URL` | ✅ | Solana RPC URL. Any premium Solana JSON-RPC endpoint works. Secret. |
| `SOL_PRICE_API_URL` | ✅ | SOL/USD price feed URL. |

### 6.3 Set-user-level signer (optional)

| Var | Default | Purpose |
|---|---|---|
| `SET_USER_LEVEL_SIGNER_WALLET` | empty | Base58 pubkey of wallet authorized to sign `POST /admin/set-user-level`. When empty, master signs. |

### 6.4 Purchase parameters

| Var | Default | Purpose |
|---|---|---|
| `PURCHASE_WALLET_EXPIRY_MINUTES` | `15` | How long a purchase can sit unpaid before it expires. |
| `PURCHASE_MIN_USD` | `6.00` | Minimum purchase size (display / gas-math tuning). |
| `GAS_BUFFER_USD` | `5.00` | Base gas buffer added on top of the purchase price. |
| `LEAVE_IN_PURCHASE_WALLET_USD` | `4.50` | Residual left in the ephemeral purchase wallet after sweep. |
| `XFEE_TOTAL_SUPPLY` | `0` | Display cap for `/stats/global`. `0` = unlimited. |

Note: purchases under $10 skip commission distribution entirely — the code has a hard-coded $10 floor in `app/services/purchase_flow.py` (`sale_usd >= 10.0`). Adjust if your product's minimum differs.

### 6.5 POWER staking

| Var | Default | Purpose |
|---|---|---|
| `POWER_DISTRIBUTION_ENABLED` | `true` | Set `false` to defer all staking; purchases still process and get `power_distribution_bonus_eligible: true` for later catch-up. |
| `POWER_DELAYED_STAKE_BONUS_MULTIPLIER` | `1.00` | Multiplier applied when the deferred stakes are eventually processed (e.g. `1.25` = 25% bonus). |
| `STAKE_REPAIR_INTERVAL_SECONDS` | `120` | How often the repair worker retries staking for previously-failed purchases. |
| `STAKE_REPAIR_MIN_AGE_MINUTES` | `5` | Grace period before a failed purchase is retried. |
| `STAKE_REPAIR_BATCH_SIZE` | `100` | Max purchases per repair scan. |

### 6.6 Global pool

| Var | Default | Purpose |
|---|---|---|
| `GLOBAL_POOL_ENABLED` | `true` | Master switch. `false` pauses point accrual and worker. |
| `GLOBAL_POOL_DURATION_DAYS` | `15` | Window length for each pool. |
| `GLOBAL_POOL_FINALIZE_INTERVAL_SECONDS` | `300` | How often the worker scans for pools ready to settle. |
| `GLOBAL_POOL_FUNDING_WALLET_PRIVATE_KEY` | — | Secret. Base58 secret of the wallet that pays user payouts. Usually the master. |
| `GLOBAL_POOL_FUNDING_BUFFER_SOL` | `0.05` | SOL reserve kept in the funding wallet (covers tx fees). |
| `GLOBAL_POOL_SETTLEMENT_CONCURRENCY` | `3` | Parallel payouts during settlement. |
| `GLOBAL_POOL_CONFIRM_RETRIES` | `30` | Signature-status polls before giving up. |

### 6.7 Root-child enforcement (optional)

| Var | Default | Purpose |
|---|---|---|
| `ENFORCE_ROOT_CHILD` | `false` | When true, master can only refer `ROOT_CHILD_WALLET_ADDRESS` and that wallet has at most `ROOT_CHILD_MAX_DIRECT_REFERRALS`. |
| `ROOT_CHILD_WALLET_ADDRESS` | empty | Only required when enforcement is on. |
| `ROOT_CHILD_LEVEL` | `14` | Only used when enforcement is on. |
| `ROOT_CHILD_MAX_DIRECT_REFERRALS` | `1` | Only used when enforcement is on. |

### 6.8 Burn dashboard (optional)

Only relevant if this deployment publishes token-burn stats via `/api/v1/burn/*`. If your product doesn't publish burns, leave these empty.

| Var | Default | Purpose |
|---|---|---|
| `BURN_TOKEN_BUY_WALLET` | empty | Wallet whose burns are aggregated as "token_buy" burns. |
| `BURN_FEE_WALLET` | empty | Wallet whose burns are aggregated as "fee" burns. |

### 6.9 Multi-currency purchase mode

Runtime toggle between SOL-mode and XFEE-mode purchases. Only one mode is
active at a time; toggling affects new `/purchase/initiate` calls only.
In-flight purchases keep their initiated mode.

| Var | Default | Purpose |
|---|---|---|
| `DEFAULT_PURCHASE_MODE` | `SOL` | Initial mode on a fresh deployment. After first boot the mode is stored in `system_meta` and updated via the admin API. |
| `XFEE_PAYMENT_TOKEN_MINT` | empty | SPL mint used as payment when mode is `XFEE`. Required if you plan to enable XFEE mode. |
| `XFEE_PAYMENT_TOKEN_DECIMALS` | `9` | Decimals of that mint. |
| `XFEE_PRICE_ORACLE_URL` | empty | GET URL returning JSON with the token's USD price. |
| `XFEE_PRICE_ORACLE_JSON_PATH` | `data.price` | Dotted path into the JSON response body pointing at the price field. |
| `XFEE_PRICE_CACHE_TTL_SECONDS` | `30` | In-process cache TTL for the price. |
| `XFEE_MODE_SOL_GAS_BUFFER_SOL` | `0.02` | Small SOL amount the buyer must include on the ephemeral wallet alongside their XFEE payment so the backend can afford commission + sweep tx fees. |
| `XFEE_MODE_RECEIVE_TOLERANCE` | `0.95` | Fraction of the quoted XFEE amount that counts as "received" (allows a small tolerance for wallet slippage). |

Toggling the mode:

```bash
# Read current mode (public)
curl https://<host>/api/v1/purchase-mode

# Flip to XFEE
curl -X POST https://<host>/api/v1/admin/purchase-mode \
  -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"mode":"XFEE","reason":"launching XFEE-only sale"}'

# Recent change history (admin)
curl https://<host>/api/v1/admin/purchase-mode/history \
  -H "X-Admin-Key: $ADMIN_API_KEY"
```

Master wallet requirements when XFEE mode is enabled:

- Must have an XFEE ATA (created automatically on the first commission or
  sweep tx; you can also pre-create it with `spl-token create-account`).
- Must hold enough SOL to pay ATA-creation rent for ancestors that don't yet
  have an XFEE ATA (~0.002 SOL per new recipient per purchase).

### 6.10 Test mode

| Var | Default | Purpose |
|---|---|---|
| `TEST_MODE` | `false` | **Never true in production.** Set true only for local dry-runs — mocks all Solana RPC calls. |
| `TEST_SOL_PRICE` | `150.00` | Only used when TEST_MODE=true. |

---

## 7. First deploy

Follow [`DEPLOY.md`](./DEPLOY.md) — the "Option A — Deploy via app spec" path is the cleanest.

Expected startup log sequence:

```
Starting XFEE Sale Backend...
MongoDB connected, indexes ensured
HTTP client initialized
Master wallet created: <MASTER_WALLET_ADDRESS>          # first boot only
Level migration to 16-tier applied (total shifted: 0)   # no-op on fresh DB
Ladder migration to 15-tier applied ...                 # no-op on fresh DB
Wallet pool OK: 0 free wallets → Generating 50 purchase wallets
Treasury XFEE balance: <amount>
Global stats: 0 XFEE sold (unlimited supply)
Stake repair worker started ...
Global pool worker started ...
XFEE Sale Backend ready
```

If any of those lines is missing or errors, fix before proceeding.

---

## 8. Post-deploy smoke test

Run the Tier-3 mainnet smoke test from [`TEST_PLAN.md`](./TEST_PLAN.md), summarized here:

1. `curl https://<app>.ondigitalocean.app/health` → `{"status":"ok"}`.
2. `curl https://<app>.ondigitalocean.app/api/v1/stats/global` → sane numbers.
3. `curl https://<app>.ondigitalocean.app/api/v1/stats/levels` → 15-tier ladder.
4. Register your own wallet as a user referred by master (POST `/api/v1/user/register`).
5. Initiate a tiny purchase ($10 or so — commissions are skipped below $10) and pay it.
6. Poll `/api/v1/purchase/{id}` until `status: "completed"`.
7. Verify on Solscan:
   - Purchase-wallet received your SOL.
   - Commission tx from purchase-wallet to master.
   - Sweep tx from purchase-wallet to master.
   - Stake tx from `POOL_AUTHORITY` (or master) to the staking program, staking POWER to your wallet.
8. Force-finalize a pool: `POST /api/v1/admin/global-pool/{index}/settle?force=true` with `X-Admin-Key`. Verify the SPL Memo transfer on Solscan.

---

## 9. What can go wrong on first boot

- **`ModuleNotFoundError: No module named 'solana.rpc.api'`** — the `solana` package pin in `requirements.txt` allowed a version ≥ 0.40. Ensure the pin `solana>=0.36,<0.40` is intact.
- **`Master wallet exists at level X` where X ≠ 15** — someone manually promoted the wallet to a level above the ladder max. Delete the user doc in Mongo and restart; the bootstrap will recreate at the correct level.
- **`Configured root child ensured` when you didn't want enforcement on** — check `ENFORCE_ROOT_CHILD` in the DO panel (not just `.do/app.yaml` — the panel overrides).
- **All purchases go to the master wallet, no commissions distributed** — check that your test purchases are ≥ $10. The `sale_usd >= 10.0` floor in `purchase_flow.py` skips commissions below that.
- **Stake failures with `checked=N staked=0 failed=N`** — inspect `power_distribution_last_error` on a purchase doc. Common causes: `POOL_ADDRESS` truncated (must be exactly 32 bytes when base58-decoded); wrong `POOL_AUTHORITY_PRIVATE_KEY` (must match the keypair that created the pool); insufficient SOL in the signing wallet for gas.

---

## 10. Post-launch operational hygiene

- Rotate `ADMIN_API_KEY` if leaked.
- Keep master wallet SOL ≥ (worst-case pool payout × 1.5). The pool worker reserves `GLOBAL_POOL_FUNDING_BUFFER_SOL` before settling, but a wallet that's dry when a pool ends can leave users unpaid.
- Enable MongoDB point-in-time backups. All state is in Mongo — losing it is worse than losing the DO app.
- Monitor `/api/v1/global-pool/funds`, `/stats/power`, and `/stats/global` from your dashboards.
- The `system_meta` collection holds migration markers. Do not delete or edit — otherwise migrations will re-run.

---

## 11. Related docs

- [`CHANGES.md`](./CHANGES.md) — what was changed in this branch vs. the Bruce Lee original.
- [`DEPLOY.md`](./DEPLOY.md) — day-2 deploy/rollback/scale operations.
- [`FRONTEND_INTEGRATION.md`](./FRONTEND_INTEGRATION.md) — API reference for the UI team.
- [`TEST_PLAN.md`](./TEST_PLAN.md) — 3-tier test strategy (local / devnet / mainnet).
- [`plan.md`](./plan.md) — original design doc (still accurate for architecture; some rate numbers are stale, use `app/utils/level.py` as the source of truth).
