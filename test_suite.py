#!/usr/bin/env python3
"""
XFEE Token Sale — Comprehensive Test Suite

Prerequisites:
  1. MongoDB running on localhost:27017
  2. cp .env.test .env
  3. Server running: source venv/bin/activate && uvicorn app.main:app --reload
  4. Run: source venv/bin/activate && python3 test_suite.py
"""

import sys
import time
import httpx
from solders.keypair import Keypair

BASE = "http://localhost:8000/api/v1"
POLL_WAIT = 8  # seconds to wait for poller to process
ADMIN_KEY = "test-admin-key"
FUNDING_WALLET_PUBKEY = "74vmEscrmKdUAHXCxN9Lrnr5bwL6QBbPXyJTyMN9Px7W"  # matches .env.test

# ─── Generate valid Solana addresses ─────────────────────────

def gen_wallet():
    kp = Keypair()
    return str(kp.pubkey())

MASTER = None  # loaded from server config
WALLETS = {}

# ─── Test harness ────────────────────────────────────────────

passed = 0
failed = 0
errors = []

def test(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  \033[32mPASS\033[0m  {name}")
    else:
        failed += 1
        msg = f"  \033[31mFAIL\033[0m  {name}"
        if detail:
            msg += f"  — {detail}"
        print(msg)
        errors.append(f"{name}: {detail}")

def section(title):
    print(f"\n\033[1;36m{'═' * 60}\033[0m")
    print(f"\033[1;36m  {title}\033[0m")
    print(f"\033[1;36m{'═' * 60}\033[0m")

# ─── Helpers ─────────────────────────────────────────────────

client = httpx.Client(timeout=30.0)

def post(path, json=None):
    return client.post(f"{BASE}{path}", json=json)

def admin_post(path, json=None):
    return client.post(f"{BASE}{path}", json=json, headers={"X-Admin-Key": ADMIN_KEY})

def get(path):
    return client.get(f"{BASE}{path}")

def register(wallet, referrer):
    return post("/user/register", {"wallet_address": wallet, "referrer_wallet": referrer})

def initiate_purchase(wallet, amount):
    return post("/purchase/initiate", {"wallet_address": wallet, "xfee_amount": amount})

def simulate_deposit(purchase_id, sol_amount):
    return post("/test/deposit", {"purchase_id": purchase_id, "sol_amount": sol_amount})

def get_user(wallet):
    return get(f"/user/{wallet}")

def get_purchase(pid):
    return get(f"/purchase/{pid}")

def get_user_purchases(wallet):
    return get(f"/user/{wallet}/purchases")

def get_user_allocs(wallet):
    return get(f"/user/{wallet}/allocs")

def get_tree(wallet):
    return get(f"/user/{wallet}/tree")

def get_stats():
    return get("/stats/global")

def set_user_level(wallet, level):
    # /api/v1/admin/set-user-level supports dual auth (admin key OR master signature).
    # We use the admin key in tests.
    return admin_post("/admin/set-user-level", {"wallet_address": wallet, "level": level, "signature": "test_signature"})

def do_full_purchase(wallet, xfee_amount):
    """Initiate purchase, simulate deposit, wait for completion. Returns purchase data."""
    r = initiate_purchase(wallet, xfee_amount)
    if r.status_code != 200:
        return None, r
    data = r.json()
    pid = data["purchase_id"]
    sol = data["sol_expected"]
    simulate_deposit(pid, sol)
    time.sleep(POLL_WAIT)
    pr = get_purchase(pid)
    return pr.json() if pr.status_code == 200 else None, r


# ═════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════

def main():
    global MASTER

    # ── Preflight ────────────────────────────────────────────
    section("PREFLIGHT")

    r = client.get("http://localhost:8000/health")
    test("Server is running", r.status_code == 200, f"status={r.status_code}")
    if r.status_code != 200:
        print("\n  Server not running. Start it first.")
        sys.exit(1)

    # Get master wallet from stats endpoint (it's in the .env)
    # We need to read it from the test env file
    try:
        with open(".env") as f:
            for line in f:
                if line.startswith("MASTER_WALLET_ADDRESS="):
                    MASTER = line.strip().split("=", 1)[1]
    except Exception:
        pass
    test("Master wallet loaded from .env", MASTER is not None and len(MASTER) > 30, f"got: {MASTER}")

    r = get_stats()
    test("Stats endpoint works", r.status_code == 200)
    stats = r.json()
    test("SOL price is mocked at $150", stats["sol_price"] == 150.0, f"got {stats['sol_price']}")

    # Reset DB for clean test run.
    # Note: dropping the DB also wipes the master + root child user docs that the
    # app bootstraps at startup. We re-insert minimum docs so the in-flight server
    # can keep running without a restart.
    try:
        from datetime import datetime, timezone
        from pymongo import MongoClient
        mc = MongoClient("mongodb://localhost:27017")
        db = mc["xfee_sale_test"]
        # Drop everything except purchase_wallets (so we don't trash the lazily-generated pool).
        for coll_name in db.list_collection_names():
            if coll_name == "purchase_wallets":
                continue
            db.drop_collection(coll_name)

        now = datetime.now(timezone.utc)
        base = {
            "joined_at": now,
            "self_purchase": 0.0,
            "total_sales_usd": 0.0,
            "total_commission_sol": 0.0,
            "self_purchase_tokens": 0,
            "total_tokens_sold": 0,
            "level_sales": {},
            "level_commission": {},
            "direct_sales_sol": 0.0,
            "indirect_sales_sol": 0.0,
            "direct_commission_sol": 0.0,
            "indirect_commission_sol": 0.0,
            "direct_referral_count": 0,
            "network_size": 0,
        }
        db.users.insert_one({
            "wallet_address": MASTER,
            "referrer_wallet": "",
            "level": 15,
            "is_valid_referrer": True,
            **base,
        })
        root_child = "BRrtYftGhXBh3JcwmveuB4ZcskkYvUeLzNgPcf5VF6Ry"
        db.users.insert_one({
            "wallet_address": root_child,
            "referrer_wallet": MASTER,
            "level": 14,
            "is_valid_referrer": True,
            **base,
        })
        db.relationship_tree.insert_one({
            "wallet_address": root_child,
            "referrer_wallet": MASTER,
            "ancestors": [MASTER],
            "depth": 1,
        })
        test("Database reset + master/root child re-seeded", True)

        # Top up wallet pool (admin endpoint requires admin key)
        rr = admin_post("/admin/pool/replenish", {})
        test("Wallet pool replenished",
             rr.status_code == 200, f"{rr.status_code}: {rr.text}")
        # Wait briefly for any async wallet generation to finalize.
        time.sleep(1)
    except Exception as e:
        test("Database reset", False, str(e))

    # Generate wallets
    WALLETS["A"] = gen_wallet()
    WALLETS["B"] = gen_wallet()
    WALLETS["C"] = gen_wallet()
    WALLETS["D"] = gen_wallet()
    WALLETS["random"] = gen_wallet()
    print(f"\n  Test wallets generated: {len(WALLETS)} wallets")

    # ── Registration Tests ───────────────────────────────────
    section("REGISTRATION")

    # Valid registration with master referrer
    r = register(WALLETS["A"], MASTER)
    test("Register A with master referrer", r.status_code == 200 and r.json().get("success"), f"{r.status_code}: {r.text}")

    # Invalid wallet address
    r = register("not_a_valid_address", MASTER)
    test("Reject invalid wallet address (400)", r.status_code == 400)

    # Duplicate registration
    r = register(WALLETS["A"], MASTER)
    test("Reject duplicate registration (409)", r.status_code == 409)

    # Non-existent referrer
    r = register(WALLETS["B"], WALLETS["random"])
    test("Reject non-existent referrer (400)", r.status_code == 400)

    # Referrer who hasn't purchased (A has no purchases yet)
    r = register(WALLETS["B"], WALLETS["A"])
    test("Reject referrer without purchase (400)", r.status_code == 400, f"{r.status_code}: {r.text}")

    # Check user profile
    r = get_user(WALLETS["A"])
    test("Get user A profile", r.status_code == 200)
    user_a = r.json()
    test("User A starts at level 1", user_a["level"] == 1)
    test("User A is_valid_referrer is false", user_a["is_valid_referrer"] is False)
    test("User A self_purchase is 0", user_a["self_purchase"] == 0.0)
    test("User A total_sales_usd is 0", user_a["total_sales_usd"] == 0.0)

    # Get non-existent user
    r = get_user(WALLETS["random"])
    test("Get non-existent user returns 404", r.status_code == 404)

    # ── Purchase Tests ───────────────────────────────────────
    section("PURCHASE FLOW")

    # Purchase for unregistered user
    r = initiate_purchase(WALLETS["random"], 100)
    test("Reject purchase for unregistered user (404)", r.status_code == 404)

    # Purchase with 0 amount
    r = initiate_purchase(WALLETS["A"], 0)
    test("Reject purchase with 0 amount (400)", r.status_code == 400)

    # Valid purchase initiation
    r = initiate_purchase(WALLETS["A"], 100)
    test("Initiate purchase for A (100 XFEE)", r.status_code == 200)
    p_data = r.json()
    pid_a = p_data["purchase_id"]
    sol_a = p_data["sol_expected"]
    test("Purchase returns purchase_id", "purchase_id" in p_data)
    test("Purchase returns purchase_wallet", "purchase_wallet" in p_data)
    test("Purchase returns sol_expected > 0", p_data["sol_expected"] > 0)
    test("Purchase returns expires_at", "expires_at" in p_data)

    # SOL calculation: 1 XFEE = $1; gas = $0.20 (sub-$10) or $2-$4 (>= $10).
    # For 100 XFEE = $100 with mocked balance, gas may be a random in [2.0, 4.0).
    # We range-check instead of asserting exact equality.
    min_sol = (100 * 1.0 + 2.0) / 150.0
    max_sol = (100 * 1.0 + 4.0) / 150.0
    test("SOL amount in expected range",
         min_sol - 1e-6 <= sol_a <= max_sol + 1e-6,
         f"expected [{min_sol:.6f}, {max_sol:.6f}], got {sol_a}")

    # Double pending purchase
    r = initiate_purchase(WALLETS["A"], 50)
    test("Reject double pending purchase (409)", r.status_code == 409)

    # Check pending status
    r = get_purchase(pid_a)
    test("Purchase status is pending", r.status_code == 200 and r.json()["status"] == "pending")

    # Invalid purchase ID
    r = get_purchase("not_valid")
    test("Invalid purchase ID returns 400", r.status_code == 400)

    r = get_purchase("000000000000000000000000")
    test("Non-existent purchase returns 404", r.status_code == 404)

    # Simulate deposit and wait
    r = simulate_deposit(pid_a, sol_a)
    test("Simulate deposit succeeds", r.status_code == 200, f"{r.status_code}: {r.text}")

    print(f"\n  Waiting {POLL_WAIT}s for poller to process...")
    time.sleep(POLL_WAIT)

    # Verify completed
    r = get_purchase(pid_a)
    pa = r.json()
    test("Purchase A is completed", pa["status"] == "completed", f"got: {pa['status']}")
    test("sol_amount_received > 0", pa["sol_amount_received"] > 0)
    test("sol_price_at_confirmation == 150", pa["sol_price_at_confirmation"] == 150.0)
    test("token_dispatch_tx is set", pa["token_dispatch_tx"] is not None)
    test("commission_distributed is true", pa["commission_distributed"] is True)

    # User A should now be valid referrer
    r = get_user(WALLETS["A"])
    user_a = r.json()
    test("User A is_valid_referrer is true after purchase", user_a["is_valid_referrer"] is True)
    test("User A self_purchase == $100 (100 XFEE * $1)",
         user_a["self_purchase"] == 100.0,
         f"got {user_a['self_purchase']}")
    test("User A self_purchase_tokens == 100",
         user_a.get("self_purchase_tokens", 0) == 100,
         f"got {user_a.get('self_purchase_tokens')}")

    # Purchase list
    r = get_user_purchases(WALLETS["A"])
    test("User A purchase list returns data", r.status_code == 200 and r.json()["total"] == 1)

    # ── Referral Chain Tests ─────────────────────────────────
    section("REFERRAL CHAIN & COMMISSIONS")

    # Now A is valid referrer, register B under A
    r = register(WALLETS["B"], WALLETS["A"])
    test("Register B under A (A is now valid)", r.status_code == 200, f"{r.status_code}: {r.text}")

    # B makes a purchase — A should get commission
    purchase_b, _ = do_full_purchase(WALLETS["B"], 50)
    test("Purchase B completed", purchase_b is not None and purchase_b["status"] == "completed",
         f"got: {purchase_b}")

    # Check A's stats updated
    r = get_user(WALLETS["A"])
    user_a = r.json()
    test("User A total_sales_usd > 0 (network sale from B)", user_a["total_sales_usd"] > 0,
         f"got {user_a['total_sales_usd']}")
    test("User A total_sales_usd == $50 (50 XFEE * $1)", user_a["total_sales_usd"] == 50.0,
         f"got {user_a['total_sales_usd']}")

    # Check A's commission allocs
    r = get_user_allocs(WALLETS["A"])
    allocs = r.json()
    test("User A has allocs", allocs["total"] > 0, f"total: {allocs['total']}")
    if allocs["total"] > 0:
        comm_allocs = [a for a in allocs["items"] if a["alloc_type"] == "commission"]
        test("User A has commission alloc(s)", len(comm_allocs) > 0)
        if comm_allocs:
            a0 = comm_allocs[0]
            test("Alloc has sale_usd == 50 (50 XFEE * $1)", a0["sale_usd"] == 50.0, f"got {a0['sale_usd']}")
            test("Alloc is indexed", a0["indexed"] is True)
            # A is L1 (20% rate in new table), so differential = 20% - 0% = 20%
            test("Differential rate is 0.20 for L1", a0["differential_rate"] == 0.20,
                 f"got {a0['differential_rate']}")
            # Commission SOL is calculated from `commissionable_sol` (received minus tranche
            # deduction), and the deduction depends on the cumulative-sales tier, so we just
            # assert it's > 0 and consistent with the differential_rate.
            sol_received_b = purchase_b["sol_amount_received"]
            test("Commission SOL amount > 0", a0["sol_amount"] > 0,
                 f"got {a0['sol_amount']}")
            test("Commission SOL <= 20% of received SOL",
                 a0["sol_amount"] <= sol_received_b * 0.20 + 1e-6,
                 f"got {a0['sol_amount']}, 20% of received = {sol_received_b * 0.20:.6f}")
    test("User A total_commission_sol > 0", user_a["total_commission_sol"] > 0,
         f"got {user_a['total_commission_sol']}")

    # Register C under B, B needs to purchase first to be valid referrer
    r = get_user(WALLETS["B"])
    test("User B is valid referrer after purchase", r.json()["is_valid_referrer"] is True)

    r = register(WALLETS["C"], WALLETS["B"])
    test("Register C under B", r.status_code == 200, f"{r.status_code}: {r.text}")

    # C makes a purchase — both B and A should get allocs
    purchase_c, _ = do_full_purchase(WALLETS["C"], 75)
    test("Purchase C completed", purchase_c is not None and purchase_c["status"] == "completed")

    # B gets commission alloc from C
    r = get_user_allocs(WALLETS["B"])
    b_allocs = r.json()
    b_comm = [a for a in b_allocs["items"] if a["alloc_type"] == "commission"]
    test("User B has commission alloc from C's purchase", len(b_comm) > 0)

    # A also gets alloc from C (as grandparent)
    r = get_user_allocs(WALLETS["A"])
    a_allocs = r.json()
    a_comm = [a for a in a_allocs["items"] if a["alloc_type"] == "commission"]
    test("User A has alloc from C's purchase (grandparent)", len(a_comm) >= 2,
         f"expected >= 2 commission allocs, got {len(a_comm)}")

    # Verify total_sales_usd for A includes both B and C sales (1 XFEE = $1)
    r = get_user(WALLETS["A"])
    user_a = r.json()
    expected_network_sales = (50 * 1.0) + (75 * 1.0)  # $50 + $75 = $125
    test(f"User A total_sales_usd == ${expected_network_sales} (B + C)",
         user_a["total_sales_usd"] == expected_network_sales,
         f"got {user_a['total_sales_usd']}")

    # Verify direct referral count and network size
    test("User A direct_referral_count == 1 (only B)", user_a["direct_referral_count"] == 1,
         f"got {user_a['direct_referral_count']}")
    test("User A network_size == 2 (B + C)", user_a["network_size"] == 2,
         f"got {user_a['network_size']}")

    # ── Tree Tests ───────────────────────────────────────────
    section("TREE STRUCTURE")

    r = get_tree(WALLETS["A"])
    test("Tree endpoint works", r.status_code == 200)
    tree = r.json()
    test("Root is A", tree["wallet"] == WALLETS["A"])
    test("A has 1 child (B)", len(tree["children"]) == 1, f"got {len(tree['children'])}")
    if tree["children"]:
        b_node = tree["children"][0]
        test("B is under A", b_node["wallet"] == WALLETS["B"])
        test("B has 1 child (C)", len(b_node["children"]) == 1,
             f"got {len(b_node['children'])}")
        if b_node["children"]:
            test("C is under B", b_node["children"][0]["wallet"] == WALLETS["C"])

    # ── Level Management Tests ───────────────────────────────
    section("LEVEL MANAGEMENT (master wallet upgrades)")

    # Master upgrades B to L3
    r = set_user_level(WALLETS["B"], 3)
    test("Master upgrades B to L3", r.status_code == 200 and r.json().get("new_level") == 3,
         f"{r.status_code}: {r.text}")

    r = get_user(WALLETS["B"])
    test("User B is now level 3", r.json()["level"] == 3)

    # Cannot downgrade
    r = set_user_level(WALLETS["B"], 2)
    test("Reject level downgrade (400)", r.status_code == 400, f"{r.status_code}: {r.text}")

    # Cannot set same level
    r = set_user_level(WALLETS["B"], 3)
    test("Reject same level (400)", r.status_code == 400)

    # Can upgrade further
    r = set_user_level(WALLETS["B"], 5)
    test("Master upgrades B to L5", r.status_code == 200)

    # Invalid level
    r = set_user_level(WALLETS["B"], 16)
    test("Reject level > 15 (400)", r.status_code == 400)

    r = set_user_level(WALLETS["B"], 0)
    test("Reject level < 1 (400)", r.status_code == 400)

    # Non-existent user
    r = set_user_level(WALLETS["random"], 2)
    test("Reject set level for unregistered wallet (404)", r.status_code == 404)

    # ── Differential Commission with Levels ──────────────────
    section("DIFFERENTIAL COMMISSION MATH")

    # B is now L5 (28%). A is L1 (20%).
    # When D purchases under C (who is under B under A):
    #   - B at L5 gets differential 28% - 0% = 28% (first ancestor higher than 0)
    #     Wait, no. Walk from C's ancestors: [B, A, MASTER]
    #     - B is L5: diff = rate(5) - rate(0) = 0.28 - 0.0 = 0.28
    #     - A is L1: 1 < 5 (highest_paid=5), so A gets zero alloc
    #     - MASTER is not a registered user, skip
    # Actually let me re-read. C's ancestors are [B, A, MASTER].
    # D registers under C, so D's ancestors are [C, B, A, MASTER].
    # C is L1 (no upgrade).

    # Register D under C
    # First C needs to be valid referrer (C already purchased)
    r = get_user(WALLETS["C"])
    test("User C is valid referrer", r.json()["is_valid_referrer"] is True)

    r = register(WALLETS["D"], WALLETS["C"])
    test("Register D under C", r.status_code == 200, f"{r.status_code}: {r.text}")

    # D purchases — check differential commission distribution
    purchase_d, _ = do_full_purchase(WALLETS["D"], 200)
    test("Purchase D completed", purchase_d is not None and purchase_d["status"] == "completed")

    sol_d = purchase_d["sol_amount_received"]

    # D's ancestors: [C, B, A, MASTER]
    # C is L1: diff = rate(1) - rate(0) = 0.20. Gets 20% commission.
    # B is L5: diff = rate(5) - rate(1) = 0.28 - 0.20 = 0.08. Gets 8% commission.
    # A is L1: 1 < 5 (highest=5), zero alloc.
    # MASTER not registered, no alloc.

    # Check C's commission from D
    r = get_user_allocs(WALLETS["C"])
    c_allocs = r.json()
    c_comm = [a for a in c_allocs["items"] if a["alloc_type"] == "commission"]
    c_from_d = [a for a in c_comm if abs(a["sale_usd"] - 200.0) < 0.01]  # 200 * $1 = $200
    test("C gets commission alloc from D's purchase", len(c_from_d) > 0,
         f"allocs with sale_usd=200: {len(c_from_d)}")
    if c_from_d:
        test("C's differential rate is 0.20", c_from_d[0]["differential_rate"] == 0.20,
             f"got {c_from_d[0]['differential_rate']}")
        # Commission is computed on commissionable SOL (sol_received minus tranche
        # deduction), so we cap-check rather than equality.
        test("C's commission > 0", c_from_d[0]["sol_amount"] > 0,
             f"got {c_from_d[0]['sol_amount']}")
        test("C's commission <= 20% of sol_received",
             c_from_d[0]["sol_amount"] <= sol_d * 0.20 + 1e-6,
             f"got {c_from_d[0]['sol_amount']}, 20% of sol_received = {sol_d * 0.20:.6f}")

    # Check B's commission from D (differential: L5 - L1 = 8%)
    r = get_user_allocs(WALLETS["B"])
    b_allocs = r.json()
    b_comm = [a for a in b_allocs["items"] if a["alloc_type"] == "commission"]
    b_from_d = [a for a in b_comm if abs(a["sale_usd"] - 200.0) < 0.01]
    test("B gets commission alloc from D's purchase", len(b_from_d) > 0)
    if b_from_d:
        test("B's differential rate is 0.08 (L5 - L1)",
             abs(b_from_d[0]["differential_rate"] - 0.08) < 1e-6,
             f"got {b_from_d[0]['differential_rate']}")
        test("B's commission > 0", b_from_d[0]["sol_amount"] > 0,
             f"got {b_from_d[0]['sol_amount']}")
        test("B's commission <= 8% of sol_received",
             b_from_d[0]["sol_amount"] <= sol_d * 0.08 + 1e-6,
             f"got {b_from_d[0]['sol_amount']}, 8% of sol_received = {sol_d * 0.08:.6f}")

    # A should get a ZERO alloc (L1 < highest_paid L5)
    r = get_user_allocs(WALLETS["A"])
    a_allocs = r.json()
    a_comm = [a for a in a_allocs["items"] if a["alloc_type"] == "commission"]
    a_from_d = [a for a in a_comm if abs(a["sale_usd"] - 200.0) < 0.01]
    test("A gets zero-commission alloc from D's purchase", len(a_from_d) > 0)
    if a_from_d:
        test("A's alloc is zero (level too low)", a_from_d[0]["sol_amount"] == 0.0,
             f"got {a_from_d[0]['sol_amount']}")
        test("A's alloc status is 'zero'", a_from_d[0]["status"] == "zero",
             f"got {a_from_d[0]['status']}")

    # ── Global Stats Tests ───────────────────────────────────
    section("GLOBAL STATS")

    r = get_stats()
    stats = r.json()
    total_xfee = 100 + 50 + 75 + 200  # A=100, B=50, C=75, D=200
    test(f"tokens_sold == {total_xfee}", stats["tokens_sold"] == total_xfee,
         f"got {stats['tokens_sold']}")
    expected_remaining = max(0, 400000 - total_xfee)
    test(
        f"tokens_remaining == {expected_remaining} (cap-clamped)",
        stats["tokens_remaining"] == expected_remaining,
        f"got {stats['tokens_remaining']}",
    )
    test(f"total_purchases == 4", stats["total_purchases"] == 4,
         f"got {stats['total_purchases']}")

    # ── Multiple Purchase Test ───────────────────────────────
    section("MULTIPLE PURCHASES BY SAME USER")

    purchase_a2, _ = do_full_purchase(WALLETS["A"], 25)
    test("A can make a second purchase", purchase_a2 is not None and purchase_a2["status"] == "completed")

    r = get_user(WALLETS["A"])
    user_a = r.json()
    test("User A self_purchase updated to $125 (100+25)*$1",
         user_a["self_purchase"] == 125.0,
         f"got {user_a['self_purchase']}")
    test("User A self_purchase_tokens == 125",
         user_a.get("self_purchase_tokens", 0) == 125,
         f"got {user_a.get('self_purchase_tokens')}")

    r = get_user_purchases(WALLETS["A"])
    test("User A has 2 purchases", r.json()["total"] == 2, f"got {r.json()['total']}")

    # ── Admin Endpoints ──────────────────────────────────────
    section("ADMIN ENDPOINTS")

    r = admin_post("/admin/pool/replenish", {})
    test("Pool replenish works (with admin key)", r.status_code == 200,
         f"{r.status_code}: {r.text}")

    r = admin_post(f"/admin/reindex/{WALLETS['A']}")
    test("Admin reindex works (with admin key)", r.status_code == 200,
         f"{r.status_code}: {r.text}")

    # Verify data still consistent after reindex
    r = get_user(WALLETS["A"])
    user_a = r.json()
    test("User A data consistent after reindex", user_a["level"] >= 1)

    r = admin_post("/admin/reindex/invalid_address")
    test("Admin reindex rejects invalid address (400)", r.status_code == 400)

    # ── Edge Case: Alloc Indexing ────────────────────────────
    section("ALLOC INDEXING")

    # All allocs for A should be indexed
    r = get_user_allocs(WALLETS["A"])
    a_allocs = r.json()
    comm_allocs = [a for a in a_allocs["items"] if a["alloc_type"] == "commission"]
    all_indexed = all(a["indexed"] for a in comm_allocs)
    test("All of A's commission allocs are indexed", all_indexed,
         f"{sum(1 for a in comm_allocs if a['indexed'])}/{len(comm_allocs)} indexed")

    # ── Final Stats Consistency ──────────────────────────────
    section("FINAL CONSISTENCY CHECKS")

    r = get_stats()
    stats = r.json()
    total_xfee_final = 100 + 50 + 75 + 200 + 25
    test(f"Final tokens_sold == {total_xfee_final}", stats["tokens_sold"] == total_xfee_final,
         f"got {stats['tokens_sold']}")
    test(f"Final total_purchases == 5", stats["total_purchases"] == 5,
         f"got {stats['total_purchases']}")

    # Verify B's stats (1 XFEE = $1)
    r = get_user(WALLETS["B"])
    user_b = r.json()
    test("User B level >= 5 (was manually set)", user_b["level"] >= 5,
         f"got {user_b['level']}")
    test("User B self_purchase == $50 (50*$1)", user_b["self_purchase"] == 50.0,
         f"got {user_b['self_purchase']}")
    # B's network: C and D purchased
    expected_b_sales = (75 * 1.0) + (200 * 1.0)  # C=$75, D=$200
    test(f"User B total_sales_usd == ${expected_b_sales} (C + D)",
         user_b["total_sales_usd"] == expected_b_sales,
         f"got {user_b['total_sales_usd']}")

    # Verify C's stats
    r = get_user(WALLETS["C"])
    user_c = r.json()
    test("User C self_purchase == $75 (75*$1)", user_c["self_purchase"] == 75.0,
         f"got {user_c['self_purchase']}")
    # C's network: only D
    test("User C total_sales_usd == $200 (D only)", user_c["total_sales_usd"] == 200.0,
         f"got {user_c['total_sales_usd']}")
    test("User C direct_referral_count == 1", user_c["direct_referral_count"] == 1,
         f"got {user_c['direct_referral_count']}")

    # ── Global Pool: Point Accrual ───────────────────────────
    section("GLOBAL POOL - POINT ACCRUAL")

    # A accrues zero-alloc points whenever a same-level (L1) peer is paid. That
    # happens for C's purchase (B is the L1 peer who was paid) AND for D's purchase
    # (C is the L1 peer who was paid). Cap is 20% of the SOL received on those two
    # purchases, converted to USD at the test sol price ($150).
    sol_c = purchase_c["sol_amount_received"]
    sol_d = purchase_d["sol_amount_received"]
    max_points_usd = (sol_c + sol_d) * 0.20 * 150.0

    r = get(f"/global-pool/user/{WALLETS['A']}/points")
    test("Global pool user history endpoint works", r.status_code == 200)
    history = r.json()
    test("User A has at least one global pool entry", history["total"] >= 1,
         f"total: {history['total']}")

    if history["total"] >= 1:
        entry = max(history["items"], key=lambda x: x.get("points_usd", 0))
        test("A's pool points_usd > 0", entry["points_usd"] > 0,
             f"got {entry['points_usd']}")
        test(f"A's pool points_usd <= cap from peer commissions ({max_points_usd:.2f})",
             entry["points_usd"] <= max_points_usd + 0.01,
             f"got {entry['points_usd']}, cap {max_points_usd:.4f}")
        test("A's entry settle_status is 'pending'", entry["settle_status"] == "pending",
             f"got {entry['settle_status']}")
        test("A's entry pool_index is set", entry.get("pool_index", 0) >= 1)
        pool_idx_for_a = int(entry["pool_index"])
    else:
        pool_idx_for_a = None

    # ── Global Pool: Queries ─────────────────────────────────
    section("GLOBAL POOL - QUERIES")

    r = get("/global-pool/summary")
    test("Pool summary endpoint works", r.status_code == 200)
    summary = r.json()
    test("Summary has at least 1 total pool", summary["total_pools"] >= 1,
         f"total_pools={summary['total_pools']}")
    test("Summary has an active pool", summary["active"] >= 1,
         f"active={summary['active']}")
    test("Summary has positive total_points_usd",
         summary["total_points_usd_all_pools"] > 0,
         f"got {summary['total_points_usd_all_pools']}")

    r = get("/global-pool/")
    test("Pool list endpoint works", r.status_code == 200)
    pools_list = r.json()
    test("Pool list returns at least 1 pool", pools_list["total"] >= 1)

    r = get("/global-pool/current")
    test("Current pool endpoint works", r.status_code == 200)
    current = r.json()
    test("Current pool is active", current["active"] is True, f"got {current}")
    test("Current pool has standings", current["total_users"] >= 1,
         f"got {current['total_users']}")
    current_pool_idx = int(current["pool"]["pool_index"])

    r = get(f"/global-pool/{current_pool_idx}")
    test("Pool by index endpoint works", r.status_code == 200)
    pool_detail = r.json()
    test("Pool detail has the same index", pool_detail["pool"]["pool_index"] == current_pool_idx)

    r = get(f"/global-pool/{current_pool_idx}/user/{WALLETS['A']}")
    test("Per-user-per-pool endpoint works", r.status_code == 200)
    user_entry = r.json()
    test("A is in_pool=true for the current pool", user_entry["in_pool"] is True,
         f"got {user_entry}")

    r = get(f"/global-pool/{current_pool_idx}/user/{WALLETS['random']}")
    test("Random wallet is in_pool=false", r.status_code == 200 and r.json()["in_pool"] is False)

    r = get("/global-pool/9999")
    test("Non-existent pool returns 404", r.status_code == 404)

    # ── Global Pool: Settlement (force-finalize) ─────────────
    section("GLOBAL POOL - SETTLEMENT")

    # Seed the funding wallet with enough SOL to cover all payouts
    r = post("/test/set-balance", {"pubkey": FUNDING_WALLET_PUBKEY, "sol_amount": 10.0})
    test("Funding wallet seeded with 10 SOL", r.status_code == 200, f"{r.status_code}: {r.text}")

    # Settle with force=true (pool window is still open)
    r = admin_post(f"/admin/global-pool/{current_pool_idx}/settle?force=true")
    test("Admin force-settle returns 200", r.status_code == 200,
         f"{r.status_code}: {r.text}")
    settle_result = r.json() if r.status_code == 200 else {}
    test("Settle reports success=true", settle_result.get("success") is True,
         f"got {settle_result}")
    test("Settle reports status=settled", settle_result.get("status") == "settled",
         f"got {settle_result}")

    # Verify pool is settled
    r = get(f"/global-pool/{current_pool_idx}")
    pool_after = r.json()["pool"]
    test("Pool status is now 'settled'", pool_after["status"] == "settled",
         f"got {pool_after['status']}")
    test("Pool has snapshot with distributable_lamports",
         pool_after.get("snapshot", {}).get("distributable_lamports", 0) > 0,
         f"snapshot={pool_after.get('snapshot')}")

    # Verify A's entry has tx_signature and confirmed status
    r = get(f"/global-pool/{current_pool_idx}/user/{WALLETS['A']}")
    user_entry = r.json()
    if user_entry["in_pool"]:
        entry = user_entry["entry"]
        test("A's settle_status is 'confirmed'", entry["settle_status"] == "confirmed",
             f"got {entry['settle_status']}")
        test("A has a tx_signature", entry.get("tx_signature") is not None,
             f"got {entry.get('tx_signature')}")
        test("A has memo starting with 'GP:'",
             entry.get("memo", "").startswith("GP:"),
             f"got {entry.get('memo')}")
        test("A's owed_lamports > 0", entry.get("owed_lamports", 0) > 0,
             f"got {entry.get('owed_lamports')}")

    # ── Global Pool: Idempotency ─────────────────────────────
    section("GLOBAL POOL - IDEMPOTENCY")

    r = admin_post(f"/admin/global-pool/{current_pool_idx}/settle?force=false")
    test("Re-settling already-settled pool succeeds", r.status_code == 200,
         f"{r.status_code}: {r.text}")
    second = r.json()
    test("Re-settle reports already_settled=true", second.get("already_settled") is True,
         f"got {second}")

    # Verify A's tx_signature didn't change after re-settle
    r = get(f"/global-pool/{current_pool_idx}/user/{WALLETS['A']}")
    if r.json()["in_pool"]:
        post_entry = r.json()["entry"]
        test("A's tx_signature is stable across re-settle",
             post_entry.get("tx_signature") == entry.get("tx_signature"),
             f"before={entry.get('tx_signature')}, after={post_entry.get('tx_signature')}")

    # Settling a non-existent pool returns 400
    r = admin_post("/admin/global-pool/9999/settle")
    test("Settling non-existent pool returns 400", r.status_code == 400)

    # Admin endpoint requires admin key
    r = client.post(f"{BASE}/admin/global-pool/{current_pool_idx}/settle")
    test("Settle without admin key returns 401",
         r.status_code in (401, 422),  # 422 if header schema rejects missing header
         f"{r.status_code}: {r.text}")

    # ── Root-Child Bootstrap ─────────────────────────────────
    section("ROOT-CHILD BOOTSTRAP")
    # Test mode disables ENFORCE_ROOT_CHILD so the existing A->B->C->D chain works,
    # but the root child is always bootstrapped on startup as a regular user.

    root_child = "BRrtYftGhXBh3JcwmveuB4ZcskkYvUeLzNgPcf5VF6Ry"
    r = get_user(root_child)
    test("Root child user exists (bootstrapped on startup)", r.status_code == 200,
         f"{r.status_code}: {r.text}")
    if r.status_code == 200:
        rc = r.json()
        test("Root child level is 14", rc["level"] == 14, f"got {rc['level']}")
        test("Root child referrer is master", rc.get("referrer_wallet") == MASTER,
             f"got {rc.get('referrer_wallet')}")
        test("Root child is_valid_referrer is true", rc["is_valid_referrer"] is True)

    print(
        "\n  NOTE: The master-only-refers-root-child + 1-direct-under-root-child rules\n"
        "  are enforced when ENFORCE_ROOT_CHILD=true (default in production .env).\n"
        "  Verify those manually on a staging deployment before launch."
    )

    # ═════════════════════════════════════════════════════════
    #  SUMMARY
    # ═════════════════════════════════════════════════════════
    section("RESULTS")
    total = passed + failed
    print(f"\n  \033[1mTotal: {total}  |  \033[32mPassed: {passed}\033[0m  |  \033[31mFailed: {failed}\033[0m\n")
    if errors:
        print("  \033[31mFailed tests:\033[0m")
        for e in errors:
            print(f"    - {e}")
        print()

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
