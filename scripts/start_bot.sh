#!/bin/bash
# Start upload bot + API server (with pre-deploy tests)
DEPLOY_DIR="/opt/phonesinventory"
cd "$DEPLOY_DIR"

# Load .env if exists
if [ -f "$DEPLOY_DIR/.env" ]; then
    export $(grep -v '^#' "$DEPLOY_DIR/.env" | xargs)
fi

# ─── Pre-deploy tests ───
echo "$(date) Running pre-deploy tests..."
python3 scripts/test_bot.py 2>&1
if [ $? -ne 0 ]; then
    echo "$(date) TESTS FAILED — aborting start. Fix issues before deploying."
    exit 1
fi
echo "$(date) Tests passed."

# Kill existing processes
pkill -f "inventory_bot.py" 2>/dev/null
pkill -f "api_server.py" 2>/dev/null
sleep 1

# Truncate logs if over 10MB
for log in /tmp/phonesinventory-api.log /tmp/upload-bot.log /var/log/inventory_bot.log; do
    if [ -f "$log" ] && [ $(stat -f%z "$log" 2>/dev/null || stat -c%s "$log" 2>/dev/null) -gt 10485760 ] 2>/dev/null; then
        tail -1000 "$log" > "${log}.tmp" && mv "${log}.tmp" "$log"
        echo "$(date) Rotated $log"
    fi
done

# Start API server
echo "$(date) Starting API server..."
nohup python3 scripts/api_server.py >> /tmp/phonesinventory-api.log 2>&1 &
echo "$(date) API server started, PID: $!"

# Start upload bot
echo "$(date) Starting upload bot (@PhoneInventoryUpload_bot)..."
nohup python3 scripts/inventory_bot.py >> /var/log/inventory_bot.log 2>&1 &
echo "$(date) Upload bot started, PID: $!"
