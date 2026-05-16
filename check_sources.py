import asyncio
import httpx
from motor.motor_asyncio import AsyncIOMotorClient
import collections

RPC_URL = "https://winter-omniscient-butterfly.solana-mainnet.quiknode.pro/2764f724d113e886388124575f3ed85b5aae3d6a/"
MONGO_URI = "mongodb+srv://xtrends:Lkklh34ll0112345kjgnMow@xtrends-market.7pgnlb.mongodb.net/admin?appName=xtrends-vanity-gen&retryWrites=true&loadBalanced=false&replicaSet=atlas-mpd8su-shard-0&readPreference=primary&srvServiceName=mongodb&connectTimeoutMS=10000&authSource=admin&authMechanism=SCRAM-SHA-1"
DB_NAME = "xfee_sale"

async def rpc_request(client: httpx.AsyncClient, method: str, params: list, max_retries=3) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for attempt in range(max_retries):
        try:
            resp = await client.post(RPC_URL, json=payload, timeout=60.0)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise Exception(f"RPC error: {data['error']}")
            return data.get("result")
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(1)

async def check_sources():
    mongo_client = AsyncIOMotorClient(MONGO_URI)
    db = mongo_client[DB_NAME]
    
    print("Loading DB purchases...")
    purchases = await db.purchases.find({"status": "completed", "token_dispatch_tx": {"$ne": None}}).to_list(None)
    
    print(f"Found {len(purchases)} completed purchases with signatures.")
    
    if not purchases:
        return
        
    sources = collections.Counter()
    
    semaphore = asyncio.Semaphore(5)
    
    async def process_sig(p):
        sig = p["token_dispatch_tx"]
        async with semaphore:
            async with httpx.AsyncClient() as client:
                try:
                    tx_data = await rpc_request(
                        client, 
                        "getTransaction", 
                        [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
                        max_retries=10
                    )
                except Exception:
                    return None
                    
        if not tx_data or not tx_data.get("meta"):
            return None
            
        pre_balances = tx_data["meta"].get("preTokenBalances", [])
        post_balances = tx_data["meta"].get("postTokenBalances", [])
        
        # Find the account whose balance decreased
        for p_bal in pre_balances:
            account_idx = p_bal.get("accountIndex")
            pre_amt = float(p_bal["uiTokenAmount"]["uiAmountString"])
            
            post_amt = 0.0
            for post_bal in post_balances:
                if post_bal.get("accountIndex") == account_idx:
                    post_amt = float(post_bal["uiTokenAmount"]["uiAmountString"])
                    
            if pre_amt > post_amt:
                pubkey = tx_data["transaction"]["message"]["accountKeys"][account_idx]["pubkey"]
                return pubkey
        return None
        
    tasks = [process_sig(p) for p in purchases]
    results = await asyncio.gather(*tasks)
    
    for pubkey in results:
        if pubkey:
            sources[pubkey] += 1
            
    print("Treasury ATAs used:")
    for ata, count in sources.items():
        print(f"{ata}: {count}")

if __name__ == "__main__":
    asyncio.run(check_sources())