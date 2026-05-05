#!/bin/bash
# Watchdog: ensure inventory bot is running
if ! pgrep -f "inventory_bot.py" > /dev/null; then
    echo "$(date) Bot not running, restarting..."
    bash /opt/phonesinventory/scripts/start_bot.sh
fi
