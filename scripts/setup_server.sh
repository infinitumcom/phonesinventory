#!/bin/bash
# ============================================
# PhoneInventory - 服务器一键部署脚本
# Bot + Website (Nginx) + 数据自动同步
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

# 4. Set up Nginx for website
echo "🌐 Setting up Nginx..."
cat > /etc/nginx/sites-available/phonesinventory <<NGINXEOF
server {
    listen 80;
    server_name $DOMAIN www.$DOMAIN;

    root $DEPLOY_DIR;
    index index.html;

    # Static files caching
    location ~* \.(css|js|png|jpg|jpeg|gif|ico|svg|woff|woff2)$ {
        expires 1h;
        add_header Cache-Control "public, immutable";
    }

    # Data files - no cache (updated frequently)
    location /data/ {
        expires -1;
        add_header Cache-Control "no-cache, no-store, must-revalidate";
    }

    location / {
        try_files \$uri \$uri/ /index.html;
    }

    # Block sensitive files
    location ~ /\. { deny all; }
    location ~ \.py$ { deny all; }
    location ~ \.sh$ { deny all; }
    location ~ \.env$ { deny all; }
    location /scripts/ { deny all; }
}
NGINXEOF

# Enable site
mkdir -p /etc/nginx/sites-enabled
ln -sf /etc/nginx/sites-available/phonesinventory /etc/nginx/sites-enabled/

# Check if sites-enabled is included in nginx.conf
if ! grep -q "sites-enabled" /etc/nginx/nginx.conf 2>/dev/null; then
    # For 宝塔 Nginx, add include directive
    if grep -q "include.*vhost" /www/server/nginx/conf/nginx.conf 2>/dev/null; then
        echo "📝 宝塔 Nginx detected, creating vhost config..."
        cp /etc/nginx/sites-available/phonesinventory /www/server/panel/vhost/nginx/$DOMAIN.conf 2>/dev/null || true
    fi
fi

# Also create in 宝塔 vhost directory if it exists
if [ -d "/www/server/panel/vhost/nginx" ]; then
    cp /etc/nginx/sites-available/phonesinventory /www/server/panel/vhost/nginx/$DOMAIN.conf
    echo "📝 Copied Nginx config to 宝塔 vhost directory"
fi

# Test and reload Nginx
nginx -t && nginx -s reload 2>/dev/null || systemctl reload nginx 2>/dev/null || service nginx reload 2>/dev/null
echo "✅ Nginx configured for $DOMAIN"

# 5. Run initial data export
echo "📊 Running initial data export..."
python3 "$DEPLOY_DIR/scripts/export_inventory.py"

# 6. Set up cron jobs
# Auto-sync every 5 minutes + restart bot if code changed
CRON_SYNC="*/5 * * * * cd $DEPLOY_DIR && BEFORE=\$(git rev-parse HEAD) && git pull origin main >> /tmp/phonesinventory-sync.log 2>&1 && AFTER=\$(git rev-parse HEAD) && [ \"\$BEFORE\" != \"\$AFTER\" ] && bash scripts/start_bot.sh >> /tmp/phonesinventory-sync.log 2>&1"
# Export inventory data every 2 minutes (SQLite → phones.js for website)
CRON_EXPORT="*/2 * * * * cd $DEPLOY_DIR && python3 scripts/export_inventory.py >> /tmp/phonesinventory-export.log 2>&1"
# Daily report at PST 20:00 (UTC 03:00)
CRON_REPORT="0 3 * * * cd $DEPLOY_DIR && python3 scripts/daily_report.py >> /tmp/phonesinventory-report.log 2>&1"
# Watchdog: check every minute, restart if bot crashed
CRON_WATCHDOG="* * * * * bash $DEPLOY_DIR/scripts/watchdog.sh >> /tmp/phonesinventory-watchdog.log 2>&1"

# Add to crontab (avoid duplicates)
(crontab -l 2>/dev/null | grep -v "phonesinventory" ; echo "# PhoneInventory auto-sync + auto-restart on code change"; echo "$CRON_SYNC"; echo "# PhoneInventory export data to website"; echo "$CRON_EXPORT"; echo "# PhoneInventory daily report PST 20:00"; echo "$CRON_REPORT"; echo "# PhoneInventory bot watchdog"; echo "$CRON_WATCHDOG") | crontab -

# 7. Start the bot
echo "🤖 Starting upload bot..."
bash "$DEPLOY_DIR/scripts/start_bot.sh"

echo ""
echo "============================================"
echo "✅ Deployment complete!"
echo "============================================"
echo ""
echo "📂 Deploy dir: $DEPLOY_DIR"
echo "🌐 Website: http://$DOMAIN"
echo "🤖 Bot: running (pgrep -f inventory_bot)"
echo "🔄 Auto-sync: every 5 min (git pull + restart if changed)"
echo "📊 Data export: every 2 min (SQLite → website)"
echo "🛡️ Watchdog: every 1 min (auto-restart if crashed)"
echo ""
echo "⚠️  DNS: 在 Namecheap 设置 A 记录:"
echo "   $DOMAIN → $(curl -s ifconfig.me 2>/dev/null || echo '服务器IP')"
echo "   www.$DOMAIN → $(curl -s ifconfig.me 2>/dev/null || echo '服务器IP')"
echo ""
echo "📋 Cron jobs:"
crontab -l | grep phonesinventory
echo ""
echo "📋 Useful commands:"
echo "  tail -f /tmp/upload-bot.log             # Bot logs"
echo "  tail -f /tmp/phonesinventory-export.log  # Export logs"
echo "  python3 $DEPLOY_DIR/scripts/export_inventory.py  # Manual export"
echo "  bash $DEPLOY_DIR/scripts/start_bot.sh    # Restart bot"
