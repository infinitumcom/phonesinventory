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
import threading
from datetime import datetime, timezone, timedelta

# ─── Config ───
BOT_TOKEN = "8682943904:AAHUj5DPOa6wdknmrNut4zJr2dZ1UTDTwLE"
# Admin user IDs (can use bot anywhere, manage settings)
ADMIN_IDS = os.environ.get("ADMIN_IDS", "7625761638").split(",")
# Allowed group chat IDs — anyone in these groups can upload
# Group IDs are negative numbers, e.g. -1001234567890
ALLOWED_GROUP_IDS = [g.strip() for g in os.environ.get("ALLOWED_GROUP_IDS", "").split(",") if g.strip()]
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

# ─── Session Context (per chat) ───
# Remembers what the user said, applies to subsequent photos
chat_context = {}  # chat_id -> { condition, region, store, notes, updated_at }

def parse_context(text):
    """Parse natural language to extract session context."""
    ctx = {}
    t = text.lower().strip()

    # Condition: new / used
    if any(w in t for w in ["新机", "新手机", "全新", "sealed", "new", "未拆封", "新的", "brand new", "bnib"]):
        ctx["condition"] = "new"
    elif any(w in t for w in ["二手", "旧机", "used", "回收", "trade-in", "翻新", "二手机", "旧的", "pre-owned", "refurbished"]):
        ctx["condition"] = "used"

    # Region
    if any(w in t for w in ["港版", "香港", "hk版", "hk", "hong kong", "双卡", "港行"]):
        ctx["region"] = "hk"
    elif any(w in t for w in ["国行", "国版", "cn版", "cn", "大陆", "中国", "china", "国产", "大陆版"]):
        ctx["region"] = "cn"
    elif any(w in t for w in ["美版", "us版", "us", "美国", "american", "美行"]):
        ctx["region"] = "us"
    elif any(w in t for w in ["日版", "jp版", "jp", "日本", "japan"]):
        ctx["region"] = "jp"
    elif any(w in t for w in ["韩版", "kr版", "kr", "韩国", "korea"]):
        ctx["region"] = "kr"

    # Store — match longer keys first to avoid partial matches
    for key, name in sorted(STORE_LIST.items(), key=lambda x: -len(x[0])):
        if key in t:
            ctx["store"] = name
            break

    return ctx


def get_context_summary(chat_id):
    """Get human-readable summary of current context."""
    ctx = chat_context.get(str(chat_id), {})
    if not ctx:
        return ""
    parts = []
    if ctx.get("condition"):
        parts.append("🆕 新机" if ctx["condition"] == "new" else "♻️ 二手")
    if ctx.get("region"):
        flags = {"us": "🇺🇸 美版", "hk": "🇭🇰 港版", "cn": "🇨🇳 国行"}
        parts.append(flags.get(ctx["region"], ctx["region"]))
    if ctx.get("store"):
        parts.append("📍 " + ctx["store"])
    return " · ".join(parts)


# Apple model number → region mapping
MODEL_REGION_MAP = {
    "LL": "us", "LL/A": "us",  # US
    "ZA": "hk", "ZA/A": "hk", "ZP": "hk", "ZP/A": "hk",  # Hong Kong
    "CH": "cn", "CH/A": "cn",  # China
    "JP": "us", "JP/A": "us",  # Japan (treat as intl)
    "KH": "us", "KH/A": "us",  # Korea
    "B": "us", "B/A": "us",    # UK/EU
}

