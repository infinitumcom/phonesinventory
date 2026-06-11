#!/usr/bin/env python3
"""
PhoneInventory 自动化测试 — 部署前验证
在 start_bot.sh 启动服务前自动运行，任何测试失败则阻止启动。
"""
import sys
import os
import re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from inventory_bot import detect_region_from_model

errors = []

def test(name, result, expected):
    if result != expected:
        errors.append(f"FAIL {name}: got {result!r}, expected {expected!r}")
        return False
    return True

# ─── 1. Region Detection ───
print("1. Region Detection...")
test("US old format (MG184LL/A)", detect_region_from_model("MG184LL/A"), "us")
test("US new format (MFXL4LL/A)", detect_region_from_model("MFXL4LL/A"), "us")
test("US new format (MG4E4LL/A)", detect_region_from_model("MG4E4LL/A"), "us")
test("HK ZA (MG8M4ZA/A)", detect_region_from_model("MG8M4ZA/A"), "hk")
test("HK ZP (MG8M4ZP/A)", detect_region_from_model("MG8M4ZP/A"), "hk")
test("CN (MU6X3CH/A)", detect_region_from_model("MU6X3CH/A"), "cn")
test("Empty model", detect_region_from_model(""), None)
test("No suffix", detect_region_from_model("iPhone 17"), None)

# ─── 2. IMEI Validation Logic ───
print("2. IMEI Validation...")

def validate_imei(raw):
    """Simulate bot's IMEI validation logic."""
    imei = raw.strip()
    imei = ''.join(c for c in imei if c.isdigit())
    if imei and imei.startswith("8904"):
        imei = ""
    if imei and (len(imei) < 14 or len(imei) > 16):
        if len(imei) > 16:
            candidate = imei[-15:]
            if candidate[:2] in ("35", "86", "01", "00"):
                imei = candidate
            else:
                imei = ""
        else:
            imei = ""
    return imei

test("Valid IMEI 15 digits", validate_imei("354110235968962"), "354110235968962")
test("EID rejected (8904 prefix)", validate_imei("89044058270002653520230505123456"), "")
test("Too short IMEI", validate_imei("12345"), "")
test("IMEI with spaces", validate_imei("35 411 023 596 8962"), "354110235968962")
test("Empty IMEI", validate_imei(""), "")

# ─── 3. Database Schema Check ───
print("3. Database Schema...")
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "inventory.db")
if os.path.exists(DB_PATH):
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Check required columns exist
    c.execute("PRAGMA table_info(inventory)")
    cols = {row[1] for row in c.fetchall()}
    for required in ["id", "imei", "model", "region", "store", "status", "raw_ocr"]:
        test(f"Column '{required}' exists", required in cols, True)
    # Check user_pins table
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_pins'")
    test("user_pins table exists", c.fetchone() is not None, True)
    conn.close()
else:
    print("  (skipped — no database file, first deploy)")

# ─── 4. Config Validation ───
print("4. Config Validation...")
from inventory_bot import BOT_TOKEN, MODEL_REGION_MAP
test("BOT_TOKEN not empty", bool(BOT_TOKEN), True)
test("MODEL_REGION_MAP has LL", "LL" in MODEL_REGION_MAP, True)
test("MODEL_REGION_MAP has ZA", "ZA" in MODEL_REGION_MAP, True)
test("MODEL_REGION_MAP has CH", "CH" in MODEL_REGION_MAP, True)

# ─── Result ───
print("=" * 50)
if errors:
    print(f"FAILED: {len(errors)} test(s) failed:")
    for e in errors:
        print(f"  {e}")
    sys.exit(1)
else:
    print("ALL TESTS PASSED")
    sys.exit(0)
