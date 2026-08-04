#!/usr/bin/env python3
"""
One-off cleanup: label anonymous walk-in sales.

After backfill_customer_fields.py recovered real names/phones, 40 historical rows
still had no usable customer info — staff had typed "1", left it blank, or wrote
"customer". These are anonymous walk-in sales, not identifiable customers. Left
as-is they collapse into a bogus single "1 · 24 orders" mega-customer in the
Find-existing-customer lookup.

This normalises them to the marker "散客" (Walk-in), so the sales table reads
sensibly and the client can exclude them from the customer directory.

Target rows: customer_phone has < 7 digits AND customer in {'1', '', 'customer'}.
Idempotent: once relabelled to "散客" the row no longer matches.

Usage:
    python3 scripts/mark_walkin_customers.py            # dry-run (default)
    python3 scripts/mark_walkin_customers.py --apply    # backup + write
"""
import os
import re
import sys
import shutil
import sqlite3
from datetime import datetime

DEPLOY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DEPLOY_DIR, "data", "inventory.db")
WALKIN = "散客"
JUNK_NAMES = {"1", "", "customer"}


def phoneless(v):
    return len(re.sub(r"\D", "", v or "")) < 7


def main():
    apply = "--apply" in sys.argv
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, customer, customer_phone FROM sales"
    ).fetchall()

    targets = [r for r in rows
               if phoneless(r["customer_phone"]) and (r["customer"] or "").strip() in JUNK_NAMES]
    print(f"total sales rows : {len(rows)}")
    print(f"walk-in targets  : {len(targets)}")
    if not targets:
        print("nothing to do — already normalised.")
        conn.close()
        return
    for r in targets[:12]:
        print(f'  {r["id"]}: "{r["customer"]}" -> "{WALKIN}"')

    if not apply:
        print("\nDRY-RUN. Re-run with --apply to back up and write.")
        conn.close()
        return

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = f"{DB_PATH}.bak-walkin-{stamp}"
    shutil.copy2(DB_PATH, bak)
    print(f"\nbackup written: {bak}")

    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        for r in targets:
            cur.execute("UPDATE sales SET customer=? WHERE id=?", (WALKIN, r["id"]))
        conn.commit()
        print(f"committed: {len(targets)} rows relabelled to '{WALKIN}'.")
    except Exception as e:
        conn.rollback()
        print("ROLLED BACK:", e)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
