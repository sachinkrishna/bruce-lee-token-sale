import asyncio
import httpx
from motor.motor_asyncio import AsyncIOMotorClient

# --- CONFIGURATION ---
TREASURY_ATA = "GnQBzasVU7RyinxixcRe3V63zM4VwWaJuqiHa2WNfe93"
RPC_URL = "https://winter-omniscient-butterfly.solana-mainnet.quiknode.pro/2764f724d113e886388124575f3ed85b5aae3d6a/" # Replace with your Quicknode URL if you have rate limits
MONGO_URI = "mongodb+srv://xtrends:Lkklh34ll0112345kjgnMow@xtrends-market.7pgnlb.mongodb.net/admin?appName=xtrends-vanity-gen&retryWrites=true&loadBalanced=false&replicaSet=atlas-mpd8su-shard-0&readPreference=primary&srvServiceName=mongodb&connectTimeoutMS=10000&authSource=admin&authMechanism=SCRAM-SHA-1"         # Replace with your production URI if needed
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
    """Paginate backwards to fetch ALL signatures for the ATA and parse out outgoing token amounts."""
    outgoing_transfers = []
    before_sig = None
    total_fetched = 0
    print(f"Fetching all historical signatures for ATA: {ata} via RPC...")
    
    semaphore = asyncio.Semaphore(20)
    
    async def process_signature(sig_info):
        sig = sig_info["signature"]
        async with semaphore:
            try:
                tx_data = await rpc_request(
                    client, 
                    "getTransaction", 
                    [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
                )
            except Exception as e:
                return None
        
        if not tx_data or not tx_data.get("meta"):
            return None
            
        meta = tx_data["meta"]
        
        # Ignore transactions that failed on the blockchain
        if meta.get("err") is not None:
            return None
            
        pre_balances = meta.get("preTokenBalances", [])
        post_balances = meta.get("postTokenBalances", [])
        
        # Find treasury ATA balance before and after the transaction
        pre_amt = 0.0
        post_amt = 0.0
        
        for bal in pre_balances:
            if bal.get("accountIndex") is not None and tx_data["transaction"]["message"]["accountKeys"][bal["accountIndex"]]["pubkey"] == ata:
                pre_amt = float(bal["uiTokenAmount"]["uiAmountString"])
                
        for bal in post_balances:
            if bal.get("accountIndex") is not None and tx_data["transaction"]["message"]["accountKeys"][bal["accountIndex"]]["pubkey"] == ata:
                post_amt = float(bal["uiTokenAmount"]["uiAmountString"])
                
        # If the balance decreased, it is an outgoing transfer!
        if post_amt < pre_amt:
            amount_sent = pre_amt - post_amt
            
            # Find the destination ATA (the account whose balance increased by amount_sent)
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
        # Set up pagination parameters
        params = {"limit": 100}
        if before_sig:
            params["before"] = before_sig
        signatures_data = await rpc_request(client, "getSignaturesForAddress", [ata, params])
        
        if not signatures_data:
            break # No more transactions to fetch
        total_fetched += len(signatures_data)
        print(f"Fetched {len(signatures_data)} signatures (Total: {total_fetched}). Parsing transactions...")
        
        tasks = [process_signature(sig_info) for sig_info in signatures_data]
        results = await asyncio.gather(*tasks)
        
        for res in results:
            if res:
                outgoing_transfers.append(res)
                
        # Set the 'before' cursor to the last (oldest) signature in this batch to get the next page
        before_sig = signatures_data[-1]["signature"]
    return outgoing_transfers

async def analyze():
    async with httpx.AsyncClient() as http_client:
        transfers = await get_all_outgoing_transfers(http_client, TREASURY_ATA)
    
    print(f"\nFinished parsing. Found {len(transfers)} total outgoing transfers on-chain.")
    
    if not transfers:
        return
    print(f"Connecting to MongoDB at {MONGO_URI}...")
    mongo_client = AsyncIOMotorClient(MONGO_URI)
    db = mongo_client[DB_NAME]
    
    print("Loading database records into memory for fast matching...")
    all_db_txs = await db.transactions.find({}).to_list(None)
    db_tx_sigs = {tx.get("tx_signature"): tx for tx in all_db_txs if tx.get("tx_signature")}
    
    all_purchases = await db.purchases.find({}).to_list(None)
    purchase_dispatch_sigs = {p.get("token_dispatch_tx"): p for p in all_purchases if p.get("token_dispatch_tx")}
    
    print("\n" + "-" * 120)
    print(f"STATUS       | AMOUNT   | ATA / BUYER HINT                                     | SIGNATURE")
    print("-" * 120)
    
    missing_count = 0
    missing_amount = 0.0
    
    for tx in transfers:
        sig = tx["signature"]
        amount = tx["amount"]
        to_ata = tx["to_ata"]
        
        # 1. Check if signature exists in transactions collection
        db_tx = db_tx_sigs.get(sig)
        
        purchase = None
        if not db_tx:
            # 2. Check if signature exists in purchases collection directly
            purchase = purchase_dispatch_sigs.get(sig)
            if not purchase:
                missing_count += 1
                missing_amount += amount
                
                # Try to find a failed/pending purchase with the exact same amount
                potential_matches = [p.get("user_wallet") for p in all_purchases if p.get("xfee_amount") == amount and p.get("status") != "completed"]
                
                buyer_hint = to_ata
                if potential_matches:
                    buyer_hint = f"{to_ata} (Match: {potential_matches[0]})"
                
                buyer_hint = (buyer_hint[:49] + "...") if len(buyer_hint) > 52 else buyer_hint
                print(f"MISSING      | {amount:<8} | {buyer_hint:<52} | {sig}")
                continue
        else:
            purchase_id = db_tx.get("purchase_id")
            purchase = next((p for p in all_purchases if p.get("_id") == purchase_id), None)
            
        if purchase:
            status = purchase.get("status")
            pid = purchase.get("_id")
            if status != "completed":
                print(f"⚠️ ORPHANED: Tx {sig} sent {amount} XFEE, but purchase {pid} is stuck in '{status}' status.")
                missing_amount += amount
                missing_count += 1

    print("-" * 120)
    print(f"\nTotal Missing/Orphaned Transfers: {missing_count}")
    print(f"Total Missing/Orphaned Tokens:    {missing_amount}")

if __name__ == "__main__":
    asyncio.run(analyze())