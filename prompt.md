# XFEE Token Sale Backend — Cursor Prompt

## SYSTEM OVERVIEW

Build a **production-ready FastAPI backend** for an XFEE token sale system on the Solana blockchain. Users purchase XFEE tokens using SOL. The system includes a **multi-level differential commission plan**, **ephemeral purchase wallets**, **on-chain SOL distribution**, **SPL token dispatch**, and a **stats indexer with relationship tree support**.

---

## TECH STACK

- **Framework:** FastAPI (async throughout — use `async def` everywhere)
- **Database:** MongoDB via `motor` (async MongoDB driver)
- **Solana SDK:** `solders` + `solana-py` (`solana` pip package)
- **SPL Tokens:** `spl-token` via `solana-py`
- **Scheduler/Background:** `asyncio` tasks + `APScheduler` for polling loops
- **Config:** `pydantic-settings` with `.env` file
- **Deployment:** DigitalOcean App Platform (single Dockerfile)

---

## ENVIRONMENT VARIABLES (`.env`)

```env
MONGO_URI=
MONGO_DB_NAME=xfee_sale

MASTER_WALLET_ADDRESS=        # SOL receiving wallet (public key)
MASTER_WALLET_PRIVATE_KEY=    # base58 encoded private key

TREASURY_WALLET_ADDRESS=      # holds XFEE SPL tokens
TREASURY_WALLET_PRIVATE_KEY=  # base58 encoded

XFEE_TOKEN_MINT=              # SPL token mint address

QUICKNODE_RPC_URL=            # premium QuickNode endpoint
SOL_PRICE_API_URL=            # custom endpoint, returns {"price": 185.42} (USD per SOL)

XFEE_PRICE_USD=2.00           # price per XFEE token in USD
XFEE_TOTAL_SUPPLY=400000      # hard cap

PURCHASE_WALLET_EXPIRY_MINUTES=15
PURCHASE_MIN_USD=6.00         # minimum incoming SOL value in USD to accept
GAS_BUFFER_USD=5.00           # extra SOL added to expected amount for gas
LEAVE_IN_PURCHASE_WALLET_USD=4.50  # SOL to leave in purchase wallet post-sweep
```

---

## MONGODB COLLECTIONS & SCHEMAS

### `users`

```json
{
  "_id": "ObjectId",
  "wallet_address": "string (Solana pubkey, indexed unique)",
  "referrer_wallet": "string (Solana pubkey)",
  "level": 1,
  "is_valid_referrer": false,
  "joined_at": "datetime",
  "total_sales_usd": 0.0,
  "total_commission_sol": 0.0,
  "total_tokens_sold": 0,
  "direct_referral_count": 0,
  "network_size": 0
}
```

- `level` is computed from `total_sales_usd` using the qualification thresholds (see COMMISSION PLAN)
- `is_valid_referrer` flips to `true` after their first completed purchase

### `purchase_wallets`

```json
{
  "_id": "ObjectId",
  "public_key": "string",
  "private_key": "string (base58, NEVER returned in API responses)",
  "assigned_to_purchase": "ObjectId | null",
  "status": "free | locked | used",
  "created_at": "datetime"
}
```

- Pre-generate a pool of 50 purchase wallets on startup if fewer than 20 `free` ones exist
- When a purchase is initiated, lock one wallet to the purchase for 15 mins
- After purchase completes or expires, mark as `used` (do not reuse — generate fresh ones to top up pool)

### `purchases`

```json
{
  "_id": "ObjectId",
  "user_wallet": "string",
  "purchase_wallet_pubkey": "string",
  "xfee_amount": 150,
  "sol_amount_expected": 1.623,
  "sol_amount_received": 0.0,
  "sol_price_at_confirmation": 0.0,
  "status": "pending | completed | expired | failed",
  "created_at": "datetime",
  "expires_at": "datetime",
  "confirmed_at": "datetime | null",
  "token_dispatch_tx": "string | null",
  "commission_distributed": false
}
```

### `allocs`

```json
{
  "_id": "ObjectId",
  "purchase_id": "ObjectId",
  "recipient_wallet": "string",
  "sol_amount": 0.045,
  "alloc_type": "commission | master_sweep",
  "ancestor_level_tier": 2,
  "differential_rate": 0.04,
  "on_chain_tx": "string | null",
  "status": "pending | sent | failed",
  "created_at": "datetime"
}
```

