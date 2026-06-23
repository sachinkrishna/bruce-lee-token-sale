# Pre-Launch Test Plan

End-to-end dry run for the XFEE / POWER sales system covering commissions, POWER staking, global pool auto-settlement, and root-child enforcement.

There are **three tiers** of testing. Run them in order; do not skip to mainnet.

| Tier | Network | Purpose | Cost | Reversible |
| --- | --- | --- | --- | --- |
| 1. Local | none (test_mode) | All logic, math, state machines | $0 | Yes |
| 2. Devnet | Solana devnet | Real RPC, real signing, real txs | $0 | Yes |
| 3. Mainnet smoke | Solana mainnet | Tiny real money | ~$5 | No (real funds) |

---

## Tier 1 — Local dry run (test_mode)

All Solana RPC calls are mocked: balances live in an in-memory dict, transfers are recorded but not broadcast.

### Prerequisites

1. MongoDB running on `localhost:27017`.
2. `requirements.txt` installed in your venv.
3. Copy the test env file:

```bash
cp .env.test .env
```

`.env.test` sets:

- `TEST_MODE=true`
- A fixed funding-wallet keypair so settlement can be tested.
- `ENFORCE_ROOT_CHILD=false` so the existing test chain `A→B→C→D under master` works without the root-child structure.
- `ADMIN_API_KEY=test-admin-key`.

### Run

Terminal 1 — start MongoDB if needed:

```bash
brew services start mongodb-community
```

Terminal 2 — start the API in test mode:

```bash
source venv/bin/activate
uvicorn app.main:app --reload
```

Wait for `XFEE Sale Backend ready` in the logs.

Terminal 3 — run the test suite:

```bash
source venv/bin/activate
python3 test_suite.py
```

### What it covers

