#!/usr/bin/env python3
"""
PhoneInventory Telegram Bot — 拍照入库
Staff sends a photo of phone label/box → Claude Vision extracts info → auto inventory entry.

Usage:
  1. Send photo with optional caption: "二手" / "新机" / store name
  2. Bot extracts: model, storage, color, IMEI, serial, battery, condition
  3. Returns structured entry, saves to SQLite
  4. /list — view recent entries
  5. /report — trigger daily report
  6. /export — export inventory JSON
"""

import os
import sys
import json
import time
import sqlite3
import base64
import urllib.request
import urllib.parse
import traceback
from datetime import datetime, timezone, timedelta

# ─── Config ───
BOT_TOKEN = "8150644814:AAEFF7axPiIOxNMaYTqfandfi7a9jAQ9z_k"
ALLOWED_CHAT_IDS = os.environ.get("ALLOWED_CHAT_IDS", "7625761638").split(",")
# Supports private chats AND group chats — add group chat IDs to .env
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

DEPLOY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DEPLOY_DIR, "data", "inventory.db")
PST = timezone(timedelta(hours=-7))

STORE_LIST = {
    "alh": "Alhambra", "alhambra": "Alhambra",
    "mp": "Monterey Park", "monterey": "Monterey Park", "monterey park": "Monterey Park",
    "sg": "San Gabriel", "san gabriel": "San Gabriel",
    "rh": "Rowland Heights", "rowland": "Rowland Heights", "rowland heights": "Rowland Heights",
    "ar1": "Arcadia 1", "arcadia 1": "Arcadia 1", "arcadia1": "Arcadia 1", "huntington": "Arcadia 1",
    "ar2": "Arcadia 2", "arcadia 2": "Arcadia 2", "arcadia2": "Arcadia 2", "baldwin": "Arcadia 2",
    "irv": "Irvine", "irvine": "Irvine",
    "rc": "Rancho Cucamonga", "rancho": "Rancho Cucamonga", "rancho cucamonga": "Rancho Cucamonga",
    "lv": "Las Vegas", "vegas": "Las Vegas", "las vegas": "Las Vegas",
    "hq": "HQ 总仓", "总仓": "HQ 总仓", "仓库": "HQ 总仓", "warehouse": "HQ 总仓", "office": "HQ 总仓",
}

# ─── Database ───
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        imei TEXT,
        imei2 TEXT,
        serial TEXT,
        brand TEXT,
        model TEXT,
        storage TEXT,
        color TEXT,
        color_en TEXT,
        condition TEXT DEFAULT 'new',
        battery_health TEXT,
        region TEXT DEFAULT 'us',
        store TEXT,
        cost REAL DEFAULT 0,
        price REAL DEFAULT 0,
        status TEXT DEFAULT 'available',
        scanned_by TEXT,
        scanned_at TEXT,
        raw_ocr TEXT,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    return conn


def save_entry(entry, raw_ocr="", scanned_by=""):
    conn = sqlite3.connect(DB_PATH)
    now = datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""INSERT INTO inventory
        (imei, imei2, serial, brand, model, storage, color, color_en,
         condition, battery_health, region, store, cost, price, status,
         scanned_by, scanned_at, raw_ocr, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (entry.get("imei",""), entry.get("imei2",""), entry.get("serial",""),
         entry.get("brand",""), entry.get("model",""), entry.get("storage",""),
         entry.get("color",""), entry.get("color_en",""),
         entry.get("condition","new"), entry.get("battery",""),
         entry.get("region","us"), entry.get("store",""),
         entry.get("cost",0), entry.get("price",0), "available",
         scanned_by, now, raw_ocr, entry.get("notes","")))
    conn.commit()
    rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return rid


