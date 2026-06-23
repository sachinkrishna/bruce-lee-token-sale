# XFEE Token Sale — Frontend Integration Guide

A self-contained reference for the team building the customer-facing UI on top of the XFEE Sales API.

---

## 1. Overview

- **Product:** A Solana-based token pre-sale with a 15-tier multi-level commission structure (cumulative differential model) and a 15-day "global pool" that rewards users in the referral tree who get bypassed by the commission cascade.
- **Identity:** Solana wallets only. There is no email/password, no JWT, no session cookie. The user's wallet public key is the user ID.
- **Wallet integrations to support:** Phantom (primary), Solflare, Backpack, any Solana standard wallet adapter.
- **Network:** Solana **mainnet-beta**.
- **API style:** REST + JSON over HTTPS. CORS is open (`Access-Control-Allow-Origin: *`). No rate limiting. No webhooks — poll for purchase completion.

---

## 2. Connection details

| Item | Value |
|---|---|
| **API base URL** | `https://brucelee-app-sale-cbsgj.ondigitalocean.app` |
| **All endpoints prefix** | `/api/v1` |
| **Swagger UI (auto-generated)** | `https://brucelee-app-sale-cbsgj.ondigitalocean.app/docs` |
| **OpenAPI JSON** | `https://brucelee-app-sale-cbsgj.ondigitalocean.app/openapi.json` |
| **Health check** | `GET /health` → `{"status":"ok"}` |
| **Solscan tx links (mainnet)** | `https://solscan.io/tx/{signature}` |

---

## 3. Fixed system constants

These are constants. Hard-code or env-config them on the frontend.

| Name | Value | Purpose |
|---|---|---|
| `MASTER_WALLET` | `DXSEB4WrtfSFvD6ZKvyiyg9GDnEgmc6uAPpkHHQBNwFB` | Top of referral tree, level 15 (100% rate). **Cannot be used as a referrer in the registration form** — backend rejects it. |
| `ROOT_CHILD_WALLET` | `BRrtYftGhXBh3JcwmveuB4ZcskkYvUeLzNgPcf5VF6Ry` | Single child of master, level 14 (95% rate). The very first user to register MUST register under this wallet. After that, registrations under it are rejected (limit = 1 direct referral). |
| `XFEE_PRICE_USD` | `1.00` | 1 XFEE always costs $1 USD. Hard-coded. The amount of SOL needed is computed from the live SOL/USD oracle. |
| `MIN_PURCHASE_XFEE` | `6` | Minimum purchase size (= $6). Smaller amounts are allowed by the API but the gas buffer math is tuned for ≥ $6. |
| `PURCHASE_TIMEOUT_MINUTES` | `15` | A purchase expires if SOL doesn't arrive in 15 minutes. |
| `XFEE supply` | Unlimited | No hard cap on tokens sold. |

---

## 4. Referral structure — the one thing that's non-obvious

The system enforces a strict referral hierarchy:

```
master (L15, 100%)
  └── root-child (L14, 95%)        [max 1 direct referral]
        └── first founder           [the very first real user]
              └── customer 1
              └── customer 2
              └── customer N
```

**Rules the frontend MUST honor:**

1. **A referrer is always required.** There is no anonymous registration. If the user lands on the site without a `?ref=` query param, redirect them to a "you need a referral link to join" screen — do not silently substitute master/root-child as the default.
2. **Master cannot be set as `referrer_wallet` for any normal user.** The backend returns `400 "Master wallet can only refer the configured root child wallet"`.
3. **Root-child can only be set as `referrer_wallet` for the very first user.** Subsequent attempts return `400 "Configured root child wallet has reached its direct referral limit (1)"`. The UI should detect this and show "this referral link has reached its limit".
4. **A referrer must have completed at least one purchase** before they can refer anyone. If you POST a registration with a brand-new referrer who hasn't bought yet, backend returns `400 "Referrer has not completed a purchase yet"`.