def detect_region_from_model(model_number):
    """Detect region from Apple model number suffix like MG184LL/A."""
    if not model_number:
        return None
    import re
    m = re.search(r'[A-Z]\d{3,4}([A-Z]{1,2})/([A-Z])', model_number.upper())
    if m:
        suffix = m.group(1) + "/" + m.group(2)
        return MODEL_REGION_MAP.get(suffix, MODEL_REGION_MAP.get(m.group(1)))
    return None


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

    phone_schema = """{
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
}"""

    prompt = f"""Analyze this phone label/box photo and extract all device information.
{condition_hint}
{f"User note: {caption}" if caption else ""}

If there are MULTIPLE phones/labels in the photo, return a JSON ARRAY of objects.
If there is only ONE phone, return a single JSON object.

Each phone object should have these fields (use empty string if not found):
{phone_schema}

IMPORTANT:
- Read ALL text carefully including tiny print and barcodes with numbers below them
- IMEI is labeled "IMEI/MEID" or "IMEI" on the box — it is exactly 15 digits, usually starting with 35 or 86
- IMEI2 is labeled "IMEI2" — also 15 digits
- EID is labeled "EID" — it is 32 digits starting with 8904, this is the eSIM identifier, NOT an IMEI. Put it in the "eid" field, NEVER in "imei"
- Do NOT confuse EID with IMEI — they are completely different numbers
- For Apple: LL/A = US, ZA/A or ZP/A = HK, CH/A = CN
- If you see multiple labels/stickers/boxes, each one is a SEPARATE phone — return an array
- Only return the JSON, no other text"""

    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 4096,
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
            # Try to find JSON array first, then single object
            json_match = json_match.strip()
            arr_start = json_match.find("[")
            obj_start = json_match.find("{")
            # If array comes first (or only array), parse as array
            if arr_start >= 0 and (obj_start < 0 or arr_start < obj_start):
                arr_end = json_match.rfind("]") + 1
                if arr_end > arr_start:
                    entries = json.loads(json_match[arr_start:arr_end])
                    if isinstance(entries, list) and len(entries) > 0:
                        return entries, text
            # Single object
            if obj_start >= 0:
                obj_end = json_match.rfind("}") + 1
                if obj_end > obj_start:
                    entry = json.loads(json_match[obj_start:obj_end])
                    return [entry], text  # Always return as list
            return None, text
    except Exception as e:
        return None, f"Claude API error: {e}"


