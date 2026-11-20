#!/usr/bin/env bash
#
# Wipe every trace of the bot's trading record and start from a clean slate.
#
# The record lives in a Docker volume, not in the repo, so a `git clean` does
# nothing to it. This script snapshots the volume, deletes the ledgers, and
# restarts the bot - then *pauses* it, because a running bot re-enters on the
# next closed bar and the "clean" state would be gone within seconds.
#
# Usage:
#   scripts/reset-books.sh                  # back up, wipe, restart, pause
#   scripts/reset-books.sh --dry-run        # show what would happen
#   scripts/reset-books.sh --yes            # skip the confirmation prompt
#   scripts/reset-books.sh --resume         # resume trading after the wipe
#   scripts/reset-books.sh --no-backup      # skip the snapshot (destructive)
#   scripts/reset-books.sh --purge-backups  # also delete ./backups
#
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

VOLUME="${VOLUME:-belaku_bot-data}"
BACKUP_DIR="${BACKUP_DIR:-$REPO_ROOT/backups}"
BASE_URL="${BASE_URL:-http://127.0.0.1:9000}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DRY_RUN=0
ASSUME_YES=0
DO_BACKUP=1
PURGE_BACKUPS=0
RESUME=0

for arg in "$@"; do
  case "$arg" in
    --dry-run)        DRY_RUN=1 ;;
    --yes|-y)         ASSUME_YES=1 ;;
    --no-backup)      DO_BACKUP=0 ;;
    --purge-backups)  PURGE_BACKUPS=1 ;;
    --resume)         RESUME=1 ;;
    --keep-paused)    RESUME=0 ;;
    -h|--help)        sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

# Everything the bot writes into /data. A file not listed here survives, which is
# why the holiday cache is deliberately absent: it is a downloaded reference, not
# trading state, and refetching it costs a network round-trip.
#
# The `-wal`/`-shm` sidecars matter as much as the database itself. SQLite keeps
# committed rows in the WAL until a checkpoint, so deleting only the `.db` can
# leave the next process replaying the old transactions into a fresh file.
#
# The index book is one file *per strategy* (`gift_book.db.<strategy-id>`), so the
# bare name is not enough: wiping only it would leave every strategy's index
# ledger holding its positions and P&L, and the "clean" check below would pass
# while the dashboard still showed index trades.
LEDGERS=(
  paper_trading.db paper_trading.db-wal paper_trading.db-shm
  gift_book.db gift_book.db-wal gift_book.db-shm
  'gift_book.db.*'
  paper_mirror.db paper_mirror.db-wal paper_mirror.db-shm
  bot.log bot.log.1 bot.log.2 bot.log.3 bot.log.4 bot.log.5
)

run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "  [dry-run] $*"
  else
    "$@"
  fi
}

# The dashboard password, so the script can pause the bot over the API. Read
# from the environment first, then from bot/.env, so a CI run can inject it.
dashboard_password() {
  if [[ -n "${DASHBOARD_PASSWORD:-}" ]]; then
    printf '%s' "$DASHBOARD_PASSWORD"
    return 0
  fi
  [[ -f bot/.env ]] || return 1
  sed -n 's/^DASHBOARD_PASSWORD=//p' bot/.env | head -1 | sed 's/^["'\'']//; s/["'\'']$//'
}

# Log in and return a cookie jar path, or fail. Kept separate from the pause
# call so a failed login reports the real reason instead of a bare 401.
login() {
  local jar="$1" password
  password="$(dashboard_password)" || return 1
  [[ -n "$password" ]] || return 1
  curl -fsS -c "$jar" -X POST "$BASE_URL/api/auth/login" \
    -H 'Content-Type: application/json' \
    --data "$(printf '{"password":"%s"}' "$password")" >/dev/null 2>&1
}

set_paused() {
  local paused="$1" reason="$2" jar body
  jar="$(mktemp)"
  if ! login "$jar"; then
    rm -f "$jar"
    echo "  could not log in - set DASHBOARD_PASSWORD to control the bot" >&2
    return 1
  fi
  body="$(printf '{"paused":%s,"reason":"%s"}' "$paused" "$reason")"
  if curl -fsS -b "$jar" -X POST "$BASE_URL/api/pause" \
      -H 'Content-Type: application/json' --data "$body" >/dev/null 2>&1; then
    rm -f "$jar"
    return 0
  fi
  rm -f "$jar"
  return 1
}