Suggested referral link format: `https://yoursite.com/?ref=<WALLET>`

```typescript
const referrer = new URLSearchParams(window.location.search).get("ref");
if (!referrer) {
  // Show "invalid invite" screen — do not let the user proceed.
}
```

---

## 5. Commission model — what to display to users

Cumulative-differential commission ladder (15 tiers, all values are commission *rate*):

| Level | Rate | Sales volume needed to reach this level (USD) | Notes |
|---|---|---|---|
| 1 | 20% | 0 (default) | Everyone starts here |
| 2 | 22% | 500 | |
| 3 | 24% | 2,500 | |
| 4 | 26% | 10,000 | |
| 5 | 28% | 25,000 | |
| 6 | 30% | 50,000 | |
| 7 | 32% | 100,000 | |
| 8 | 34% | 250,000 | |
| 9 | 36% | 1,000,000 | |
| 10 | 40% | 2,500,000 | |
| 11 | 45% | 1,000,000,000 | Effectively manual-only |
| 12 | 60% | 3,000,000,000 | Manual-only |
| 13 | 75% | 5,000,000,000 | Manual-only |
| 14 | 95% | 9,000,000,000 | Reserved (root-child) |
| 15 | 100% | 10,000,000,000 | Reserved (master) |

Levels 1–10 are reachable organically via `total_sales_usd` volume. Levels 11–15 are manual upgrades only — they exist for special operator wallets.

These values are also exposed via `GET /api/v1/stats/levels` so the frontend can fetch them at runtime rather than hard-coding.

### How "cumulative differential" pays out

For each purchase, the backend walks up the buyer's referrer chain. Each ancestor receives `(their_rate − max_rate_already_paid_below) × sale_amount`.

**Example:** Buyer C (L1, 20%) buys $100. Their chain is B (L1, 20%) → A (L1, 20%) → root-child (L14, 95%) → master (L15, 100%).

| Ancestor | Their rate | Max paid below | Receives | USD |
|---|---|---|---|---|
| B (L1) | 20% | 0% | 20% | $20.00 |
| A (L1) | 20% | 20% | **0%** — zero-alloc | $0 → global pool point of $20 |
| root-child (L14) | 95% | 20% | 75% | $75.00 |
| master (L15) | 100% | 95% | 5% | $5.00 |

So even though A is in C's tree, they receive 0 SOL on this purchase because B already consumed the L1 differential. A is recorded as a "zero-alloc" — they accrue **global pool points** equal to the USD value B received ($20).

---

## 6. Endpoint reference

### Public endpoints (no auth)

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness probe |
| GET | `/api/v1/stats/global` | Tokens sold, total purchases, live SOL price |
| GET | `/api/v1/stats/levels` | Full commission ladder data |
| GET | `/api/v1/burn/summary` | Aggregated burns (token_buy + fee wallets) |
| GET | `/api/v1/burn/recent?limit=5` | Recent burn records |

### User profile / tree

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/user/register` | One-time registration when a wallet first connects |
| GET | `/api/v1/user/{wallet}` | Full profile (level, sales, commission stats) |
| GET | `/api/v1/user/{wallet}/directs` | List of all wallets directly referred by this user |
| GET | `/api/v1/user/{wallet}/tree` | Nested tree (up to 5 levels deep) |
| GET | `/api/v1/user/{wallet}/purchases?page=1&limit=20` | This user's purchase history |
| GET | `/api/v1/user/{wallet}/allocs?page=1&limit=20` | Commissions/zero-allocs received by this user |

### Purchase flow

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/purchase/estimate?xfee_amount=N` | Compute SOL needed without creating a purchase |
| POST | `/api/v1/purchase/initiate` | Start a purchase, get ephemeral receive wallet |
| GET | `/api/v1/purchase/{id}` | Poll for status until `completed` / `expired` / `failed` |

