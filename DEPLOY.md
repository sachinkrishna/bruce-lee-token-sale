# DigitalOcean App Platform — Deploy Guide

The app is set up to deploy as a Docker service on DO App Platform with auto-deploy on push.

## Prerequisites

1. **GitHub repo** — code committed and pushed (origin is `sachinkrishna/bruce-lee-token-sale`).
2. **MongoDB Atlas cluster** — free tier (`M0`) is fine for launch. Get a `mongodb+srv://...` connection string. Whitelist `0.0.0.0/0` initially (then narrow to DO's egress IPs after the first deploy).
3. **Solana RPC** — QuickNode / Helius / Triton — premium endpoint. The free RPC will rate-limit you in minutes.
4. **SOL price API** — CoinGecko works without a key. If you want higher reliability, get a paid key.
5. **Solana wallets** — three on mainnet:
   - **Master / funding wallet**: `DXSEB4WrtfSFvD6ZKvyiyg9GDnEgmc6uAPpkHHQBNwFB` (already in the app spec). Pre-fund with the SOL you want to use as global-pool payouts plus a small buffer for commission/staking gas (start with ~1 SOL above whatever you want to pool).
   - **Treasury wallet**: holds the entire XFEE supply. The app dispatches XFEE from here to buyers.
   - **(implicit) Root child** at `BRrtYftGhXBh3JcwmveuB4ZcskkYvUeLzNgPcf5VF6Ry` — bootstrapped on first startup, no on-chain action needed.
6. **POWER staking program + pool address** — `POOL_ADDRESS` for the staking program (already in your prior `.env`).
7. **doctl CLI** (optional, but the cleanest path):

```bash
brew install doctl
doctl auth init
```

## Option A — Deploy via app spec (recommended)

This uses the committed `.do/app.yaml` so the deploy is reproducible.

1. Push the latest code:

   ```bash
   git add -A
   git commit -m "auto-settle global pool, dry-run green, DO app spec"
   git push origin main
   ```

2. Create the app from spec:

   ```bash
   doctl apps create --spec .do/app.yaml
   ```

   doctl returns an app ID. Save it.

3. Fill in the secrets (the spec leaves these blank — they cannot live in git):

   ```bash
   APP_ID=<id from step 2>
   doctl apps update $APP_ID --spec .do/app.yaml      # noop, used for refresh after edits
   ```

   Now go to **DO Dashboard → Apps → your app → Settings → App-Level Environment Variables** and fill the following SECRET values (also listed in `.do/app.yaml` with `value: ""`):

   - `MONGO_URI`
   - `MASTER_WALLET_PRIVATE_KEY`
   - `TREASURY_WALLET_PRIVATE_KEY`
   - `QUICKNODE_RPC_URL`
   - `ADMIN_API_KEY` — generate with `openssl rand -hex 32`
   - `GLOBAL_POOL_FUNDING_WALLET_PRIVATE_KEY` — same as `MASTER_WALLET_PRIVATE_KEY` for our setup (master = funding wallet)

   And the non-secret values that the spec has as `""`:

   - `TREASURY_WALLET_ADDRESS`
   - `XFEE_TOKEN_MINT`
   - `POOL_ADDRESS`

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
   Master wallet exists: DXSEB...
   Configured root child created: BRrtYftG...   (or "ensured" on later deploys)
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

1. Push your code (same as Option A step 1).
2. DO Dashboard → **Create App** → connect the GitHub repo → choose `main` branch → Dockerfile autodetected.
3. Set HTTP port to **8000**, health check path to **/health**.
4. Paste each env var manually from `.do/app.yaml`. Mark private keys + RPC URLs + Mongo URI as **encrypted**.
5. Deploy.

## After first deploy

1. Verify root child + master in Mongo:

   ```bash
   # via doctl (from logs) or your Mongo dashboard:
   db.users.findOne({wallet_address: "DXSEB4WrtfSFvD6ZKvyiyg9GDnEgmc6uAPpkHHQBNwFB"})
   db.users.findOne({wallet_address: "BRrtYftGhXBh3JcwmveuB4ZcskkYvUeLzNgPcf5VF6Ry"})
   ```

2. Set a strict admin key and store it in your secrets manager:

   ```bash
   openssl rand -hex 32
   ```

3. Test the public endpoints from the production URL:

   ```bash
   curl https://<your-app>.ondigitalocean.app/api/v1/stats/global
   curl https://<your-app>.ondigitalocean.app/api/v1/global-pool/summary
   ```

4. Do the **Tier 3 mainnet smoke** in `TEST_PLAN.md` — small ($10) purchase, verify commissions land, force-settle a pool with a tiny pool, verify the SPL Memo transfer on Solscan.

## Rollback

The Dockerfile + app spec are stable; rolling back is just redeploying an older commit:

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
