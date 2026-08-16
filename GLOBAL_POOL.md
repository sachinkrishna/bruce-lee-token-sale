# Global Pool — Design & Reimplementation Guide

Standalone reference for the Global Pool subsystem. Written so another team can port this design to a different backend / DB / chain with confidence. Concrete file paths refer to this repo, but every algorithm and invariant is described independently of the current Python + MongoDB + Solana stack.

---

## 0. Summary (30 seconds)

The Global Pool is a **rolling reward pool for users whom the primary commission cascade skipped**. It runs in fixed-length windows (default: 15 days). During a window, each "skip event" (a downline sale where an upline ancestor was in the tree but received zero commission because a peer at their tier already got paid) accrues **USD-denominated points** to that skipped user. When the window ends, a single funding wallet auto-pays every user a share of the wallet's available balance proportional to their point share in that window. Payouts are on-chain, memo-tagged for idempotency, and split by originating currency (SOL vs SPL token) so users are paid in the same currency the missed commission would have been paid in.

Key properties:

- **No on-chain program.** The pool is backend + DB + normal transfers.
- **Direct auto-settlement** from a single funding wallet (typically the same wallet that receives the commission "sweep" from each purchase).
- **Fully resumable / crash-safe.** DB uniqueness + memo-based reconciliation means every stage of settlement is retriable without double-pay risk.
- **Bifurcated by currency.** Two independent payout tracks (SOL, XFEE-SPL) share one accrual timeline.

---

## 1. Motivation — the differential-commission gap

The parent system uses **cumulative-differential commissions** on a level ladder (levels 1..N with a rate for each). On every purchase, we walk the buyer's ancestor chain top-to-bottom and for each ancestor at level L pay a rate of `rate(L) - rate(highest_paid_below)`. This has the elegant property that when all levels are present, exactly 100% of the commissionable amount gets distributed.

The downside: **the first ancestor to hit a given tier "consumes" it for that purchase**. Any deeper ancestor sitting at the same or lower tier receives a zero-commission alloc even though they're in the referral chain.

**Concrete example.** Ancestor chain, top→bottom: `A(L3) → B(L2) → C(L2) → D(L1)`. Rates: L1=20%, L2=22%, L3=24%.

| Ancestor | Level | Highest paid below | Differential | Result |
|---|---|---|---|---|
| A | 3 | 2 | 24% − 22% = 2% | paid 2% |
| B | 2 | 1 | 22% − 20% = 2% | paid 2% |
| C | 2 | 2 | (already at 2) | **zero-alloc** |
| D | 1 | 0 | 20% − 0 = 20% | paid 20% |

C got nothing. In a naive design C's business efforts are unrewarded — even though a peer at their tier (B) received 2%. The Global Pool solves this: C accrues **"missed commission" points** in USD terms equal to what B was actually paid for this purchase. Those points are settled at the end of the window from the funding wallet's balance.

The design principle: *the money is already in the system* (it went to master as part of the sweep). The pool just redistributes it fairly at a cadence.

---

## 2. Conceptual model

### 2.1 Pools = fixed-length windows

- Each pool has an integer `pool_index` (1, 2, 3, ...), a `start_at`, and `end_at = start_at + duration` (default 15 days).
- Only one pool is `active` at any moment — the one whose `[start_at, end_at)` covers "now".
- Pools are **created lazily on demand**: the first purchase that would accrue into a new window triggers creation of that window's pool.
- The next pool's `start_at` equals the previous pool's `end_at` — no gaps, no overlaps.
- If the system sits idle across multiple windows (unlikely in production, common in tests), pool creation "jumps" — the new pool's `pool_index` is computed as `last_index + floor((now - last_end) / duration) + 1`. Empty intermediate windows are simply never materialized.

### 2.2 Points

- Points are **USD-valued** on the accrual side (so a $10 sale and a 10-XFEE sale produce equivalent point deltas at oracle price).
- A user's points are accumulated across a whole window; a single user accrues at most one row per pool (`(pool_id, wallet_address)` is a unique key).
- Points come from **one and only one source**: a zero-commission alloc for an ancestor at a tier where a peer already got paid on the same purchase. Nothing else creates points.

### 2.3 Points formula

For a purchase, walking the ancestor chain top-to-bottom, we maintain `paid_by_level: {tier → currency_amount}` recording the commission actually paid to whichever ancestor first hit that tier. When we hit a later ancestor at the same tier (a "peer") we:

1. Write a zero-alloc for them (currency amount = 0).
2. Look up `peer_commission = paid_by_level[their_tier]` — the currency amount the earlier peer received.
3. If `peer_commission > 0`, mark the alloc `global_pool_points_pending = true` with `global_pool_points_native = peer_commission`.

After the batch commit of all allocs, we do a follow-up pass that converts native → USD (using SOL price or oracle price depending on origin currency) and writes:

- On the alloc: `global_pool_points_recorded = true`, `global_pool_points_usd = N`, `global_pool_points_currency = "SOL" | "XFEE"`.
- On the pool_points row: `$inc { points_usd, points_usd_<currency> }`, plus `$addToSet` for tracking the source `alloc_ids` / `purchase_ids`.
- On the global_pools doc: `$inc { total_points_usd, total_points_usd_<currency> }`.

