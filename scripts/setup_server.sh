#!/bin/bash
# ============================================
# PhoneInventory - 服务器一键部署脚本
# Bot + API Server + Website (Nginx) + 数据自动同步
# 在宝塔面板终端执行此脚本
# ============================================

DEPLOY_DIR="/opt/phonesinventory"
REPO_URL="https://github.com/infinitumcom/phonesinventory.git"
DOMAIN="phonesinventory.com"

echo "📦 Setting up PhoneInventory..."

# 1. Clone or update repo
if [ -d "$DEPLOY_DIR/.git" ]; then
    echo "📂 Directory exists, pulling latest..."
    cd "$DEPLOY_DIR" && git pull --ff-only origin main
else
    echo "📥 Cloning repo..."
    rm -rf "$DEPLOY_DIR"
    git clone "$REPO_URL" "$DEPLOY_DIR"
fi

cd "$DEPLOY_DIR"

# 2. Create .env if not exists
if [ ! -f "$DEPLOY_DIR/.env" ]; then
    echo "⚙️ Creating .env file (edit with your keys!)..."
    cat > "$DEPLOY_DIR/.env" <<'ENVEOF'
ANTHROPIC_API_KEY=sk-ant-your-key-here
BOT_TOKEN=your-telegram-bot-token
ADMIN_IDS=your-admin-user-id
REPORT_CHAT_ID=your-report-chat-id
ALLOWED_GROUP_IDS=
AUTH_SECRET=generate-with-openssl-rand-hex-32
IMEI_API_KEY=
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=noreply@phonesinventory.com
SMTP_PASS=
MAIL_FROM_NAME=PhonesInventory
ENVEOF
    echo "⚠️  请编辑 $DEPLOY_DIR/.env 填入全部凭据（BOT_TOKEN/AUTH_SECRET 为必填）"
fi

# 3. Create data directory
mkdir -p "$DEPLOY_DIR/data"

# 4. Set up Nginx — copy config from repo
echo "🌐 Setting up Nginx..."
if [ -d "/www/server/panel/vhost/nginx" ]; then
    cp "$DEPLOY_DIR/scripts/nginx.conf" "/www/server/panel/vhost/nginx/$DOMAIN.conf"
    echo "📝 Nginx config → 宝塔 vhost"
fi
mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled 2>/dev/null
cp "$DEPLOY_DIR/scripts/nginx.conf" "/etc/nginx/sites-available/phonesinventory" 2>/dev/null
ln -sf /etc/nginx/sites-available/phonesinventory /etc/nginx/sites-enabled/ 2>/dev/null

# 改 Nginx 必须走 nginx-safe-reload（服务器站点隔离保护）
if command -v nginx-safe-reload > /dev/null 2>&1; then
    nginx-safe-reload
else
    nginx -t && nginx -s reload 2>/dev/null || systemctl reload nginx 2>/dev/null || service nginx reload 2>/dev/null
fi
echo "✅ Nginx configured for $DOMAIN"

# 5. Set up cron jobs
CRON_SYNC="*/5 * * * * cd $DEPLOY_DIR && BEFORE=\$(git rev-parse HEAD) && git pull --ff-only origin main >> /tmp/phonesinventory-sync.log 2>&1 && AFTER=\$(git rev-parse HEAD) && [ \"\$BEFORE\" != \"\$AFTER\" ] && bash scripts/start_bot.sh >> /tmp/phonesinventory-sync.log 2>&1"
CRON_REPORT="0 3 * * * cd $DEPLOY_DIR && python3 scripts/daily_report.py >> /tmp/phonesinventory-report.log 2>&1"
CRON_WATCHDOG="* * * * * bash $DEPLOY_DIR/scripts/watchdog.sh >> /tmp/phonesinventory-watchdog.log 2>&1"
CRON_AUDIT="0 * * * * cd $DEPLOY_DIR && python3 scripts/data_audit.py >> /tmp/phonesinventory-audit.log 2>&1"
CRON_BACKUP="0 2 * * * bash $DEPLOY_DIR/scripts/backup_db.sh >> /tmp/phonesinventory-backup.log 2>&1"

(crontab -l 2>/dev/null | grep -v "phonesinventory" ; echo "# PhoneInventory auto-sync"; echo "$CRON_SYNC"; echo "# PhoneInventory daily report"; echo "$CRON_REPORT"; echo "# PhoneInventory watchdog"; echo "$CRON_WATCHDOG"; echo "# PhoneInventory data audit"; echo "$CRON_AUDIT"; echo "# PhoneInventory daily db backup"; echo "$CRON_BACKUP") | crontab -

# 7. Start bot + API server
echo "🤖 Starting services..."
bash "$DEPLOY_DIR/scripts/start_bot.sh"

echo ""
echo "============================================"
echo "✅ Deployment complete!"
echo "============================================"
echo ""
echo "📂 Deploy dir: $DEPLOY_DIR"
echo "🌐 Website: http://$DOMAIN"
echo "🔌 API: http://127.0.0.1:8580"
echo "🤖 Bot: running"
echo "🔄 Auto-sync: every 5 min"
echo "🛡️ Watchdog: every 1 min"
echo "🔍 Data audit: every 1 hour (alerts via Telegram)"
echo "💾 DB backup: daily 02:00 (keep 14 days)"
echo ""
echo "📋 Useful commands:"
echo "  tail -f /tmp/upload-bot.log             # Bot logs"
echo "  tail -f /tmp/phonesinventory-api.log    # API logs"
echo "  bash $DEPLOY_DIR/scripts/start_bot.sh   # Restart all"