### Global pool (read-only for frontend)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/global-pool/summary` | System-wide pool stats |
| GET | `/api/v1/global-pool/` | List all pools (paginated, newest first) |
| GET | `/api/v1/global-pool/current` | Active pool + live standings |
| GET | `/api/v1/global-pool/{pool_index}` | Any past or current pool by index |
| GET | `/api/v1/global-pool/{pool_index}/user/{wallet}` | User's standing in a single pool |
| GET | `/api/v1/global-pool/user/{wallet}/points` | User's pool history across all pools |

Settlement of pools is fully automated server-side. The frontend just reads.

### Admin (used only by operator panels)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/v1/admin/set-user-level` | `X-Admin-Key` header **OR** master wallet signature | Manually upgrade a user's level |
| POST | `/api/v1/admin/global-pool/{idx}/settle?force=true` | `X-Admin-Key` | Force-finalize a pool early (operator only) |

Skip admin endpoints in the customer-facing build unless you're also shipping an operator console.

---

## 7. Detailed schemas

### 7.1 `POST /api/v1/user/register`

```json
// Request
{
  "wallet_address": "BuyerPublicKey…",
  "referrer_wallet": "ReferrerPublicKey…"
}
```

```json
// 200 Success
{ "success": true, "wallet_address": "BuyerPublicKey…" }
```

| Status | Body `detail` | Frontend action |
|---|---|---|
| 200 | — | Proceed to purchase flow |
| 400 | `Invalid Solana address: …` | Show "invalid wallet address" |
| 400 | `Master wallet can only refer the configured root child wallet` | The user passed master as their referrer — show "invalid invite link" |
| 400 | `Configured root child wallet has reached its direct referral limit (1)` | First-founder slot is already taken; this referral link is no longer usable |
| 400 | `Referrer not found` | Referrer wallet isn't registered yet |
| 400 | `Referrer has not completed a purchase yet` | Referrer is registered but hasn't bought — they aren't a valid referrer yet |
| 409 | `Wallet already registered` | **Not an error** — proceed straight to the dashboard |

### 7.2 `GET /api/v1/user/{wallet_address}`

```json
{
  "wallet_address": "…",
  "referrer_wallet": "…",
  "level": 2,
  "self_purchase": 500.0,
  "total_sales_usd": 12400.0,
  "total_commission_sol": 1.245,
  "self_purchase_tokens": 500,
  "total_tokens_sold": 12400,
  "level_sales": { "1": 8400.0, "2": 4000.0 },
  "level_commission": { "1": 0.8, "2": 0.445 },
  "direct_sales_sol": 6.2,
  "indirect_sales_sol": 11.8,
  "direct_commission_sol": 0.81,
  "indirect_commission_sol": 0.43,
  "direct_referral_count": 5,
  "network_size": 43,
  "is_valid_referrer": true,
  "joined_at": "2026-03-15T10:30:00Z"
}
```

| Field | Meaning |
|---|---|
| `level` | Current commission tier (1–15) |
| `self_purchase` / `self_purchase_tokens` | USD / XFEE this user bought for themselves |
| `total_sales_usd` / `total_tokens_sold` | USD / XFEE bought by anyone in this user's downline tree |
| `total_commission_sol` | All commissions ever earned in SOL |
| `level_sales` / `level_commission` | Breakdowns keyed by ancestor tier |
| `direct_sales_sol` / `indirect_sales_sol` | Sales from direct vs. deeper referrals |
| `direct_commission_sol` / `indirect_commission_sol` | Same split for commissions |
| `direct_referral_count` | Number of immediate downline wallets |
| `network_size` | Total recursive downline count |
| `is_valid_referrer` | `true` only after this user has completed ≥ 1 purchase; gates whether others can register under them |

`404` if the wallet hasn't been registered yet — use this to detect "new user, send to registration page".

### 7.3 `GET /api/v1/stats/global`

```json
{
  "tokens_sold": 12400,
  "tokens_remaining": null,
  "total_supply": null,
  "total_purchases": 843,
  "sol_price": 69.07
}
```