One alloc document per wallet per commission event. Always one alloc for the master sweep too.

### `relationship_tree` (denormalized for fast tree queries)

```json
{
  "_id": "ObjectId",
  "wallet_address": "string (indexed unique)",
  "referrer_wallet": "string",
  "ancestors": ["wallet1", "wallet2", "..."],
  "depth": 3
}
```

`ancestors` is the full ordered path from direct parent up to root. Populated at registration time. Used for ancestor-walk commission logic.

### `transactions` (audit log)

```json
{
  "_id": "ObjectId",
  "purchase_id": "ObjectId",
  "tx_type": "token_dispatch | commission | master_sweep",
  "from_wallet": "string",
  "to_wallet": "string",
  "amount_sol": 0.0,
  "tx_signature": "string",
  "created_at": "datetime"
}
```

---

## COMMISSION PLAN — DIFFERENTIAL SYSTEM

### Qualification Levels (based on `total_sales_usd` of the ancestor)

| Level | Rate | Qualification (`total_sales_usd`) |
|-------|------|-----------------------------------|
| 1     | 20%  | $0                                |
| 2     | 22%  | $500                              |
| 3     | 24%  | $2,500                            |
| 4     | 26%  | $10,000                           |
| 5     | 28%  | $25,000                           |
| 6     | 30%  | $50,000                           |
| 7     | 32%  | $100,000                          |
| 8     | 34%  | $250,000                          |
| 9     | 36%  | $1,000,000                        |
| 10    | 40%  | $2,500,000                        |
| 11    | 45%  | $1,000,000,000                    |
| 12    | 60%  | $3,000,000,000                    |
| 13    | 75%  | $5,000,000,000                    |
| 14    | 95%  | $9,000,000,000                    |
| 15    | 100% | $10,000,000,000                   |

### Differential Distribution Logic

Total commission pool = **100% of commissionable SOL** (before sweep).

Walk the `ancestors` array of the purchasing user from closest (index 0 = direct parent) upward:

```python
highest_level_paid_so_far = 0

for each ancestor in ancestors:
    ancestor_level = get_level_from_sales(ancestor.total_sales_usd)

    if ancestor_level > highest_level_paid_so_far:
        differential_rate = rate(ancestor_level) - rate(highest_level_paid_so_far)
        commission_sol = total_sol_received * differential_rate
        # → create alloc for this ancestor
        # → transfer commission_sol on-chain to ancestor.wallet_address
        highest_level_paid_so_far = ancestor_level

    if highest_level_paid_so_far == 15:
        break  # max commission level reached, stop walking
```

