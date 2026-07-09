#!/usr/bin/env python3
"""
PhoneInventory Daily Telegram Report
Reads inventory/sales data from SQLite database and sends formatted report via Telegram.
"""

import json
import os
import sqlite3
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# ─── Config (credentials from .env via env_loader — no defaults in code) ───
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env_loader

DEPLOY_DIR = env_loader.DEPLOY_DIR
DB_PATH = os.path.join(DEPLOY_DIR, "data", "inventory.db")
BOT_TOKEN = env_loader.require_env("BOT_TOKEN")
CHAT_ID = env_loader.require_env("REPORT_CHAT_ID")
PST = timezone(timedelta(hours=-7))

# Store key → display info
STORE_INFO = {
    'Alhambra': {'taxLabel': 'CA 10.25%'},
    'Monterey Park': {'taxLabel': 'CA 9.5%'},
    'San Gabriel': {'taxLabel': 'CA 9.5%'},
    'Rowland Heights': {'taxLabel': 'CA 9.5%'},
    'Arcadia 1': {'taxLabel': 'CA 10.25%'},
    'Arcadia 2': {'taxLabel': 'CA 10.25%'},
    'Irvine': {'taxLabel': 'CA 7.75%'},
    'Rancho Cucamonga': {'taxLabel': 'CA 7.75%'},
    'Las Vegas': {'taxLabel': 'NV 8.375%'},
    'HQ 总仓': {'taxLabel': 'N/A'},
}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def build_report():
    """Build report from database data."""
    now = datetime.now(PST)
    date_str = now.strftime("%Y-%m-%d %A")
    today_str = now.strftime("%Y-%m-%d")
    month_start = now.strftime("%Y-%m-01")

    conn = get_db()

    # ── Inventory stats ──
    total = conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
    available = conn.execute("SELECT COUNT(*) FROM inventory WHERE status='available'").fetchone()[0]
    sold = conn.execute("SELECT COUNT(*) FROM inventory WHERE status='sold'").fetchone()[0]
    new_count = conn.execute("SELECT COUNT(*) FROM inventory WHERE status='available' AND condition='new'").fetchone()[0]
    used_count = conn.execute("SELECT COUNT(*) FROM inventory WHERE status='available' AND condition='used'").fetchone()[0]

    # Brand breakdown
    brands_rows = conn.execute(
        "SELECT brand, COUNT(*) as cnt FROM inventory WHERE status='available' GROUP BY brand ORDER BY cnt DESC"
    ).fetchall()
    brand_line = " · ".join(f"{r['brand'] or 'Other'} {r['cnt']}" for r in brands_rows) or "—"

    # Region breakdown
    region_rows = conn.execute(
        "SELECT region, COUNT(*) as cnt FROM inventory WHERE status='available' GROUP BY region ORDER BY cnt DESC"
    ).fetchall()
    flags = {"us": "🇺🇸", "hk": "🇭🇰", "cn": "🇨🇳", "jp": "🇯🇵", "kr": "🇰🇷"}
    region_line = " · ".join(f"{flags.get(r['region'], '🌐')}{(r['region'] or 'US').upper()} {r['cnt']}" for r in region_rows) or "—"

    # Today's inventory value
    value_row = conn.execute("SELECT SUM(price) as v, SUM(cost) as c FROM inventory WHERE status='available'").fetchone()
    total_value = int(value_row['v'] or 0)
    total_cost = int(value_row['c'] or 0)

    # ── Sales stats ──
    today_sales = conn.execute(
        "SELECT COUNT(*) as cnt, SUM(total) as rev FROM sales WHERE status='completed' AND created_at >= ?",
        (today_str,)
    ).fetchone()
    today_sold = today_sales['cnt'] or 0
    today_rev = int(today_sales['rev'] or 0)

    mtd_sales = conn.execute(
        "SELECT COUNT(*) as cnt, SUM(total) as rev FROM sales WHERE status='completed' AND created_at >= ?",
        (month_start,)
    ).fetchone()
    mtd_sold = mtd_sales['cnt'] or 0
    mtd_rev = int(mtd_sales['rev'] or 0)

    # Today's sold transactions
    today_txns = conn.execute(
        "SELECT phone_name, storage, store, total FROM sales WHERE status='completed' AND created_at >= ? ORDER BY created_at DESC",
        (today_str,)
    ).fetchall()
    sold_lines = []
    for t in today_txns:
        sold_lines.append(f"  • {t['phone_name']} {t['storage']} → {t['store']} · ${int(t['total'] or 0):,}")

    # ── Store breakdown ──
    store_rows = conn.execute("""
        SELECT store, COUNT(*) as stock,
            SUM(CASE WHEN condition='new' THEN 1 ELSE 0 END) as stock_new,
            SUM(CASE WHEN condition='used' THEN 1 ELSE 0 END) as stock_used
        FROM inventory WHERE status='available' AND store != '' GROUP BY store ORDER BY stock DESC
    """).fetchall()

    # Sales by store this month
    store_sales = {}
    for row in conn.execute("""
        SELECT store, COUNT(*) as cnt, SUM(total) as rev FROM sales
        WHERE status='completed' AND created_at >= ? GROUP BY store
    """, (month_start,)).fetchall():
        store_sales[row['store']] = {'mtd': row['cnt'], 'rev': int(row['rev'] or 0)}

    # Sales by store today
    for row in conn.execute("""
        SELECT store, COUNT(*) as cnt, SUM(total) as rev FROM sales
        WHERE status='completed' AND created_at >= ? GROUP BY store
    """, (today_str,)).fetchall():
        if row['store'] in store_sales:
            store_sales[row['store']]['today'] = row['cnt']
            store_sales[row['store']]['today_rev'] = int(row['rev'] or 0)
        else:
            store_sales[row['store']] = {'mtd': row['cnt'], 'rev': int(row['rev'] or 0), 'today': row['cnt'], 'today_rev': int(row['rev'] or 0)}

    # Rank stores by MTD sales
    all_stores = {}
    for r in store_rows:
        name = r['store']
        s = store_sales.get(name, {})
        all_stores[name] = {
            'stock': r['stock'], 'new': r['stock_new'], 'used': r['stock_used'],
            'mtd': s.get('mtd', 0), 'today': s.get('today', 0),
            'today_rev': s.get('today_rev', 0),
            'tax': STORE_INFO.get(name, {}).get('taxLabel', ''),
        }

    ranked = sorted(all_stores.items(), key=lambda x: -x[1]['mtd'])
    store_lines = []
    for i, (name, s) in enumerate(ranked, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, "  ")
        store_lines.append(
            f"{medal} *{name}*\n"
            f"    📦 库存 {s['stock']} (新{s['new']}/二手{s['used']})\n"
            f"    💰 今日 {s['today']}台 ${s['today_rev']:,}\n"
            f"    📅 本月 {s['mtd']}台 · 排名 #{i}\n"
            f"    {s['tax']}"
        )

    conn.close()

    # ── Build Message ──
    msg = f"""📊 *iFixForU 手机库存日报*
━━━━━━━━━━━━━━━━━━
📅 {date_str}

📦 *一、库存总览*
┌ 总库存: *{total}* 部
│ 在售 {available} · 已售 {sold}
│ 新机 {new_count} · 二手 {used_count}
│ 品牌: {brand_line}
│ 版本: {region_line}
└ 库存总值: *${total_value:,}* (成本 ${total_cost:,})

💰 *二、今日销售*
┌ 今日成交: *{today_sold}* 台
│ 今日营收: *${today_rev:,}*
└ 本月累计: *{mtd_sold}* 台 · ${mtd_rev:,}"""

    if sold_lines:
        msg += "\n\n📝 *交易明细:*\n" + "\n".join(sold_lines)

    if store_lines:
        msg += f"""

🏪 *三、门店详情*
{'─' * 20}
""" + "\n\n".join(store_lines)

    msg += f"""

━━━━━━━━━━━━━━━━━━
🤖 PhoneInventory Bot · {now.strftime('%H:%M PST')}
🌐 https://phonesinventory.com"""

    return msg


def send_telegram(text, chat_id=CHAT_ID):
    """Send message via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("ok"):
                print(f"Report sent to chat {chat_id}")
            else:
                print(f"Telegram error: {result}")
            return result
    except Exception as e:
        print(f"Failed to send: {e}")
        return None


def main():
    print("PhoneInventory Daily Report")
    print(f"{datetime.now(PST).strftime('%Y-%m-%d %H:%M PST')}")

    if not os.path.exists(DB_PATH):
        print(f"Database not found: {DB_PATH}")
        return

    report = build_report()
    print(f"\n{'='*40}\n{report}\n{'='*40}\n")
    send_telegram(report)


if __name__ == "__main__":
    main()