# ─── Message Handlers ───
def handle_photo(msg):
    """Process a photo message — extract phone info via Claude Vision."""
    chat_id = msg["chat"]["id"]
    cid = str(chat_id)
    from_user = msg.get("from", {})
    username = from_user.get("first_name", "")
    if from_user.get("last_name"):
        username += " " + from_user["last_name"]
    if not username.strip():
        username = from_user.get("username", "Unknown")
    username = username.strip()
    caption = msg.get("caption", "")
    msg_id = msg["message_id"]

    # If caption has context info, parse it into session
    if caption:
        cap_ctx = parse_context(caption)
        if cap_ctx:
            ctx = chat_context.get(cid, {})
            ctx.update(cap_ctx)
            chat_context[cid] = ctx

    # Get current session context
    ctx = chat_context.get(cid, {})

    # Get largest photo
    photos = msg.get("photo", [])
    if not photos:
        send_msg(chat_id, "❌ 无法获取照片 / Could not get photo", reply_to=msg_id)
        return
    file_id = photos[-1]["file_id"]

    # Acknowledge with context info
    ctx_summary = get_context_summary(chat_id)
    ack_text = "📸 收到照片，正在识别中...\n_Analyzing photo..._"
    if ctx_summary:
        ack_text += f"\n📋 当前设定: {ctx_summary}"
    send_msg(chat_id, ack_text, reply_to=msg_id)

    # Download
    image_bytes = download_photo(file_id)
    if not image_bytes:
        send_msg(chat_id, "❌ 下载照片失败 / Failed to download photo", reply_to=msg_id)
        return

    # Build context hint for Claude Vision
    context_hint = caption or ""
    if ctx.get("condition"):
        context_hint += f"\nUser specified condition: {ctx['condition']}"
    if ctx.get("region"):
        context_hint += f"\nUser specified region: {ctx['region']}"
    if ctx.get("store"):
        context_hint += f"\nUser specified store: {ctx['store']}"

    # Analyze with Claude Vision
    entries, raw = analyze_photo(image_bytes, context_hint)
    if not entries:
        send_msg(chat_id, f"❌ 识别失败 / Recognition failed\n\n`{raw[:500]}`", reply_to=msg_id)
        return

    saved = []
    skipped = []

    for entry in entries:
        # Apply session context overrides (user's word takes priority)
        if ctx.get("condition") and not caption:
            entry["condition"] = ctx["condition"]
        if ctx.get("store"):
            entry["store"] = ctx["store"]

        # Region: user context > caption > model number detection > Claude guess
        if ctx.get("region"):
            entry["region"] = ctx["region"]
        elif not entry.get("region") or entry["region"] == "us":
            detected = detect_region_from_model(entry.get("model_number", ""))
            if detected:
                entry["region"] = detected

        # Parse store from caption if not set by context
        if not entry.get("store") and caption:
            for key, name in sorted(STORE_LIST.items(), key=lambda x: -len(x[0])):
                if key in caption.lower():
                    entry["store"] = name
                    break

        # IMEI validation — must be 14-16 digits, reject ICCID (SIM card numbers starting with 8904)
        imei = entry.get("imei", "").strip()
        # Strip non-digit characters
        imei = ''.join(c for c in imei if c.isdigit())
        if imei and imei.startswith("8904"):
            # This is an ICCID (SIM card number), not an IMEI — try to extract IMEI from it
            # ICCID is typically 19-20 digits; if longer, IMEI may be concatenated
            imei = ""  # discard, OCR mixed up SIM card number
        if imei and (len(imei) < 14 or len(imei) > 16):
            # Invalid length — skip or truncate
            if len(imei) > 16:
                # Might be ICCID+IMEI concatenated, try last 15 digits
                candidate = imei[-15:]
                if candidate[:2] in ("35", "86", "01", "00"):
                    imei = candidate
                else:
                    imei = ""  # discard invalid
            else:
                imei = ""  # too short
        entry["imei"] = imei
        if imei and len(imei) >= 14:
            conn = sqlite3.connect(DB_PATH)
            dup = conn.execute("SELECT id, model, scanned_at FROM inventory WHERE imei = ?", (imei,)).fetchone()
            conn.close()
            if dup:
                skipped.append({"entry": entry, "dup_id": dup[0], "dup_model": dup[1], "dup_time": dup[2]})
                continue

        # Reject entries without valid IMEI
        if not imei:
            skipped.append({"entry": entry, "dup_id": None, "dup_model": None, "dup_time": None, "reason": "no_imei"})
            continue

        # Save to DB
        rid = save_entry(entry, raw_ocr=raw, scanned_by=username)
        saved.append({"entry": entry, "id": rid})

    # Format response
    if not saved and not skipped:
        send_msg(chat_id, "❌ 未能识别到手机信息", reply_to=msg_id)
        return

    reply_parts = []

    for item in saved:
        entry = item["entry"]
        rid = item["id"]
        cond_emoji = "🆕" if entry.get("condition") == "new" else "♻️"
        cond_label = "新机" if entry.get("condition") == "new" else "二手"
        region_flag = {"us": "🇺🇸", "hk": "🇭🇰", "cn": "🇨🇳", "jp": "🇯🇵", "kr": "🇰🇷"}.get(entry.get("region", ""), "🌐")

        part = f"""✅ *入库成功 #{rid}*
{cond_emoji} *{entry.get('brand', '?')} {entry.get('model', '?')}*
┌ 容量: *{entry.get('storage', '?')}*
│ 颜色: {entry.get('color', '')} {entry.get('color_en', '')}
│ IMEI: `{entry.get('imei', '?')}`"""
        if entry.get("imei2"):
            part += f"\n│ IMEI2: `{entry['imei2']}`"
        if entry.get("serial"):
            part += f"\n│ 序列号: `{entry['serial']}`"
        if entry.get("battery"):
            part += f"\n│ 电池: {entry['battery']}"
        part += f"\n│ {cond_label} · {region_flag} {entry.get('region', 'US').upper()}"
        part += f"\n└ 📍 {entry.get('store', '待分配')}"
        reply_parts.append(part)

    for item in skipped:
        entry = item["entry"]
        if item.get("reason") == "no_imei":
            reply_parts.append(
                f"❌ *IMEI 识别失败* — {entry.get('brand', '?')} {entry.get('model', '?')}\n"
                f"未能从照片中识别有效 IMEI，请重新拍摄清晰的 IMEI 条码照片"
            )
        else:
            reply_parts.append(
                f"⚠️ *重复跳过* — {entry.get('brand', '?')} {entry.get('model', '?')}\n"
                f"IMEI: `{entry.get('imei', '?')}`\n"
                f"已在 #{item['dup_id']} 录入 ({item['dup_time']})"
            )

    summary = f"📋 操作人: {username} · {datetime.now(PST).strftime('%Y-%m-%d %H:%M PST')}"
    if len(saved) > 1:
        summary = f"📦 共识别 {len(saved) + len(skipped)} 台，入库 {len(saved)} 台\n" + summary

    reply = "\n━━━━━━━━━━━━━━━━━━\n".join(reply_parts)
    reply += f"\n\n{summary}"

    send_msg(chat_id, reply, reply_to=msg_id)


