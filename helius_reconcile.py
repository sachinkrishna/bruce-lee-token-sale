import asyncio
import httpx
from motor.motor_asyncio import AsyncIOMotorClient
import os
#https://api-mainnet.helius-rpc.com/v0/transactions/?api-key=
# --- CONFIGURATION ---
TREASURY_ATA = "AzsuDb76qgGPUxLFp4ZHcSQKHVBQ4u6UkiG7BJcYGMbo"
MONGO_URI = "mongodb+srv://xtrends:Lkklh34ll0112345kjgnMow@xtrends-market.7pgnlb.mongodb.net/admin?appName=xtrends-vanity-gen&retryWrites=true&loadBalanced=false&replicaSet=atlas-mpd8su-shard-0&readPreference=primary&srvServiceName=mongodb&connectTimeoutMS=10000&authSource=admin&authMechanism=SCRAM-SHA-1"
DB_NAME = "xfee_sale"
HELIUS_API_KEY = "86c830da-67f9-4c06-9d98-ccdcb6b8393a"
# ---------------------

async def get_helius_transfers(client: httpx.AsyncClient, ata: str):
    outgoing_transfers = []
    before_sig = None
    total_fetched = 0
    
    print(f"Fetching all historical signatures for ATA: {ata} via Helius API...")
    
    while True:
        url = f"https://api-mainnet.helius-rpc.com/v0/addresses/{ata}/transactions?api-key={HELIUS_API_KEY}&type=TRANSFER&limit=100"
        if before_sig:
            url += f"&before-signature={before_sig}"
            
        try:
            resp = await client.get(url, timeout=30.0)
            
            # Handle potential non-JSON responses from Helius (e.g. rate limit HTML pages)
            try:
                data = resp.json()
            except ValueError:
                print(f"Failed to parse JSON response. Status code: {resp.status_code}")
                break
            
            if "error" in data:
                if "Failed to find events within the search period" in str(data.get("error")):
                    import re
                    match = re.search(r"parameter set to ([A-Za-z0-9]+)", data["error"])
                    if match:
                        before_sig = match.group(1)
                        print(f"No results in this period. Continuing search from: {before_sig}")
                        continue
                print(f"Helius API error: {data['error']}")
                break
                
            if not isinstance(data, list) or len(data) == 0:
                break
                
            total_fetched += len(data)
            print(f"Fetched batch of {len(data)} transactions (Total: {total_fetched})")
            
            for tx in data:
                sig = tx.get("signature")
                token_transfers = tx.get("tokenTransfers", [])
                
                # Check for outgoing transfer from the ATA
                amount_sent = 0.0
                to_ata = "Unknown"
                
                for tt in token_transfers:
                    # Token transfer logic: either the fromTokenAccount or the fromUserAccount matches our ATA
                    if tt.get("fromTokenAccount") == ata or tt.get("fromUserAccount") == ata:
                        amount_sent += float(tt.get("tokenAmount", 0))
                        to_ata = tt.get("toTokenAccount") or tt.get("toUserAccount") or "Unknown"
                        
                if amount_sent > 0:
                    outgoing_transfers.append({
                        "signature": sig,
                        "amount": amount_sent,
                        "to_ata": to_ata
                    })
            
            before_sig = data[-1]["signature"]
            
        except Exception as e:
            print(f"Error fetching from Helius: {e}")
            break
            
    return outgoing_transfers

async def reconcile():
    if not HELIUS_API_KEY:
        print("ERROR: HELIUS_API_KEY environment variable is not set!")
        return

    print("Connecting to MongoDB...")
    mongo_client = AsyncIOMotorClient(MONGO_URI)
    db = mongo_client[DB_NAME]
    
    print("Loading DB purchases and transactions...")
    all_purchases = await db.purchases.find({"status": "completed"}).to_list(None)
    all_txs = await db.transactions.find({}).to_list(None)
    
    purchase_to_tx = {}
    for tx in all_txs:
        if tx.get("purchase_id"):
            purchase_to_tx[str(tx["purchase_id"])] = tx.get("tx_signature")
            
    async with httpx.AsyncClient() as http_client:
        transfers = await get_helius_transfers(http_client, TREASURY_ATA)
        
    print(f"\nHelius reports {len(transfers)} outgoing transfers.")
    chain_total_xfee = sum(t["amount"] for t in transfers)
    print(f"Total outgoing XFEE: {chain_total_xfee}")
    
    chain_transfers_by_sig = {t["signature"]: t for t in transfers}
    
    print("\n--- RECONCILIATION DISCREPANCIES ---")
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
            print(f"[NOT FOUND ON CHAIN] Purchase {pid} claims signature {sig} for {xfee_amount} XFEE, but not found in Helius results!")
            continue
            
        chain_amt = chain_tx["amount"]
        if abs(chain_amt - xfee_amount) > 0.001:
            print(f"[AMOUNT MISMATCH] Purchase {pid}: DB says {xfee_amount}, Helius says {chain_amt} (sig: {sig})")
            
    print("\n--- ON-CHAIN TRANSFERS NOT IN COMPLETED PURCHASES ---")
    missing_count = 0
    missing_amt = 0.0
    for t in transfers:
        if t["signature"] not in db_sigs_seen:
            print(f"[EXTRA CHAIN TX] Amount: {t['amount']:<10} | To: {t['to_ata']:<44} | Sig: {t['signature']}")
            missing_count += 1
            missing_amt += t["amount"]

    print("-" * 120)
    print(f"\nTotal Missing/Orphaned Transfers: {missing_count}")
    print(f"Total Missing/Orphaned Tokens:    {missing_amt}")

if __name__ == "__main__":
    asyncio.run(reconcile())