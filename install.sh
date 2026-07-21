#!/usr/bin/env bash
# install.sh - install rm-render alongside an existing rmfakecloud deployment
#
# Run from anywhere; the script locates itself and uses paths relative to
# the repo root. Safe to re-run on an existing installation.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER="${USER:-$(id -un)}"
SYSTEMD_SERVICE="rm-render@${USER}.service"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

info()    { echo "  $*"; }
success() { echo "  OK  $*"; }
warn()    { echo "  WARN  $*"; }
fatal()   { echo "  ERROR  $*"; exit 1; }

ask() {
    local prompt="$1"
    local reply
    read -r -p "  $prompt [y/N] " reply
    [[ "${reply,,}" == "y" ]]
}

ask_path() {
    local prompt="$1"
    local reply
    read -r -p "  $prompt: " reply
    echo "$reply"
}

section() {
    echo ""
    echo "── $* ──"
}

# ---------------------------------------------------------------------------
# Check prerequisites
# ---------------------------------------------------------------------------

section "Checking prerequisites"

MISSING=()

command -v docker >/dev/null 2>&1 && success "docker found" || MISSING+=("docker")
docker compose version >/dev/null 2>&1 && success "docker compose found" || MISSING+=("docker compose")
command -v inotifywait >/dev/null 2>&1 && success "inotifywait found" || MISSING+=("inotify-tools (sudo apt install inotify-tools)")

UV=""
for candidate in \
    "$(command -v uv 2>/dev/null || true)" \
    "/home/${USER}/.local/bin/uv" \
    "/usr/local/bin/uv"; do
    if [ -x "$candidate" ]; then
        UV="$candidate"
        break
    fi
done
[ -n "$UV" ] && success "uv found: $UV" || MISSING+=("uv (https://astral.sh/uv)")