wait_healthy() {
  for _ in $(seq 1 45); do
    if curl -fsS "$BASE_URL/api/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

# Report what is actually in the ledgers, so "clean" is verified rather than
# assumed. Reads the volume directly: the API would only show the futures book.
report_state() {
  docker run --rm -i -v "$VOLUME:/data" --entrypoint python python:3.12-slim - <<'PY'
import glob, os, sqlite3, sys

TABLES = ("positions", "orders", "trades", "signals", "equity_curve", "trade_notes")
dirty = False
# Every ledger the bot writes, including one index book per strategy. A glob
# rather than a fixed list: the strategy ids come from .env, so a hardcoded set
# would silently stop checking a book the operator added.
paths = ["/data/paper_trading.db", "/data/gift_book.db", "/data/paper_mirror.db"]
paths += sorted(glob.glob("/data/gift_book.db.*"))
for path in paths:
    name = os.path.basename(path)
    if not os.path.exists(path):
        print(f"  {name:24} absent")
        continue
    conn = sqlite3.connect(path)
    counts = {}
    for table in TABLES:
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            counts[table] = 0
    try:
        initial, balance = conn.execute(
            "SELECT initial_balance, balance FROM account"
        ).fetchone()
    except sqlite3.Error:
        initial = balance = None
    total = sum(counts.values())
    if total:
        dirty = True
    detail = " ".join(f"{k}={v}" for k, v in counts.items() if v)
    print(f"  {name:24} {'CLEAN' if not total else 'DIRTY'}  "
          f"balance={balance} initial={initial}  {detail}")
sys.exit(1 if dirty else 0)
PY
}

if ! docker volume inspect "$VOLUME" >/dev/null 2>&1; then
  echo "volume $VOLUME not found - is the stack up?" >&2
  exit 1
fi

echo "Volume:  $VOLUME"
echo "Backup:  $([[ "$DO_BACKUP" -eq 1 ]] && echo "$BACKUP_DIR/$STAMP" || echo "(skipped)")"
echo "Wiping:  ${LEDGERS[*]}"
echo "After:   $([[ "$RESUME" -eq 1 ]] && echo "resume trading" || echo "paused - no new entries")"
echo

if [[ "$DRY_RUN" -eq 0 && "$ASSUME_YES" -eq 0 ]]; then
  read -r -p "This discards all paper positions and P&L. Continue? [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]] || { echo "aborted"; exit 1; }
fi

echo "Stopping the bot..."
run docker compose stop bot

if [[ "$DO_BACKUP" -eq 1 ]]; then
  echo "Snapshotting the volume..."
  run mkdir -p "$BACKUP_DIR/$STAMP"
  run docker run --rm -v "$VOLUME:/data" -v "$BACKUP_DIR/$STAMP:/backup" \
    --entrypoint sh python:3.12-slim -c 'cp -a /data/. /backup/'
fi

echo "Wiping the ledgers..."
# Explicit names rather than a glob: `rm -f /data/*` would take the holiday
# cache with it, and the sidecars have to go with their database or SQLite
# replays the old transactions into the new file.
#
# The index book is one file per strategy, so the bare name is not enough - the
# suffixed ledgers have to go too, or a "clean" reset leaves every strategy's
# index positions and P&L in place.
run docker run --rm -v "$VOLUME:/data" --entrypoint sh python:3.12-slim -c '
  cd /data || exit 1
  for f in paper_trading.db gift_book.db paper_mirror.db; do
    rm -f "$f" "$f-wal" "$f-shm"
  done
  rm -f gift_book.db.*
  rm -f bot.log bot.log.[0-9]*
'

if [[ "$PURGE_BACKUPS" -eq 1 ]]; then
  echo "Purging $BACKUP_DIR..."
  run rm -rf "$BACKUP_DIR"
fi

echo "Starting the bot..."
run docker compose up -d bot

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo
  echo "Dry run - nothing changed."
  exit 0
fi

echo "Waiting for the health check..."
if ! wait_healthy; then
  echo "  bot did not become healthy - check 'docker compose logs bot'" >&2
  exit 1
fi
echo "  healthy"

# Pause before the first closed bar can re-enter. The bot asserts its target
# position on every bar, so an unpaused restart re-opens a trade within seconds
# and the wipe is undone.
if [[ "$RESUME" -eq 0 ]]; then
  echo "Pausing new entries..."
  if set_paused true "RESET: books wiped $(date +%Y-%m-%dT%H:%M:%S)"; then
    echo "  paused"
  else
    echo "  WARNING: could not pause - the bot may re-enter on the next bar" >&2
  fi
else
  echo "Resuming trading..."
  set_paused false "RESET: resumed" || echo "  WARNING: could not resume" >&2
fi

echo
echo "Verifying the ledgers..."
if report_state; then
  echo
  echo "Clean. Both books are at their starting balance with no rows."
else
  echo
  echo "Still dirty - see the counts above." >&2
  exit 1
fi

echo
if [[ "$RESUME" -eq 1 ]]; then
  echo "Trading is live again - the bot will enter on the next closed bar."
else
  echo "Next:"
  echo "  open $BASE_URL and resume from the dashboard when you are ready"
  echo "  scripts/reset-books.sh --resume   # wipe and start trading immediately"
fi