def get_recent_entries(limit=10):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM inventory ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_inventory_stats():
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
    available = conn.execute("SELECT COUNT(*) FROM inventory WHERE status='available'").fetchone()[0]
    new_count = conn.execute("SELECT COUNT(*) FROM inventory WHERE condition='new'").fetchone()[0]
    used_count = conn.execute("SELECT COUNT(*) FROM inventory WHERE condition='used'").fetchone()[0]
    today = datetime.now(PST).strftime("%Y-%m-%d")
    today_count = conn.execute("SELECT COUNT(*) FROM inventory WHERE scanned_at LIKE ?", (today+"%",)).fetchone()[0]
    conn.close()
    return {"total": total, "available": available, "new": new_count, "used": used_count, "today": today_count}


# ─── Telegram API ───
def tg_api(method, data=None, files=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    if data:
        encoded = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(url, data=encoded)
    else:
        req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"TG API error ({method}): {e}")
        return None


def send_msg(chat_id, text, parse_mode="Markdown", reply_to=None):
    data = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_to:
        data["reply_to_message_id"] = reply_to
    return tg_api("sendMessage", data)


def download_photo(file_id):
    """Download a photo from Telegram, return bytes."""
    result = tg_api("getFile", {"file_id": file_id})
    if not result or not result.get("ok"):
        return None
    file_path = result["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


# ─── Claude Vision API ───
def analyze_photo(image_bytes, caption=""):
    """Send photo to Claude Vision for OCR and classification."""
    if not ANTHROPIC_API_KEY:
        return None, "ANTHROPIC_API_KEY not set"

    b64 = base64.b64encode(image_bytes).decode("utf-8")

    # Detect media type
    media_type = "image/jpeg"
    if image_bytes[:4] == b'\x89PNG':
        media_type = "image/png"

    condition_hint = ""
    if caption:
        cl = caption.lower()
        if "二手" in cl or "used" in cl:
            condition_hint = "This is a USED / second-hand phone."
        elif "新" in cl or "new" in cl or "sealed" in cl:
            condition_hint = "This is a NEW / sealed phone."

    prompt = f"""Analyze this phone label/box photo and extract all device information.
{condition_hint}
{f"User note: {caption}" if caption else ""}

Return a JSON object with these fields (use empty string if not found):
{{
  "brand": "Apple/Samsung/Google/OnePlus/etc",
  "model": "full model name, e.g. iPhone 16 Pro Max",
  "storage": "e.g. 256GB",
  "color": "Chinese color name if visible, or translate",
  "color_en": "English color name",
  "imei": "primary IMEI (15 digits)",
  "imei2": "secondary IMEI if present",
  "serial": "serial number",
  "battery": "battery health % if shown",
  "condition": "new or used",
  "region": "us/hk/cn based on model number (LL=US, ZA/ZP=HK, CH=CN) or label language",
  "model_number": "e.g. MG184LL/A",
  "eid": "EID if present",
  "notes": "any other relevant info"
}}

IMPORTANT:
- Read ALL text carefully including tiny print and barcodes with numbers below them
- The barcode number IS the IMEI
- For Apple: LL/A = US, ZA/A or ZP/A = HK, CH/A = CN
- Only return the JSON, no other text"""

    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": prompt}
            ]
        }]
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01"
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            text = result.get("content", [{}])[0].get("text", "")
            # Extract JSON from response
            json_match = text
            if "```" in text:
                import re
                m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
                if m:
                    json_match = m.group(1)
            # Try to find JSON object
            start = json_match.find("{")
            end = json_match.rfind("}") + 1
            if start >= 0 and end > start:
                entry = json.loads(json_match[start:end])
                return entry, text
            return None, text
    except Exception as e:
        return None, f"Claude API error: {e}"


