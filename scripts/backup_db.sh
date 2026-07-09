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

if sqlite3 "$DB" ".backup '$OUT'"; then
    gzip -f "$OUT"
    echo "$(date) Backup OK: $OUT.gz ($(du -h "$OUT.gz" | cut -f1))"
else
    rm -f "$OUT"
    echo "$(date) Backup FAILED for $DB"
    exit 1
fi

# Retention: 14 days
find "$BACKUP_DIR" -name 'inventory-*.db.gz' -mtime +14 -delete
