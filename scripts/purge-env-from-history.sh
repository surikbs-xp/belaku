#!/usr/bin/env bash
#
# Remove bot/.env from this repository's git history.
#
# READ THIS FIRST
# ---------------
# This script is the *second* step, not the first. The credentials in that file
# were pushed to a public repository, so they must be treated as compromised:
# anyone who cloned or scraped the repo may already have them. Rewriting history
# does not un-publish them and does not invalidate them.
#
#   1. Rotate KITE_API_SECRET (and the access token) in the Kite developer
#      console. Do this first. Until it is done, this script buys nothing.
#   2. Then run this script to stop the values being re-discovered from history.
#
# The script is deliberately interactive and refuses to run on a dirty tree,
# because `git filter-repo` rewrites every commit and a mistake here is
# expensive to undo.
#
# Usage:
#   scripts/purge-env-from-history.sh --dry-run
#   scripts/purge-env-from-history.sh --apply
#
set -euo pipefail

TARGET="bot/.env"
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

MODE="${1:-}"
if [[ "$MODE" != "--dry-run" && "$MODE" != "--apply" ]]; then
  echo "usage: $0 [--dry-run|--apply]" >&2
  exit 2
fi

echo "Repository: $REPO_ROOT"
echo "Target:     $TARGET"
echo

# --- Preconditions ---------------------------------------------------------
if [[ -n "$(git status --porcelain)" ]]; then
  echo "ERROR: working tree is dirty. Commit or stash first." >&2
  git status --short >&2
  exit 1
fi

if [[ -z "$(git log --oneline --all -- "$TARGET")" ]]; then
  echo "Nothing to do: $TARGET does not appear in history."
  exit 0
fi

echo "Commits that touch $TARGET:"
git log --oneline --all -- "$TARGET" | sed 's/^/  /'
echo

if ! command -v git-filter-repo >/dev/null 2>&1; then
  cat >&2 <<'EOF'
ERROR: git-filter-repo is not installed.

  brew install git-filter-repo
  # or: pipx install git-filter-repo

git-filter-repo is preferred over `git filter-branch`, which is deprecated,
orders of magnitude slower, and easy to get subtly wrong.
EOF
  exit 1
fi

# --- Safety net ------------------------------------------------------------
BACKUP_DIR="$REPO_ROOT/../belaku-backup-$(date +%Y%m%d-%H%M%S)"
echo "Creating a full backup at: $BACKUP_DIR"
git clone --mirror "$REPO_ROOT" "$BACKUP_DIR" >/dev/null 2>&1
echo "  done. Restore with: git clone $BACKUP_DIR belaku-restored"
echo

if [[ "$MODE" == "--dry-run" ]]; then
  cat <<EOF
DRY RUN - nothing was changed.

To apply, re-run with --apply. That will:
  1. rewrite every commit to drop $TARGET
  2. drop the now-empty commits
  3. expire reflogs and garbage-collect

Afterwards you must force-push, and every collaborator must re-clone:
  git push --force --all
  git push --force --tags

EOF
  exit 0
fi

# --- Rewrite ---------------------------------------------------------------
echo "Rewriting history..."
git filter-repo --force --invert-paths --path "$TARGET"

echo
echo "Expiring reflogs and garbage-collecting..."
git reflog expire --expire=now --all
git gc --prune=now --aggressive

echo
echo "Verifying $TARGET is gone from all reachable history:"
if [[ -n "$(git log --oneline --all -- "$TARGET")" ]]; then
  echo "  STILL PRESENT - the rewrite did not fully succeed." >&2
  exit 1
fi
echo "  clean."

cat <<EOF

Done. Remaining steps, in order:

  1. Force-push the rewritten history:
       git push --force --all
       git push --force --tags

  2. Ask GitHub Support to purge cached views of the old commits. Force-pushing
     does not remove them from GitHub's servers; the old objects stay reachable
     by SHA until support runs a garbage collection.

  3. Treat the old credentials as permanently compromised. If you have not
     already rotated KITE_API_SECRET, do it now - the rewrite does not help
     against anyone who already cloned the repository.

  4. Confirm the file is ignored going forward:
       git check-ignore -v bot/.env

EOF
