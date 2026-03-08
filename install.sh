#!/usr/bin/env bash
# =============================================================================
# install.sh – Raspberry Pi installer for gphoto2-astro-webui
#
# Usage:
#   chmod +x install.sh
#   ./install.sh [--dev]
#
# What it does:
#   1. Installs system dependencies (gphoto2, python3-venv, Node.js, nginx)
#   2. Creates a Python virtual environment in ./backend/.venv
#   3. Installs Python dependencies
#   4. Installs Node.js dependencies and builds the frontend
#   5. Writes a systemd service file so the backend starts on boot
#   6. Optionally configures nginx as a reverse proxy
# =============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV_MODE=false

for arg in "$@"; do
  [[ "$arg" == "--dev" ]] && DEV_MODE=true
done

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
info "Updating package list…"
sudo apt-get update -qq

info "Installing system dependencies…"
sudo apt-get install -y -qq \
    gphoto2 \
    python3 \
    python3-pip \
    python3-venv \
    libgphoto2-dev \
    libturbojpeg0 \
    libjpeg-dev \
    zlib1g-dev \
    libopenblas0 \
    git \
    curl

# Node.js (v20 LTS)
if ! command -v node &>/dev/null; then
    # Detect architecture; NodeSource does not support armhf (32-bit ARM).
    # On armhf/armv7l use the official nodejs.org binary instead.
    SYS_ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)
    if [[ "$SYS_ARCH" == "armhf" || "$SYS_ARCH" == "armv7l" ]]; then
        info "Detected armhf – NodeSource does not support this architecture."
        info "Installing Node.js 20 LTS from nodejs.org for armv7l…"
        NODE_VER=$(curl -fsSL https://nodejs.org/dist/latest-v20.x/ \
            | grep -oP 'node-v\K[0-9.]+(?=-linux-armv7l\.tar\.xz)' \
            | head -1)
        if [[ -z "$NODE_VER" ]]; then
            error "Could not determine latest Node.js 20 LTS version from nodejs.org."
        fi
        curl -fsSL "https://nodejs.org/dist/v${NODE_VER}/node-v${NODE_VER}-linux-armv7l.tar.xz" \
            -o /tmp/nodejs-armv7l.tar.xz \
            || error "Failed to download Node.js ${NODE_VER} for armv7l."
        sudo tar -xf /tmp/nodejs-armv7l.tar.xz -C /usr/local --strip-components=1 \
            || error "Failed to extract Node.js tarball."
        rm -f /tmp/nodejs-armv7l.tar.xz
    else
        info "Installing Node.js 20 LTS via NodeSource…"
        curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
        sudo apt-get install -y nodejs
    fi
else
    info "Node.js already installed: $(node --version)"
fi

# Nginx (optional, for production)
if ! $DEV_MODE; then
    sudo apt-get install -y -qq nginx
fi

# ---------------------------------------------------------------------------
# Disable gvfs-gphoto2-volume-monitor
# The GNOME Virtual File System daemon auto-mounts cameras over MTP/PTP and
# can hold the USB interface, preventing gphoto2 from accessing the device
# (error -53: 'Could not claim the USB device').  Mask and stop it now so
# the camera is available to this service on every boot.
# ---------------------------------------------------------------------------
info "Masking gvfs-gphoto2-volume-monitor to prevent USB device conflicts…"
systemctl --user mask gvfs-gphoto2-volume-monitor 2>/dev/null \
    && systemctl --user stop gvfs-gphoto2-volume-monitor 2>/dev/null \
    || warn "Could not mask gvfs-gphoto2-volume-monitor (may not be installed – this is fine)"

# ---------------------------------------------------------------------------
# 2. Python virtual environment
# ---------------------------------------------------------------------------
info "Setting up Python virtual environment…"
python3 -m venv "${REPO_DIR}/backend/.venv"
source "${REPO_DIR}/backend/.venv/bin/activate"

info "Installing Python dependencies…"
pip install --upgrade pip -q
pip install -q -r "${REPO_DIR}/backend/requirements.txt"

deactivate

# ---------------------------------------------------------------------------
# 3. Frontend build
# ---------------------------------------------------------------------------
info "Installing npm dependencies…"
cd "${REPO_DIR}/frontend"
npm ci --prefer-offline 2>/dev/null || npm install

info "Building frontend…"
npm run build
cd "${REPO_DIR}"

# ---------------------------------------------------------------------------
# 4. Systemd service
# ---------------------------------------------------------------------------
SERVICE_FILE="/etc/systemd/system/gphoto2-astro-webui.service"

if [[ ! -f "$SERVICE_FILE" ]]; then
    info "Installing systemd service…"
    sudo tee "$SERVICE_FILE" > /dev/null << EOF
[Unit]
Description=gphoto2 Astro WebUI backend
After=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${REPO_DIR}/backend
Environment="GALLERY_ROOT=${REPO_DIR}/galleries"
ExecStartPre=-/usr/bin/pkill -f gvfs-gphoto2-volume-monitor
ExecStart=${REPO_DIR}/backend/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable gphoto2-astro-webui.service
    info "Service installed and enabled."
else
    warn "Systemd service file already exists – skipping."
fi

# ---------------------------------------------------------------------------
# 5. Nginx reverse proxy (production only)
# ---------------------------------------------------------------------------
if ! $DEV_MODE; then
    NGINX_CONF="/etc/nginx/sites-available/gphoto2-astro-webui"
    if [[ ! -f "$NGINX_CONF" ]]; then
        info "Configuring nginx reverse proxy…"
        sudo tee "$NGINX_CONF" > /dev/null << 'EOF'
server {
    listen 80 default_server;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:8000/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 120;
        proxy_send_timeout 120;
    }
}
EOF

        sudo ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/gphoto2-astro-webui
        sudo rm -f /etc/nginx/sites-enabled/default
        sudo nginx -t && sudo systemctl reload nginx
    else
        warn "Nginx config already exists – skipping."
    fi
fi

# ---------------------------------------------------------------------------
# 6. Create default galleries directory
# ---------------------------------------------------------------------------
mkdir -p "${REPO_DIR}/galleries"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
info "Installation complete!"
echo
if $DEV_MODE; then
    echo "  Start the backend:   cd backend && source .venv/bin/activate && uvicorn main:app --reload"
    echo "  Start the frontend:  cd frontend && npm run dev"
else
    sudo systemctl start gphoto2-astro-webui.service
    IP=$(hostname -I | awk '{print $1}')
    echo "  Open http://${IP} in your browser"
    echo "  Service status:  sudo systemctl status gphoto2-astro-webui"
    echo "  View logs:       journalctl -u gphoto2-astro-webui -f"
fi
