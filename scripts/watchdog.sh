#!/bin/bash
# Watchdog: ensure bot and API server are running + functional health check
DEPLOY_DIR="/opt/phonesinventory"
RESTART=0

# 1. Process check
if ! pgrep -f "inventory_bot.py" > /dev/null; then
    echo "$(date) Bot not running"
    RESTART=1
fi

if ! pgrep -f "api_server.py" > /dev/null; then
    echo "$(date) API server not running"
    RESTART=1
fi

# 2. API health check (only if process is running)
if [ $RESTART -eq 0 ]; then
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 http://localhost:8580/api/health 2>/dev/null)
    if [ "$HTTP_CODE" != "200" ]; then
        echo "$(date) API health check failed (HTTP $HTTP_CODE)"
        RESTART=1
    fi
fi

# 3. Restart if needed
if [ $RESTART -eq 1 ]; then
    echo "$(date) Restarting services..."
    bash "$DEPLOY_DIR/scripts/start_bot.sh"
fi