if [ ${#MISSING[@]} -gt 0 ]; then
    echo ""
    warn "The following prerequisites are missing:"
    for item in "${MISSING[@]}"; do
        echo "    - $item"
    done
    fatal "Please install the missing prerequisites and re-run install.sh"
fi

# ---------------------------------------------------------------------------
# Locate rmfakecloud directory
# ---------------------------------------------------------------------------

section "Locating rmfakecloud"

RMFAKECLOUD_DIR=""

CANDIDATES=(
    "$HOME/remotehomes/remarkable"
    "$HOME/remarkable"
    "/opt/remarkable"
    "/srv/remarkable"
)

for candidate in "${CANDIDATES[@]}"; do
    if [ -f "$candidate/docker-compose.yml" ] && [ -d "$candidate/data" ]; then
        RMFAKECLOUD_DIR="$candidate"
        info "Found rmfakecloud at: $RMFAKECLOUD_DIR"
        break
    fi
done

if [ -z "$RMFAKECLOUD_DIR" ]; then
    warn "Could not auto-detect rmfakecloud directory."
    info "Looking for a directory containing docker-compose.yml and data/"
    RMFAKECLOUD_DIR=$(ask_path "Enter the path to your rmfakecloud directory")
    RMFAKECLOUD_DIR="${RMFAKECLOUD_DIR%/}"
    if [ ! -f "$RMFAKECLOUD_DIR/docker-compose.yml" ]; then
        fatal "No docker-compose.yml found at $RMFAKECLOUD_DIR"
    fi
    if [ ! -d "$RMFAKECLOUD_DIR/data" ]; then
        fatal "No data/ directory found at $RMFAKECLOUD_DIR"
    fi
fi

if ! ask "Use $RMFAKECLOUD_DIR as the rmfakecloud directory?"; then
    RMFAKECLOUD_DIR=$(ask_path "Enter the correct path")
    RMFAKECLOUD_DIR="${RMFAKECLOUD_DIR%/}"
fi

# Verify sync data is accessible
USERS_DIR="$RMFAKECLOUD_DIR/data/users"
if [ ! -d "$USERS_DIR" ]; then
    fatal "users directory not found at $USERS_DIR"
fi

# Check if data is readable without sudo
if ! ls "$USERS_DIR" >/dev/null 2>&1; then
    echo ""
    warn "The rmfakecloud data directory is not readable by $USER."
    info "This is likely because Docker wrote the files as root."
    info "The following command will fix ownership:"
    echo ""
    echo "    sudo chown -R $(id -u):$(id -g) $USERS_DIR"
    echo ""
    if ask "Fix ownership now?"; then
        sudo chown -R "$(id -u):$(id -g)" "$USERS_DIR"
        success "Ownership fixed"
    else
        warn "Skipping. The converter will not be able to read sync data."
    fi
fi

# ---------------------------------------------------------------------------
# Create rendered directory
# ---------------------------------------------------------------------------

section "Setting up rendered directory"

RENDERED_DIR="$RMFAKECLOUD_DIR/rendered"
if [ ! -d "$RENDERED_DIR" ]; then
    info "Creating $RENDERED_DIR"
    mkdir -p "$RENDERED_DIR"
    success "Created $RENDERED_DIR"
else
    success "$RENDERED_DIR already exists"
fi

# ---------------------------------------------------------------------------
# Deploy nginx files
# ---------------------------------------------------------------------------

section "Deploying nginx configuration"

FILES_CHANGED=0

deploy_file() {
    local src="$1"
    local dst="$2"
    if [ ! -f "$dst" ] || ! diff -q "$src" "$dst" >/dev/null 2>&1; then
        cp "$src" "$dst"
        success "Deployed $(basename "$dst")"
        FILES_CHANGED=1
    else
        info "$(basename "$dst") already up to date"
    fi
}

deploy_file "$REPO_DIR/deploy/nginx.conf"                   "$RMFAKECLOUD_DIR/nginx.conf"
deploy_file "$REPO_DIR/deploy/docker-compose.override.yml"  "$RMFAKECLOUD_DIR/docker-compose.override.yml"
deploy_file "$REPO_DIR/deploy/index.html"                   "$RMFAKECLOUD_DIR/index.html"

# Remove index.html from rendered/ if present (old location)
if [ -f "$RENDERED_DIR/index.html" ]; then
    rm "$RENDERED_DIR/index.html"
    info "Removed index.html from rendered/ (moved to rmfakecloud root)"
    FILES_CHANGED=1
fi

# ---------------------------------------------------------------------------
# Start/restart nginx
# ---------------------------------------------------------------------------

section "Starting nginx"

cd "$RMFAKECLOUD_DIR"

NGINX_RUNNING=$(docker compose ps --status running --services 2>/dev/null | grep -c "rendered" || true)

if [ "$FILES_CHANGED" -gt 0 ] || [ "$NGINX_RUNNING" -eq 0 ]; then
    info "Starting/restarting nginx container..."
    sudo docker compose up -d rendered
    success "nginx started"
else
    success "nginx already running and up to date"
fi

# ---------------------------------------------------------------------------
# Install Python dependencies
# ---------------------------------------------------------------------------

section "Installing Python dependencies"

info "Running uv sync in $REPO_DIR"
"$UV" sync --project "$REPO_DIR"
success "Dependencies installed"

# ---------------------------------------------------------------------------
# Install scripts to ~/.local/bin
# ---------------------------------------------------------------------------

section "Installing scripts"

LOCAL_BIN="$HOME/.local/bin"
mkdir -p "$LOCAL_BIN"

# Install watch script, patching SCRIPT_DIR to point to the repo
install_watch() {
    cp "$REPO_DIR/watch.sh" "$LOCAL_BIN/rm-render-watch"
    chmod +x "$LOCAL_BIN/rm-render-watch"
    # Replace the dynamic SCRIPT_DIR detection with the actual repo path
    sed -i \
        's|SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE\[0\]}")" && pwd)"|SCRIPT_DIR="'"$REPO_DIR"'"|' \
        "$LOCAL_BIN/rm-render-watch"
    success "Installed rm-render-watch to $LOCAL_BIN"
}

if [ ! -f "$LOCAL_BIN/rm-render-watch" ]; then
    install_watch
else
    # Check if source changed (compare ignoring the SCRIPT_DIR line)
    SRC_HASH=$(grep -v 'SCRIPT_DIR' "$REPO_DIR/watch.sh" | md5sum)
    DST_HASH=$(grep -v 'SCRIPT_DIR' "$LOCAL_BIN/rm-render-watch" | md5sum)
    if [ "$SRC_HASH" != "$DST_HASH" ]; then
        install_watch
    else
        info "rm-render-watch already up to date"
    fi
fi

# ---------------------------------------------------------------------------
# Install systemd service
# ---------------------------------------------------------------------------

section "Installing systemd service"

SERVICE_FILE="/etc/systemd/system/rm-render@.service"

if [ ! -f "$SERVICE_FILE" ] || ! diff -q "$REPO_DIR/rm-render@.service" "$SERVICE_FILE" >/dev/null 2>&1; then
    info "Installing $SERVICE_FILE"
    sudo cp "$REPO_DIR/rm-render@.service" "$SERVICE_FILE"
    sudo systemctl daemon-reload
    success "Service file installed"
else
    info "Service file already up to date"
fi

if ! systemctl is-enabled "$SYSTEMD_SERVICE" >/dev/null 2>&1; then
    info "Enabling $SYSTEMD_SERVICE"
    sudo systemctl enable "$SYSTEMD_SERVICE"
    success "Service enabled"
else
    success "Service already enabled"
fi

if systemctl is-active "$SYSTEMD_SERVICE" >/dev/null 2>&1; then
    info "Restarting $SYSTEMD_SERVICE"
    sudo systemctl restart "$SYSTEMD_SERVICE"
else
    info "Starting $SYSTEMD_SERVICE"
    sudo systemctl start "$SYSTEMD_SERVICE"
fi
success "Service running"

# ---------------------------------------------------------------------------
# Initial render
# ---------------------------------------------------------------------------

section "Running initial render"

# Find first user's sync data
FIRST_USER_DIR=$(ls -d "$USERS_DIR"/*/ 2>/dev/null | head -1)
if [ -z "$FIRST_USER_DIR" ]; then
    warn "No user data found in $USERS_DIR -- skipping initial render"
else
    info "Rendering documents from $FIRST_USER_DIR"
    info "This may take a few minutes on first run..."
    "$UV" run --project "$REPO_DIR" \
        python3 "$REPO_DIR/src/rm_render/convert.py" \
        "$FIRST_USER_DIR" \
        "$RENDERED_DIR"
    success "Initial render complete"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  rm-render installed successfully"
echo ""
echo "  Documents: http://$(hostname -I | awk '{print $1}')"
echo "  Logs:      journalctl -u $SYSTEMD_SERVICE -f"
echo "  Status:    systemctl status $SYSTEMD_SERVICE"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"