# ─── Message Handlers ───
def handle_photo(msg):
    """Process a photo message — extract phone info via Claude Vision."""
    chat_id = msg["chat"]["id"]
    from_user = msg.get("from", {})
    username = from_user.get("first_name", "Unknown")
    caption = msg.get("caption", "")
    msg_id = msg["message_id"]

    # Get largest photo
    photos = msg.get("photo", [])
    if not photos:
        send_msg(chat_id, "❌ 无法获取照片 / Could not get photo", reply_to=msg_id)
        return
    file_id = photos[-1]["file_id"]

    # Acknowledge
    send_msg(chat_id, "📸 收到照片，正在识别中...\n_Analyzing photo..._", reply_to=msg_id)

    # Download
    image_bytes = download_photo(file_id)
    if not image_bytes:
        send_msg(chat_id, "❌ 下载照片失败 / Failed to download photo", reply_to=msg_id)
        return

    # Analyze with Claude Vision
    entry, raw = analyze_photo(image_bytes, caption)
    if not entry:
        send_msg(chat_id, f"❌ 识别失败 / Recognition failed\n\n`{raw[:500]}`", reply_to=msg_id)
        return

    # Parse store from caption
    store = ""
    if caption:
        for key, name in STORE_LIST.items():
            if key in caption.lower():
                store = name
                break
    entry["store"] = store or entry.get("store", "")

    # Save to DB
    rid = save_entry(entry, raw_ocr=raw, scanned_by=username)

    # Format response
    cond_emoji = "🆕" if entry.get("condition") == "new" else "♻️"
    cond_label = "新机 NEW" if entry.get("condition") == "new" else "二手 USED"
    region_flag = {"us": "🇺🇸", "hk": "🇭🇰", "cn": "🇨🇳"}.get(entry.get("region", ""), "🌐")

    reply = f"""✅ *入库成功 · Stock In #{rid}*
━━━━━━━━━━━━━━━━━━

{cond_emoji} *{entry.get('brand', '?')} {entry.get('model', '?')}*
┌ 容量: *{entry.get('storage', '?')}*
│ 颜色: {entry.get('color', '')} {entry.get('color_en', '')}
│ IMEI: `{entry.get('imei', '?')}`"""

    if entry.get("imei2"):
        reply += f"\n│ IMEI2: `{entry['imei2']}`"
    if entry.get("serial"):
        reply += f"\n│ 序列号: `{entry['serial']}`"
    if entry.get("battery"):
        reply += f"\n│ 电池: {entry['battery']}"
    if entry.get("model_number"):
        reply += f"\n│ 型号: {entry['model_number']}"

    reply += f"""
│ 状态: {cond_label}
│ 版本: {region_flag} {entry.get('region', 'US').upper()}
└ 门店: {entry.get('store', '待分配')}

📋 操作人: {username}
🕐 {datetime.now(PST).strftime('%Y-%m-%d %H:%M PST')}"""

    if entry.get("notes"):
        reply += f"\n📝 {entry['notes']}"

    send_msg(chat_id, reply, reply_to=msg_id)