When the sale is unlimited (production default), `tokens_remaining` and `total_supply` are both `null`. Display "Tokens sold: 12,400" and skip any "remaining"/progress-bar UI in that case.

### 7.4 `GET /api/v1/purchase/estimate?xfee_amount=N`

```json
{
  "xfee_amount": 100,
  "token_cost_usd": 100.0,
  "gas_buffer_usd": 2.45,
  "total_usd": 102.45,
  "sol_price_usd": 69.07,
  "sol_needed": 1.483
}
```

Use this to pre-display the SOL cost before the user commits. The gas buffer is small and intentionally varied. `sol_needed` is the exact figure to forward into `purchase/initiate`. The same calculation runs server-side on `initiate`, so the final figure may differ by a few cents if the SOL price moves between the two calls.

### 7.5 `POST /api/v1/purchase/initiate`

```json
// Request
{ "wallet_address": "BuyerPublicKey…", "xfee_amount": 100 }
```

```json
// 200 Success
{
  "purchase_id": "6a3a5ebda3c6077001d1673d",
  "purchase_wallet": "G6QcpHmgFMXcmxRHN2ExMVBgf6xdNCxwvmJ2iiwomQ13",
  "sol_expected": 1.483,
  "expires_at": "2026-06-23T22:15:00Z"
}
```

| Status | Body | Frontend action |
|---|---|---|
| 200 | — | Send `sol_expected` SOL to `purchase_wallet` |
| 400 | `xfee_amount must be positive` | Validation |
| 404 | `User not registered` | Send user to registration first |
| 409 | `You already have an active pending purchase…` | The user has an unfinished purchase; surface its `purchase_id` (fetch `/user/{wallet}/purchases?page=1&limit=1`) and let them either complete it or wait 15 min for expiry |
| 503 | `No purchase wallets available, try again shortly` | Backend pool is replenishing; auto-retry in 3–5s |

**Important:** `purchase_wallet` is a one-time ephemeral wallet that the server pre-generated and locked specifically for this purchase. **Do not reuse the same purchase_wallet across multiple purchases.** It's discarded after 15 minutes.

### 7.6 Send SOL from the user's wallet

Use Phantom (or any Solana wallet adapter) to transfer exactly `sol_expected` SOL to `purchase_wallet`:

```typescript
import {
  Connection,
  PublicKey,
  Transaction,
  SystemProgram,
  LAMPORTS_PER_SOL,
} from "@solana/web3.js";

const MAINNET_RPC = "https://api.mainnet-beta.solana.com"; // use your own RPC for production
const connection = new Connection(MAINNET_RPC, "confirmed");

async function payForPurchase(
  wallet: SolanaWalletAdapter,
  purchaseWallet: string,
  solExpected: number,
) {
  const lamports = Math.ceil(solExpected * LAMPORTS_PER_SOL);
  const recipient = new PublicKey(purchaseWallet);

  const tx = new Transaction().add(
    SystemProgram.transfer({
      fromPubkey: wallet.publicKey!,
      toPubkey: recipient,
      lamports,
    }),
  );

  const { blockhash } = await connection.getLatestBlockhash("finalized");
  tx.recentBlockhash = blockhash;
  tx.feePayer = wallet.publicKey!;

  const signature = await wallet.sendTransaction(tx, connection);
  await connection.confirmTransaction(signature, "confirmed");
  return signature;
}
```

It's safe to send slightly more than `sol_expected` (e.g., to round up to a clean number). The backend only needs at least `sol_expected`; any overage stays in the system. **Don't send less** — the backend will treat the purchase as expired if the funds never arrive in full within 15 min.

### 7.7 `GET /api/v1/purchase/{purchase_id}` — poll until done

Poll this every 5 seconds:

