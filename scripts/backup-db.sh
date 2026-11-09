#!/usr/bin/env bash
#
# Back up the bot's SQLite database.
#
# The database holds the whole trading record: positions, orders, trades, the
# equity curve and journal notes. It lives in a Docker volume, so losing the
# volume loses all of it, and there is no other copy.
#
# Uses SQLite's own `.backup` rather than `cp`. Copying a live SQLite file can
# capture a torn write, and with WAL enabled the `-wal` sidecar holds committed
# transactions that a plain copy of the main file would miss. `.backup` takes a
# consistent snapshot of a database that is being written to.
#
# Usage:
#   scripts/backup-db.sh                    # writes to ./backups
#   BACKUP_DIR=/mnt/backups scripts/backup-db.sh
#   KEEP_DAYS=30 scripts/backup-db.sh
#
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

BACKUP_DIR="${BACKUP_DIR:-$REPO_ROOT/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
VOLUME="${VOLUME:-belaku_bot-data}"
DB_NAME="${DB_NAME:-paper_trading.db}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_NAME="belaku-$STAMP.db"

mkdir -p "$BACKUP_DIR"

echo "Backing up $DB_NAME from volume $VOLUME"
echo "  -> $BACKUP_DIR/$OUT_NAME"

# The Python is fed on stdin rather than as a nested heredoc inside `sh -c`,
# which would need two levels of quoting and silently mangles the script.
# Running in a throwaway container means the snapshot uses the same SQLite build
# as the bot and does not require the bot to be stopped.
#
# The volume is mounted read-write, not :ro. The database is in WAL mode, and
# SQLite must create the `-shm` shared-memory file to open it at all - a
# read-only mount fails with "unable to open database file" before any query
# runs. The script itself only reads; `src.backup(dst)` never writes to /data.
docker run --rm -i \
  -e "DB_NAME=$DB_NAME" \
  -e "OUT_NAME=$OUT_NAME" \
  -v "$VOLUME:/data" \
  -v "$BACKUP_DIR:/backup" \
  --entrypoint python \
  python:3.12-slim - <<'PY'
import os
import sqlite3

db_name = os.environ["DB_NAME"]
out_name = os.environ["OUT_NAME"]

src = sqlite3.connect(f"file:/data/{db_name}?mode=ro", uri=True)
dst = sqlite3.connect(f"/backup/{out_name}")
with dst:
    src.backup(dst)
dst.close()
src.close()
print("  snapshot written")
PY

# Verify the snapshot is a usable database before trusting it. A backup that
# cannot be opened is worse than no backup, because it is believed.
echo "Verifying the snapshot..."
python3 - "$BACKUP_DIR/$OUT_NAME" <<'PY'
import sqlite3
import sys

path = sys.argv[1]
conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
if integrity != "ok":
    print(f"  FAILED integrity check: {integrity}", file=sys.stderr)
    sys.exit(1)

tables = {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'"
)}
counts = {
    table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    for table in ("positions", "orders", "trades")
    if table in tables
}
conn.close()
print("  integrity: ok")
print(f"  rows: {counts}")
PY

# Prune old snapshots.
if [[ "$KEEP_DAYS" -gt 0 ]]; then
  echo "Pruning snapshots older than $KEEP_DAYS days..."
  find "$BACKUP_DIR" -name 'belaku-*.db' -type f -mtime "+$KEEP_DAYS" -print -delete \
    | sed 's/^/  removed /' || true
fi

echo
echo "Done. $(find "$BACKUP_DIR" -name 'belaku-*.db' -type f | wc -l | tr -d ' ') snapshot(s) in $BACKUP_DIR"
echo
echo "Schedule it with cron, e.g. after the close:"
echo "  30 16 * * 1-5 cd $REPO_ROOT && ./scripts/backup-db.sh >> $BACKUP_DIR/backup.log 2>&1"
echo
echo "NOTE: this writes to the same machine. Copy $BACKUP_DIR off-box, or the"
echo "backup dies with the host."