The alloc-side `global_pool_points_recorded` guard makes the operation exactly-once even if the point-recording pass is retried.

### 2.4 Currency bifurcation

Because purchases can happen in SOL or XFEE (see `SETUP.md §6.9`), zero-allocs can accrue from either currency. Rather than pay every user in SOL (which would force us to sell XFEE received during the window), the pool tracks two independent buckets:

| Track | Source of accrual | Source of payout |
|---|---|---|
| SOL | zero-allocs on SOL-mode purchases | funding wallet's SOL balance |
| XFEE | zero-allocs on XFEE-mode purchases | funding wallet's XFEE ATA balance |

The two tracks are financially independent — a user with SOL-side points but no XFEE-side points only gets a SOL payout, and vice versa. Users with both get two independent transfers.

At settlement time, the SOL side computes `owed_sol_lamports = (user_sol_points / total_sol_points) * available_sol_lamports`, and the XFEE side computes `owed_xfee_raw = (user_xfee_points / total_xfee_points) * available_xfee_raw`. Both use the same wallet lock (see §5.6).

---

## 3. Data model

### 3.1 Collections

Three collections plus a marker document. Names below are the current implementation (see `app/database.py`); rename freely.

- `global_pools` — one doc per pool window (past + current + future).
- `pool_points` — one doc per `(pool, user)` pair. Cardinality: N pools × M unique participating users.
- `system_meta` — key/value docs for one-time migration markers. Also used elsewhere (purchase-mode toggle).

### 3.2 `global_pools` doc

```jsonc
{
  "_id": ObjectId,
  "pool_index": 5,                      // stable integer, monotonic
  "start_at": ISODate,                  // window start (UTC)
  "end_at": ISODate,                    // window end (UTC), start_at + duration
  "status": "active"                    // lifecycle (see §3.4)
          | "ready_to_settle"
          | "settling"
          | "settled",
  "total_points_usd": 4382.61,          // sum of all points_usd on pool_points rows in this pool
  "total_points_usd_sol": 3120.14,      //   SOL-track subtotal
  "total_points_usd_xfee": 1262.47,     //   XFEE-track subtotal
  "created_at": ISODate,
  "updated_at": ISODate,

  // Present after status ∈ {ready_to_settle, settling, settled}
  "snapshot": {
    "funding_wallet": "…pubkey…",
    "funding_balance_lamports": 12345678,   // SOL balance at snapshot time
    "funding_balance_xfee_raw": 987654321,  // XFEE raw balance at snapshot time
    "buffer_lamports": 50000000,            // configured SOL reserve
    "distributable_lamports": 12295678,     // funding − buffer, minimum 0
    "distributable_xfee_raw": 987654321,    // no buffer on XFEE (SOL fees only)
    "snapshot_at": ISODate,
    "total_users": 42,
    "settlement_id": "a1b2c3d4e5f6"         // random hex, distinguishes settlement attempts
  },

  // Present after status transitions to settling
  "settlement": {
    "lock_owner": "worker-9f8e7d6c",        // WORKER_ID of holder
    "lock_until": ISODate,                  // TTL for stale locks
    "started_at": ISODate,
    "completed_at": ISODate,                // present only when settled
    "outstanding": 0                        // last-known count of non-terminal rows
  },

  // Present only when settled early via force flag (§5.7)
  "forced_finalized": true,

  // Optional metadata during no-points early exit (§5.4)
  "settled_at": ISODate
}
```

### 3.3 `pool_points` doc

```jsonc
{
  "_id": ObjectId,
  "pool_id": ObjectId,                  // FK to global_pools._id
  "pool_index": 5,                      // denormalized for cheap queries
  "wallet_address": "…pubkey…",
  "points_usd": 137.42,                 // = points_usd_sol + points_usd_xfee (denormalized)
  "points_usd_sol": 90.10,
  "points_usd_xfee": 47.32,
  "event_count": 4,                     // number of zero-allocs contributing
  "alloc_ids": [ObjectId, ...],         // source allocs; grows monotonically
  "purchase_ids": [ObjectId, ...],      // source purchases; deduped

  // Populated at snapshot time
  "owed_lamports": 8123456,             // SOL-side owed
  "owed_sol": 0.008123,                 // convenience UI value
  "owed_xfee_raw": 6543210,             // XFEE-side owed
  "owed_xfee_ui": 6.54321,              // convenience UI value

  "memo": "GP:5:a1b2c3d4e5f6:9f8e7d6c",       // SOL-side idempotency memo
  "xfee_memo": "GP-X:5:a1b2c3d4e5f6:9f8e7d6c",// XFEE-side idempotency memo

  // Per-track state machines (see §5.5)
  "settle_status": "pending"            // SOL side
                 | "sending" | "sent" | "confirmed"
                 | "failed" | "skipped_zero",
  "attempts": 1,
  "tx_signature": "…sig…",              // set on send
  "sending_at": ISODate,
  "sent_at": ISODate,
  "confirmed_at": ISODate,
  "reconciled": true,                   // set if recovered via memo scan
  "last_error": "…",                    // set on transient failure

  "xfee_settle_status": "…",            // XFEE side (parallel to above)
  "xfee_attempts": 1,
  "xfee_tx_signature": "…sig…",
  "xfee_sending_at": ISODate,
  "xfee_sent_at": ISODate,
  "xfee_confirmed_at": ISODate,
  "xfee_reconciled": true,
  "xfee_last_error": "…",

  "created_at": ISODate,
  "updated_at": ISODate
}
```