def handle_command(msg):
    """Handle bot commands."""
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()
    msg_id = msg["message_id"]

    if text == "/start":
        send_msg(chat_id, """👋 *PhoneInventory Bot*

📸 发送手机标签/包装盒照片即可自动入库
可在照片说明中注明: 新机/二手 + 门店名

*命令:*
/list — 最近入库记录
/stats — 库存统计
/report — 发送日报
/help — 帮助

*门店缩写:*
ALH=Alhambra · MP=Monterey Park
SG=San Gabriel · RH=Rowland Heights
AR1=Arcadia 1 · AR2=Arcadia 2
IRV=Irvine · RC=Rancho Cucamonga
LV=Las Vegas""", reply_to=msg_id)

    elif text == "/list":
        entries = get_recent_entries(8)
        if not entries:
            send_msg(chat_id, "📭 暂无入库记录 / No entries yet", reply_to=msg_id)
            return
        lines = ["📋 *最近入库 / Recent Entries*\n"]
        for e in entries:
            cond = "🆕" if e["condition"] == "new" else "♻️"
            lines.append(
                f"{cond} *#{e['id']}* {e['brand']} {e['model']} {e['storage']}\n"
                f"    IMEI: `{e['imei']}` · {e['store'] or '?'}\n"
                f"    {e['scanned_at'] or ''}"
            )
        send_msg(chat_id, "\n".join(lines), reply_to=msg_id)

    elif text == "/stats":
        stats = get_inventory_stats()
        send_msg(chat_id, f"""📊 *库存统计 / Inventory Stats*
━━━━━━━━━━━━━━━━━━
┌ 总入库: *{stats['total']}* 部
│ 在售: {stats['available']}
│ 新机: {stats['new']} · 二手: {stats['used']}
└ 今日入库: *{stats['today']}* 部""", reply_to=msg_id)

    elif text == "/report":
        send_msg(chat_id, "📊 正在生成日报...", reply_to=msg_id)
        try:
            report_script = os.path.join(DEPLOY_DIR, "scripts", "daily_report.py")
            os.system(f"python3 {report_script}")
            send_msg(chat_id, "✅ 日报已发送", reply_to=msg_id)
        except Exception as e:
            send_msg(chat_id, f"❌ 日报发送失败: {e}", reply_to=msg_id)

    elif text.startswith("/export"):
        entries = get_recent_entries(100)
        if not entries:
            send_msg(chat_id, "📭 暂无数据 / No data", reply_to=msg_id)
            return
        export = json.dumps(entries, ensure_ascii=False, indent=2)
        # Save to file and send
        export_path = os.path.join(DEPLOY_DIR, "data", "export.json")
        with open(export_path, "w", encoding="utf-8") as f:
            f.write(export)
        send_msg(chat_id, f"📦 导出 {len(entries)} 条记录到 `data/export.json`", reply_to=msg_id)

    elif text == "/help":
        send_msg(chat_id, """*📖 使用说明*

*入库方式:*
1️⃣ 拍摄手机标签或包装盒
2️⃣ 添加说明 (可选): `二手 ALH` = 二手机 Alhambra店
3️⃣ 发送给 Bot，自动识别入库

*照片要求:*
• 标签文字清晰可读
• 包含 IMEI 条形码
• 一张照片对应一台手机

*说明格式:*
`新机 AR1` — 新机，Arcadia 1 店
`二手 MP` — 二手机，Monterey Park 店
`used SG 成本480` — 二手，San Gabriel，成本$480""", reply_to=msg_id)

    else:
        send_msg(chat_id, "💡 发送手机照片即可入库，或输入 /help 查看帮助", reply_to=msg_id)


# ─── Main Loop ───
def main():
    print(f"🤖 PhoneInventory Bot starting...")
    print(f"📂 Deploy dir: {DEPLOY_DIR}")
    print(f"💾 Database: {DB_PATH}")
    print(f"🔑 Anthropic API: {'SET' if ANTHROPIC_API_KEY else '⚠️ NOT SET'}")

    init_db()

    if not ANTHROPIC_API_KEY:
        print("⚠️  ANTHROPIC_API_KEY not set! Photo recognition will not work.")
        print("   Set it: export ANTHROPIC_API_KEY=sk-ant-...")

    offset = 0
    print("✅ Bot is running, polling for messages...")

    while True:
        try:
            result = tg_api("getUpdates", {"offset": offset, "timeout": 30})
            if not result or not result.get("ok"):
                time.sleep(5)
                continue

            for update in result.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message")
                if not msg:
                    continue

                chat_id = str(msg["chat"]["id"])

                # Allow configured private chats and group chats
                if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
                    # In groups, also check if sender is allowed
                    from_id = str(msg.get("from", {}).get("id", ""))
                    if from_id not in ALLOWED_CHAT_IDS:
                        send_msg(chat_id, "⛔ 未授权 / Unauthorized\nChat ID: `" + chat_id + "`")
                        continue

                # Photo → inventory scan
                if "photo" in msg:
                    handle_photo(msg)
                # Text commands
                elif "text" in msg:
                    handle_command(msg)

        except KeyboardInterrupt:
            print("\n🛑 Bot stopped.")
            break
        except Exception as e:
            print(f"❌ Error: {e}")
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    main()
