import asyncio
import httpx
from motor.motor_asyncio import AsyncIOMotorClient
import collections

# --- CONFIGURATION ---
TREASURY_ATA = "GnQBzasVU7RyinxixcRe3V63zM4VwWaJuqiHa2WNfe93"
RPC_URL = "https://winter-omniscient-butterfly.solana-mainnet.quiknode.pro/2764f724d113e886388124575f3ed85b5aae3d6a/"
MONGO_URI = "mongodb+srv://xtrends:Lkklh34ll0112345kjgnMow@xtrends-market.7pgnlb.mongodb.net/admin?appName=xtrends-vanity-gen&retryWrites=true&loadBalanced=false&replicaSet=atlas-mpd8su-shard-0&readPreference=primary&srvServiceName=mongodb&connectTimeoutMS=10000&authSource=admin&authMechanism=SCRAM-SHA-1"
DB_NAME = "xfee_sale"
# ---------------------

async def rpc_request(client: httpx.AsyncClient, method: str, params: list, max_retries=3) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for attempt in range(max_retries):
        try:
            resp = await client.post(RPC_URL, json=payload, timeout=60.0)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                if attempt < max_retries - 1 and (data["error"].get("code") == 429 or "Too Many Requests" in str(data["error"])):
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise Exception(f"RPC error: {data['error']}")
            return data.get("result")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429 and attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            if attempt == max_retries - 1:
                raise
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(2 ** attempt)

async def get_all_outgoing_transfers(client: httpx.AsyncClient, ata: str):
    outgoing_transfers = []
    before_sig = None
    total_fetched = 0
    print(f"Fetching all historical signatures for ATA: {ata} via RPC...")
    
    semaphore = asyncio.Semaphore(5)
    
    async def process_signature(sig_info):
        sig = sig_info["signature"]
        async with semaphore:
            try:
                tx_data = await rpc_request(
                    client, 
                    "getTransaction", 
                    [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
                    max_retries=10
                )
            except Exception as e:
                print(f"FAILED to fetch tx {sig}: {e}")
                return None
        
        if not tx_data or not tx_data.get("meta"):
            return None
            
        meta = tx_data["meta"]
        
        if meta.get("err") is not None:
            return None
            
        pre_balances = meta.get("preTokenBalances", [])
        post_balances = meta.get("postTokenBalances", [])
        
        pre_amt = 0.0
        post_amt = 0.0
        
        for bal in pre_balances:
            if bal.get("accountIndex") is not None and tx_data["transaction"]["message"]["accountKeys"][bal["accountIndex"]]["pubkey"] == ata:
                pre_amt = float(bal["uiTokenAmount"]["uiAmountString"])
                
        for bal in post_balances:
            if bal.get("accountIndex") is not None and tx_data["transaction"]["message"]["accountKeys"][bal["accountIndex"]]["pubkey"] == ata:
                post_amt = float(bal["uiTokenAmount"]["uiAmountString"])
                
        if post_amt < pre_amt:
            amount_sent = pre_amt - post_amt
            
            to_ata = "Unknown"
            for post_bal in post_balances:
                if post_bal.get("accountIndex") is not None and tx_data["transaction"]["message"]["accountKeys"][post_bal["accountIndex"]]["pubkey"] != ata:
                    to_ata = tx_data["transaction"]["message"]["accountKeys"][post_bal["accountIndex"]]["pubkey"]
                    break
            
            return {
                "signature": sig,
                "amount": amount_sent,
                "to_ata": to_ata
            }
        return None

    while True:
        params = {"limit": 1000}
        if before_sig:
            params["before"] = before_sig
        signatures_data = await rpc_request(client, "getSignaturesForAddress", [ata, params])
        
        if not signatures_data:
            break
        total_fetched += len(signatures_data)
        
        tasks = [process_signature(sig_info) for sig_info in signatures_data]
        results = await asyncio.gather(*tasks)
        
        for res in results:
            if res:
                outgoing_transfers.append(res)
                
        before_sig = signatures_data[-1]["signature"]
    return outgoing_transfers

async def reconcile():
    print(f"Connecting to MongoDB...")
    mongo_client = AsyncIOMotorClient(MONGO_URI)
    db = mongo_client[DB_NAME]
    
    # 1. Fetch DB data
    print("Loading DB purchases and transactions...")
    all_purchases = await db.purchases.find({"status": "completed"}).to_list(None)
    all_txs = await db.transactions.find({}).to_list(None)
    
    db_total_xfee = sum(p.get("xfee_amount", 0) for p in all_purchases)
    print(f"DB says {len(all_purchases)} completed purchases.")
    print(f"DB total tokens sold (completed): {db_total_xfee}")
    
    # Map purchase ID to its transaction
    purchase_to_tx = {}
    for tx in all_txs:
        if tx.get("purchase_id"):
            purchase_to_tx[str(tx["purchase_id"])] = tx.get("tx_signature")
    
    # 2. Fetch on-chain data
    async with httpx.AsyncClient() as http_client:
        transfers = await get_all_outgoing_transfers(http_client, TREASURY_ATA)
    
    chain_total_xfee = sum(t["amount"] for t in transfers)
    print(f"\nOn-chain says {len(transfers)} outgoing transfers.")
    print(f"On-chain total tokens transferred out: {chain_total_xfee}")
    
    chain_transfers_by_sig = {t["signature"]: t for t in transfers}
    
    print("\n--- RECONCILIATION DISCREPANCIES ---")
    # Check DB vs Chain mismatch for EACH completed purchase
    db_sigs_seen = set()
    for p in all_purchases:
        pid = str(p["_id"])
        xfee_amount = p.get("xfee_amount", 0)
        
        # Check token_dispatch_tx first, fallback to transactions collection
        sig = p.get("token_dispatch_tx")
        if not sig:
            sig = purchase_to_tx.get(pid)
            
        if not sig:
            print(f"[MISSING SIG IN DB] Completed Purchase {pid} for {xfee_amount} XFEE has no signature recorded!")
            continue
            
        db_sigs_seen.add(sig)
        
        chain_tx = chain_transfers_by_sig.get(sig)
        if not chain_tx:
            print(f"[NOT FOUND ON CHAIN] Purchase {pid} claims signature {sig} for {xfee_amount} XFEE, but not found on-chain!")
            continue
            
        chain_amt = chain_tx["amount"]
        if abs(chain_amt - xfee_amount) > 0.001:
            print(f"[AMOUNT MISMATCH] Purchase {pid}: DB says {xfee_amount}, Chain says {chain_amt} (sig: {sig})")
            
    # Find any on-chain transfers that we DIDN'T see associated with a COMPLETED purchase
    print("\n--- ON-CHAIN TRANSFERS NOT IN COMPLETED PURCHASES ---")
    for t in transfers:
        if t["signature"] not in db_sigs_seen:
            print(f"[EXTRA CHAIN TX] Amount: {t['amount']:<10} | Sig: {t['signature']}")

if __name__ == "__main__":
    asyncio.run(reconcile())