### 3.4 Pool lifecycle

```
active ──(end_at reached, worker picks up)──▶ ready_to_settle
   │                                                │
   │                                                ▼
   │                                          _snapshot_pool() writes:
   │                                            - status=ready_to_settle
   │                                            - snapshot.* (frozen)
   │                                            - per-user owed_* + memos
   │                                                │
   │                                                ▼
   └─▶ (force=true; admin)────────────────▶  _acquire_wallet_lock() succeeds
                                                    │
                                                    ▼
                                                settling
                                                    │
                                        (all rows terminal)
                                                    │
                                                    ▼
                                                settled
```

- `active` → `ready_to_settle` on the first settlement attempt after `end_at`.
- `active` → `ready_to_settle` immediately when `force=true` is passed (sets `end_at = now`, `forced_finalized = true`).
- If total points are 0 across both tracks, snapshot short-circuits directly to `settled` with `snapshot.reason = "no_points"` (no wallet balance reads, no transfers).
- `settling` is held by exactly one worker via a lock. If another pool tries to settle from the same wallet, it defers.
- `settled` is terminal.

### 3.5 Indexes

Only the ones the settlement path actually depends on:

```
global_pools:
  { pool_index: 1 }                               unique
  { status: 1, end_at: 1 }                        for worker scan
  { start_at: 1, end_at: 1 }                      for window resolution

pool_points:
  { pool_id: 1, wallet_address: 1 }               unique   ← THE key idempotency guarantee
  { pool_index: 1, points_usd: -1 }               for leaderboard/standings
  { wallet_address: 1 }                           for user-history queries
```

The unique index on `(pool_id, wallet_address)` is what prevents duplicate accruals under any race. **Don't skip it in a port.**

---

## 4. Algorithms

### 4.1 Point accrual (called from the commission path)

Called once per zero-alloc that has a paying peer at the same tier. Idempotent via the alloc's `global_pool_points_recorded` guard.

```
record_missed_commission_points(alloc_id, wallet, purchase_id, event_time, points_usd, currency, points_native):
  if not global_pool_enabled or points_usd <= 0:
    return
  pool = resolve_active_pool(event_time)
  if pool is None or pool.status != "active":
    return
  updated = allocs.update_one(
    filter: { _id: alloc_id, global_pool_points_recorded != true },
    set:    { global_pool_points_recorded: true,
              global_pool_points_usd: points_usd,
              global_pool_points_currency: currency,
              global_pool_points_<sol|xfee_raw>: points_native,
              global_pool_index: pool.pool_index,
              global_pool_id: pool._id }
  )
  if updated.modified_count == 0:
    return  # already recorded on a prior attempt
  inc_fields = { points_usd, event_count: 1, points_usd_<currency>: points_usd }
  pool_points.upsert(
    { pool_id: pool._id, wallet_address: wallet },
    setOnInsert: { pool_id, pool_index, wallet_address,
                   settle_status: "pending", xfee_settle_status: "pending",
                   created_at: now },
    inc:         inc_fields,
    set:         { updated_at: now },
    addToSet:    { alloc_ids, purchase_ids }
  )
  global_pools.update({ _id: pool._id }, inc: { total_points_usd, total_points_usd_<currency> })
```

Notes:
- `resolve_active_pool` is the auto-advance logic in §4.2.
- The alloc's `global_pool_points_recorded` flag is set **before** the pool_points upsert. If the process dies between the two writes, on retry the alloc guard trips and the upsert is skipped — no double-count. Cost: one accrual could be lost. In practice this is bounded (single alloc, single purchase). If your system needs stricter guarantees, wrap both writes in a transaction.

### 4.2 Pool window resolution

```
resolve_active_pool(event_time):
  # Fast path: the current active pool covers this time
  existing = global_pools.find_one({ start_at <= event_time < end_at })
  if existing: return existing

  # Slow path: figure out which window we're in
  last = global_pools.find_one(sort_desc(pool_index))
  if last is None:
    # Bootstrap: first-ever pool starts at this event
    return upsert(new_pool_doc(pool_index=1, start_at=event_time))

  if event_time < last.start_at:
    return last  # backdated purchase — attribute to the earliest known window

  if event_time >= last.end_at:
    # Jump forward: how many empty windows have passed?
    n_jumped = floor((event_time - last.end_at) / duration)
    start_at = last.end_at + n_jumped * duration
    pool_index = last.pool_index + n_jumped + 1
  else:
    # We're still inside last's window (race with insert we haven't observed)
    start_at = last.start_at
    pool_index = last.pool_index

  return upsert(new_pool_doc(pool_index, start_at))
```

