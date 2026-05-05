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

# 2. Test the report script
echo "🧪 Testing report script..."
python3 scripts/daily_report.py

# 3. Set up auto-sync + daily report cron
# Auto-sync every 5 minutes (pull latest from GitHub)
# Daily report at PST 20:00 (UTC 03:00)
CRON_SYNC="*/5 * * * * cd $DEPLOY_DIR && git pull origin main >> /tmp/phonesinventory-sync.log 2>&1"
CRON_REPORT="0 3 * * * cd $DEPLOY_DIR && python3 scripts/daily_report.py >> /tmp/phonesinventory-report.log 2>&1"

# Add to crontab (avoid duplicates)
(crontab -l 2>/dev/null | grep -v "phonesinventory" ; echo "# PhoneInventory auto-sync"; echo "$CRON_SYNC"; echo "# PhoneInventory daily report PST 20:00"; echo "$CRON_REPORT") | crontab -

echo ""
echo "✅ Setup complete!"
echo "📂 Deploy dir: $DEPLOY_DIR"
echo "🔄 Auto-sync: every 5 minutes"
echo "📊 Daily report: PST 20:00 (UTC 03:00)"
echo ""
echo "📋 Cron jobs:"
crontab -l | grep phonesinventory