```json
{
  "id": "6a3a5ebda3c6077001d1673d",
  "user_wallet": "BuyerPublicKey…",
  "purchase_wallet_pubkey": "G6QcpHmg…",
  "xfee_amount": 100,
  "sol_amount_expected": 1.483,
  "sol_amount_received": 1.483,
  "sol_price_at_confirmation": 69.07,
  "status": "completed",
  "created_at": "2026-06-23T10:00:00Z",
  "expires_at": "2026-06-23T10:15:00Z",
  "confirmed_at": "2026-06-23T10:00:08Z",
  "token_dispatch_tx": null,
  "commission_distributed": true
}
```

#### `status` values

| Status | Meaning | Frontend action |
|---|---|---|
| `pending` | Purchase created, awaiting SOL | Show countdown to `expires_at`; keep polling |
| `completed` | SOL received, commissions distributed | Show success page; refresh `/user/{wallet}` for updated dashboard |
| `expired` | 15 min passed, no full SOL received | Show "expired" message, offer to retry |
| `failed` | Edge case — usually means a downstream tx failed; rare | Show error, instruct user to contact support |

#### Notes

- `commission_distributed: true` means commissions were paid out on-chain. The full ancestor list can be fetched via `/user/{wallet}/allocs` (queried per recipient).
- `token_dispatch_tx` is **currently `null` in the launch configuration** because POWER token staking is disabled at launch. When staking is enabled later, this will hold the on-chain signature for the POWER stake.
- Poll interval suggestion: 5 s during `pending`, exponential backoff to 15 s after 90 s elapsed.

```typescript
async function pollUntilDone(purchaseId: string, onUpdate: (p: any) => void) {
  for (;;) {
    const res = await fetch(`${API}/api/v1/purchase/${purchaseId}`);
    const p = await res.json();
    onUpdate(p);
    if (p.status !== "pending") return p;
    await new Promise((r) => setTimeout(r, 5000));
  }
}
```

### 7.8 `GET /api/v1/user/{wallet}/allocs`

The recipient view of commissions and zero-allocs.

```json
{
  "items": [
    {
      "id": "6a3a5ec4a3c6077001d16740",
      "purchase_id": "6a3a5ebda3c6077001d1673d",
      "recipient_wallet": "…",
      "sol_amount": 0.137799,
      "sale_usd": 10.0,
      "sale_tokens": 10,
      "alloc_type": "commission",
      "ancestor_level_tier": 14,
      "differential_rate": 0.95,
      "on_chain_tx": "bqqPXMN5cEKrtazfdkpmWcRBKXrHENRGxBwsdK4dJMnFQpP45qWSDypJqubxjxKDBefs99iqe1ep2QLkLbfgTLR",
      "status": "sent",
      "indexed": true,
      "created_at": "2026-06-23T10:24:02.772000"
    }
  ],
  "total": 12,
  "page": 1,
  "limit": 20
}
```

| Field | Meaning |
|---|---|
| `alloc_type` | `commission` (normal payout), `master_sweep` (rare residual sweep) |
| `sale_usd` / `sale_tokens` | USD / XFEE of the purchase that triggered this alloc |
| `differential_rate` | Percentage of the sale this user actually received (0 = zero-alloc) |
| `ancestor_level_tier` | The tier-rank this user occupied at the moment of distribution |
| `on_chain_tx` | Solana tx signature (null when `sol_amount` is 0). Link: `https://solscan.io/tx/{on_chain_tx}` |
| `status` | `sent` (confirmed on-chain), `failed` (transient — retry pending), `zero` (in tree but received nothing) |

To display "my earnings": filter rows where `sol_amount > 0` and link `on_chain_tx`. Zero-alloc rows (`differential_rate == 0`, `on_chain_tx == null`) become the user's global-pool points and should be displayed under the "Global Pool" section rather than the earnings list.

### 7.9 `GET /api/v1/user/{wallet}/tree`

```json
{
  "wallet": "UserA…",
  "level": 3,
  "children": [
    {
      "wallet": "UserB…",
      "level": 1,
      "children": [
        { "wallet": "UserC…", "level": 1, "children": [] }
      ]
    }
  ]
}
```

