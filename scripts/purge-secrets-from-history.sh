#!/usr/bin/env bash
#
# Remove committed credentials and database backups from this repository's git
# history.
#
# READ THIS FIRST
# ---------------
# This script is the *second* step, not the first. Anything in that history was
# pushed to a public repository, so anyone who cloned or scraped the repo may
# already have it. Rewriting history does not un-publish it and does not
# invalidate it.
#
#   1. Rotate anything that was exposed. Do this first - until it is done, this
#      script buys nothing.
#   2. Then run this script so the values cannot be re-discovered from history.
#
# What it removes:
#   * every `.env` variant that was ever committed (root and bot/)
#   * `backups/*.db` - these hold the full trading record and account balances
#
# What it deliberately does NOT remove:
#   * `.env.example` / `.env.example` templates. Those are meant to be public
#     and hold no values.
#
# Usage:
#   scripts/purge-secrets-from-history.sh --dry-run
#   scripts/purge-secrets-from-history.sh --apply
#
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

MODE="${1:-}"
if [[ "$MODE" != "--dry-run" && "$MODE" != "--apply" ]]; then
  echo "usage: $0 [--dry-run|--apply]" >&2
  exit 2
fi

# Paths to drop. `--path` in git-filter-repo is an exact path match, so each
# variant has to be listed. Patterns are passed with --path-glob instead.
PATHS=(
  ".env"
  "bot/.env"
  "backend/.env"
  "backend/bot/.env"
  "bot/.env.local"
  ".env.local"
)
GLOBS=("backups/*.db" "backups/*.db-shm" "backups/*.db-wal")
PROTECT=("*.env.example" ".env.example" "bot/.env.example")

echo "Repository: $REPO_ROOT"
echo "Mode:       ${MODE#--}"
echo

# --- Preconditions ---------------------------------------------------------
if [[ -n "$(git status --porcelain)" ]]; then
  echo "ERROR: working tree is dirty. Commit or stash first." >&2
  git status --short >&2
  exit 1
fi

echo "Checking which targets actually appear in history..."
present_paths=()
for p in "${PATHS[@]}"; do
  if [[ -n "$(git log --oneline --all -- "$p" 2>/dev/null)" ]]; then
    present_paths+=("$p")
    printf '  found  %s\n' "$p"
  fi
done
present_globs=()
for g in "${GLOBS[@]}"; do
  if [[ -n "$(git log --oneline --all -- "$g" 2>/dev/null)" ]]; then
    present_globs+=("$g")
    printf '  found  %s\n' "$g"
  fi
done

if [[ ${#present_paths[@]} -eq 0 && ${#present_globs[@]} -eq 0 ]]; then
  echo "Nothing to do: none of the targets appear in history."
  exit 0
fi
echo

# --- Safety net ------------------------------------------------------------
BACKUP_DIR="$REPO_ROOT/../belaku-mirror-backup-$(date +%Y%m%d-%H%M%S)"
echo "Creating a full mirror backup at: $BACKUP_DIR"
git clone --mirror "$REPO_ROOT" "$BACKUP_DIR" >/dev/null 2>&1
echo "  done. Restore with: git clone $BACKUP_DIR belaku-restored"
echo

if [[ "$MODE" == "--dry-run" ]]; then
  cat <<EOF
DRY RUN - nothing was changed.

To apply, re-run with --apply. That will:
  1. rewrite every commit to drop the paths above
  2. expire reflogs and garbage-collect

Afterwards you must force-push, and every collaborator must re-clone:
  git push --force --all
  git push --force --tags

EOF
  exit 0
fi

if ! command -v git-filter-repo >/dev/null 2>&1 && [[ ! -x "$REPO_ROOT/.venv/bin/git-filter-repo" ]]; then
  cat >&2 <<'EOF'
ERROR: git-filter-repo is not installed.

  brew install git-filter-repo
  # or: .venv/bin/pip install git-filter-repo

git-filter-repo is preferred over `git filter-branch`, which is deprecated,
orders of magnitude slower, and easy to get subtly wrong.
EOF
  exit 1
fi
FR="$(command -v git-filter-repo || echo "$REPO_ROOT/.venv/bin/git-filter-repo")"

# --- Rewrite ---------------------------------------------------------------
args=(--force --invert-paths)
for p in "${present_paths[@]}"; do args+=(--path "$p"); done
for g in "${present_globs[@]}"; do args+=(--path-glob "$g"); done
# Belt and braces: even if a template matched, keep it.
for p in "${PROTECT[@]}"; do args+=(--path-glob "$p"); done

echo "Rewriting history..."
"$FR" "${args[@]}"

echo
echo "Expiring reflogs and garbage-collecting..."
git reflog expire --expire=now --all
git gc --prune=now

echo
echo "Verifying the targets are gone from all reachable history:"
failed=0
for p in "${present_paths[@]}"; do
  if [[ -n "$(git log --oneline --all -- "$p" 2>/dev/null)" ]]; then
    echo "  STILL PRESENT: $p" >&2
    failed=1
  fi
done
for g in "${present_globs[@]}"; do
  if [[ -n "$(git log --oneline --all -- "$g" 2>/dev/null)" ]]; then
    echo "  STILL PRESENT: $g" >&2
    failed=1
  fi
done
[[ "$failed" -eq 0 ]] || exit 1
echo "  clean."

echo
echo "Templates still present (expected):"
for p in "${PROTECT[@]}"; do
  git ls-files --error-unmatch "$p" >/dev/null 2>&1 && echo "  kept  $p"
done

cat <<EOF

Done. Remaining steps, in order:

  1. Force-push the rewritten history:
       git push --force --all
       git push --force --tags

  2. Ask GitHub Support to purge cached views of the old commits. Force-pushing
     does not remove them from GitHub's servers; the old objects stay reachable
     by SHA until support runs a garbage collection.

  3. Treat the old credentials as permanently compromised. The rewrite does not
     help against anyone who already cloned the repository.

  4. Confirm the targets are ignored going forward:
       git check-ignore -v bot/.env backups/anything.db

EOF
