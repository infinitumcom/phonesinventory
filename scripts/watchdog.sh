#!/bin/bash
# Watchdog: per-process liveness + API health check, independent restarts,
# Telegram alert after 3 consecutive failed revival attempts (deduped).
DEPLOY_DIR="/opt/phonesinventory"
FAIL_FILE="/tmp/pi-watchdog-fails"
ALERT_FILE="/tmp/pi-watchdog-alerted"

# .env for BOT_TOKEN / REPORT_CHAT_ID (alerts)
if [ -f "$DEPLOY_DIR/.env" ]; then
    set -a; source "$DEPLOY_DIR/.env"; set +a
fi

send_telegram() {
    [ -n "$BOT_TOKEN" ] && [ -n "$REPORT_CHAT_ID" ] || return 0
    curl -s -m 10 "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${REPORT_CHAT_ID}" \
        --data-urlencode "text=$1" > /dev/null 2>&1
}

NEED_FIX=0

# ─── API: process + functional health ───
API_OK=1
if ! pgrep -f "python3 .*api_server\.py" > /dev/null; then
    echo "$(date) API server not running"
    API_OK=0
else
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 http://localhost:8580/api/health 2>/dev/null)
    if [ "$HTTP_CODE" != "200" ]; then
        echo "$(date) API health check failed (HTTP $HTTP_CODE)"
        API_OK=0
    fi
fi
if [ $API_OK -eq 0 ]; then
    NEED_FIX=1
    echo "$(date) Restarting API server..."
    # 复活路径绕过测试门禁：测试只挡部署，不挡复活
    bash "$DEPLOY_DIR/scripts/start_bot.sh" --skip-tests --only-api
fi

# ─── Bot: process check ───
if ! pgrep -f "python3 .*inventory_bot\.py" > /dev/null; then
    NEED_FIX=1
    echo "$(date) Bot not running, restarting..."
    bash "$DEPLOY_DIR/scripts/start_bot.sh" --skip-tests --only-bot
fi

# ─── Verify recovery + alerting ───
if [ $NEED_FIX -eq 1 ]; then
    sleep 3
    STILL_BAD=0
    pgrep -f "python3 .*api_server\.py" > /dev/null || STILL_BAD=1
    pgrep -f "python3 .*inventory_bot\.py" > /dev/null || STILL_BAD=1

    if [ $STILL_BAD -eq 1 ]; then
        FAILS=$(($(cat "$FAIL_FILE" 2>/dev/null || echo 0) + 1))
        echo "$FAILS" > "$FAIL_FILE"
        echo "$(date) Revival failed (consecutive: $FAILS)"
        if [ "$FAILS" -ge 3 ] && [ ! -f "$ALERT_FILE" ]; then
            send_telegram "🚨 PhonesInventory 看门狗告警：服务连续 ${FAILS} 次拉起失败，需要人工介入。$(date '+%Y-%m-%d %H:%M')"
            touch "$ALERT_FILE"
        fi
        exit 1
    fi
fi

# Healthy (or recovered): clear counter, send recovery notice if we had alerted
if [ -f "$ALERT_FILE" ]; then
    send_telegram "✅ PhonesInventory 服务已恢复正常。$(date '+%Y-%m-%d %H:%M')"
    rm -f "$ALERT_FILE"
fi
rm -f "$FAIL_FILE"