def handle_command(msg, is_group=False):
    """Handle bot commands and natural language context."""
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()
    msg_id = msg["message_id"]

    # In groups, ignore messages that aren't commands or context keywords
    # This prevents the bot from replying to every chat message
    if is_group and not text.startswith("/"):
        # Only try to parse as context, respond only if context was detected
        ctx = parse_context(text)
        if ctx:
            cid = str(chat_id)
            existing = chat_context.get(cid, {})
            existing.update(ctx)
            chat_context[cid] = existing
            summary = get_context_summary(chat_id)
            send_msg(chat_id, f"✅ *已设定:* {summary}\n\n后续发送的照片将自动应用此设定。", reply_to=msg_id)
        # If no context detected, silently ignore (don't spam the group)
        return

    if text == "/start":
        send_msg(chat_id, """👋 *PhoneInventory Upload Bot*

📸 发送手机标签/包装盒照片即可自动入库

💬 *自然语言设定（发照片前说一句）:*
• 「这批是新机」→ 后续全部按新机入库
• 「港版二手」→ 港版 + 二手
• 「这些入Alhambra」→ 分配到 Alhambra 店
• 「美版新机 入HQ」→ 美版 + 新机 + 总仓

*命令:*
/context — 查看当前设定
/clear — 清除设定
/list — 最近入库记录
/stats — 库存统计
/addgroup — 授权当前群组 (管理员)
/help — 帮助""", reply_to=msg_id)

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

    elif text == "/addgroup":
        # Admin-only: authorize current group
        from_id = str(msg.get("from", {}).get("id", ""))
        if from_id not in ADMIN_IDS:
            send_msg(chat_id, "⛔ 仅管理员可执行此命令", reply_to=msg_id)
            return
        chat_type = msg["chat"].get("type", "private")
        if chat_type not in ("group", "supergroup"):
            send_msg(chat_id, "❌ 此命令只能在群组中使用", reply_to=msg_id)
            return
        cid = str(chat_id)
        if cid not in ALLOWED_GROUP_IDS:
            ALLOWED_GROUP_IDS.append(cid)
        group_name = msg["chat"].get("title", "Unknown")
        send_msg(chat_id, f"✅ *群组已授权*\n\n群名: {group_name}\nGroup ID: `{cid}`\n\n现在群内所有成员都可以发送照片入库。\n\n⚠️ 重启后需重新授权，或将此 ID 加入 ALLOWED\\_GROUP\\_IDS 环境变量：\n`ALLOWED_GROUP_IDS={cid}`", reply_to=msg_id)

    elif text == "/removegroup":
        # Admin-only: revoke current group
        from_id = str(msg.get("from", {}).get("id", ""))
        if from_id not in ADMIN_IDS:
            send_msg(chat_id, "⛔ 仅管理员可执行此命令", reply_to=msg_id)
            return
        cid = str(chat_id)
        if cid in ALLOWED_GROUP_IDS:
            ALLOWED_GROUP_IDS.remove(cid)
        send_msg(chat_id, "🚫 已取消此群授权", reply_to=msg_id)

    elif text == "/clear":
        chat_context.pop(str(chat_id), None)
        send_msg(chat_id, "🔄 已清除当前设定 / Context cleared", reply_to=msg_id)

    elif text == "/context":
        summary = get_context_summary(chat_id)
        if summary:
            send_msg(chat_id, f"📋 *当前设定:* {summary}\n\n输入 /clear 可清除", reply_to=msg_id)
        else:
            send_msg(chat_id, "📋 当前无设定。\n发送文字设定批次信息，例如:\n「这批是港版新机 入Alhambra」", reply_to=msg_id)

    else:
        # Natural language → try to parse as context
        ctx = parse_context(text)
        if ctx:
            cid = str(chat_id)
            existing = chat_context.get(cid, {})
            existing.update(ctx)
            chat_context[cid] = existing
            summary = get_context_summary(chat_id)
            send_msg(chat_id, f"✅ *已设定:* {summary}\n\n后续发送的照片将自动应用此设定。\n发送 /clear 可清除。", reply_to=msg_id)
        else:
            send_msg(chat_id, "💡 发送手机照片即可入库\n\n或发送文字设定批次:\n「这批是新机」「港版二手」「入ALH」\n\n/help 查看更多帮助", reply_to=msg_id)


