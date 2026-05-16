#!/bin/bash
# ─────────────────────────────────────────────────────────────
# XFEE Token Sale — Full flow test script (test mode)
#
# Prerequisites:
#   1. MongoDB running locally on port 27017
#   2. pip install -r requirements.txt
#   3. cp .env.test .env
#   4. uvicorn app.main:app --reload
#
# Then in another terminal:  bash test_flow.sh
# ─────────────────────────────────────────────────────────────

BASE="http://localhost:8000/api/v1"
MASTER="75q6RyMp55YcGVF9ZNhp8jkU9jUXjrYEypC5iiNoi71s"
USER_A="CfUUDPa3Dwwh6aZBzJcmFsCTRd9u3WX9Hog1njbjqvfL"
USER_B="3QTdAQKsd9Tq4B7CpdVzyHQuyw5k4ismaYc3G8jRJPBi"

set -e

echo "=== 1. Health check ==="
curl -s http://localhost:8000/health | python3 -m json.tool

echo ""
echo "=== 2. Global stats (should be empty) ==="
curl -s $BASE/stats/global | python3 -m json.tool

echo ""
echo "=== 3. Register User A (referred by master) ==="
curl -s -X POST $BASE/user/register \
  -H "Content-Type: application/json" \
  -d "{\"wallet_address\": \"$USER_A\", \"referrer_wallet\": \"$MASTER\"}" | python3 -m json.tool

echo ""
echo "=== 4. Initiate purchase for User A (100 XFEE) ==="
PURCHASE_RESP=$(curl -s -X POST $BASE/purchase/initiate \
  -H "Content-Type: application/json" \
  -d "{\"wallet_address\": \"$USER_A\", \"xfee_amount\": 100}")
echo "$PURCHASE_RESP" | python3 -m json.tool

PURCHASE_ID=$(echo "$PURCHASE_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['purchase_id'])")
echo "Purchase ID: $PURCHASE_ID"

echo ""
echo "=== 5. Check purchase status (should be pending) ==="
curl -s $BASE/purchase/$PURCHASE_ID | python3 -m json.tool

echo ""
echo "=== 6. Simulate SOL deposit ==="
SOL_EXPECTED=$(echo "$PURCHASE_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['sol_expected'])")
curl -s -X POST $BASE/test/deposit \
  -H "Content-Type: application/json" \
  -d "{\"purchase_id\": \"$PURCHASE_ID\", \"sol_amount\": $SOL_EXPECTED}" | python3 -m json.tool

echo ""
echo "=== 7. Wait for poller to detect deposit... ==="
sleep 8

echo ""
echo "=== 8. Check purchase status (should be completed) ==="
curl -s $BASE/purchase/$PURCHASE_ID | python3 -m json.tool

echo ""
echo "=== 9. Check User A profile ==="
curl -s $BASE/user/$USER_A | python3 -m json.tool

echo ""
echo "=== 10. Register User B (referred by User A) ==="
curl -s -X POST $BASE/user/register \
  -H "Content-Type: application/json" \
  -d "{\"wallet_address\": \"$USER_B\", \"referrer_wallet\": \"$USER_A\"}" | python3 -m json.tool

echo ""
echo "=== 11. Purchase for User B (50 XFEE) ==="
PURCHASE_B=$(curl -s -X POST $BASE/purchase/initiate \
  -H "Content-Type: application/json" \
  -d "{\"wallet_address\": \"$USER_B\", \"xfee_amount\": 50}")
echo "$PURCHASE_B" | python3 -m json.tool

PID_B=$(echo "$PURCHASE_B" | python3 -c "import sys,json; print(json.load(sys.stdin)['purchase_id'])")
SOL_B=$(echo "$PURCHASE_B" | python3 -c "import sys,json; print(json.load(sys.stdin)['sol_expected'])")

echo ""
echo "=== 12. Simulate deposit for User B ==="
curl -s -X POST $BASE/test/deposit \
  -H "Content-Type: application/json" \
  -d "{\"purchase_id\": \"$PID_B\", \"sol_amount\": $SOL_B}" | python3 -m json.tool

sleep 8

echo ""
echo "=== 13. User A profile (should show commission + network sales) ==="
curl -s $BASE/user/$USER_A | python3 -m json.tool

echo ""
echo "=== 14. User A commission allocs ==="
curl -s "$BASE/user/$USER_A/allocs" | python3 -m json.tool

echo ""
echo "=== 15. User A tree ==="
curl -s "$BASE/user/$USER_A/tree" | python3 -m json.tool

echo ""
echo "=== 16. Global stats ==="
curl -s $BASE/stats/global | python3 -m json.tool

echo ""
echo "=== 17. Set User B level (master upgrades B to L3) ==="
curl -s -X POST $BASE/user/set-user-level \
  -H "Content-Type: application/json" \
  -d "{\"wallet_address\": \"$USER_B\", \"level\": 3, \"signature\": \"test_signature\"}" | python3 -m json.tool

echo ""
echo "=== 18. User B profile (should be level 3) ==="
curl -s $BASE/user/$USER_B | python3 -m json.tool

echo ""
echo "=== DONE ==="