Idempotent under concurrent callers via the `pool_index` unique index — one racer's insert wins, others read the winner on `DuplicateKeyError`.

### 4.3 Snapshot (freezing the pool)

Called exactly once per pool (idempotent — checks `snapshot.snapshot_at`).

```
_snapshot_pool(pool):
  if pool.snapshot?.snapshot_at exists: return pool

  total_sol   = pool.total_points_usd_sol
  total_xfee  = pool.total_points_usd_xfee

  if total_sol + total_xfee <= 0:
    mark pool settled with reason="no_points"
    return

  balance_lamports  = rpc.get_balance(funding_wallet)
  buffer_lamports   = config.funding_buffer_sol * 1e9
  distributable_sol = max(0, balance_lamports - buffer_lamports)

  distributable_xfee = 0
  if total_xfee > 0 and xfee_mint configured:
    distributable_xfee = rpc.get_spl_balance(funding_wallet, xfee_mint)

  if total_sol > 0 and distributable_sol <= 0 and (total_xfee <= 0 or distributable_xfee <= 0):
    raise RuntimeError("funding wallet is broke on the side(s) we owe on")

  settlement_id = random_hex(6)
  for each row in pool_points.find({ pool_id: pool._id }):
    owed_sol  = int((row.points_usd_sol  / total_sol)  * distributable_sol)  if total_sol  > 0 and row.points_usd_sol  > 0 else 0
    owed_xfee = int((row.points_usd_xfee / total_xfee) * distributable_xfee) if total_xfee > 0 and row.points_usd_xfee > 0 else 0
    row.update({
      owed_lamports: owed_sol,   owed_sol: owed_sol / 1e9,
      owed_xfee_raw: owed_xfee,  owed_xfee_ui: owed_xfee / 10**decimals,
      settle_status:      "pending" if owed_sol  > 0 else "skipped_zero",
      xfee_settle_status: "pending" if owed_xfee > 0 else "skipped_zero",
      memo:      "GP:{pool_index}:{settlement_id}:{wallet[:8]}",
      xfee_memo: "GP-X:{pool_index}:{settlement_id}:{wallet[:8]}",
    })

  pool.update({
    status: "ready_to_settle",
    snapshot: { funding_wallet, funding_balance_lamports, funding_balance_xfee_raw,
                buffer_lamports, distributable_lamports, distributable_xfee_raw,
                snapshot_at: now, total_users, settlement_id }
  })
```

Design notes:
- Integer-truncation on owed amounts is intentional. Total distributed will be ≤ distributable (never over). The rounding-down "dust" stays in the funding wallet.
- The settlement_id is only used to build memos. Storing it lets you scan on-chain history if the DB gets restored from an older backup.
- **Snapshot amounts are immutable after write.** Even if the funding wallet balance changes after snapshot, per-user owed amounts don't recompute. This is what makes the whole thing resumable — retries transfer the exact same amount to the exact same user.

### 4.4 Wallet-level lock

Two mechanisms:

1. **Cross-pool lock** — an atomic check-then-set that only lets one pool settle from a given funding wallet at a time. Prevents "pool 5 and pool 6 both try to drain wallet W" races.
2. **Intra-pool lock** — the same doc's `settlement.lock_owner` + `lock_until` fields let a single worker hold ownership across the multi-second settlement run. TTL-based so a crashed worker's lock expires (default 600s) and another worker can take over.

Pseudocode:

```
_acquire_wallet_lock(pool, ttl):
  now = utcnow()
  # If another pool is currently settling from our wallet, defer.
  busy = global_pools.find_one({
    _id != pool._id, status: "settling",
    snapshot.funding_wallet == pool.snapshot.funding_wallet,
    settlement.lock_until > now
  })
  if busy: return False

  # Grab our own pool's lock (idempotent for the same worker, stealing on TTL expiry).
  result = global_pools.update_one(
    filter: {
      _id: pool._id,
      $or: [
        settlement.lock_owner not exists,
        settlement.lock_owner == our_worker_id,
        settlement.lock_until <= now
      ]
    },
    set: { status: "settling", settlement.lock_owner: worker_id, settlement.lock_until: now + ttl, ... }
  )
  return result.modified_count > 0
```

Ports note: swap `global_pools` for whatever your DB is; the atomic conditional update is the important part.

### 4.5 Per-user payout state machine

Run twice per user per pool — once for SOL, once for XFEE — with parallel field sets. States:

```
       ┌─────────┐
       │ pending │  (set at snapshot when owed>0)
       └────┬────┘
            │ atomic transition guarded by
            │ settle_status ∈ {pending, failed, sending}
            ▼
       ┌─────────┐
       │ sending │  (attempts++)
       └────┬────┘
            │ transfer submit
            ├──── exception ─────▶ ┌────────┐
            │                      │ failed │  (last_error set)
            │                      └───┬────┘
            │                          │ retried on next settle_pool run
            ▼                          │
       ┌─────────┐                     │
       │  sent   │  (tx_sig set)       │
       └────┬────┘                     │
            │ confirm_transaction()    │
            ├── ok ─▶ ┌───────────┐    │
            │        │ confirmed │  ◀─┘  (terminal)
            │        └───────────┘
            └── timeout ─▶ stays "sent" (retried on next run,
                            memo scan will pick up the actual signature)

Also terminal: skipped_zero (owed was 0 at snapshot)
```

