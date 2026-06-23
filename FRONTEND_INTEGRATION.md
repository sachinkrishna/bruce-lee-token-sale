# XFEE Token Sale — Frontend Integration Guide

**Base URL:** `https://your-api-domain.com/api/v1`

**Swagger UI:** `https://your-api-domain.com/docs`

---

## Authentication

There is no JWT/session auth. Users are identified by their **Solana wallet address**. The frontend should connect the user's wallet (Phantom, Solflare, etc.) and use the public key as the identifier for all API calls.

---

## Core User Flow

```
1. Connect wallet
2. Register (with referrer link)
3. View dashboard
4. Buy XFEE tokens
5. View commissions & referral tree
```

---

## Endpoints

### 1. Register User

**`POST /user/register`**

Call this once when a new user connects their wallet for the first time. The referrer wallet comes from the referral link (e.g. `?ref=WALLET_ADDRESS`).

```json
// Request
{
  "wallet_address": "UserPublicKey...",
  "referrer_wallet": "ReferrerPublicKey..."
}

// Success (200)
{
  "success": true,
  "wallet_address": "UserPublicKey..."
}

// Errors
// 400 — Invalid Solana address
// 400 — Referrer not found
// 400 — Referrer has not completed a purchase yet
// 409 — Wallet already registered
```

**Frontend notes:**
- If the user has no referral link, use the master wallet address as the referrer.
- A referrer must have completed at least one purchase (`is_valid_referrer: true`).
- On 409 (already registered), proceed to dashboard — the user exists.

---

### 2. Get User Profile

**`GET /user/{wallet_address}`**

Use this to populate the dashboard.

```json
// Response (200)
{
  "wallet_address": "...",
  "level": 2,
  "self_purchase": 500.0,        // USD value of own purchases
  "total_sales_usd": 12400.0,    // USD value of all network sales
  "total_commission_sol": 1.245,  // Total SOL earned as commission
  "self_purchase_tokens": 50,    // Own XFEE tokens purchased
  "total_tokens_sold": 250,     // Total XFEE tokens sold in network (from allocs)
  "direct_referral_count": 5,    // Number of direct referrals
  "network_size": 43,            // Total people in downline
  "is_valid_referrer": true,     // Can others use this wallet as referrer
  "joined_at": "2026-03-15T10:30:00Z"
}
```

**Frontend notes:**
- Call this on dashboard load and after any purchase completes.
- `level` determines commission rate (L1=20%, L2=22%, L3=24%, L4=26%, L5=28%, L6=30%, L7=32%, L8=34%, L9=36%, L10=40%, L11=45%, L12=60%, L13=75%, L14=95%, L15=100%).
- Display `self_purchase` separately from `total_sales_usd` — they're different metrics.

---

### 3. Get Global Stats

**`GET /stats/global`**

Public endpoint, no wallet needed. Use for landing page / progress bar.

```json
// Response (200)
{
  "tokens_sold": 210000,
  "tokens_remaining": 190000,
  "total_purchases": 843,
  "sol_price": 185.42
}
```

---

### 4. Buy XFEE Tokens (Purchase Flow)

This is a **3-step process** from the frontend's perspective:

#### Step 1: Initiate Purchase

**`POST /purchase/initiate`**

```json
// Request
{
  "wallet_address": "UserPublicKey...",
  "xfee_amount": 150
}

// Success (200)
{
  "purchase_id": "6654abc...",
  "purchase_wallet": "EphemeralWalletPubkey...",
  "sol_expected": 2.036667,
  "expires_at": "2026-03-29T22:15:00Z"
}

// Errors
// 400 — xfee_amount must be positive
// 400 — Insufficient supply
// 404 — User not registered
// 409 — You already have an active pending purchase
```

**Frontend notes:**
- `sol_expected` includes a gas buffer. Display this as the amount to send.
- `purchase_wallet` is a one-time ephemeral wallet. The user sends SOL to this address.
- `expires_at` is 15 minutes from now. Show a countdown timer.
- Only one pending purchase per wallet at a time.

#### Step 2: Prompt User to Send SOL

After receiving the response, prompt the user to send **exactly `sol_expected` SOL** (or more) to the `purchase_wallet` address using their connected wallet (Phantom `signAndSendTransaction`).

```typescript
// Example with @solana/web3.js
const transaction = new Transaction().add(
  SystemProgram.transfer({
    fromPubkey: userWallet.publicKey,
    toPubkey: new PublicKey(purchaseWallet),
    lamports: Math.ceil(solExpected * LAMPORTS_PER_SOL),
  })
);
await wallet.sendTransaction(transaction, connection);
```