Tree is capped at 5 levels deep. Use this for a visual org chart. For per-child stats (sales, commissions), pair with `/user/{wallet}/directs` which returns the full `UserResponse` for every immediate child.

### 7.10 Global pool endpoints

#### `GET /api/v1/global-pool/summary`

```json
{
  "total_pools": 4,
  "active": 1,
  "in_progress": 0,
  "settled": 3,
  "total_points_usd_all_pools": 12500.45,
  "settlement_counts": {
    "confirmed": { "total_owed_lamports": 9800000000, "count": 128 },
    "pending":   { "total_owed_lamports": 0, "count": 0 }
  },
  "current_pool_index": 4
}
```

#### `GET /api/v1/global-pool/current`

The active 15-day pool plus live standings (paginated). Use for "what's the current pool look like" widget.

```json
{
  "active": true,
  "pool": {
    "pool_index": 4,
    "start_at": "2026-06-08T00:00:00+00:00",
    "end_at": "2026-06-23T00:00:00+00:00",
    "status": "active",
    "total_points_usd": 320.5
  },
  "standings": [
    {
      "id": "…",
      "pool_index": 4,
      "wallet_address": "…",
      "points_usd": 120.0,
      "event_count": 3,
      "settle_status": "pending"
    }
  ],
  "total_users": 12,
  "page": 1,
  "limit": 50
}
```

#### `GET /api/v1/global-pool/user/{wallet}/points`

The user's pool history across all pools. Use this for the user's "Global Pool History" tab.

```json
{
  "wallet_address": "…",
  "items": [
    {
      "pool_index": 3,
      "points_usd": 45.5,
      "owed_lamports": 1450000,
      "owed_sol": 0.00145,
      "settle_status": "confirmed",
      "tx_signature": "5xHFa…",
      "memo": "GP:3:a1b2c3d4e5f6:abcdef12"
    }
  ],
  "total": 1,
  "page": 1,
  "limit": 20
}
```

#### `settle_status` values

| Status | Meaning | UI label suggestion |
|---|---|---|
| `pending` | Pool snapshot taken, payout not yet attempted | "Queued" |
| `sending` | Tx broadcast in progress (transient) | "Settling…" |
| `sent` | Tx broadcast successful, awaiting confirmation | "Settling…" |
| `confirmed` | Tx confirmed on-chain — claim is paid | "Paid" + Solscan link |
| `failed` | Last attempt errored — worker will retry automatically | "Retrying…" |
| `skipped_zero` | Computed owed lamports rounded to zero | "Below dust" |

Global pool settlement is **fully automated server-side**. Pools auto-finalize 15 days after they open, payouts happen from the funding wallet, and the worker reconciles via on-chain SPL Memos (`GP:{pool_index}:{settlement_id}:{wallet_prefix}`) so double-payment is impossible even across crashes. The frontend doesn't trigger anything — it just renders the status.

### 7.11 `POST /api/v1/admin/set-user-level` (operator panel only)

Two auth modes — either works:

**Mode A — admin API key** (server-set secret, used by operator backend):

```bash
curl -sS -X POST "$API/api/v1/admin/set-user-level" \
  -H "X-Admin-Key: $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"wallet_address":"…", "level": 5}'
```

**Mode B — master wallet signature** (used when the master-wallet holder runs an operator UI in the browser):

```typescript
import bs58 from "bs58";

const message = `set-user-level:${userWallet}:${newLevel}`;
const encoded = new TextEncoder().encode(message);
const { signature } = await window.solana.signMessage(encoded, "utf8");
const sigB58 = bs58.encode(signature);

await fetch(`${API}/api/v1/admin/set-user-level`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    wallet_address: userWallet,
    level: newLevel,
    signature: sigB58,
  }),
});
```

Rules:
- Levels go up only, never down. Backend returns `400` on a downgrade attempt.
- Range is 1–15, but reserve 14/15 for root-child/master.
- Without the `X-Admin-Key` header **and** without a valid `signature`, the call returns `401`.

---

