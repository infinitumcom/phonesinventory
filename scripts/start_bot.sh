#!/bin/bash
# Start upload bot + API server (with pre-deploy tests)
# Usage: start_bot.sh [--skip-tests] [--only-api|--only-bot]
#   --skip-tests  跳过部署门禁测试（watchdog 复活路径专用：测试只挡部署，不挡复活）
#   --only-api    只重启 API server
#   --only-bot    只重启 Telegram bot
DEPLOY_DIR="/opt/phonesinventory"
cd "$DEPLOY_DIR"

# 互斥锁：防 cron 部署与 watchdog 并发拉起双实例
exec 200>/tmp/pi-start.lock
flock -n 200 || { echo "$(date) Another start is in progress, skipping."; exit 0; }

# Load .env（source 方式，值含空格/引号也安全）
if [ -f "$DEPLOY_DIR/.env" ]; then
    set -a; source "$DEPLOY_DIR/.env"; set +a
fi

SKIP_TESTS=0
ONLY=""
for arg in "$@"; do
    case "$arg" in
        --skip-tests) SKIP_TESTS=1 ;;
        --only-api)   ONLY="api" ;;
        --only-bot)   ONLY="bot" ;;
    esac
done

# ─── Pre-deploy tests ───
if [ "$SKIP_TESTS" -eq 0 ]; then
    echo "$(date) Running pre-deploy tests..."
    python3 scripts/test_bot.py 2>&1
    if [ $? -ne 0 ]; then
        echo "$(date) TESTS FAILED — aborting start. Fix issues before deploying."
        exit 1
    fi
    echo "$(date) Tests passed."
fi

# Truncate logs if over 10MB
for log in /tmp/phonesinventory-api.log /tmp/upload-bot.log /var/log/inventory_bot.log; do
    if [ -f "$log" ] && [ $(stat -f%z "$log" 2>/dev/null || stat -c%s "$log" 2>/dev/null) -gt 10485760 ] 2>/dev/null; then
        tail -1000 "$log" > "${log}.tmp" && mv "${log}.tmp" "$log"
        echo "$(date) Rotated $log"
    fi
done

start_api() {
    pkill -f "python3 .*api_server\.py" 2>/dev/null
    sleep 1
    echo "$(date) Starting API server..."
    # 200>&- 关闭锁 FD：防后台进程继承锁导致永久持锁（dashboard 死锁前科）
    nohup python3 scripts/api_server.py >> /tmp/phonesinventory-api.log 2>&1 200>&- &
    echo "$(date) API server started, PID: $!"
}

start_bot() {
    pkill -f "python3 .*inventory_bot\.py" 2>/dev/null
    sleep 1
    echo "$(date) Starting upload bot (@PhoneInventoryUpload_bot)..."
    nohup python3 scripts/inventory_bot.py >> /var/log/inventory_bot.log 2>&1 200>&- &
    echo "$(date) Upload bot started, PID: $!"
}

case "$ONLY" in
    api) start_api ;;
    bot) start_bot ;;
    *)   start_api; start_bot ;;
esac
