import asyncio
import csv
from motor.motor_asyncio import AsyncIOMotorClient

# --- CONFIGURATION ---
# Change this to your production MongoDB URI before running
MONGO_URI = "mongodb+srv://xtrends:Lkklh34ll0112345kjgnMow@xtrends-market.7pgnlb.mongodb.net/admin?appName=xtrends-vanity-gen&retryWrites=true&loadBalanced=false&replicaSet=atlas-mpd8su-shard-0&readPreference=primary&srvServiceName=mongodb&connectTimeoutMS=10000&authSource=admin&authMechanism=SCRAM-SHA-1"         # Replace with your production URI if needed
DB_NAME = "xfee_sale"
# ---------------------

async def analyze():
    print(f"Connecting to MongoDB at {MONGO_URI}...")
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]
    
    transfers = []
    print("Reading transfers.csv...")
    with open("transfers.csv", "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["Flow"].lower() == "out":
                sig = row["Signature"]
                amount_raw = int(row["Amount"])
                decimals = int(row["Decimals"])
                amount = amount_raw / (10 ** decimals)
                to_address = row["To"]
                time_str = row["Human Time"]
                transfers.append({
                    "signature": sig, 
                    "amount": amount,
                    "to_ata": to_address,
                    "time": time_str
                })
                
    print(f"Total outgoing transfers in CSV: {len(transfers)}\n")
    print("-" * 120)
    print(f"STATUS       | AMOUNT   | ATA / BUYER HINT                                     | SIGNATURE")
    print("-" * 120)
    
    missing_count = 0
    missing_amount = 0.0

    for tx in transfers:
        sig = tx["signature"]
        amount = tx["amount"]
        to_ata = tx["to_ata"]
        
        # 1. Check if signature exists in transactions collection
        db_tx = await db.transactions.find_one({"tx_signature": sig})
        
        if not db_tx:
            # 2. Check if signature exists in purchases collection directly
            purchase = await db.purchases.find_one({"token_dispatch_tx": sig})
            if not purchase:
                missing_count += 1
                missing_amount += amount
                
                # Let us try to find a failed/expired/pending purchase with the exact same amount
                # to guess who the buyer might be in production.
                potential_matches = []
                async for p in db.purchases.find({"xfee_amount": amount, "status": {"$ne": "completed"}}).limit(3):
                    potential_matches.append(p.get("user_wallet"))
                
                buyer_hint = to_ata
                if potential_matches:
                    buyer_hint = f"{to_ata} (Match: {potential_matches[0]})"
                
                # Truncate buyer_hint to 52 chars to keep output clean
                buyer_hint = (buyer_hint[:49] + "...") if len(buyer_hint) > 52 else buyer_hint
                
                print(f"MISSING      | {amount:<8} | {buyer_hint:<52} | {sig}")
                continue
        else:
            purchase = await db.purchases.find_one({"_id": db_tx["purchase_id"]})
            
        if purchase:
            status = purchase.get("status")
            pid = purchase["_id"]
            if status == "completed":
                # It's all good, we don't need to print it unless you want to see everything
                pass 
            else:
                print(f"⚠️ ORPHANED: Tx {sig} sent {amount} XFEE, but purchase {pid} is stuck in '{status}' status.")
                missing_tokens_total += amount

    print("-" * 120)
    print(f"\nTotal Missing Transfers: {missing_count}")
    print(f"Total Missing Tokens:    {missing_amount}")

if __name__ == "__main__":
    asyncio.run(analyze())
