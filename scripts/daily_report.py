#!/usr/bin/env python3
"""
PhoneInventory Daily Telegram Report
Reads phone/store data from index.html, formats and sends via Telegram Bot.
"""

import re
import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# ─── Config ───
BOT_TOKEN = "8150644814:AAEFF7axPiIOxNMaYTqfandfi7a9jAQ9z_k"
CHAT_ID = "7625761638"
DATA_URL = "https://infinitumcom.github.io/phonesinventory/index.html"
LOCAL_PATH = "index.html"

PST = timezone(timedelta(hours=-7))  # PDT (summer)


def fetch_html():
    """Try local file first, then remote."""
    import os
    if os.path.exists(LOCAL_PATH):
        with open(LOCAL_PATH, "r", encoding="utf-8") as f:
            return f.read()
    req = urllib.request.Request(DATA_URL, headers={"User-Agent": "PhoneInventoryBot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def extract_js_array(html, var_name):
    """Extract a JS array/object assigned to var_name."""
    pattern = rf"const\s+{var_name}\s*=\s*(\[[\s\S]*?\n\]);"
    m = re.search(pattern, html)
    if not m:
        pattern = rf"const\s+{var_name}\s*=\s*(\{{[\s\S]*?\n\}});"
        m = re.search(pattern, html)
    if not m:
        return None
    raw = m.group(1)
    # Convert JS to JSON-ish: replace single quotes, strip trailing commas, handle unquoted keys
    raw = re.sub(r"//.*?$", "", raw, flags=re.MULTILINE)  # remove comments
    raw = re.sub(r"/\*.*?\*/", "", raw, flags=re.DOTALL)  # remove block comments
    raw = re.sub(r"'", '"', raw)  # single → double quotes
    raw = re.sub(r"(\w+)\s*:", r'"\1":', raw)  # unquoted keys
    raw = re.sub(r",\s*([}\]])", r"\1", raw)  # trailing commas
    raw = re.sub(r'""(\w+)""', r'"\1"', raw)  # fix double-quoted keys
    # Fix already-quoted keys that got double-quoted
    raw = re.sub(r'""+', '"', raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def extract_store_data(html):
    """Extract STORE_DATA as dict."""
    pattern = r"const\s+STORE_DATA\s*=\s*\{([\s\S]*?)\n\};"
    m = re.search(pattern, html)
    if not m:
        return {}
    raw = "{" + m.group(1) + "}"
    raw = re.sub(r"//.*?$", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"'", '"', raw)
    raw = re.sub(r"(\w[\w-]*)\s*:", r'"\1":', raw)
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    raw = re.sub(r'""+', '"', raw)
    # Fix keys with hyphens
    raw = re.sub(r'"(\w+)-(\w+)":', r'"\1-\2":', raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def parse_phones_manual(html):
    """Fallback: extract phone data using regex per-entry."""
    phones = []
    pattern = r"\{\s*cond\s*:\s*['\"](\w+)['\"].*?brand\s*:\s*['\"](\w+)['\"].*?name\s*:\s*['\"]([^'\"]+)['\"].*?storage\s*:\s*['\"]([^'\"]+)['\"].*?store\s*:\s*['\"]([^'\"]+)['\"].*?storeName\s*:\s*['\"]([^'\"]+)['\"].*?price\s*:\s*(\d+).*?cost\s*:\s*(\d+).*?status\s*:\s*['\"](\w+)['\"].*?region\s*:\s*['\"](\w+)['\"].*?ageDays\s*:\s*(\d+)"
    for m in re.finditer(pattern, html, re.DOTALL):
        phones.append({
            "cond": m.group(1),
            "brand": m.group(2),
            "name": m.group(3),
            "storage": m.group(4),
            "store": m.group(5),
            "storeName": m.group(6),
            "price": int(m.group(7)),
            "cost": int(m.group(8)),
            "status": m.group(9),
            "region": m.group(10),
            "ageDays": int(m.group(11)),
        })
    return phones


def parse_stores_manual(html):
    """Fallback: extract store data using regex."""
    stores = {}
    block_pattern = r"['\"]([a-z][\w-]*)['\"]:\s*\{([^}]+)\}"
    for m in re.finditer(block_pattern, html[html.find("STORE_DATA"):html.find("STORE_DATA") + 5000]):
        key = m.group(1)
        block = m.group(2)

        def g(field, is_num=False):
            p = rf"{field}\s*:\s*['\"]?([^'\",\n]+)['\"]?"
            mm = re.search(p, block)
            if not mm:
                return 0 if is_num else ""
            v = mm.group(1).strip()
            if is_num:
                return int(float(v)) if v.replace(".", "").isdigit() else 0
            return v

        stores[key] = {
            "nameEn": g("nameEn"), "nameZh": g("nameZh"),
            "mgr": g("mgr"), "phone": g("phone"),
            "stock": g("stock", True), "stockNew": g("stockNew", True), "stockUsed": g("stockUsed", True),
            "todaySold": g("todaySold", True), "todayRev": g("todayRev", True),
            "mtdSold": g("mtdSold", True), "mtdRank": g("mtdRank", True),
            "cash": g("cash", True), "taxRateLabel": g("taxRateLabel"),
        }
    return stores


def build_report(phones, stores):
    """Build formatted Telegram report message."""
    now = datetime.now(PST)
    date_str = now.strftime("%Y-%m-%d %A")

    # ── Inventory Summary ──
    total = len(phones)
    new_phones = [p for p in phones if p.get("cond") == "new"]
    used_phones = [p for p in phones if p.get("cond") == "used"]
    available = [p for p in phones if p.get("status") == "available"]
    sold = [p for p in phones if p.get("status") == "sold"]
    reserved = [p for p in phones if p.get("status") == "reserved"]
    transit = [p for p in phones if p.get("status") == "transit"]
    aged = [p for p in phones if p.get("ageDays", 0) >= 60]

    total_value = sum(p.get("price", 0) for p in available)
    total_cost = sum(p.get("cost", 0) for p in available)

    # Brand breakdown
    brands = {}
    for p in phones:
        b = p.get("brand", "other")
        brands[b] = brands.get(b, 0) + 1
    brand_line = " · ".join(f"{k.title()} {v}" for k, v in sorted(brands.items(), key=lambda x: -x[1]))

    # Region breakdown
    regions = {}
    for p in available:
        r = p.get("region", "us")
        flag = {"us": "🇺🇸", "hk": "🇭🇰", "cn": "🇨🇳"}.get(r, "🌐")
        regions[flag + r.upper()] = regions.get(flag + r.upper(), 0) + 1
    region_line = " · ".join(f"{k} {v}" for k, v in regions.items())

    # ── Sales Summary ──
    total_sold_today = sum(s.get("todaySold", 0) for s in stores.values())
    total_rev_today = sum(s.get("todayRev", 0) for s in stores.values())
    total_mtd = sum(s.get("mtdSold", 0) for s in stores.values())

    # ── Store Details ──
    total_cash = sum(s.get("cash", 0) for s in stores.values())

    store_lines = []
    sorted_stores = sorted(stores.items(), key=lambda x: x[1].get("mtdRank", 99))
    for key, s in sorted_stores:
        name = s.get("nameEn") or s.get("nameZh") or key
        rank_medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(s.get("mtdRank", 0), "  ")
        store_lines.append(
            f"{rank_medal} *{name}*\n"
            f"    📦 库存 {s.get('stock', 0)} (新{s.get('stockNew', 0)}/二手{s.get('stockUsed', 0)})\n"
            f"    💰 今日 {s.get('todaySold', 0)}台 ${s.get('todayRev', 0):,}\n"
            f"    📅 本月 {s.get('mtdSold', 0)}台 · 排名 #{s.get('mtdRank', '-')}\n"
            f"    💵 现金 ${s.get('cash', 0):,} · {s.get('taxRateLabel', '')}"
        )

    # ── Sold Transactions Today ──
    sold_lines = []
    for p in sold:
        sold_lines.append(
            f"  • {p.get('name', '?')} {p.get('storage', '')} → {p.get('storeName', '?')} · ${p.get('price', 0):,}"
        )

    # ── Aged Stock Warning ──
    aged_lines = []
    for p in aged[:5]:
        aged_lines.append(
            f"  ⚠️ {p.get('name', '?')} {p.get('storage', '')} · {p.get('storeName', '?')} · {p.get('ageDays', 0)}天"
        )

    # ── Build Message ──
    msg = f"""📊 *iFixForU 手机库存日报*
━━━━━━━━━━━━━━━━━━
📅 {date_str}

📦 *一、库存总览*
┌ 总库存: *{total}* 部
│ 新机 {len(new_phones)} · 二手 {len(used_phones)}
│ 在售 {len(available)} · 已售 {len(sold)} · 预留 {len(reserved)} · 在途 {len(transit)}
│ 品牌: {brand_line}
│ 版本: {region_line}
│ 库存总值: *${total_value:,}* (成本 ${total_cost:,})
└ 滞销 (>60天): {len(aged)} 部

💰 *二、今日销售*
┌ 今日成交: *{total_sold_today}* 台
│ 今日营收: *${total_rev_today:,}*
└ 本月累计: *{total_mtd}* 台"""

    if sold_lines:
        msg += "\n\n📝 *交易明细:*\n" + "\n".join(sold_lines)

    msg += f"""

🏪 *三、门店详情*
{'─' * 20}
""" + "\n\n".join(store_lines)

    msg += f"""

💵 *四、现金汇总*
┌ 全部门店现金合计: *${total_cash:,}*
└ 共 {len(stores)} 家门店"""

    if aged_lines:
        msg += f"""

⚠️ *五、滞销预警*
""" + "\n".join(aged_lines)

    msg += f"""

━━━━━━━━━━━━━━━━━━
🤖 PhoneInventory Bot · {now.strftime('%H:%M PST')}
🌐 https://infinitumcom.github.io/phonesinventory/"""

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
                print(f"✅ Report sent to chat {chat_id}")
            else:
                print(f"❌ Telegram error: {result}")
            return result
    except Exception as e:
        print(f"❌ Failed to send: {e}")
        return None


def main():
    print("📊 PhoneInventory Daily Report")
    print(f"⏰ {datetime.now(PST).strftime('%Y-%m-%d %H:%M PST')}")

    # Fetch and parse
    html = fetch_html()
    print(f"📄 HTML loaded: {len(html):,} chars")

    phones = parse_phones_manual(html)
    stores = parse_stores_manual(html)
    print(f"📱 Phones: {len(phones)} | 🏪 Stores: {len(stores)}")

    if not phones:
        print("❌ No phone data found")
        return
    if not stores:
        print("⚠️ No store data found, proceeding with phone data only")

    # Build and send
    report = build_report(phones, stores)
    print(f"\n{'='*40}\n{report}\n{'='*40}\n")
    send_telegram(report)


if __name__ == "__main__":
    main()
