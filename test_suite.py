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
    return post("/user/set-user-level", {"wallet_address": wallet, "level": level, "signature": "test_signature"})

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

    # Reset DB for clean test run
    try:
        from pymongo import MongoClient
        mc = MongoClient("mongodb://localhost:27017")
        mc.drop_database("xfee_sale_test")
        # Give server a moment to reinitialize pool after DB drop
        time.sleep(2)
        # Trigger pool replenish
        rr = post("/admin/pool/replenish", {})
        test("Database reset + pool replenished", rr.status_code == 200)
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

    # SOL calculation: (100 * $2 + $5 gas) / $150 = $205 / $150 ≈ 1.366667
    expected_sol = (100 * 2.0 + 5.0) / 150.0
    test("SOL amount calculation correct", abs(sol_a - expected_sol) < 0.001,
         f"expected {expected_sol:.6f}, got {sol_a}")

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
    test("User A self_purchase == $200", user_a["self_purchase"] == 200.0,
         f"got {user_a['self_purchase']}")
    test("User A total_tokens_sold == 100", user_a["total_tokens_sold"] == 100)

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
    test("User A total_sales_usd == $100 (50 XFEE * $2)", user_a["total_sales_usd"] == 100.0,
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
            test("Alloc has sale_usd == 100", a0["sale_usd"] == 100.0, f"got {a0['sale_usd']}")
            test("Alloc is indexed", a0["indexed"] is True)
            # A is L1 (10%), so differential = 10% - 0% = 10%
            test("Differential rate is 0.10 for L1", a0["differential_rate"] == 0.10,
                 f"got {a0['differential_rate']}")
            # Commission SOL = sol_received * 0.10
            sol_received_b = purchase_b["sol_amount_received"]
            expected_commission = sol_received_b * 0.10
            test("Commission SOL amount correct (10% of received)",
                 abs(a0["sol_amount"] - expected_commission) < 0.0001,
                 f"expected {expected_commission:.6f}, got {a0['sol_amount']}")
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

    # Verify total_sales_usd for A includes both B and C sales
    r = get_user(WALLETS["A"])
    user_a = r.json()
    expected_network_sales = (50 * 2.0) + (75 * 2.0)  # $100 + $150 = $250
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
    c_from_d = [a for a in c_comm if abs(a["sale_usd"] - 400.0) < 0.01]  # 200 * $2 = $400
    test("C gets commission alloc from D's purchase", len(c_from_d) > 0,
         f"allocs with sale_usd=400: {len(c_from_d)}")
    if c_from_d:
        test("C's differential rate is 0.20", c_from_d[0]["differential_rate"] == 0.20,
             f"got {c_from_d[0]['differential_rate']}")
        expected_c_comm = sol_d * 0.20
        test("C's commission amount correct (20%)",
             abs(c_from_d[0]["sol_amount"] - expected_c_comm) < 0.001,
             f"expected {expected_c_comm:.6f}, got {c_from_d[0]['sol_amount']}")

    # Check B's commission from D (differential: L5 - L1 = 8%)
    r = get_user_allocs(WALLETS["B"])
    b_allocs = r.json()
    b_comm = [a for a in b_allocs["items"] if a["alloc_type"] == "commission"]
    b_from_d = [a for a in b_comm if abs(a["sale_usd"] - 400.0) < 0.01]
    test("B gets commission alloc from D's purchase", len(b_from_d) > 0)
    if b_from_d:
        test("B's differential rate is 0.08 (L5 minus L1)", b_from_d[0]["differential_rate"] == 0.08,
             f"got {b_from_d[0]['differential_rate']}")
        expected_b_comm = sol_d * 0.08
        test("B's commission amount correct (8%)",
             abs(b_from_d[0]["sol_amount"] - expected_b_comm) < 0.001,
             f"expected {expected_b_comm:.6f}, got {b_from_d[0]['sol_amount']}")

    # A should get a ZERO alloc (L1 < highest_paid L5)
    r = get_user_allocs(WALLETS["A"])
    a_allocs = r.json()
    a_comm = [a for a in a_allocs["items"] if a["alloc_type"] == "commission"]
    a_from_d = [a for a in a_comm if abs(a["sale_usd"] - 400.0) < 0.01]
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
    test("User A self_purchase updated to $250 (100+25)*$2",
         user_a["self_purchase"] == 250.0,
         f"got {user_a['self_purchase']}")
    test("User A total_tokens_sold == 125", user_a["total_tokens_sold"] == 125,
         f"got {user_a['total_tokens_sold']}")

    r = get_user_purchases(WALLETS["A"])
    test("User A has 2 purchases", r.json()["total"] == 2, f"got {r.json()['total']}")

    # ── Admin Endpoints ──────────────────────────────────────
    section("ADMIN ENDPOINTS")

    r = post("/admin/pool/replenish", {})
    test("Pool replenish works", r.status_code == 200)

    r = client.post(f"{BASE}/admin/reindex/{WALLETS['A']}")
    test("Admin reindex works", r.status_code == 200)

    # Verify data still consistent after reindex
    r = get_user(WALLETS["A"])
    user_a = r.json()
    test("User A data consistent after reindex", user_a["level"] >= 1)

    r = client.post(f"{BASE}/admin/reindex/invalid_address")
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

    # Verify B's stats
    r = get_user(WALLETS["B"])
    user_b = r.json()
    test("User B level >= 5 (was manually set)", user_b["level"] >= 5,
         f"got {user_b['level']}")
    test("User B self_purchase == $100 (50*$2)", user_b["self_purchase"] == 100.0,
         f"got {user_b['self_purchase']}")
    # B's network: C and D purchased
    expected_b_sales = (75 * 2.0) + (200 * 2.0)  # C=$150, D=$400
    test(f"User B total_sales_usd == ${expected_b_sales} (C + D)",
         user_b["total_sales_usd"] == expected_b_sales,
         f"got {user_b['total_sales_usd']}")

    # Verify C's stats
    r = get_user(WALLETS["C"])
    user_c = r.json()
    test("User C self_purchase == $150 (75*$2)", user_c["self_purchase"] == 150.0,
         f"got {user_c['self_purchase']}")
    # C's network: only D
    test("User C total_sales_usd == $400 (D only)", user_c["total_sales_usd"] == 400.0,
         f"got {user_c['total_sales_usd']}")
    test("User C direct_referral_count == 1", user_c["direct_referral_count"] == 1,
         f"got {user_c['direct_referral_count']}")

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
