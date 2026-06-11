#!/usr/bin/env python3
"""
Export inventory data from SQLite to phones.js for the web frontend.
Run periodically via cron to keep the website in sync with bot uploads.

Usage: python3 scripts/export_inventory.py
"""
import sqlite3
import json
import os

DEPLOY_DIR = os.environ.get("DEPLOY_DIR", "/opt/phonesinventory")
DB_PATH = os.path.join(DEPLOY_DIR, "data", "inventory.db")
OUTPUT_PATH = os.path.join(DEPLOY_DIR, "data", "phones.js")

# Brand detection from model name
def detect_brand(brand_raw, model):
    b = (brand_raw or "").lower()
    m = (model or "").lower()
    if "apple" in b or "iphone" in m or "ipad" in m:
        return "apple"
    if "samsung" in b or "galaxy" in m:
        return "samsung"
    if "google" in b or "pixel" in m:
        return "google"
    return "other"

# Color hex mapping
COLOR_HEX = {
    "black": "#1d1d1f", "黑色": "#1d1d1f", "宇宙黑": "#1d1d1f", "cosmic black": "#1d1d1f",
    "白色": "#f5f5f0", "white": "#f5f5f0",
    "金色": "#f4e3c1", "gold": "#f4e3c1",
    "蓝色": "#5b7fb5", "blue": "#5b7fb5", "深蓝色": "#3a5a8c", "dark blue": "#3a5a8c",
    "紫色": "#b8a9d4", "purple": "#b8a9d4",
    "粉色": "#f5c6c6", "pink": "#f5c6c6",
    "红色": "#c94040", "red": "#c94040",
    "绿色": "#4a8c6f", "green": "#4a8c6f",
    "银色": "#d1d1d1", "silver": "#d1d1d1",
    "灰色": "#8a8a8a", "gray": "#8a8a8a", "grey": "#8a8a8a",
    "原色": "#e8dcc8", "natural": "#e8dcc8",
    "钛色": "#9a9a95", "titanium": "#9a9a95",
    "沙漠色": "#c8b88a", "desert": "#c8b88a", "desert titanium": "#c8b88a",
    "cosmic orange": "#e87040", "宇宙橙": "#e87040",
    "ultramarine": "#4169e1", "群青": "#4169e1",
    "teal": "#008080", "青色": "#008080",
}

# Store key → display name (canonical names used in inventory)
STORE_NAMES = {
    "alhambra": "Alhambra",
    "monterey park": "Monterey Park", "monterey": "Monterey Park",
    "san gabriel": "San Gabriel",
    "rowland heights": "Rowland Heights", "rowland": "Rowland Heights",
    "arcadia 1": "Arcadia 1", "arcadia 1 (huntington)": "Arcadia 1",
    "arcadia 2": "Arcadia 2", "arcadia 2 (baldwin)": "Arcadia 2",
    "irvine": "Irvine", "irvine (99 ranch)": "Irvine",
    "rancho cucamonga": "Rancho Cucamonga", "rancho cucamonga (99 ranch)": "Rancho Cucamonga",
    "las vegas": "Las Vegas", "vegas": "Las Vegas",
    "hq 总仓": "HQ 总仓", "hq warehouse": "HQ 总仓", "warehouse": "HQ 总仓",
}

# Region flag
REGION_MAP = {"us": "us", "cn": "cn", "hk": "hk", "jp": "jp", "kr": "kr", "eu": "eu"}


def get_color_hex(color, color_en):
    for c in [color, color_en]:
        if c:
            cl = c.lower().strip()
            for key, hex_val in COLOR_HEX.items():
                if key in cl:
                    return hex_val
    return "#8a8a8a"


def get_store_name(store):
    if not store:
        return ""
    sl = store.lower().strip()
    for key, name in sorted(STORE_NAMES.items(), key=lambda x: -len(x[0])):
        if key in sl:
            return name
    return store


def export():
    if not os.path.exists(DB_PATH):
        print(f"Database not found: {DB_PATH}")
        # Write empty array
        os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
        with open(OUTPUT_PATH, "w") as f:
            f.write("const phones = [];\n")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM inventory ORDER BY id DESC").fetchall()

    phones = []
    for row in rows:
        brand = detect_brand(row["brand"], row["model"])
        color_hex = get_color_hex(row["color"], row["color_en"])
        store_name = get_store_name(row["store"])
        region = REGION_MAP.get((row["region"] or "us").lower(), "us")
        cond = "new" if (row["condition"] or "").lower() in ("new", "新机", "全新") else "used"

        phone = {
            "id": row["id"],
            "name": row["model"] or "Unknown",
            "brand": brand,
            "storage": row["storage"] or "",
            "color": row["color"] or "",
            "colorEn": row["color_en"] or row["color"] or "",
            "colorHex": color_hex,
            "cond": cond,
            "condition": "全新" if cond == "new" else "二手",
            "conditionEn": "Brand New" if cond == "new" else "Pre-owned",
            "imei": row["imei"] or "",
            "serial": row["serial"] or "",
            "battery": row["battery_health"] or "",
            "region": region,
            "store": row["store"] or "",
            "storeName": store_name,
            "shelf": "",
            "purchaseDate": (row["scanned_at"] or "")[:10],
            "supplier": row["scanned_by"] or "",
            "supplierEn": row["scanned_by"] or "",
            "cost": row["cost"] or 0,
            "price": row["price"] or 0,
            "status": row["status"] or "available",
            "notes": row["notes"] or "",
        }
        phones.append(phone)

    conn.close()

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write("const phones = ")
        json.dump(phones, f, ensure_ascii=False, indent=1)
        f.write(";\n")

    print(f"Exported {len(phones)} phones to {OUTPUT_PATH}")


if __name__ == "__main__":
    export()
