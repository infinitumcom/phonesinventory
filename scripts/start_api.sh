#!/bin/bash
# Start API server for cross-device data sync
DEPLOY_DIR="/opt/phonesinventory"
cd "$DEPLOY_DIR"

# Load .env
if [ -f "$DEPLOY_DIR/.env" ]; then
    export $(grep -v '^#' "$DEPLOY_DIR/.env" | xargs)
fi

# Kill existing API server
pkill -f "api_server.py" 2>/dev/null
sleep 1

# Start API server
echo "$(date) Starting API server..."
nohup python3 scripts/api_server.py >> /tmp/phonesinventory-api.log 2>&1 &
echo "$(date) API server started, PID: $!"
