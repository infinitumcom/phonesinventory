#!/bin/bash
# Daily SQLite hot backup (WAL-safe via .backup), keep 14 days.
DEPLOY_DIR="/opt/phonesinventory"
DB="$DEPLOY_DIR/data/inventory.db"
BACKUP_DIR="$DEPLOY_DIR/backups"
STAMP=$(date +%F)
OUT="$BACKUP_DIR/inventory-$STAMP.db"

mkdir -p "$BACKUP_DIR"

if [ ! -f "$DB" ]; then
    echo "$(date) Backup skipped: $DB not found"
    exit 1
fi

do_backup() {
    if command -v sqlite3 > /dev/null 2>&1; then
        sqlite3 "$DB" ".backup '$OUT'"
    else
        # Fallback: python3 stdlib sqlite3 has the same WAL-safe backup API
        python3 - "$DB" "$OUT" <<'PYEOF'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PYEOF
    fi
}

if do_backup; then
    gzip -f "$OUT"
    echo "$(date) Backup OK: $OUT.gz ($(du -h "$OUT.gz" | cut -f1))"
else
    rm -f "$OUT"
    echo "$(date) Backup FAILED for $DB"
    exit 1
fi

# Retention: 14 days
find "$BACKUP_DIR" -name 'inventory-*.db.gz' -mtime +14 -delete
