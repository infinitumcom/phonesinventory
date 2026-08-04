#!/usr/bin/env python3
"""
One-off data migration: recover corrupted customer fields in the `sales` table.

Root cause (fixed in completeSale() 2026-08-03): the sale form read customer
name/phone by positional .form-input index, but the IMEI scan input is also a
.form-input and comes first, shifting every field by one. Historical result:

    customer        <- IMEI            (redundant, discard; imei column is correct)
    customer_phone  <- typed name      (the real customer name)
    customer_email  <- typed phone     (the real customer phone)

This script shifts each affected row back:

    new customer        = old customer_phone            (real name)
    new customer_phone  = old customer_email  if phone-like (>=7 digits) else ''
    new customer_email  = ''                             (real email was never captured)

Idempotency guard: only rows where `customer` == `imei` (the buggy signature).
After a successful run customer holds a name, so re-running is a no-op.

Usage:
    python3 scripts/backfill_customer_fields.py            # dry-run (default)
    python3 scripts/backfill_customer_fields.py --apply    # backup + write
"""
import os
import re
import sys
import shutil
import sqlite3
from datetime import datetime

DEPLOY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DEPLOY_DIR, "data", "inventory.db")

DIGITS = re.compile(r"\D")


def phone_like(v):
    return len(DIGITS.sub("", v or "")) >= 7


def main():
    apply = "--apply" in sys.argv
    if not os.path.exists(DB_PATH):
        print("DB not found:", DB_PATH)
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, imei, customer, customer_phone, customer_email FROM sales"
    ).fetchall()

    affected = [r for r in rows if (r["customer"] or "").strip() == (r["imei"] or "").strip()
                and (r["customer"] or "").strip()]
    print(f"total sales rows      : {len(rows)}")
    print(f"affected (customer==imei): {len(affected)}")
    if not affected:
        print("nothing to do — data already clean.")
        conn.close()
        return

    updates = []
    for r in affected:
        new_name = (r["customer_phone"] or "").strip()
        old_email = (r["customer_email"] or "").strip()
        new_phone = old_email if phone_like(old_email) else ""
        new_email = ""
        updates.append((r["id"], new_name, new_phone, new_email))

    recovered_names = sum(1 for u in updates if u[1])
    recovered_phones = sum(1 for u in updates if u[2])
    print(f"  will recover names : {recovered_names}/{len(updates)}")
    print(f"  will recover phones: {recovered_phones}/{len(updates)}")
    print("\n--- preview (first 12) ---")
    for u in updates[:12]:
        print(f'  {u[0]}: name="{u[1]}"  phone="{u[2]}"  email="{u[3]}"')

    if not apply:
        print("\nDRY-RUN. Re-run with --apply to back up and write.")
        conn.close()
        return

    # Backup before writing
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = f"{DB_PATH}.bak-custfix-{stamp}"
    shutil.copy2(DB_PATH, bak)
    print(f"\nbackup written: {bak}")

    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        for sid, name, phone, email in updates:
            cur.execute(
                "UPDATE sales SET customer=?, customer_phone=?, customer_email=? WHERE id=?",
                (name, phone, email, sid),
            )
        conn.commit()
        print(f"committed: {len(updates)} rows updated.")
    except Exception as e:
        conn.rollback()
        print("ROLLED BACK due to error:", e)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