#### Step 3: Poll for Completion

**`GET /purchase/{purchase_id}`**

Poll this endpoint every 5 seconds until `status` changes from `"pending"`.

```json
// Response (200)
{
  "id": "6654abc...",
  "user_wallet": "...",
  "purchase_wallet_pubkey": "...",
  "xfee_amount": 150,
  "sol_amount_expected": 2.036667,
  "sol_amount_received": 2.036667,
  "sol_price_at_confirmation": 185.42,
  "status": "completed",           // "pending" | "completed" | "expired" | "failed"
  "created_at": "2026-03-29T22:00:00Z",
  "expires_at": "2026-03-29T22:15:00Z",
  "confirmed_at": "2026-03-29T22:01:23Z",
  "token_dispatch_tx": "5UxR7...",  // Solana tx signature for token transfer
  "commission_distributed": true
}
```

**Status meanings:**

| Status | What happened | Frontend action |
|--------|--------------|-----------------|
| `pending` | Waiting for SOL | Keep polling, show countdown |
| `completed` | SOL received, XFEE sent, commissions distributed | Show success, link to `token_dispatch_tx` on Solscan |
| `expired` | 15 min passed, no SOL received | Show "expired" message, let user try again |
| `failed` | SOL received but token dispatch failed | Show error, tell user to contact support |

**Polling example:**

```typescript
const pollPurchase = async (purchaseId: string) => {
  const interval = setInterval(async () => {
    const res = await fetch(`${BASE}/purchase/${purchaseId}`);
    const data = await res.json();

    if (data.status === "completed") {
      clearInterval(interval);
      showSuccess(data.token_dispatch_tx);
    } else if (data.status === "expired" || data.status === "failed") {
      clearInterval(interval);
      showError(data.status);
    }
  }, 5000);
};
```

---

### 5. Purchase History

**`GET /user/{wallet_address}/purchases?page=1&limit=20`**

```json
// Response (200)
{
  "items": [ /* array of PurchaseResponse objects */ ],
  "total": 5,
  "page": 1,
  "limit": 20
}
```

---

### 6. Commission History

**`GET /user/{wallet_address}/allocs?page=1&limit=20`**

```json
// Response (200)
{
  "items": [
    {
      "id": "...",
      "purchase_id": "...",
      "recipient_wallet": "...",
      "sol_amount": 0.2036,         // SOL earned (0 for zero-commission allocs)
      "sale_usd": 300.0,            // USD value of the purchase that triggered this
      "alloc_type": "commission",    // "commission" or "master_sweep"
      "ancestor_level_tier": 2,
      "differential_rate": 0.04,
      "on_chain_tx": "4xKm...",     // Solana tx signature (null for zero allocs)
      "status": "sent",             // "sent" | "failed" | "zero"
      "indexed": true,
      "created_at": "2026-03-29T22:01:25Z"
    }
  ],
  "total": 12,
  "page": 1,
  "limit": 20
}
```

**Frontend notes:**
- Filter by `alloc_type == "commission"` to show only commission earnings.
- `status: "zero"` means this ancestor was in the tree but didn't qualify for a differential commission on this purchase.
- `on_chain_tx` can be linked to Solscan: `https://solscan.io/tx/{on_chain_tx}`

---

### 7. Direct Referrals

**`GET /user/{wallet_address}/directs`**

Returns full profile of every direct referral.

```json
// Response (200)
[
  {
    "wallet_address": "...",
    "level": 1,
    "self_purchase": 200.0,
    "total_sales_usd": 0.0,
    "total_commission_sol": 0.0,
    "total_tokens_sold": 100,
    "direct_referral_count": 3,
    "network_size": 7,
    "is_valid_referrer": true,
    "joined_at": "2026-03-20T14:00:00Z"
  }
]
```

---

### 8. Referral Tree

**`GET /user/{wallet_address}/tree`**

Returns nested tree up to 5 levels deep.

```json
// Response (200)
{
  "wallet": "UserA...",
  "level": 3,
  "children": [
    {
      "wallet": "UserB...",
      "level": 1,
      "children": [
        {
          "wallet": "UserC...",
          "level": 1,
          "children": []
        }
      ]
    }
  ]
}
```

**Frontend notes:**
- Render as a visual tree / org chart.
- Max depth is 5 levels to prevent timeouts on large networks.
- Use `/directs` for the flat list with full stats, use `/tree` for the visual hierarchy.

---

### 9. Set User Level (Master Wallet Only — Signature Required)

**`POST /user/set-user-level`**

The master wallet can upgrade any user's level. **This endpoint requires the master wallet to sign a message** to prove ownership.