# ─── Main Loop ───
def main():
    print(f"🤖 PhoneInventory Bot starting...")
    print(f"📂 Deploy dir: {DEPLOY_DIR}")
    print(f"💾 Database: {DB_PATH}")
    print(f"🔑 Anthropic API: {'SET' if ANTHROPIC_API_KEY else '⚠️ NOT SET'}")
    print(f"👤 Admin IDs: {ADMIN_IDS}")
    print(f"👥 Allowed groups: {ALLOWED_GROUP_IDS or 'none (add via /addgroup in a group)'}")

    init_db()

    if not ANTHROPIC_API_KEY:
        print("⚠️  ANTHROPIC_API_KEY not set! Photo recognition will not work.")
        print("   Set it: export ANTHROPIC_API_KEY=sk-ant-...")

    # Note: Bot must have Privacy Mode DISABLED via @BotFather → /setprivacy → Disable
    # Otherwise bot won't receive photos in groups
    print("⚠️  确保已在 @BotFather 中关闭 Privacy Mode: /setprivacy → Disable")

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
                chat_type = msg["chat"].get("type", "private")
                from_id = str(msg.get("from", {}).get("id", ""))

                # Access control:
                # - Private chat: must be admin
                # - Group/supergroup: group ID must be in ALLOWED_GROUP_IDS, OR sender must be admin
                if chat_type == "private":
                    if from_id not in ADMIN_IDS:
                        send_msg(chat_id, "⛔ 未授权 / Unauthorized\nYour ID: `" + from_id + "`")
                        continue
                elif chat_type in ("group", "supergroup"):
                    if chat_id not in ALLOWED_GROUP_IDS and from_id not in ADMIN_IDS:
                        # First time seeing this group? Tell admin the group ID so they can whitelist it
                        group_name = msg["chat"].get("title", "Unknown Group")
                        send_msg(chat_id, f"⛔ 此群未授权 / Group not authorized\n\n群名: {group_name}\nGroup ID: `{chat_id}`\n\n请管理员将此 ID 加入 ALLOWED\\_GROUP\\_IDS 环境变量")
                        continue

                # Photo → inventory scan (threaded for parallel processing)
                if "photo" in msg:
                    t = threading.Thread(target=handle_photo, args=(msg,), daemon=True)
                    t.start()
                # Text commands
                elif "text" in msg:
                    handle_command(msg, is_group=(chat_type in ("group", "supergroup")))

        except KeyboardInterrupt:
            print("\n🛑 Bot stopped.")
            break
        except Exception as e:
            print(f"❌ Error: {e}")
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    main()