- **Preflight**: server up, master loaded from env, sol_price mocked at $150, DB reset, wallet pool replenished.
- **Registration**: valid registration, invalid addresses (400), duplicates (409), non-existent referrer (400), referrer-without-purchase (400).
- **Purchase flow**: initiate → simulate deposit → poll → completed. Asserts `sol_amount_received`, `sol_price_at_confirmation`, `token_dispatch_tx`, `commission_distributed`.
- **Referral chain & commissions**: A→B→C→D chain, B upgraded to L5. Verifies differential rates (L1=0.20, L5−L1=0.08), zero-allocs for too-low ancestors (A under D's purchase with status `zero`), and proper `total_sales_usd` rollups.
- **Tree**: depth, child counts.
- **Level management**: master upgrades B to L3 then L5; downgrades and same-level requests are rejected; out-of-range levels (0, 16) are rejected.
- **Differential commission math**: D's purchase generates expected commissions for C, B, and a zero alloc for A.
- **Multi-purchase**: same user buys twice, totals roll up.
- **Admin endpoints**: pool replenish, reindex, validation rejections.
- **Alloc indexing**: every commission alloc reports `indexed: true`.
- **Global pool — point accrual**: A earns global pool points from D's purchase (peer C at L1 was paid), `settle_status=pending`.
- **Global pool — queries**: `/summary`, list, `/current`, `/{pool_index}`, `/{pool_index}/user/{wallet}`, `/user/{wallet}/points`, plus 404 for non-existent pool.
- **Global pool — settlement (force-finalize)**: seeds the funding wallet with 10 mock SOL, admin force-settles the current pool, verifies pool status=`settled`, snapshot present, A's entry transitions to `confirmed` with `tx_signature` + `memo`.
- **Global pool — idempotency**: re-calling settle returns `already_settled=true`; A's `tx_signature` is stable; settling a non-existent pool returns 400; calling settle without admin key returns 401/422.
- **Root-child bootstrap**: configured root child exists, level 14, valid referrer, referrer is master.

### Pass criteria

Final output: `Total: N | Passed: N | Failed: 0`. Any failure must be investigated before moving on.

---

## Tier 2 — Devnet dry run (real Solana RPC, mocked SOL economics)

Same code path, but `TEST_MODE=false`, real RPC, real wallets with devnet SOL.

### Prerequisites

1. Two funded devnet wallets:
   - **master / funding** wallet — gets airdropped ~10 devnet SOL.
   - **treasury** wallet — gets airdropped some devnet SOL + holds devnet XFEE token mint.
2. Devnet XFEE token mint + pool address (or a devnet copy of mainnet ones).
3. A devnet RPC URL (Helius/Quicknode/etc.).
4. `TEST_MODE=false`, `ENFORCE_ROOT_CHILD=true` (production behavior).
5. `GLOBAL_POOL_FUNDING_WALLET_PRIVATE_KEY` set to the master/funding wallet's base58 private key.
6. `MASTER_WALLET_ADDRESS`, `TREASURY_WALLET_ADDRESS`, `MASTER_WALLET_PRIVATE_KEY`, `TREASURY_WALLET_PRIVATE_KEY` all set to devnet wallets.

### Manual test scripts (in order)

| Step | Action | Expected |
| --- | --- | --- |
| 1 | `GET /health` | 200 OK |
| 2 | Check logs: master + root child bootstrapped, treasury XFEE balance logged | "Master wallet ..."  + "Configured root child created: ..." |
| 3 | `POST /user/register` for a new wallet under master | 400 (only root child allowed) |
| 4 | `POST /user/register` for root_child under master | already exists |
| 5 | Use the existing root child wallet, register one direct user `A` under it | 200 |
| 6 | Try a second direct user under root_child | 400 (max 1 direct) |
| 7 | `POST /purchase/initiate` for `A` ($10+ purchase) → send real devnet SOL from `A`'s wallet to the returned `purchase_wallet` | 200 + purchase enters `pending` |
| 8 | Watch logs until purchase status = `completed`, `token_dispatch_tx` set, `commission_distributed=true` | OK |
| 9 | `GET /user/{root_child}/allocs` | One commission alloc for root_child with `status=sent` and `on_chain_tx` set (verify on Solscan devnet) |
| 10 | Register B under A (after A's purchase completes), B buys, verify the full ancestor chain pays out | OK |
| 11 | Force a zero-alloc + peer points scenario (you can set someone's level via `/user/set-user-level`), then verify a `pool_points` doc is created | OK |
| 12 | `GET /global-pool/current` shows the active pool, A in standings if applicable | OK |
| 13 | `POST /admin/global-pool/{idx}/settle?force=true` with admin key | 200, pool transitions to `settled`. Verify on Solscan that **funding wallet → user** SOL transfers happened with an SPL memo. |
| 14 | Call settle again | `already_settled=true`. |
| 15 | Cause a failure (briefly take funding wallet below buffer), call settle. Then top up wallet, retry. | First call errors with insufficient balance; second call succeeds and reconciles by memo, no double-pay. |

### Pass criteria

All steps green. Inspect at least 3 settlement txs on Solscan and confirm: correct amount, correct recipient, memo present.

---

## Tier 3 — Mainnet smoke

Same as Tier 2, but with mainnet RPC and **small** real amounts.

### Prerequisites

1. Production `.env` filled in:
   - `MASTER_WALLET_ADDRESS=DXSEB4WrtfSFvD6ZKvyiyg9GDnEgmc6uAPpkHHQBNwFB` and its private key.
   - `TREASURY_WALLET_ADDRESS` + private key.
   - `XFEE_TOKEN_MINT`, `POOL_ADDRESS`, `QUICKNODE_RPC_URL`, `SOL_PRICE_API_URL`.
   - `ADMIN_API_KEY` (strong, secret).
   - `GLOBAL_POOL_ENABLED=true`, `GLOBAL_POOL_FUNDING_WALLET_PRIVATE_KEY` = master wallet private key.
   - `ENFORCE_ROOT_CHILD=true`.
   - `POWER_DISTRIBUTION_ENABLED` (true or false depending on the launch decision).
2. Funding wallet pre-loaded with a small SOL amount to cover gas + tiny payouts (e.g., 0.5 SOL).
3. App deployed to DigitalOcean App Platform (or your target). HTTPS + healthcheck passing.

### Smoke script

| Step | Action | Expected |
| --- | --- | --- |
| 1 | Hit `/health` from prod URL | 200 OK |
| 2 | Verify `/api/v1/stats/global` returns sane numbers | OK |
| 3 | Register the root child (or confirm bootstrap created it) | OK |
| 4 | Register **one** real user A under root child (your wallet) | OK |
| 5 | A buys a **tiny** amount (e.g., 5 XFEE = $10) using a real wallet | Purchase completes; check Solscan for: token dispatch, commission to root_child + master, sweep to master |
| 6 | If POWER staking is enabled, verify the stake tx on the staking program | OK |
| 7 | Optionally cause a zero-alloc by manually raising an ancestor's level via `/user/set-user-level`, run another tiny purchase | Pool points appear |
| 8 | `POST /admin/global-pool/{idx}/settle?force=true` with admin key | Real SOL transfer from master to A's wallet with memo. Verify on Solscan. |

### Production readiness checklist

- [ ] `.env` does NOT contain `TEST_MODE=true`.
- [ ] `ADMIN_API_KEY` is set, long, secret, and **not** the test value.
- [ ] `MASTER_WALLET_PRIVATE_KEY` is set and the address matches `MASTER_WALLET_ADDRESS`.
- [ ] `TREASURY_WALLET_PRIVATE_KEY` is set and address matches.
- [ ] Treasury wallet's XFEE ATA holds enough XFEE for projected sales.
- [ ] Master wallet has enough SOL for purchase-wallet rent + commission txs + the first global pool payout buffer.
- [ ] `GLOBAL_POOL_FUNDING_BUFFER_SOL` is high enough to cover tx fees for the largest expected pool's payout count (≈0.000005 SOL per tx, so 0.05 SOL = ~10k tx headroom).
- [ ] `ROOT_CHILD_WALLET_ADDRESS` matches the agreed wallet (`BRrtYftGhXBh3JcwmveuB4ZcskkYvUeLzNgPcf5VF6Ry`).
- [ ] `ENFORCE_ROOT_CHILD=true`.
- [ ] `POWER_DISTRIBUTION_ENABLED` set to the intended launch state.
- [ ] If `POWER_DISTRIBUTION_ENABLED=false`, `POWER_DELAYED_STAKE_BONUS_MULTIPLIER` is set to the desired bonus (e.g., `1.25`).
- [ ] `XFEE_PRICE_USD`, `XFEE_TOTAL_SUPPLY`, `PURCHASE_MIN_USD`, `GAS_BUFFER_USD`, `LEAVE_IN_PURCHASE_WALLET_USD` reviewed.
- [ ] `QUICKNODE_RPC_URL` is the production endpoint (not the trial one).
- [ ] `SOL_PRICE_API_URL` is set and the endpoint responds.
- [ ] Mongo connection string points at the production cluster, not staging.
- [ ] DigitalOcean App Platform `PORT` is honored by the Dockerfile (it is — `CMD` uses `${PORT:-8000}`).
- [ ] `/health` check passes from the platform.
- [ ] All indexes auto-created on first startup (`ensure_indexes` runs in `lifespan`).
- [ ] DB backups configured.
- [ ] An on-call rota / monitoring (logs + alert on `commission_distributed=false` for completed purchases) in place.

---

## Endpoints to monitor in production

- `GET /health` — liveness.
- `GET /api/v1/stats/global` — sales / supply numbers.
- `GET /api/v1/global-pool/summary` — overall pool counts and sums.
- `GET /api/v1/global-pool/current` — current pool standings.
- `GET /api/v1/global-pool/{pool_index}` — historical pool details.
- `GET /api/v1/global-pool/user/{wallet}/points` — per-user pool history.

## Manual triggers (admin-protected with `X-Admin-Key`)

- `POST /api/v1/admin/global-pool/{pool_index}/settle?force=false` — finalize/retry a settled or due pool.
- `POST /api/v1/admin/global-pool/{pool_index}/settle?force=true` — end an active pool early and settle.
- `POST /api/v1/admin/global-pool/process-due` — run the due-pool scan once.
- `POST /api/v1/admin/staking/run-repair-scan` — repair POWER staking for pending purchases.
- `POST /api/v1/admin/pool/replenish` — top up the purchase wallet pool.
- `POST /api/v1/admin/reindex/{wallet}` — re-index a single user's allocs.

## Rollback

If something goes wrong during launch:

1. Switch `POWER_DISTRIBUTION_ENABLED=false` → POWER pauses, purchases continue, bonus eligibility flagged for later catch-up.
2. Switch `GLOBAL_POOL_ENABLED=false` → no new points accrued, the active pool stays unsettled until you flip it back.
3. Deploy a revert if needed; all state machines are idempotent so retrying after a fix is safe.