#### Signing flow (frontend — master wallet admin panel):

```typescript
// 1. Build the message the backend expects
const message = `set-user-level:${userWallet}:${newLevel}`;

// 2. Sign it with the master wallet (Phantom example)
const encodedMessage = new TextEncoder().encode(message);
const { signature } = await window.solana.signMessage(encodedMessage, "utf8");

// 3. Convert signature to base58
import bs58 from "bs58";
const signatureB58 = bs58.encode(signature);

// 4. Send the request
const res = await fetch(`${BASE}/user/set-user-level`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    wallet_address: userWallet,
    level: newLevel,
    signature: signatureB58,
  }),
});
```

```json
// Request
{
  "wallet_address": "UserPubkey...",
  "level": 3,
  "signature": "base58EncodedSignature..."
}

// Success (200)
{
  "success": true,
  "wallet_address": "...",
  "previous_level": 1,
  "new_level": 3
}

// Errors
// 400 — Level must be between 1 and 15
// 400 — User is already at level X. Can only upgrade to a higher level
// 401 — Invalid master wallet signature
// 404 — User not found
```

**Frontend notes:**
- Only show this option next to direct referrals.
- Levels can only go up, never down.
- The dropdown should only show levels higher than the child's current level.
- The **signature** field is mandatory — the backend verifies that the parent wallet owner actually signed the request.
- In **test mode**, use `"test_signature"` as the signature value to bypass verification.

---

## Commission Level Reference

Display this in the user's dashboard:

| Level | Commission Rate | Network Sales Required |
|-------|----------------|----------------------|
| 1     | 10%            | $0 (default)         |
| 2     | 14%            | $2,000               |
| 3     | 18%            | $4,000               |
| 4     | 22%            | $12,000              |
| 5     | 26%            | $25,000              |
| 6     | 30%            | $50,000              |
| 7     | 35%            | $5,000,000           |
| 8     | 40%            | $10,000,000          |

- Level is determined by `total_sales_usd` (network sales volume) OR manual upgrade by parent — whichever is higher.
- Commission is **differential**: each ancestor only earns the difference between their rate and the highest rate already paid below them.

---

## Referral Link Format

Suggested format: `https://yoursite.com/?ref=WALLET_ADDRESS`

On load, extract the `ref` parameter and pass it as `referrer_wallet` during registration.

```typescript
const urlParams = new URLSearchParams(window.location.search);
const referrer = urlParams.get("ref") || MASTER_WALLET_ADDRESS;
```

---

## Error Handling

All errors return:

```json
{
  "detail": "Human-readable error message"
}
```

| Status Code | Meaning |
|-------------|---------|
| 400 | Validation error (bad input, invalid address, insufficient supply) |
| 403 | Forbidden (e.g. setting level for non-direct referral) |
| 404 | Not found (user, purchase) |
| 409 | Conflict (duplicate registration, active pending purchase) |
| 503 | No purchase wallets available (temporary, retry in a few seconds) |

---

## Global Pool Points

Global pools run in consecutive 15-day windows from the first completed sale. Points are earned when a user is in the tree but receives a zero commission alloc because a same-level peer already received that level's differential commission in the sale.

**Settlement model:** No on-chain program. When a pool finalizes, the backend transfers each user's owed SOL directly from the configured funding wallet (typically the top/master wallet). Every payout includes an SPL Memo so the backend can reconcile and never double-pay even across restarts.

Pool status transitions:

```
active -> ready_to_settle -> settling -> settled
```

Per-user settlement status (`settle_status`):

```
pending     - point row created, pool not yet settled
sending     - tx broadcast in progress (transient)
sent        - tx broadcast successfully, awaiting confirmation
confirmed   - tx confirmed on-chain
failed      - last attempt errored; will be retried by worker / admin retry
skipped_zero - owed lamports rounded to 0
```

All pool data is fully queryable for the current and previous pools.

### `GET /api/v1/global-pool/summary`

System-wide stats across all pools.

```json
{
  "total_pools": 4,
  "active": 1,
  "in_progress": 0,
  "settled": 3,
  "total_points_usd_all_pools": 12500.45,
  "settlement_counts": {
    "pending":   { "total_owed_lamports": 0, "count": 0 },
    "confirmed": { "total_owed_lamports": 9800000000, "count": 128 },
    "failed":    { "total_owed_lamports": 0, "count": 0 }
  },
  "current_pool_index": 4
}
```

### `GET /api/v1/global-pool/`

List all pools (paginated, newest first).

Query params:
- `status` (optional): `active` | `ready_to_settle` | `settling` | `settled`
- `page` (default 1)
- `limit` (default 20, max 200)