Any **undistributed commission** (if the tree doesn't reach Level 15) goes to the master wallet as part of the sweep.

---

## API ENDPOINTS

### `POST /api/v1/user/register`

- **Body:** `{ "wallet_address": "...", "referrer_wallet": "..." }`
- Verifies referrer exists in DB AND `is_valid_referrer == true` (or equals master wallet address)
- Creates user at Level 1, `is_valid_referrer: false`
- Inserts `relationship_tree` entry: fetch referrer's ancestors, prepend referrer to get this user's ancestors
- **Returns:** `{ "success": true, "wallet_address": "..." }`

### `GET /api/v1/user/{wallet_address}`

- Returns user profile (no private keys, no internal IDs exposed)
- Fields: `wallet_address`, `level`, `total_sales_usd`, `total_commission_sol`, `total_tokens_sold`, `direct_referral_count`, `network_size`, `is_valid_referrer`, `joined_at`

### `POST /api/v1/purchase/initiate`

- **Body:** `{ "wallet_address": "...", "xfee_amount": 150 }`
- Validates: user exists, XFEE remaining supply sufficient, no active pending purchase for this user
- Fetches SOL price from `SOL_PRICE_API_URL`
- Calculates: `sol_needed = (xfee_amount * XFEE_PRICE_USD + GAS_BUFFER_USD) / sol_price`
- Locks a free purchase wallet from the pool
- Creates purchase document (`status: pending`, `expires_at: now + 15 min`)
- Starts background polling task for this purchase wallet
- **Returns:** `{ "purchase_id": "...", "purchase_wallet": "...", "sol_expected": 1.623, "expires_at": "..." }`
- **NEVER return private key in any response**

### `GET /api/v1/purchase/{purchase_id}`

- Returns purchase status, amounts, timestamps
- Poll-friendly endpoint for frontend

### `GET /api/v1/user/{wallet_address}/purchases`

- Paginated list of purchases for a user

### `GET /api/v1/user/{wallet_address}/allocs`

- Paginated list of commission allocs received by this wallet

### `GET /api/v1/user/{wallet_address}/tree`

- Returns the user's downline network as a nested tree (up to 5 levels deep)
- Shape: `{ "wallet": "...", "level": 2, "children": [ {...}, {...} ] }`
- Query from `relationship_tree` collection

### `GET /api/v1/stats/global`

- Returns: `{ "tokens_sold": 210000, "tokens_remaining": 190000, "total_purchases": 843, "sol_price": 185.42 }`

### `POST /api/v1/admin/pool/replenish`

- Internal endpoint (note where to add API key middleware)
- Manually trigger purchase wallet pool top-up

### `POST /api/v1/admin/reindex/{wallet_address}`

- Manually trigger indexer recomputation for a specific wallet

---

## PURCHASE WALLET POLLING (Background Task)

For each initiated purchase, spawn an `asyncio` task:

```python
async def poll_purchase_wallet(
    purchase_id: str,
    pubkey: str,
    expected_sol: float,
    expires_at: datetime
):
    sol_price = await get_sol_price()
    min_sol = PURCHASE_MIN_USD / sol_price

    while datetime.utcnow() < expires_at:
        balance_lamports = await rpc_get_balance(pubkey)
        balance_sol = balance_lamports / 1e9

        if balance_sol >= min_sol:
            await process_completed_purchase(purchase_id, balance_sol)
            return

        await asyncio.sleep(5)

    # Expired
    await mark_purchase_expired(purchase_id)
    await release_purchase_wallet(purchase_id)
```

Use QuickNode RPC `getBalance` JSON-RPC call directly via `httpx.AsyncClient` (do not block event loop).

---

## COMPLETED PURCHASE FLOW (`process_completed_purchase`)

Execute the following steps **in order**, with each step wrapped in try/except and logged:

1. **Mark purchase confirmed** in DB (`status: completed`, `confirmed_at: now`, `sol_amount_received`, `sol_price_at_confirmation`)
2. **Dispatch XFEE tokens** to buyer:
   - Build SPL token transfer from treasury wallet to buyer's associated token account
   - Create ATA for buyer if it doesn't exist
   - Sign with treasury wallet private key
   - Submit and confirm transaction, store tx signature in `purchases.token_dispatch_tx`
3. **Calculate commission pool:** `commission_pool_sol = commissionable_sol * 1.00`
4. **Walk ancestor tree** and distribute differential commissions (see commission logic above):
   - For each ancestor getting a commission: create alloc doc, transfer SOL on-chain from purchase wallet, store tx signature in alloc, log to `transactions`
5. **Calculate undistributed commission** = `commission_pool_sol` minus actually distributed
6. **Sweep remaining SOL to master wallet:**
   - `sweep_amount = balance_sol - distributed_commissions - LEAVE_IN_PURCHASE_WALLET_USD / sol_price`
   - Transfer from purchase wallet to master wallet
   - Create an alloc doc with `alloc_type: "master_sweep"` (includes undistributed commission)
   - Log to `transactions`
7. **Mark purchase wallet as `used`**
8. **Trigger indexer update** for the buyer and all ancestors who received commissions
9. **Mark `is_valid_referrer = true`** for buyer (first purchase completed)
10. **Top up wallet pool** if free wallet count drops below 20

All on-chain transfers from the purchase wallet must be signed using the purchase wallet's stored private key. Load it from MongoDB fresh at execution time — never cache private keys in memory.

---

## INDEXER

The indexer recomputes and upserts into the `users` collection:

```python
async def run_indexer_for_wallet(wallet_address: str):
    # total_sales_usd:
    #   sum of (sol_amount_received * sol_price_at_confirmation * XFEE_PRICE_USD)
    #   for all completed purchases by this user

    # total_tokens_sold:
    #   sum of xfee_amount for completed purchases by this user

    # total_commission_sol:
    #   sum of alloc.sol_amount where recipient = wallet and alloc_type = "commission"

    # level:
    #   computed from total_sales_usd using qualification thresholds

    # direct_referral_count:
    #   count of users where referrer_wallet = this wallet

    # network_size:
    #   count of all wallets where this wallet appears in their ancestors array

    # upsert all fields into users collection
```

> **Note:** Store `sol_price_at_confirmation` on each purchase at confirmation time so the indexer can compute USD value accurately without re-fetching historical prices.

---

## PROJECT STRUCTURE

```
xfee-backend/
├── app/
│   ├── main.py                   # FastAPI app init, lifespan, router includes
│   ├── config.py                 # pydantic-settings, load .env
│   ├── database.py               # motor client, collection accessors
│   ├── routers/
│   │   ├── users.py
│   │   ├── purchases.py
│   │   ├── admin.py
│   │   └── stats.py
│   ├── services/
│   │   ├── solana_rpc.py         # getBalance, sendTransaction helpers via httpx
│   │   ├── sol_price.py          # fetch + cache SOL price (cache 30s)
│   │   ├── token_dispatch.py     # SPL token transfer logic
│   │   ├── commission.py         # differential commission walk + on-chain distribution
│   │   ├── purchase_flow.py      # process_completed_purchase orchestrator
│   │   ├── wallet_pool.py        # keypair generation, pool management
│   │   └── indexer.py            # stats aggregation
│   ├── models/
│   │   ├── user.py               # Pydantic response models (no private keys)
│   │   ├── purchase.py
│   │   └── alloc.py
│   ├── tasks/
│   │   └── poller.py             # asyncio polling task
│   └── utils/
│       ├── keypair.py            # solders Keypair generation helpers
│       └── level.py              # get_level_from_sales() function
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## CRITICAL SECURITY RULES

Enforce these throughout the entire codebase:

1. **Private keys are NEVER returned in any API response, log line, or error message.** Build a `SafePurchaseWallet` Pydantic model that excludes `private_key` using `model_config = ConfigDict(exclude={"private_key"})`.
2. All SOL transfers from purchase wallets use the private key loaded **fresh from MongoDB at execution time only** — never cache private keys in application memory.
3. Do not expose MongoDB `_id` fields directly — convert to string where needed, or use a separate `id` alias field.
4. **Rate limit purchase initiation:** one active `pending` purchase per wallet at a time.
5. Validate all incoming wallet addresses as valid Solana public keys using `solders.pubkey.Pubkey.from_string()` wrapped in try/except — return a clear 400 error on invalid input.
6. **Hard cap enforcement:** before any purchase, atomically check `sum of xfee_amount for completed purchases < 400000`. Use a MongoDB aggregation with a session if needed.

---

## STARTUP BEHAVIOUR (FastAPI `lifespan`)

On startup:

1. Init all MongoDB indexes (`wallet_address` unique on `users`, `relationship_tree`; `status` on `purchase_wallets` and `purchases`)
2. Check purchase wallet pool — generate wallets until at least 20 `free` ones exist
3. Verify treasury wallet has a non-zero XFEE token balance via RPC — log a warning if below 10,000 XFEE
4. Log current global stats (tokens sold, tokens remaining)

---

## REQUIREMENTS.TXT

```
fastapi
uvicorn[standard]
motor
pydantic-settings
solana
solders
httpx
apscheduler
python-dotenv
```

---

## IMPLEMENTATION NOTES

- Use `asyncio.create_task()` to spawn polling tasks — **do not use FastAPI's `BackgroundTasks`** for long-running loops
- All RPC calls must go through a shared `httpx.AsyncClient` session initialised in the lifespan context
- SOL price must be cached for **30 seconds** to avoid hammering the price API on every poll cycle
- When building the `/tree` response, limit recursion depth to **5 levels** to prevent timeouts on large networks
- All on-chain transactions must be logged to the `transactions` collection with their signature for a full audit trail
- The `commission_distributed` flag on purchases must be set atomically after all allocs are created to prevent double-distribution on retries
