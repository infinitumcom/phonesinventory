#!/bin/bash
# Watchdog: ensure bot and API server are running
RESTART=0

if ! pgrep -f "inventory_bot.py" > /dev/null; then
    echo "$(date) Bot not running"
    RESTART=1
fi

if ! pgrep -f "api_server.py" > /dev/null; then
    echo "$(date) API server not running"
    RESTART=1
fi

if [ $RESTART -eq 1 ]; then
    echo "$(date) Restarting services..."
    bash /opt/phonesinventory/scripts/start_bot.sh
fi