Before submitting a transfer, the state machine tries two shortcuts:

1. **`tx_signature` already set** — this means a previous attempt sent a tx we haven't confirmed. Check its status now; if confirmed, mark and exit.
2. **Memo-based reconciliation** — scan the funding wallet's recent signatures for one containing this row's memo. If found (with no error), attribute it and mark confirmed.

Only if both fail do we cut a new tx.

The "sending" state exists to prevent two workers from both firing a fresh transfer for the same row: the atomic filter `settle_status ∈ {pending, failed, sending}` transitions to "sending" and only the winner proceeds. If the second worker gets stuck in "sending", the memo reconciliation on the next run will catch the winning worker's tx.

### 4.6 Full settlement orchestration

```
settle_pool(pool_index, force=False):
  pool = global_pools.find_one({ pool_index })
  if pool.status == "settled": return already_settled_result

  if pool.end_at > now:
    if not force: raise "not due yet"
    pool.update({ end_at: now, forced_finalized: true })

  pool = _snapshot_pool(pool)
  if pool.status == "settled":       # no_points shortcut
    return no_points_result

  ensure_next_pool(pool)             # pre-create pool_index+1 so accruals during
                                     # settlement have a home
  if not _acquire_wallet_lock(pool):
    return { reason: "funding_wallet_busy_with_other_pool" }

  try:
    rows = pool_points.find({
      pool_id: pool._id,
      $or: [
        { owed_lamports > 0, settle_status not in {confirmed, skipped_zero} },
        { owed_xfee_raw > 0, xfee_settle_status not in {confirmed, skipped_zero} }
      ]
    }).sort(points_usd desc)

    # Bounded concurrency — default 3 in-flight transfers per pool
    with Semaphore(config.settlement_concurrency):
      run _process_row(pool, row) for each row  # runs BOTH SOL and XFEE tracks

    outstanding = pool_points.count(same filter as above)
    if outstanding == 0:
      pool.update({ status: "settled", settled_at: now, settlement.completed_at: now })
      final = "settled"
    else:
      pool.update({ settlement.outstanding: outstanding })
      final = "settling"
  finally:
    _release_wallet_lock(pool)

  return { pool_index, status: final, stats: per-status counts }
```

Key points:
- The transfer semaphore keeps the RPC-facing load bounded regardless of pool size.
- `_process_row` runs the SOL and XFEE state machines **sequentially per user** (not in parallel), so each user's on-chain identity is only one tx-in-flight at a time. Cross-user parallelism comes from the semaphore.
- If any row remains non-terminal after the run, the pool stays `settling`. The worker loop picks it up again next tick. Every retry is safe (see §5).

### 4.7 Worker loop

The scheduler is dead-simple:

```
async def global_pool_worker_loop():
  while true:
    try:
      process_due_pools()  # scans all pools with end_at <= now AND status ∈ {active, ready_to_settle, settling}
    except Exception: log_and_continue
    sleep(finalize_interval_seconds)  # default 300s
```

`process_due_pools` walks in ascending `pool_index` so if multiple pools are due (e.g. after a long outage) they settle in order — important because the wallet-level lock means each has to wait its turn.

---

## 5. Idempotency & crash-recovery

The whole subsystem is designed under the assumption that **any single line of code can be the last one to execute before a crash**, and the next process boot must be able to pick up cleanly.

Guarantees, ranked by "how bad a bug in them would be":

### 5.1 No double payment

Two layers of protection:

1. **DB-level:** `pool_points` has a unique index on `(pool_id, wallet_address)` and per-row state transitions are done with `filter: { _id, settle_status: allowed_source_states }`. A second worker attempting the same transition sees `modified_count = 0` and backs off.
2. **On-chain-level:** every transfer carries a memo containing `(pool_index, settlement_id, wallet_prefix)`. Before submitting a new tx, we scan the funding wallet's recent signatures for that memo. If we find a prior successful tx (no error), we adopt it as the payout. This is what makes the system survive scenarios like "we sent a tx, then the DB write to record `tx_signature` failed, then we crashed" — on restart, the memo scan finds the tx we forgot about.

### 5.2 No missed payment (eventually)

The worker loop retries `settling` pools every tick. A row can be in `sending` / `sent` / `failed` after a partial run; the next run:

- Confirms an existing `tx_signature` (if present).
- Reconciles by memo (if the crash lost the signature but the tx confirmed).
- Retries a fresh transfer (if the previous attempt actually failed).

The state machine reliably converges to `confirmed` (or `failed` with an error surface) for every row that had a non-zero `owed_*`.

### 5.3 No accrual after snapshot

Once a pool is snapshotted, `record_missed_commission_points` still resolves the *active* pool (which by then is the *next* pool, since `ensure_next_pool` runs at snapshot time). So a purchase completing during settlement of pool 5 accrues into pool 6, not pool 5. This is the reason `ensure_next_pool` is called before the wallet lock is acquired — accrual mustn't block behind settlement.

