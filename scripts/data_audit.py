#!/usr/bin/env python3
"""
PhoneInventory 数据质量审计 — 定时运行
每小时自动检查数据库，发现异常立即通过 Telegram 告警。
由 cron 调用: 0 * * * * python3 /opt/phonesinventory/scripts/data_audit.py
"""
import sqlite3
import json
import re
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env_loader

DEPLOY_DIR = env_loader.DEPLOY_DIR
DB_PATH = os.path.join(DEPLOY_DIR, "data", "inventory.db")
BOT_TOKEN = env_loader.require_env("BOT_TOKEN")
ADMIN_CHAT = env_loader.require_env("REPORT_CHAT_ID")

PST = timezone(timedelta(hours=-7))

# Canonical store names
VALID_STORES = {
    'Alhambra', 'Monterey Park', 'San Gabriel', 'Rowland Heights',
    'Arcadia 1', 'Arcadia 2', 'Irvine', 'Rancho Cucamonga',
    'Las Vegas', 'HQ 总仓',
}

def send_alert(text):
    """Send alert to admin via Telegram."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": ADMIN_CHAT,
        "text": text,
        "parse_mode": "Markdown"
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"Failed to send alert: {e}")


def run_audit():
    if not os.path.exists(DB_PATH):
        print("No database found, skipping audit")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    issues = []
    auto_fixed = []

    # 1. Invalid IMEIs
    c.execute("SELECT id, imei, model, store FROM inventory")
    for r in c.fetchall():
        imei = r[1] or ""
        if not imei:
            issues.append(f"#{r[0]} 空IMEI {r[2]} {r[3]}")
        elif len(imei) != 15:
            issues.append(f"#{r[0]} IMEI长度={len(imei)} {r[2]}")
        elif not imei.isdigit():
            issues.append(f"#{r[0]} IMEI含非数字 {r[2]}")
        if imei.startswith("8904"):
            issues.append(f"#{r[0]} EID误存为IMEI {r[2]}")

    # 2. Duplicate IMEIs
    c.execute("SELECT imei, COUNT(*) FROM inventory WHERE imei != '' GROUP BY imei HAVING COUNT(*) > 1")
    for r in c.fetchall():
        issues.append(f"IMEI重复: {r[0]} 出现{r[1]}次")

    # 3. Empty store
    c.execute("SELECT id, imei, model FROM inventory WHERE store IS NULL OR store = ''")
    for r in c.fetchall():
        issues.append(f"#{r[0]} 缺少门店 {r[2]}")

    # 4. Invalid region
    c.execute("SELECT id, imei, model, region FROM inventory WHERE region IS NULL OR region = '' OR region NOT IN ('us','hk','cn','jp','kr')")
    for r in c.fetchall():
        issues.append(f"#{r[0]} 无效区域[{r[3]}] {r[2]}")

    # 5. Non-standard store names — auto-fix
    STORE_FIX_MAP = {
        'las-vegas': 'Las Vegas', 'las vegas': 'Las Vegas',
        'san-gabriel': 'San Gabriel', 'san gabriel': 'San Gabriel',
        'monterey-park': 'Monterey Park',
        'rowland-heights': 'Rowland Heights',
        'arcadia-1': 'Arcadia 1', 'arcadia-2': 'Arcadia 2',
        'rancho-cucamonga': 'Rancho Cucamonga',
        'hq-warehouse': 'HQ 总仓',
    }
    c.execute("SELECT DISTINCT store FROM inventory WHERE store IS NOT NULL AND store != ''")
    for r in c.fetchall():
        store = r[0]
        if store not in VALID_STORES:
            fix = STORE_FIX_MAP.get(store.lower())
            if fix:
                c.execute("UPDATE inventory SET store = ? WHERE store = ?", (fix, store))
                conn.commit()
                auto_fixed.append(f"门店名 '{store}' → '{fix}'")
            else:
                issues.append(f"非标门店名: '{store}'")

    # 6. Region vs model number mismatch
    c.execute("SELECT id, imei, model, region, raw_ocr FROM inventory WHERE status='available'")
    for r in c.fetchall():
        raw = r[4] or ""
        m = re.search(r'(LL|ZA|ZP|CH)/[A-Z]', raw)
        if m:
            suffix = m.group(1)
            correct = "us" if suffix == "LL" else "hk" if suffix in ("ZA", "ZP") else "cn"
            if r[3] != correct:
                issues.append(f"#{r[0]} 区域不匹配: 标记={r[3]} 实际={correct} {r[2]}")

    # 7. Sales-inventory sync
    c.execute("""SELECT s.imei, s.id, i.status FROM sales s
                 LEFT JOIN inventory i ON s.imei = i.imei
                 WHERE s.status='completed' AND (i.status IS NULL OR i.status != 'sold')""")
    for r in c.fetchall():
        # Auto-fix: mark as sold in inventory
        imei, sale_id, inv_status = r
        if inv_status and inv_status != 'sold':
            c.execute("UPDATE inventory SET status = 'sold' WHERE imei = ?", (imei,))
            conn.commit()
            auto_fixed.append(f"IMEI {imei} 状态→sold (已售订单{sale_id})")
        elif inv_status is None:
            issues.append(f"销售#{sale_id} IMEI={imei} 已完成但无库存记录")

    # (phones.js sync check removed — public export retired, frontend now
    #  reads token-protected /api/phones directly)

    # Stats
    c.execute("SELECT COUNT(*) FROM inventory")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM inventory WHERE status='available'")
    avail = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM inventory WHERE status='sold'")
    sold = c.fetchone()[0]
    conn.close()

    now = datetime.now(PST).strftime("%m/%d %H:%M")

    if auto_fixed:
        fix_msg = f"🔧 *自动修复* ({now} PST)\n\n"
        for f in auto_fixed:
            fix_msg += f"  ✅ {f}\n"
        send_alert(fix_msg)
        print(f"AUTO-FIXED: {len(auto_fixed)} issues")
        for f in auto_fixed:
            print(f"  ✅ {f}")

    if issues:
        msg = f"⚠️ *数据质量告警* ({now} PST)\n\n"
        msg += f"发现 {len(issues)} 个问题:\n"
        for i, issue in enumerate(issues[:10], 1):
            msg += f"{i}. {issue}\n"
        if len(issues) > 10:
            msg += f"\n...还有 {len(issues)-10} 个问题"
        msg += f"\n📊 库存: {total}总 | {avail}可售 | {sold}已售"
        send_alert(msg)
        print(f"ALERT: {len(issues)} issues found, notification sent")
        for issue in issues:
            print(f"  - {issue}")
    else:
        print(f"OK: {now} — {total} total, {avail} available, {sold} sold — no issues")


if __name__ == "__main__":
    run_audit()
