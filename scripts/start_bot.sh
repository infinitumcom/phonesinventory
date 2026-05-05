#!/bin/bash
# Start upload bot (@PhoneInventoryUpload_bot)
DEPLOY_DIR="/opt/phonesinventory"
cd "$DEPLOY_DIR"

# Load .env if exists
if [ -f "$DEPLOY_DIR/.env" ]; then
    export $(grep -v '^#' "$DEPLOY_DIR/.env" | xargs)
fi

# Kill existing upload bot
pkill -f "inventory_bot.py" 2>/dev/null
sleep 1

# Start upload bot
echo "$(date) Starting upload bot (@PhoneInventoryUpload_bot)..."
nohup python3 scripts/inventory_bot.py >> /tmp/upload-bot.log 2>&1 &
echo "$(date) Upload bot started, PID: $!"
