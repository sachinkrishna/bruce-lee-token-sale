# Notes / Open Items

Running log of items to come back to. Add newest at the top with a date.

---

## 2026-09-14 — Liquidity wallet removed from tree; Ghq6fH + DjTmMT promoted

**Change shipped** (pending deploy):
- **`app/utils/level.py` — unchanged** (15-tier ladder rates stay: L14=95%,
  L13=50%, L12=45%, L11=42.5%, L10=40%, ...). No new ladder migration required.
- `app/main.py`: added idempotent one-shot startup migration
  `_migrate_remove_liquidity_wallet` (marker `remove_liquidity_wallet_2026_09`):
    * Reparents the liquidity wallet (`root_child_wallet_address` = BRrtYf) **out of
      the tree**: its direct child (Ghq6fH) becomes a direct child of master; every
      descendant in Ghq6fH's subtree has BRrtYf dropped from `ancestors` and `depth`
      decremented by 1; BRrtYf's `relationship_tree` doc is deleted; BRrtYf's user
      doc is kept for auditing and tagged with `removed_from_tree_at`; BRrtYf's
      cached `level` is recomputed from sales volume.
    * Promotes the two wallets that inherit its position in the tree:
        - `Ghq6fHG9H5bqPrhhXwqpnXf5DbxJV239geenCzGfNYWY` : L13 → **L14** (95%)
        - `DjTmMTBnMbRqY4fdBumKkwoPsrNoaiKX5fSVeihgLv7R` : L12 → **L13** (50%)
    * Refreshes master's `direct_referral_count` + `network_size`.
    * Defensive: any straggler at L14 that isn't master or in the promotion set
      gets recomputed from sales.

**Economic impact per downline purchase in Ghq6fH's subtree**:
- Ghq6fH: differential **5% → 45%** (L14 95% cum. − L13 50% below).
- DjTmMT: differential **5% → 10%** (L13 50% cum. − L10 40% below).
- Master: differential **5% (unchanged)** — Ghq6fH now occupies BRrtYf's old L14
  slot with the same rate, so master's residual is unchanged.
- BRrtYf: no longer in ancestor walk, receives no future commission allocs.

**Historical commissions**: BRrtYf's 28.67 SOL already-paid-out balance is left as-is
(paid on-chain, can't be reversed). No retroactive alloc rewrite. If those funds need
to move, do it manually.

**Wallets hardcoded in the migration**: the promoted wallets are specified in the
`_POST_LIQUIDITY_PROMOTIONS` list in `app/main.py`. If the same migration is
deployed to a fresh environment (dev/staging) where these wallets don't exist, the
updates no-op and the marker is stamped anyway (safe).

**Resolves prior open items**:
- 2026-09-10 "Tree structure anomaly: child L14 above parent L5" — the whole basis
  for the anomaly (BRrtYf's demoted L5 above Ghq6fH's admin-set L14) is gone once
  the migration runs.

---

## 2026-09-10 — "I don't see my sales numbers" reports — root-caused

**Reports**: some users saying their sales / referral counts show zero even though they
have referrals.

**Root cause**: `app/services/purchase_flow.py:210` — commission distribution is skipped
for purchases with `sale_usd < 10.0`. No allocs are created for any ancestor. Since the
`run_indexer_batch()` path (which refreshes `direct_referral_count`, `network_size`,
`total_sales_usd`, `level_sales`, etc. on the ancestor doc) is only triggered by
alloc-generating purchases, ancestors whose only downline activity has been tiny
(<$10) purchases see their referral stats stuck at their initial `0` values.

**Extent**: 27 historical $1 purchases across 23 wallets (before $50 min was in force).
Total unpropagated volume system-wide: ~$27. Six ancestors visibly short by $1–$5.
No stuck / half-completed purchases; every >= $50 purchase in the system is fully
propagated. Global aggregates all reconcile (92 completed, $49,571 self-purchase sum,
matches `stats/global.tokens_sold`).

**Most affected user (likely the reporter)**: `5Qsa4iG7M7gU4CB6LTrkFxWKPF2qtTpMLGXv4UAvgaFK`

- self_purchase = $101, is_founder = true, level = 10
- direct_referral_count = **0** (real: 2)
- network_size = **0** (real: 5)
- total_sales_usd = **$0** (real: $2 of downline volume)

Their two direct referrals (`5FCsaExm...` and `9H6YA1Hi...`) each bought exactly $1, so
their tree parent's ancestor-index refresh was never triggered.

**Fix options**
1. On user registration in `users.py:register_user`, immediately `$inc` the referrer's
   `direct_referral_count` and every ancestor's `network_size`; add a one-shot backfill
   that recomputes both counts for every existing user from `relationship_tree`. Fixes
   the display bug ("0 referrals") for the reporting user. Doesn't touch `total_sales_usd`.
2. Backfill zero-value allocs for the 27 legacy <$10 purchases so ancestors run through
   the indexer normally. Cleanest but writes retroactive rows to `allocs`.
3. Do both. Recommended.

Pending: user decision on which option to go with.

---

## 2026-09-10 — Tree structure anomaly: child L14 above parent L5

Ran a full tree integrity audit from master `DXSEB4WrtfSFvD6ZKvyiyg9GDnEgmc6uAPpkHHQBNwFB`
(107 nodes). All checks passed **except one**:

```
parent  BRrtYftGhXBh3JcwmveuB4ZcskkYvUeLzNgPcf5VF6Ry   L5    $49,550.00
child   Ghq6fHG9H5bqPrhhXwqpnXf5DbxJV239geenCzGfNYWY   L14   $49,525.00
```

### Origin (very likely)
- `BRrtYft...F6Ry` was the retired `ROOT_CHILD_WALLET_ADDRESS` (used to be pinned at L14
  by config). The 15-tier ladder migration recomputed its level from sales volume — $49,550
  puts it at L5 by the algorithm.
- `Ghq6fH...NYWY` is the current `SET_USER_LEVEL_SIGNER_WALLET`. Its L14 is not what the
  sales-volume algorithm would produce for $49k — it was manually set via
  `POST /admin/set-user-level`.

### Why it might matter
If Ghq6fH sits in BRrtYf's downline and collects L14-rate commissions on flow that comes
through an L5 parent, the effective commission structure on BRrtYf's subtree may be
inconsistent with the intent. Needs a business-rules decision, not a bugfix.

### Options
1. Leave as-is (business decision that L14 signer wallet was intentional).
2. Recompute Ghq6fH from sales (would demote to L5 as well).
3. Add an invariant to `/admin/set-user-level` that refuses to set a wallet's level above
   its parent's level.

### Broader gap
Current audit runs API-side from master's tree, so it can't detect **orphaned users**
(users not descending from master) or `relationship_tree` structural bugs (bad `ancestors`,
`depth` mismatches, `users.referrer_wallet` vs `relationship_tree.referrer_wallet`
disagreement). If needed, add a server-side admin endpoint that scans Mongo directly.
