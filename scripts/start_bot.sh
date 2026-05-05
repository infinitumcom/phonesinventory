#!/bin/bash
# Start inventory bot with environment
DEPLOY_DIR="/opt/phonesinventory"
cd "$DEPLOY_DIR"

# Load .env if exists
if [ -f "$DEPLOY_DIR/.env" ]; then
    export $(grep -v '^#' "$DEPLOY_DIR/.env" | xargs)
fi

# Kill existing bot
pkill -f "inventory_bot.py" 2>/dev/null
sleep 1

# Start bot
echo "$(date) Starting inventory bot..."
nohup python3 scripts/inventory_bot.py >> /tmp/inventory-bot.log 2>&1 &
echo "$(date) Bot started, PID: $!"
