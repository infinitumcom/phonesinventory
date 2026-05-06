#!/bin/bash
# ============================================
# PhoneInventory Bot - 服务器一键部署脚本
# 在宝塔面板终端执行此脚本
# ============================================

DEPLOY_DIR="/opt/phonesinventory"
REPO_URL="https://github.com/infinitumcom/phonesinventory.git"

echo "📦 Setting up PhoneInventory Bot..."

# 1. Clone or update repo
if [ -d "$DEPLOY_DIR" ]; then
    echo "📂 Directory exists, pulling latest..."
    cd "$DEPLOY_DIR" && git pull origin main
else
    echo "📥 Cloning repo..."
    git clone "$REPO_URL" "$DEPLOY_DIR"
fi

cd "$DEPLOY_DIR"

# 2. Create .env if not exists
if [ ! -f "$DEPLOY_DIR/.env" ]; then
    echo "⚙️ Creating .env file (edit with your keys!)..."
    cat > "$DEPLOY_DIR/.env" <<'ENVEOF'
ANTHROPIC_API_KEY=sk-ant-your-key-here
ADMIN_IDS=7625761638
ALLOWED_GROUP_IDS=
ENVEOF
    echo "⚠️  请编辑 $DEPLOY_DIR/.env 填入 ANTHROPIC_API_KEY"
fi

# 3. Create data directory
mkdir -p "$DEPLOY_DIR/data"

# 4. Set up cron jobs
# Auto-sync every 5 minutes + restart bot if code changed
CRON_SYNC="*/5 * * * * cd $DEPLOY_DIR && BEFORE=\$(git rev-parse HEAD) && git pull origin main >> /tmp/phonesinventory-sync.log 2>&1 && AFTER=\$(git rev-parse HEAD) && [ \"\$BEFORE\" != \"\$AFTER\" ] && bash scripts/start_bot.sh >> /tmp/phonesinventory-sync.log 2>&1"
# Daily report at PST 20:00 (UTC 03:00)
CRON_REPORT="0 3 * * * cd $DEPLOY_DIR && python3 scripts/daily_report.py >> /tmp/phonesinventory-report.log 2>&1"
# Watchdog: check every minute, restart if bot crashed
CRON_WATCHDOG="* * * * * bash $DEPLOY_DIR/scripts/watchdog.sh >> /tmp/phonesinventory-watchdog.log 2>&1"

# Add to crontab (avoid duplicates)
(crontab -l 2>/dev/null | grep -v "phonesinventory" ; echo "# PhoneInventory auto-sync + auto-restart on code change"; echo "$CRON_SYNC"; echo "# PhoneInventory daily report PST 20:00"; echo "$CRON_REPORT"; echo "# PhoneInventory bot watchdog"; echo "$CRON_WATCHDOG") | crontab -

# 5. Start the bot
echo "🤖 Starting upload bot..."
bash "$DEPLOY_DIR/scripts/start_bot.sh"

echo ""
echo "✅ Setup complete!"
echo "📂 Deploy dir: $DEPLOY_DIR"
echo "🤖 Bot: running (PID check: pgrep -f inventory_bot)"
echo "🔄 Auto-sync: every 5 minutes (auto-restart on code change)"
echo "🛡️ Watchdog: every minute (auto-restart if crashed)"
echo "📊 Daily report: PST 20:00 (UTC 03:00)"
echo ""
echo "📋 Cron jobs:"
crontab -l | grep phonesinventory
echo ""
echo "📋 Useful commands:"
echo "  tail -f /tmp/upload-bot.log        # Bot logs"
echo "  tail -f /tmp/phonesinventory-sync.log  # Sync logs"
echo "  bash $DEPLOY_DIR/scripts/start_bot.sh  # Restart bot"