Guard against edge case: if the snapshot pass is happening concurrently with a purchase that just chose pool 5 as its active pool moments before snapshot, that purchase's points *are* in pool 5's totals. The snapshot reads those totals fresh; no lost points.

### 5.4 No settlement of empty pools

Snapshot short-circuits to `status = "settled"` with `snapshot.reason = "no_points"` when there's zero total accrual. This costs one RPC call in the naive path — we skip that too by checking totals in the DB first.

### 5.5 Deterministic settlement on retry

After snapshot, all `owed_*` amounts are frozen in the DB. Retries never recompute them (even if the funding wallet balance drifts). This is what lets us confidently retry transfers without worrying about "did the amount change?"

### 5.6 Cross-pool serialization

The wallet-level lock (`_acquire_wallet_lock`) ensures at most one pool is actively spending from a given funding wallet. Without this, pool 5 and pool 6 both racing for the wallet's balance would produce over-spend if the second one snapshotted based on a stale balance.

### 5.7 Force finalization

`settle_pool(pool_index, force=True)` sets `end_at = now` before proceeding. This is the only mechanism to end an active pool early — mainly for testing and for operator recovery in scenarios like "wall of purchases just happened, we want to settle now."

---

## 6. API surface

### 6.1 Public (frontend consumable)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/global-pool/summary` | System-wide totals: total_pools, active/settled counts, cumulative points USD (all + per-track), current pool index, settlement-status counts per track. |
| GET | `/api/v1/global-pool/funds` | Live snapshot of the current pool + funding wallet: pool window, points totals per track, master-collected-in-window (SOL and XFEE), funding wallet balances (SOL + XFEE), buffer, `available_for_settlement_*`. |
| GET | `/api/v1/global-pool/` (list) | Paginated list of all pools, newest first, with `user_count`, `sol_collected`, `xfee_collected_*`, and (if snapshotted) `distributable_*`. |
| GET | `/api/v1/global-pool/current` | Active pool + top-N standings (paged), sorted by `points_usd`. |
| GET | `/api/v1/global-pool/{pool_index}` | Any past or current pool + standings. |
| GET | `/api/v1/global-pool/{pool_index}/user/{wallet}` | Single user's row inside a specific pool (in_pool + full row). |
| GET | `/api/v1/global-pool/user/{wallet}/points` | User's history across all pools (paginated). |

### 6.2 Admin

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/admin/global-pool/{pool_index}/settle?force=true` | Settle a pool. Idempotent. `force=true` ends an active pool early. |
| POST | `/api/v1/admin/global-pool/process-due` | Force one worker scan cycle immediately (settles every due pool). |

Auth: standard `X-Admin-Key` header dependency (shared with other admin routes in this codebase).

### 6.3 What a frontend needs

For a leaderboard: `GET /global-pool/current` (standings sorted by `points_usd`, paged).

For a user dashboard: `GET /global-pool/user/{wallet}/points` (their pool history) + optionally `GET /global-pool/{pool_index}/user/{wallet}` for a single detailed row.

For a live "pool status" widget: `GET /global-pool/funds` — this is the one endpoint that mixes DB-level pool metadata with live on-chain balances so operators can see both what's owed and what's actually funded.

---

## 7. Operations

### 7.1 Funding wallet requirements

Whatever wallet is designated as the funding wallet must have:

- **Enough SOL** to cover all SOL-side payouts in the current pool + a small buffer for tx fees + the configured operator buffer (`GLOBAL_POOL_FUNDING_BUFFER_SOL`, default 0.05 SOL).
- **An ATA for the XFEE mint** (auto-created lazily via idempotent ATA instructions if not pre-created).
- **Enough XFEE** in that ATA to cover all XFEE-side payouts.
- **Enough SOL** on top of the above to cover the ATA-creation rent (~0.002 SOL each) for XFEE recipients who don't yet have an XFEE ATA.

Sizing rule of thumb: the funding wallet should hold *at minimum* the sum of every non-terminal `owed_*` across all currently unsettled pools + the buffer.

In practice this project reuses the master wallet as the funding wallet (same wallet that receives every purchase's sweep), which keeps the money in one place.

### 7.2 Buffer

`GLOBAL_POOL_FUNDING_BUFFER_SOL` (default 0.05) is subtracted from the wallet's SOL balance at snapshot time and never distributed. Its job is to keep the wallet solvent for tx fees on the settlement transfers themselves. **Don't set it to 0** — you'll hit "insufficient lamports for tx fee" during settlement.

There is no equivalent buffer on the XFEE side because the funding wallet only pays SOL for tx fees, never XFEE.

### 7.3 Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `funding wallet balance is below buffer` at snapshot | Funding wallet drained | Top up the wallet, retry `POST /admin/global-pool/{n}/settle` — snapshot picks up the new balance. |
| `funding_wallet_busy_with_other_pool` returned | Two due pools; lock held by the first | Wait for the first to finish (usually seconds) or retry — worker picks it up automatically. |
| Individual row stuck in `sending` for minutes | Worker crashed mid-transfer, lock hasn't TTL'd yet | Wait for `lock_until` to expire; next worker tick picks it up. Alternative: manually clear `settlement.lock_until` (not usually needed). |
| Individual row stuck in `sent` (not `confirmed`) | Confirmation timed out but tx may have landed | Next `settle_pool` run reconciles via memo scan. If persistent, check `tx_signature` on-chain manually — if landed, DB will catch up on next attempt. |
| Row in `failed` with `last_error` | Transfer submit failed (RPC issue, insufficient funds, invalid recipient) | Read the error, fix the root cause, retry. |
| Pool stuck in `settling` indefinitely | Some rows are `failed` with a non-transient error | Inspect the rows via `GET /global-pool/{n}` → standings, resolve manually (e.g. fund the wallet, invalid recipient — rare because we validate at accrual time). |

### 7.4 Deploying with the pool enabled

1. Set `GLOBAL_POOL_ENABLED=true` (default).
2. Set `GLOBAL_POOL_FUNDING_WALLET_PRIVATE_KEY` — base58 secret of the funding wallet. Almost always the same as the master wallet.
3. Set `GLOBAL_POOL_DURATION_DAYS` (default 15). Larger windows accrue more before payout; smaller windows create more frequent but smaller payouts.
4. Set `GLOBAL_POOL_FINALIZE_INTERVAL_SECONDS` (default 300). How often the worker checks for due pools. Doesn't need to be shorter than a few minutes.
5. Optional: `GLOBAL_POOL_SETTLEMENT_CONCURRENCY` (default 3) — parallel transfers per pool. Turn up for large pools if your RPC is fast, down if you're rate-limited.
6. Optional: `GLOBAL_POOL_CONFIRM_RETRIES` (default 30) — how many `getSignatureStatuses` polls before treating a tx as unconfirmed (falls through to memo reconciliation next tick).

### 7.5 Ending an active pool early

For testing or one-off operator scenarios:

```bash
curl -X POST "https://<host>/api/v1/admin/global-pool/5/settle?force=true" \
  -H "X-Admin-Key: $ADMIN_API_KEY"
