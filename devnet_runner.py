"""
Devnet dry-run runner for the deployed Bruce-Lee-Sales-system.

Usage:
  python devnet_runner.py preflight        # check balances + API health
  python devnet_runner.py register         # register A->B->C->D chain
  python devnet_runner.py purchase WALLET XFEE_AMOUNT
                                           # one purchase from one buyer
  python devnet_runner.py chain            # run full chain rc->A->B->C->D purchases
  python devnet_runner.py upgrade WALLET LEVEL
                                           # admin set-user-level
  python devnet_runner.py settle POOL_IDX  # admin settle pool (force=true)
  python devnet_runner.py status WALLET    # show user + recent allocs
  python devnet_runner.py pool-current     # show current pool
  python devnet_runner.py pool POOL_IDX    # show specific pool standings
  python devnet_runner.py poolsummary      # summary

Env (overrideable):
  API_URL    default: https://brucelee-app-sale-cbsgj.ondigitalocean.app
  ADMIN_KEY  default: 5idzSgPntFhW!4ViFNAcezWMibDfUNHUP
  KEYS_FILE  default: .devnet_keys.json
  RPC_URL    default: https://api.devnet.solana.com
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import base58
import httpx
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction

API_URL = os.environ.get("API_URL", "https://brucelee-app-sale-cbsgj.ondigitalocean.app").rstrip("/")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "5idzSgPntFhW!4ViFNAcezWMibDfUNHUP")
KEYS_FILE = os.environ.get("KEYS_FILE", ".devnet_keys.json")
RPC_URL = os.environ.get("RPC_URL", "https://api.devnet.solana.com")

LAMPORTS = 1_000_000_000

# Mapping of friendly name → key in .devnet_keys.json
ROLES = ["master", "treasury", "buyerA", "buyerB", "buyerC", "buyerD"]


# ── helpers ──────────────────────────────────────────────────────────────────

def load_keys() -> dict:
    p = Path(KEYS_FILE)
    if not p.exists():
        die(f"Missing {KEYS_FILE}")
    return json.loads(p.read_text())


def die(msg: str, code: int = 1) -> None:
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(code)


def kp_from(role: str, keys: dict) -> Keypair:
    secret = base58.b58decode(keys[role]["private_b58"])
    return Keypair.from_bytes(secret)


def short(s: str, n: int = 8) -> str:
    return f"{s[:n]}…{s[-4:]}"


def rpc(method: str, params=None) -> dict:
    with httpx.Client(timeout=30.0) as c:
        r = c.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []})
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"RPC {method} error: {body['error']}")
        return body["result"]


def get_balance_sol(addr: str) -> float:
    res = rpc("getBalance", [addr])
    return res["value"] / LAMPORTS


def get_blockhash() -> Hash:
    res = rpc("getLatestBlockhash", [{"commitment": "finalized"}])
    return Hash.from_string(res["value"]["blockhash"])


def send_sol(from_kp: Keypair, to_pubkey: str, sol: float) -> str:
    lamports = int(round(sol * LAMPORTS))
    bh = get_blockhash()
    ix = transfer(TransferParams(from_pubkey=from_kp.pubkey(), to_pubkey=Pubkey.from_string(to_pubkey), lamports=lamports))
    msg = Message.new_with_blockhash([ix], from_kp.pubkey(), bh)
    tx = Transaction.new_unsigned(msg)
    tx.sign([from_kp], bh)
    import base64

    enc = base64.b64encode(bytes(tx)).decode()
    res = rpc("sendTransaction", [enc, {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed"}])
    return res


def confirm_sig(sig: str, timeout_s: int = 60) -> bool:
    start = time.time()
    while time.time() - start < timeout_s:
        res = rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
        val = (res.get("value") or [None])[0]
        if val and val.get("confirmationStatus") in ("confirmed", "finalized"):
            return val.get("err") is None
        time.sleep(2)
    return False


def api_get(path: str, **params):
    with httpx.Client(timeout=30.0) as c:
        r = c.get(f"{API_URL}{path}", params=params)
        return r.status_code, _safe_json(r)


def api_post(path: str, json_body=None, admin: bool = False):
    headers = {"X-Admin-Key": ADMIN_KEY} if admin else {}
    with httpx.Client(timeout=60.0) as c:
        r = c.post(f"{API_URL}{path}", json=json_body, headers=headers)
        return r.status_code, _safe_json(r)


def _safe_json(r: httpx.Response):
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text}


def pp(label: str, code: int, body):
    icon = "OK " if 200 <= code < 300 else "ERR"
    print(f"[{icon} {code}] {label} -> {json.dumps(body, default=str)[:500]}")


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_preflight():
    keys = load_keys()
    print(f"API:    {API_URL}")
    print(f"RPC:    {RPC_URL}")
    print(f"KEYS:   {KEYS_FILE}")
    print()
    code, body = api_get("/health")
    pp("/health", code, body)
    code, body = api_get("/api/v1/stats/global")
    pp("/stats/global", code, body)
    code, body = api_get("/api/v1/global-pool/summary")
    pp("/global-pool/summary", code, body)

    print()
    print("Devnet balances:")
    total = 0.0
    for role in ROLES:
        pk = keys[role]["pubkey"]
        try:
            bal = get_balance_sol(pk)
        except Exception as e:
            bal = -1.0
            print(f"  {role:9s}  {pk}  ERR {e}")
            continue
        total += bal
        flag = "OK " if bal >= 0.05 else "LOW"
        print(f"  [{flag}] {role:9s}  {pk}  {bal:.4f} SOL")
    print(f"  ─────────────────────────────")
    print(f"  total devnet SOL across wallets: {total:.4f}")
    if total < 0.5:
        print("\nWARN: low overall funding. Fund via https://faucet.solana.com/ (paste each pubkey).")


def cmd_register():
    keys = load_keys()
    root_child = keys["master"]["pubkey"]  # master wallet, but root-child is in DB already
    # Hierarchy: root_child (BRrtY…) -> A -> B -> C -> D
    rc_pub = "BRrtYftGhXBh3JcwmveuB4ZcskkYvUeLzNgPcf5VF6Ry"  # already on-server
    chain = [
        ("buyerA", rc_pub),
        ("buyerB", keys["buyerA"]["pubkey"]),
        ("buyerC", keys["buyerB"]["pubkey"]),
        ("buyerD", keys["buyerC"]["pubkey"]),
    ]
    for role, ref in chain:
        wallet = keys[role]["pubkey"]
        code, body = api_post("/api/v1/user/register", {"wallet_address": wallet, "referrer_wallet": ref})
        pp(f"register {role}={short(wallet)} ref={short(ref)}", code, body)


def _wait_purchase_complete(purchase_id: str, buyer_wallet: str, timeout_s: int = 300) -> dict:
    start = time.time()
    last_status = ""
    final_body = None
    while time.time() - start < timeout_s:
        code, body = api_get(f"/api/v1/purchase/{purchase_id}")
        if code == 200:
            status = body.get("status", "")
            if status != last_status:
                print(f"   purchase {purchase_id} status={status} cdist={body.get('commission_distributed')} tx={body.get('token_dispatch_tx')}")
                last_status = status
            if status in ("completed", "confirmed", "failed", "expired"):
                final_body = body
                if status in ("failed", "expired"):
                    return body
                if not body.get("commission_distributed"):
                    time.sleep(3)
                    continue
                ucode, ubody = api_get(f"/api/v1/user/{buyer_wallet}")
                if ucode == 200 and ubody.get("is_valid_referrer"):
                    print(f"   buyer {short(buyer_wallet)} is_valid_referrer=True")
                    return body
        time.sleep(3)
    if final_body:
        print(f"   WARN: purchase {purchase_id} reached {final_body.get('status')} but is_valid_referrer not flipped in {timeout_s}s")
        return final_body
    raise TimeoutError(f"Purchase {purchase_id} did not complete in {timeout_s}s")


def cmd_purchase(role: str, xfee_amount: int):
    keys = load_keys()
    if role not in keys:
        die(f"unknown role {role}; one of {list(keys)}")
    kp = kp_from(role, keys)
    wallet = keys[role]["pubkey"]

    # 1) initiate
    code, body = api_post("/api/v1/purchase/initiate", {"wallet_address": wallet, "xfee_amount": xfee_amount})
    pp(f"initiate {role}={short(wallet)} xfee={xfee_amount}", code, body)
    if code != 200:
        return
    purchase_id = body["purchase_id"]
    purchase_wallet = body["purchase_wallet"]
    sol_expected = float(body["sol_expected"])
    print(f"   purchase_id={purchase_id} purchase_wallet={purchase_wallet} sol_expected={sol_expected}")

    # 2) check buyer balance
    bal = get_balance_sol(wallet)
    if bal < sol_expected + 0.01:
        die(f"buyer {role} balance {bal:.4f} < required {sol_expected:.4f} (+gas). Fund via faucet.")

    # 3) send SOL
    sig = send_sol(kp, purchase_wallet, sol_expected)
    print(f"   sent {sol_expected} SOL  tx={sig}")
    ok = confirm_sig(sig)
    print(f"   confirmation: {'OK' if ok else 'FAILED'}")

    # 4) wait for backend to detect + distribute
    final = _wait_purchase_complete(purchase_id, wallet)
    print(f"   FINAL: {json.dumps(final, default=str)[:500]}")
    return final


def _register_one(role: str, ref: str, keys: dict) -> bool:
    wallet = keys[role]["pubkey"]
    code, body = api_post("/api/v1/user/register", {"wallet_address": wallet, "referrer_wallet": ref})
    if code == 200:
        pp(f"register {role}={short(wallet)} ref={short(ref)}", code, body)
        return True
    if code == 409 and "already registered" in str(body):
        pp(f"register {role}={short(wallet)} (already)", code, body)
        return True
    pp(f"register {role}={short(wallet)}", code, body)
    return False


def cmd_chain(start_role: str = "buyerA"):
    """Interleaved chain from start_role through buyerD.
    Skips purchase for any role that already has is_valid_referrer=True."""
    keys = load_keys()
    rc_pub = "BRrtYftGhXBh3JcwmveuB4ZcskkYvUeLzNgPcf5VF6Ry"
    steps = [
        ("buyerA", rc_pub),
        ("buyerB", keys["buyerA"]["pubkey"]),
        ("buyerC", keys["buyerB"]["pubkey"]),
        ("buyerD", keys["buyerC"]["pubkey"]),
    ]
    started = False
    for role, ref in steps:
        if role == start_role:
            started = True
        if not started:
            continue
        wallet = keys[role]["pubkey"]
        print(f"\n=== {role} (referrer={short(ref)}) ===")
        if not _register_one(role, ref, keys):
            print(f"   register FAILED; aborting chain")
            return
        code, body = api_get(f"/api/v1/user/{wallet}")
        if code == 200 and body.get("is_valid_referrer"):
            print(f"   {role} already valid_referrer; skipping purchase")
            continue
        cmd_purchase(role, 10)
        time.sleep(2)
    print("\n=== Final user states ===")
    cmd_status_all()
    print("\n=== Global pool ===")
    cmd_poolsummary()
    cmd_pool_current()


def cmd_status_all():
    keys = load_keys()
    rc_pub = "BRrtYftGhXBh3JcwmveuB4ZcskkYvUeLzNgPcf5VF6Ry"
    for label, pub in [
        ("master", keys["master"]["pubkey"]),
        ("rootchild", rc_pub),
        ("buyerA", keys["buyerA"]["pubkey"]),
        ("buyerB", keys["buyerB"]["pubkey"]),
        ("buyerC", keys["buyerC"]["pubkey"]),
        ("buyerD", keys["buyerD"]["pubkey"]),
    ]:
        code, body = api_get(f"/api/v1/user/{pub}")
        if code == 200:
            print(f"  {label:9s} L{body['level']}  sales=${body['total_sales_usd']:.2f}  comm={body['total_commission_sol']:.6f}  directs={body['direct_referral_count']}  valid_ref={body['is_valid_referrer']}")
        else:
            print(f"  {label:9s} ERR {code} {body}")


def cmd_upgrade(wallet: str, level: int):
    code, body = api_post("/api/v1/admin/set-user-level", {"wallet_address": wallet, "level": int(level)}, admin=True)
    pp(f"set-user-level {short(wallet)}->L{level}", code, body)


def cmd_settle(pool_index: int):
    code, body = api_post(f"/api/v1/admin/global-pool/{pool_index}/settle?force=true", admin=True)
    pp(f"settle pool {pool_index} force=true", code, body)


def cmd_status(wallet: str):
    code, body = api_get(f"/api/v1/user/{wallet}")
    pp(f"user/{short(wallet)}", code, body)
    code, body = api_get(f"/api/v1/user/{wallet}/allocs")
    pp(f"user/{short(wallet)}/allocs", code, body)
    code, body = api_get(f"/api/v1/global-pool/user/{wallet}/points")
    pp(f"global-pool points {short(wallet)}", code, body)


def cmd_pool_current():
    code, body = api_get("/api/v1/global-pool/current")
    pp("global-pool/current", code, body)


def cmd_pool(pool_index: int):
    code, body = api_get(f"/api/v1/global-pool/{pool_index}")
    pp(f"global-pool/{pool_index}", code, body)


def cmd_poolsummary():
    code, body = api_get("/api/v1/global-pool/summary")
    pp("global-pool/summary", code, body)


COMMANDS = {
    "preflight": lambda *a: cmd_preflight(),
    "register": lambda *a: cmd_register(),
    "purchase": lambda *a: cmd_purchase(a[0], int(a[1])),
    "chain": lambda *a: cmd_chain(a[0] if a else "buyerA"),
    "upgrade": lambda *a: cmd_upgrade(a[0], int(a[1])),
    "settle": lambda *a: cmd_settle(int(a[0])),
    "status": lambda *a: cmd_status(a[0]),
    "pool-current": lambda *a: cmd_pool_current(),
    "pool": lambda *a: cmd_pool(int(a[0])),
    "poolsummary": lambda *a: cmd_poolsummary(),
    "status-all": lambda *a: cmd_status_all(),
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(2)
    COMMANDS[sys.argv[1]](*sys.argv[2:])


if __name__ == "__main__":
    main()