Response:

```json
{
  "items": [
    {
      "_id": "...",
      "pool_index": 4,
      "status": "active",
      "start_at": "2026-06-08T00:00:00+00:00",
      "end_at": "2026-06-23T00:00:00+00:00",
      "total_points_usd": 320.5,
      "user_count": 12,
      "onchain": null
    }
  ],
  "total": 4,
  "page": 1,
  "limit": 20
}
```

### `GET /api/v1/global-pool/current`

Returns the active pool, live standings (paginated), and total user count.

Query params: `page` (default 1), `limit` (default 50, max 500).

Response:

```json
{
  "active": true,
  "pool": { "pool_index": 4, "status": "active", "total_points_usd": 320.5, "start_at": "...", "end_at": "..." },
  "standings": [
    {
      "id": "...",
      "pool_index": 4,
      "wallet_address": "...",
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

### `GET /api/v1/global-pool/{pool_index}`

Same shape as `/current` but for any past or current pool. Once settled, `pool.snapshot` includes `funding_balance_lamports`, `distributable_lamports`, `funding_wallet`, `settlement_id`, and `total_users`; `pool.settlement` includes `started_at`, `completed_at`, and lock metadata.

Query params: `page`, `limit` (default 100, max 1000).

### `GET /api/v1/global-pool/{pool_index}/user/{wallet_address}`

A user's full standing in a single pool, including final settlement status once paid out.

Response when the user participated:

```json
{
  "pool": { "pool_index": 3, "status": "settled", "snapshot": { "distributable_lamports": 99000000000, "funding_wallet": "..." } },
  "wallet_address": "...",
  "in_pool": true,
  "entry": {
    "id": "...",
    "pool_index": 3,
    "wallet_address": "...",
    "points_usd": 45.5,
    "event_count": 2,
    "owed_lamports": 1450000,
    "owed_sol": 0.00145,
    "settle_status": "confirmed",
    "tx_signature": "5xHFa...",
    "memo": "GP:3:a1b2c3d4e5f6:abcdef12",
    "sent_at": "2026-06-23T01:02:03+00:00",
    "confirmed_at": "2026-06-23T01:02:08+00:00",
    "attempts": 1
  }
}
```

When the user wasn't in that pool: `in_pool: false`, `entry: null`.

### `GET /api/v1/global-pool/user/{wallet_address}/points`

The user's full history across all pools (paginated). Use this for the user's pool history page.

Query params: `page` (default 1), `limit` (default 20, max 200).

Response:

```json
{
  "wallet_address": "...",
  "items": [
    {
      "pool_index": 3,
      "points_usd": 45.5,
      "owed_lamports": 1450000,
      "owed_sol": 0.00145,
      "settle_status": "confirmed",
      "tx_signature": "5xHFa...",
      "memo": "GP:3:a1b2c3d4e5f6:abcdef12"
    }
  ],
  "total": 1,
  "page": 1,
  "limit": 20
}
```

### `settle_status` values

- `pending` - settlement snapshot taken, payout not yet attempted
- `sending` - tx broadcast in progress (transient state, normally short-lived)
- `sent` - tx broadcast successful, awaiting on-chain confirmation
- `confirmed` - tx confirmed on-chain
- `failed` - last attempt errored (RPC, balance, etc.); worker will retry
- `skipped_zero` - computed owed lamports rounded to zero

### Admin Settle

`POST /api/v1/admin/global-pool/{pool_index}/settle?force=false`

The background worker settles ended pools automatically. Admin can call this endpoint to retry/resume a failed settlement or force-settle the active pool early with `force=true`. Idempotent — safe to call multiple times; on-chain memo lookups prevent double-payout.

`POST /api/v1/admin/global-pool/process-due`

Manually trigger the worker scan for all pools whose window has ended.

### Idempotency / double-spend guarantees

Each payout transaction includes an SPL Memo of the form `GP:{pool_index}:{settlement_id}:{wallet_prefix}`. Before sending, the backend:

1. Checks the DB row's `settle_status`; skips if already `confirmed`.
2. Confirms any persisted `tx_signature` first.
3. Scans recent funding-wallet signatures for the unique memo; if found, marks as `confirmed` without re-sending.

This makes settlement safe to restart after crashes or worker interruptions.

---

## Solscan Links

For any transaction signature returned by the API:

- **Mainnet:** `https://solscan.io/tx/{signature}`
- **Devnet:** `https://solscan.io/tx/{signature}?cluster=devnet`

Use these to let users verify their token transfers and commission payouts on-chain.