```

The end result is identical to letting the window elapse naturally: snapshot, settle, mark done. The only difference is `forced_finalized: true` on the pool doc.

---

## 8. Reimplementation checklist

If you're porting this design to a different stack, run through this list:

### 8.1 Core requirements

- [ ] A durable KV / document store with **conditional updates** and **unique indexes**. (Mongo works; Postgres works with proper constraints; DynamoDB works with condition expressions.)
- [ ] A way to derive an idempotency memo per transfer that lands on-chain (or in your equivalent audit stream). SPL Memo works on Solana; on other chains use whatever provides recoverable transaction metadata.
- [ ] A single-signer funding wallet you control, holding both currencies.
- [ ] A background scheduler / worker (async loop, cron, systemd timer, k8s job — anything).
- [ ] An HTTP layer for the API surface if you need it.

### 8.2 Order of implementation

1. **Data model** — collections + unique indexes (§3).
2. **Pool resolution** — `resolve_active_pool` and `ensure_next_pool` (§4.2).
3. **Accrual** — hook `record_missed_commission_points` into your commission engine's zero-alloc path (§4.1). Write a small integration test that produces a mock zero-alloc and asserts pool_points has one row with the expected `points_usd`.
4. **Snapshot** — `_snapshot_pool` (§4.3). Test with a mock funding balance and 3 users with different point shares; assert `owed_*` sums ≤ `distributable_*`.
5. **State machines** — `_process_row_sol` and `_process_row_xfee` (§4.5). Test each state transition in isolation with a fake transfer function.
6. **Orchestration** — `settle_pool` end-to-end (§4.6).
7. **Wallet lock** — `_acquire_wallet_lock` (§4.4). Test with two pool docs pointing at the same funding wallet.
8. **Worker loop** — trivial (§4.7).
9. **API layer** — public + admin (§6).
10. **Migration** — see §9.

### 8.3 Config knobs to expose

- `pool_duration_days`
- `finalize_interval_seconds`
- `funding_wallet_secret` + `funding_wallet_address`
- `funding_buffer_sol`
- `settlement_concurrency`
- `confirm_retries`
- `xfee_mint` + `xfee_decimals` (if bifurcating currencies)
- `xfee_price_oracle_url` + json path (if you need on-the-fly USD conversion for accrual)

### 8.4 What to NOT reinvent

- **The unique index on (pool, user).** It's what makes the whole thing safe under concurrent writes.
- **The memo-based reconciliation.** Skipping this leaves you exposed to double-pay after any partial crash.
- **Snapshot immutability.** Do not recompute owed amounts on retry.
- **The lock-first, settle-second ordering.** Snapshotting before acquiring the lock is fine (idempotent), but transferring before locking is not.

---

## 9. Migration considerations

If you're adding this to an existing system that already has commission distribution:

### 9.1 Backfilling from historical data

Not recommended. The design assumes points accrue in real time from the commission engine. Retroactively reconstructing "which zero-allocs would have accrued" for past purchases is fragile — level thresholds may have changed, users may have been promoted/demoted since, etc. Just start counting from the first pool after enablement.

### 9.2 Adding currency bifurcation to a live single-currency pool

This is the migration this repo just did (SOL → SOL+XFEE). The idempotent one-time backfill is in `ensure_pool_points_bucket_backfill()`:

- For every `pool_points` row without `points_usd_sol`: set `points_usd_sol = points_usd`, `points_usd_xfee = 0` (historical accrual was 100% SOL by construction).
- For every `global_pools` doc without `total_points_usd_sol`: same.
- Guard with a `system_meta._id = "pool_points_bucket_backfill_v1"` marker so it runs exactly once.

The pattern generalizes: **any schema evolution should be guarded by a `system_meta` marker doc** so re-deploys don't re-run migrations.

### 9.3 Renaming statuses / adding new states

Free to do post-hoc as long as you gate reads with a "map old status → new" translator until the migration is complete. Any live worker mid-settle must be drained first (either by restarting after code deploy or by including old status names in the state-machine transition filters temporarily).

---

## 10. Audit / testing invariants

Useful assertions for CI and for periodic on-chain reconciliation:

### 10.1 DB-level

For any settled pool `p`:
- `p.total_points_usd == sum(pool_points{pool_id=p._id}.points_usd)`
- `p.total_points_usd_sol == sum(pool_points{...}.points_usd_sol)`  (both `null`-safe)
- `p.total_points_usd_xfee == sum(pool_points{...}.points_usd_xfee)`
- `sum(pool_points{...}.owed_lamports) <= p.snapshot.distributable_lamports`  (integer truncation only lowers the sum)
- `sum(pool_points{...}.owed_xfee_raw) <= p.snapshot.distributable_xfee_raw`
- Every row: `owed_lamports > 0` iff `settle_status ≠ "skipped_zero"`
- Every row: `owed_xfee_raw > 0` iff `xfee_settle_status ≠ "skipped_zero"`

### 10.2 On-chain vs DB

For any settled pool `p` and any confirmed row `r`:
- If `r.owed_lamports > 0` and `r.settle_status = "confirmed"`:
  - The tx at `r.tx_signature` transferred exactly `r.owed_lamports` lamports from `p.snapshot.funding_wallet` to `r.wallet_address`.
  - The tx includes an SPL memo containing `r.memo`.
- Same on XFEE side with `r.owed_xfee_raw` and `r.xfee_memo`.

A reconciliation script can enumerate the funding wallet's outgoing txs in the pool's `[settlement.started_at, settled_at]` window, join by memo, and flag any tx without a DB match (indicates unauthorized spending) and any DB row without a tx match (indicates missed payout that state machine should recover on next tick).

### 10.3 Accrual sanity

For any purchase P with zero-allocs A1..An having peers at their tier:
- Each Ai should have `global_pool_points_recorded = true` (except during a brief window between purchase completion and pool accrual).
- Each Ai's `global_pool_points_usd` should equal `points_native × price_at_accrual` for its currency.
- The pool_points row for `(A_i.pool, A_i.wallet)` should have `alloc_ids` containing `A_i._id`.

---

## 11. Known limitations & future work

- **Rounding-down dust stays in the wallet.** Not distributed. Over many pools this accumulates. Fine for now (operator can sweep manually); could be redistributed to the highest-point user on the next settlement if needed.
- **Points-USD conversion uses the price at accrual time.** If the token price moves significantly during the window, the USD-denominated points don't reflect current value at payout. Users are still paid a proportional share of the wallet balance, so the *dollar amount received* tracks the pool's collected wealth. Users who accrued early aren't disadvantaged vs. late accruers (their $-share is the same relative to the total).
- **No partial-window payouts.** A user who accrued a huge amount on day 1 waits the full window for payout. Not currently a business requirement; if needed, this becomes an "urgent settle" flag on the pool_points row that bypasses the window.
- **Single funding wallet per pool.** If the wallet is compromised, all past unsettled pools are at risk. Mitigations: rotate the funding wallet between pools (would need `GLOBAL_POOL_FUNDING_WALLET_*` to be per-pool config), or use a program-derived escrow (breaks the "no on-chain program" property).
- **No slashing / clawback.** Once a payout is `confirmed`, it's final. This is by design — the pool is not a court, just an equalizer.
- **The memo scan is bounded to the funding wallet's last 1000 signatures.** For very high-throughput wallets or long-delayed retries, memos can fall off. Mitigation: run settlement promptly after `end_at` (default worker cadence of 5 min ensures this).

---

## 12. Where to look in this repo

Source of truth for every algorithm in this doc:

| Concept | File |
|---|---|
| Data model + collections | `app/database.py` |
| Point accrual | `app/services/commission.py` (both `distribute_commissions` and `distribute_commissions_xfee`) |
| Pool lifecycle + settlement | `app/services/global_pool.py` |
| Config knobs | `app/config.py` (search "global_pool") |
| Public API | `app/routers/global_pool.py` |
| Worker startup | `app/main.py` (search "global_pool_worker_loop") |
| Migration marker | `app/services/global_pool.py` (`ensure_pool_points_bucket_backfill`) |

The core service module is ~1000 lines and readable end-to-end — recommend reading it top-to-bottom after this doc, in that order.
