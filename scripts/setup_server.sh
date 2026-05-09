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
    cd "$DEPLOY_DIR" && git pull origin main
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
ADMIN_IDS=7625761638
ALLOWED_GROUP_IDS=-5281197356
ENVEOF
    echo "⚠️  请编辑 $DEPLOY_DIR/.env 填入 ANTHROPIC_API_KEY"
fi

# 3. Create data directory + initial phones.js
mkdir -p "$DEPLOY_DIR/data"
if [ ! -f "$DEPLOY_DIR/data/phones.js" ]; then
    echo "const phones = [];" > "$DEPLOY_DIR/data/phones.js"
fi

# 4. Set up Nginx — copy config from repo
echo "🌐 Setting up Nginx..."
if [ -d "/www/server/panel/vhost/nginx" ]; then
    cp "$DEPLOY_DIR/scripts/nginx.conf" "/www/server/panel/vhost/nginx/$DOMAIN.conf"
    echo "📝 Nginx config → 宝塔 vhost"
fi
mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled 2>/dev/null
cp "$DEPLOY_DIR/scripts/nginx.conf" "/etc/nginx/sites-available/phonesinventory" 2>/dev/null
ln -sf /etc/nginx/sites-available/phonesinventory /etc/nginx/sites-enabled/ 2>/dev/null

nginx -t && nginx -s reload 2>/dev/null || systemctl reload nginx 2>/dev/null || service nginx reload 2>/dev/null
echo "✅ Nginx configured for $DOMAIN"

# 5. Run initial data export
echo "📊 Running initial data export..."
python3 "$DEPLOY_DIR/scripts/export_inventory.py"

# 6. Set up cron jobs
CRON_SYNC="*/5 * * * * cd $DEPLOY_DIR && BEFORE=\$(git rev-parse HEAD) && git pull origin main >> /tmp/phonesinventory-sync.log 2>&1 && AFTER=\$(git rev-parse HEAD) && [ \"\$BEFORE\" != \"\$AFTER\" ] && bash scripts/start_bot.sh >> /tmp/phonesinventory-sync.log 2>&1"
CRON_EXPORT="*/2 * * * * cd $DEPLOY_DIR && python3 scripts/export_inventory.py >> /tmp/phonesinventory-export.log 2>&1"
CRON_REPORT="0 3 * * * cd $DEPLOY_DIR && python3 scripts/daily_report.py >> /tmp/phonesinventory-report.log 2>&1"
CRON_WATCHDOG="* * * * * bash $DEPLOY_DIR/scripts/watchdog.sh >> /tmp/phonesinventory-watchdog.log 2>&1"

(crontab -l 2>/dev/null | grep -v "phonesinventory" ; echo "# PhoneInventory auto-sync"; echo "$CRON_SYNC"; echo "# PhoneInventory export"; echo "$CRON_EXPORT"; echo "# PhoneInventory daily report"; echo "$CRON_REPORT"; echo "# PhoneInventory watchdog"; echo "$CRON_WATCHDOG") | crontab -

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
echo "📊 Data export: every 2 min"
echo "🛡️ Watchdog: every 1 min"
echo ""
echo "📋 Useful commands:"
echo "  tail -f /tmp/upload-bot.log             # Bot logs"
echo "  tail -f /tmp/phonesinventory-api.log    # API logs"
echo "  bash $DEPLOY_DIR/scripts/start_bot.sh   # Restart all"