## 8. Error model

All errors share the same shape:

```json
{ "detail": "Human-readable message" }
```

| Code | Meaning | Typical UI |
|---|---|---|
| 200 | Success | — |
| 400 | Validation failure | Show `detail` inline |
| 401 | Admin auth failed | Operator UI only |
| 404 | Resource not found | Send to registration or "not found" page |
| 409 | Conflict (already registered / pending purchase) | Treat as benign (re-route to dashboard) or surface the active purchase |
| 422 | Body schema invalid | Bug — log and report |
| 503 | Wallet pool replenishing | Auto-retry in 3–5 s |

---

## 9. End-to-end example (TypeScript)

```typescript
const API = "https://brucelee-app-sale-cbsgj.ondigitalocean.app";

// 1. Get the referrer from the URL
const referrer = new URLSearchParams(location.search).get("ref");
if (!referrer) throw new Error("Missing referral link");

// 2. Connect wallet (Phantom)
await window.solana.connect();
const buyerPubkey = window.solana.publicKey.toString();

// 3. Register (idempotent — 409 means already registered, which is fine)
const regRes = await fetch(`${API}/api/v1/user/register`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ wallet_address: buyerPubkey, referrer_wallet: referrer }),
});
if (![200, 409].includes(regRes.status)) {
  const err = await regRes.json();
  throw new Error(err.detail);
}

// 4. Show user profile
const me = await (await fetch(`${API}/api/v1/user/${buyerPubkey}`)).json();
renderDashboard(me);

// 5. Buy 50 XFEE
const xfee_amount = 50;
const init = await (
  await fetch(`${API}/api/v1/purchase/initiate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ wallet_address: buyerPubkey, xfee_amount }),
  })
).json();
// init = { purchase_id, purchase_wallet, sol_expected, expires_at }

// 6. Have Phantom send the SOL
const sig = await payForPurchase(window.solana, init.purchase_wallet, init.sol_expected);
console.log("buyer-side tx:", `https://solscan.io/tx/${sig}`);

// 7. Poll
const final = await pollUntilDone(init.purchase_id, (p) => renderStatus(p));
if (final.status === "completed") {
  renderSuccess(final);
  const fresh = await (await fetch(`${API}/api/v1/user/${buyerPubkey}`)).json();
  renderDashboard(fresh);
}
```

---

## 10. Recommended pages for the customer UI

| Page | Endpoints used |
|---|---|
| Landing / hero | `/stats/global`, `/stats/levels` |
| Invite required | (none — gate before connect) |
| Connect & register | `/user/register`, `/user/{wallet}` |
| Dashboard | `/user/{wallet}`, `/user/{wallet}/allocs?limit=5`, `/stats/global` |
| Buy XFEE | `/purchase/estimate`, `/purchase/initiate`, `/purchase/{id}` (poll) |
| Purchase history | `/user/{wallet}/purchases` |
| Commissions / earnings | `/user/{wallet}/allocs` |
| Network / referrals | `/user/{wallet}/directs`, `/user/{wallet}/tree` |
| Global pool — current | `/global-pool/current` |
| Global pool — my history | `/global-pool/user/{wallet}/points` |
| Share invite | (build URL from buyer pubkey) |

---

## 11. Quick reference card

```
BASE             https://brucelee-app-sale-cbsgj.ondigitalocean.app
MASTER           DXSEB4WrtfSFvD6ZKvyiyg9GDnEgmc6uAPpkHHQBNwFB
ROOT_CHILD       BRrtYftGhXBh3JcwmveuB4ZcskkYvUeLzNgPcf5VF6Ry
PRICE            1 XFEE = $1 USD (always)
SOL ORACLE       live via /api/v1/stats/global → sol_price
NETWORK          mainnet-beta
SOLSCAN          https://solscan.io/tx/{signature}
PURCHASE TIMEOUT 15 minutes
CORS             open ("*")
RATE LIMIT       none
AUTH             none (wallet pubkey only)
```
