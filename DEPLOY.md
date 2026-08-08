# DigitalOcean App Platform — Deploy Guide

The app is set up to deploy as a Docker service on DO App Platform with auto-deploy on push. For a full fresh-deployment checklist (bootstrapping wallets, MongoDB, etc.), see [`SETUP.md`](./SETUP.md). This document is the operational cheat-sheet.

## Prerequisites

1. **GitHub repo** — code committed and pushed. Update the `github.repo` and `github.branch` values in `.do/app.yaml` to your repo/branch.
2. **MongoDB Atlas cluster** — free tier (`M0`) is fine for launch. Get a `mongodb+srv://...` connection string. Whitelist `0.0.0.0/0` initially (then narrow to DO's egress IPs after the first deploy).
3. **Solana RPC** — QuickNode / Helius / Triton — premium endpoint. The free RPC will rate-limit you in minutes.
4. **SOL price API** — CoinGecko works without a key. If you want higher reliability, get a paid key.
5. **Solana wallets** — three on mainnet:
   - **Master / funding wallet**: top of the referral tree. Pre-fund with the SOL you want to use as global-pool payouts plus a small buffer for commission/staking gas.
   - **Treasury wallet**: holds the entire XFEE supply. The app dispatches XFEE from here to buyers.
   - **(optional) Root child** — only needed when `ENFORCE_ROOT_CHILD=true` (defaults to false; the reserved-second-from-top slot is disabled by default).
6. **POWER staking program + pool address** — set `POOL_ADDRESS` (the pool PDA) and, when needed, `STAKING_PROGRAM_ID`. If the program is the default one (`EX7YLYMv9pjarwgFF8JN5kwSohuhgVVmTDfD31ETekBC`), leave `STAKING_PROGRAM_ID` empty.
7. **doctl CLI** (optional, but the cleanest path):

```bash
brew install doctl
doctl auth init
```

## Option A — Deploy via app spec (recommended)

This uses the committed `.do/app.yaml` so the deploy is reproducible.

1. Push the latest code to the branch referenced in `.do/app.yaml`:

   ```bash
   git add -A
   git commit -m "prep for deploy"
   git push origin <your-branch>
   ```

2. Create the app from spec:

   ```bash
   doctl apps create --spec .do/app.yaml
   ```

   doctl returns an app ID. Save it.

3. Fill in the secrets (the spec leaves these blank — they cannot live in git). In **DO Dashboard → Apps → your app → Settings → App-Level Environment Variables**, set:

   - `MONGO_URI`
   - `MASTER_WALLET_ADDRESS`, `MASTER_WALLET_PRIVATE_KEY`
   - `TREASURY_WALLET_ADDRESS`, `TREASURY_WALLET_PRIVATE_KEY`
   - `XFEE_TOKEN_MINT`
   - `POOL_ADDRESS`
   - `POOL_AUTHORITY_PRIVATE_KEY` — leave empty to sign stakes with `MASTER_WALLET_PRIVATE_KEY`; set for a dedicated pool-authority keypair
   - `STAKING_PROGRAM_ID` — leave empty for the default program; set only if you deployed the staking program yourself
   - `QUICKNODE_RPC_URL`
   - `ADMIN_API_KEY` — generate with `openssl rand -hex 32`
   - `GLOBAL_POOL_FUNDING_WALLET_PRIVATE_KEY` — usually the same as `MASTER_WALLET_PRIVATE_KEY`

   Click "Save" — DO will redeploy.

4. Watch logs:

   ```bash
   doctl apps logs $APP_ID --tail
   ```

   You should see, in order:

   ```
   Starting XFEE Sale Backend...
   MongoDB connected, indexes ensured
   HTTP client initialized
   Master wallet set to level 15
   Wallet pool OK: N free wallets
   Treasury XFEE balance: <amount>
   Stake repair worker started ...
   Global pool worker started ...
   XFEE Sale Backend ready
   ```

5. Hit the public health endpoint:

   ```bash
   curl https://<your-app>.ondigitalocean.app/health
   # {"status":"ok"}
   ```

## Option B — Deploy via DO dashboard (no CLI)

1. Push your code.
2. DO Dashboard → **Create App** → connect the GitHub repo → choose your branch → Dockerfile autodetected.
3. Set HTTP port to **8000**, health check path to **/health**.
4. Paste each env var manually from `.do/app.yaml`. Mark private keys + RPC URLs + Mongo URI as **encrypted**.
5. Deploy.

## After first deploy

1. Verify master exists in Mongo at the correct level:

   ```bash
   db.users.findOne({wallet_address: "<MASTER_WALLET_ADDRESS>"})
   ```

2. Smoke-test the public endpoints:

   ```bash
   curl https://<your-app>.ondigitalocean.app/api/v1/stats/global
   curl https://<your-app>.ondigitalocean.app/api/v1/global-pool/summary
   curl https://<your-app>.ondigitalocean.app/api/v1/stats/levels
   ```

3. Do the **Tier 3 mainnet smoke** in `TEST_PLAN.md` — small purchase, verify commissions land, force-settle a pool with a tiny pool, verify the SPL Memo transfer on Solscan.

## Rollback

Rolling back is just redeploying an older commit:

```bash
doctl apps create-deployment $APP_ID --image-digest <prior digest>
```

Or via dashboard → **Deployments** → choose a green deploy → **Promote**.

For runtime rollback without redeploy:

- `POWER_DISTRIBUTION_ENABLED=false` pauses staking; purchases still complete; users get bonus eligibility flag for catch-up later.
- `GLOBAL_POOL_ENABLED=false` pauses point accrual and worker; current pool stays unsettled until you flip it back.

Both are env var flips in the dashboard — DO redeploys with the new value in seconds.

## Scaling

- For ≤500 daily purchases, `basic-xs` (1 vCPU, 1GB) is enough.
- For more, move to `basic-s` or `professional-xs`. Set `instance_count: 2+` for HA. **Important:** the `WORKER_ID` lock on global-pool settlement is per-instance — with multiple instances, two pools could try to settle from the same funding wallet at once. The lock is wallet-level and stored in Mongo, so it still serializes correctly, but you should size the worker concurrency conservatively.
