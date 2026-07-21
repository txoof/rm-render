#!/usr/bin/env bash
# watch.sh - watch rmfakecloud .tree files and trigger rm-render on changes
#
# Usage: watch.sh <data_root>
#   data_root: rmfakecloud base directory containing data/ and rendered/
#
# Watches all .tree files under <data_root>/data/users/*/
# Waits SETTLE_SECONDS after the last change before running rm-render
# Output goes to stdout/stderr (captured by systemd)

set -euo pipefail

SETTLE_SECONDS=5

if [ $# -lt 1 ]; then
    echo "Usage: watch.sh <data_root>"
    echo "  data_root: rmfakecloud base directory containing data/ and rendered/"
    exit 1
fi

DATA_ROOT="${1%/}"
USERS_DIR="$DATA_ROOT/data/users"
RENDERED_DIR="$DATA_ROOT/rendered"

if [ ! -d "$USERS_DIR" ]; then
    echo "ERROR: users directory not found: $USERS_DIR"
    exit 1
fi

if [ ! -d "$RENDERED_DIR" ]; then
    echo "Creating rendered directory: $RENDERED_DIR"
    mkdir -p "$RENDERED_DIR"
fi

# Locate uv -- check PATH first, then common install locations
UV=$(command -v uv 2>/dev/null ||      ls /home/*/.local/bin/uv /root/.local/bin/uv /usr/local/bin/uv 2>/dev/null | head -1 ||      echo "")
if [ -z "$UV" ]; then
    echo "ERROR: uv not found in PATH or common install locations (~/.local/bin/uv)"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

VERSION=$(grep '^version' "$SCRIPT_DIR/pyproject.toml" 2>/dev/null | head -1 | sed 's/.*= *"//' | sed 's/"//' || echo "unknown")

echo "rm-render $VERSION watcher starting"
echo "  data root: $DATA_ROOT"
echo "  users dir: $USERS_DIR"
echo "  rendered:  $RENDERED_DIR"
echo "  settle:    ${SETTLE_SECONDS}s"
echo "  script:    $SCRIPT_DIR"

run_converter() {
    local user_dir="$1"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] running converter for $user_dir"
    "$UV" run --project "$SCRIPT_DIR" \
        python3 "$SCRIPT_DIR/src/rm_render/convert.py" \
        "$user_dir" \
        "$RENDERED_DIR"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] converter finished"
}

# Run once on startup to catch any changes that happened while we were down
for user_dir in "$USERS_DIR"/*/; do
    [ -f "$user_dir/sync/root" ] && run_converter "$user_dir"
done

# Watch for changes with settling delay
# inotifywait fires on each event; we use a background timer approach:
# on each event, kill any pending timer and start a new one
PENDING_PID=""
PENDING_USER=""

handle_change() {
    local user_dir="$1"

    # Kill existing pending timer if any
    if [ -n "$PENDING_PID" ] && kill -0 "$PENDING_PID" 2>/dev/null; then
        kill "$PENDING_PID" 2>/dev/null
    fi

    PENDING_USER="$user_dir"

    # Start a new settling timer in the background
    (sleep "$SETTLE_SECONDS" && run_converter "$PENDING_USER") &
    PENDING_PID=$!
}

echo "[$(date '+%Y-%m-%d %H:%M:%S')] watching for changes..."

inotifywait \
    --monitor \
    --recursive \
    --quiet \
    --event close_write \
    --format '%w%f' \
    "$USERS_DIR" |
while IFS= read -r changed_path; do
    # React to root file changes (sync15 root hash, updated on every sync)
    if [[ "$changed_path" == */sync/root ]]; then
        user_dir="$(dirname "$(dirname "$changed_path")")"
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] sync root changed: $user_dir"
        handle_change "$user_dir"
    fi
done