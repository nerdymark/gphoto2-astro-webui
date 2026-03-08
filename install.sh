#!/usr/bin/env bash
# =============================================================================
# install.sh – Raspberry Pi installer for gphoto2-astro-webui
#
# Usage:
#   chmod +x install.sh
#   ./install.sh [--dev]
#
# What it does:
#   1. Removes any distro-packaged gphoto2/libgphoto2 (apt) if present
#   2. Builds libgphoto2 and gphoto2 from the latest upstream source
#      (ensures full ltdl/dynamic plugin support and newest camera drivers)
#   3. Creates a Python virtual environment in ./backend/.venv
#   4. Installs Python dependencies
#   5. Installs Node.js dependencies and builds the frontend
#   6. Writes a systemd service file so the backend starts on boot
#   7. Optionally configures nginx as a reverse proxy
#
# Options:
#   --dev  Development mode: skips nginx and does not start the service.
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
  case "$arg" in
    --dev) DEV_MODE=true ;;
  esac
done

# ---------------------------------------------------------------------------
# Build gphoto2 + libgphoto2 from upstream source
#
# Distro packages are often many releases behind and are compiled without
# ltdl (dynamic plugin loading), which limits camera driver support.
# Building from source guarantees the latest upstream version with full
# driver support.  Installed to /usr/local.
# ---------------------------------------------------------------------------
build_gphoto2_from_source() {
    info "Building libgphoto2 and gphoto2 from upstream source…"
    info "This may take 20–40 minutes on a Raspberry Pi – please be patient."

    # Build-time dependencies
    info "Installing gphoto2 build dependencies…"
    sudo apt-get install -y -qq \
        build-essential \
        autoconf \
        automake \
        libtool \
        pkg-config \
        libusb-1.0-0-dev \
        libexif-dev \
        libpopt-dev \
        libltdl-dev \
        gettext

    local WORK
    WORK=$(mktemp -d /tmp/gphoto2-src-XXXXXX)

    # _build_one GITHUB_REPO LOCAL_NAME
    #   Downloads the latest release tarball, builds, and installs to /usr/local.
    _build_one() {
        local repo="$1" name="$2"

        info "Fetching latest ${name} release info from GitHub…"
        local api_response tag
        api_response=$(curl -fsSL "https://api.github.com/repos/${repo}/releases/latest") \
            || error "Failed to reach GitHub API for ${repo} – check network connectivity."
        tag=$(printf '%s' "$api_response" \
              | grep -m1 '"tag_name"' \
              | sed 's/.*"tag_name" *: *"\([^"]*\)".*/\1/')
        [[ -z "$tag" ]] && error "Could not determine latest ${name} release tag (API response may be malformed or rate-limited)."

        local ver="${tag#v}"   # strip any leading 'v'
        local tarball="${name}-${ver}.tar.bz2"
        local url="https://github.com/${repo}/releases/download/${tag}/${tarball}"

        info "Downloading ${name} ${ver}…"
        curl -fsSL "$url" -o "${WORK}/${tarball}" \
            || error "Failed to download ${name} ${ver} from ${url}."

        info "Extracting ${tarball}…"
        tar -xjf "${WORK}/${tarball}" -C "$WORK" \
            || error "Failed to extract ${tarball}."

        local src_dir="${WORK}/${name}-${ver}"
        [[ -d "$src_dir" ]] || error "Expected source directory ${src_dir} not found after extraction."

        info "Configuring ${name} ${ver}…"
        pushd "$src_dir" > /dev/null
        # autoreconf regenerates the build system; harmless if already up-to-date.
        # Export PKG_CONFIG_PATH before configure AND make so every build step
        # can locate the freshly-installed libgphoto2 pkg-config files.
        export PKG_CONFIG_PATH="/usr/local/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
        if ! autoreconf --install 2>/dev/null; then
            warn "autoreconf returned non-zero for ${name} – attempting ./configure anyway."
        fi
        ./configure --prefix=/usr/local --disable-rpath \
            || error "${name} ./configure failed."

        info "Compiling ${name} ${ver} ($(nproc) thread(s))…"
        make -j"$(nproc)" || error "${name} make failed."

        info "Installing ${name} ${ver}…"
        sudo make install || error "${name} make install failed."
        popd > /dev/null

        info "${name} ${ver} installed to /usr/local."
    }

    _build_one "gphoto/libgphoto2" "libgphoto2"
    # Refresh the dynamic-linker cache so the new libgphoto2 is found at
    # link time when building the gphoto2 CLI below.
    sudo ldconfig

    _build_one "gphoto/gphoto2" "gphoto2"

    rm -rf "$WORK"

    local built_ver
    built_ver=$(gphoto2 --version 2>&1 | head -1 || true)
    info "gphoto2 from source ready: ${built_ver}"
}

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
info "Updating package list…"
sudo apt-get update -qq

info "Installing system dependencies…"
sudo apt-get install -y -qq \
    python3 \
    python3-pip \
    python3-venv \
    libturbojpeg0 \
    libjpeg-dev \
    zlib1g-dev \
    libopenblas0 \
    git \
    curl

# ---------------------------------------------------------------------------
# Remove any distro-packaged gphoto2/libgphoto2 to avoid conflicts with the
# locally built version.
# ---------------------------------------------------------------------------
info "Removing any distro-packaged gphoto2/libgphoto2 (if installed)…"
sudo apt-get remove -y -qq gphoto2 libgphoto2-dev libgphoto2-6 2>&1 || true

# ---------------------------------------------------------------------------
# Remove gvfs camera/MTP backends
#
# gvfs-backends ships the gphoto2 and MTP backends that auto-mount cameras
# over PTP/MTP when a device is plugged in.  Even after masking the
# gvfs-gphoto2-volume-monitor and gvfs-mtp-volume-monitor user services,
# gvfs-udisks2-volume-monitor (provided by gvfs-backends) can still claim the
# camera's USB interface and trigger "PTP Access Denied".  Removing the
# backends package eliminates all gvfs camera/MTP code from the system.
# gvfs-fuse provides the FUSE mount for gvfs; on a headless Raspberry Pi
# used purely for astrophotography it is not required.
# ---------------------------------------------------------------------------
info "Removing gvfs camera/MTP backends (gvfs-backends, gvfs-fuse)…"
sudo apt-get remove -y -qq gvfs-backends gvfs-fuse 2>&1 || true

build_gphoto2_from_source

# Register the locally built binary with update-alternatives so that
# /usr/bin/gphoto2 always points to the local build regardless of PATH order.
# This prevents the systemd service (or any other process) from accidentally
# picking up a distro-packaged binary that may be installed later.
info "Registering locally built gphoto2 with update-alternatives…"
# Priority 100 is intentionally higher than the Debian/Raspbian package default
# (50), so the locally built binary remains the active alternative even if the
# distro package is ever reinstalled.
sudo update-alternatives --install /usr/bin/gphoto2 gphoto2 /usr/local/bin/gphoto2 100
sudo update-alternatives --set gphoto2 /usr/local/bin/gphoto2

# Refresh the dynamic-linker cache so the locally built libgphoto2 is found
# by anything that links against it (including the gphoto2 CLI registered above).
sudo ldconfig

# ---------------------------------------------------------------------------
# udev rule: prevent gvfs from auto-mounting cameras controlled by gphoto2
#
# Cameras like the Nikon D780 use PTP/MTP as their only USB mode.  When such
# a camera is plugged in, gvfs-mtp-volume-monitor (on desktop systems) or
# gvfs-gphoto2-volume-monitor automatically opens an MTP/PTP session with it.
# If that session is still active when gphoto2 tries to capture an image, the
# camera firmware returns "PTP Access Denied" and the capture fails.
#
# Setting GVFS_IGNORE=1 in the udev environment tells both
# gvfs-mtp-volume-monitor and gvfs-gphoto2-volume-monitor not to mount any
# device that libgphoto2 recognises (ENV{ID_GPHOTO2} is set to "1" by
# /lib/udev/rules.d/69-libgphoto2.rules for all supported cameras).
# The GROUP and MODE lines grant the plugdev group read/write access to the
# camera's USB device node so gphoto2 can open it without root.
# ---------------------------------------------------------------------------
UDEV_RULE_FILE="/etc/udev/rules.d/70-gphoto2-noautomount.rules"
if [[ ! -f "$UDEV_RULE_FILE" ]]; then
    info "Installing udev rule to prevent gvfs from auto-mounting gphoto2 cameras…"
    sudo tee "$UDEV_RULE_FILE" > /dev/null << 'EOF'
# Prevent gvfs from auto-mounting cameras that gphoto2 manages.
# Without this rule, gvfs-mtp-volume-monitor or gvfs-gphoto2-volume-monitor
# opens a session automatically when a PTP/MTP-only camera (e.g. Nikon D780)
# is connected, causing gphoto2 to receive "PTP Access Denied" when it tries
# to capture an image.  GVFS_IGNORE tells both volume monitors to skip these
# devices.
SUBSYSTEM=="usb", ENV{ID_GPHOTO2}=="1", ENV{GVFS_IGNORE}="1", GROUP="plugdev", MODE="0664"
EOF
else
    info "udev rule ${UDEV_RULE_FILE} already exists – preserving."
fi

# Reload udev rules and re-tag any already-connected camera devices so the
# new permissions and GVFS_IGNORE flag apply immediately without a replug.
info "Reloading udev rules…"
sudo udevadm control --reload-rules
sudo udevadm trigger --action=add --subsystem-match=usb 2>/dev/null \
    || sudo udevadm trigger

# ---------------------------------------------------------------------------
# Ensure the service user is in the plugdev group
#
# The udev rule above sets the camera device node to group plugdev with mode
# 0664.  The user running the service must therefore be a member of plugdev
# to open the device without root.  Group membership changes take effect on
# the next login; newgrp or a re-login is needed in the current shell.
# ---------------------------------------------------------------------------
if ! groups "${USER}" | grep -qw plugdev; then
    info "Adding ${USER} to the plugdev group for camera USB access…"
    sudo usermod -aG plugdev "${USER}"
    warn "Group membership change takes effect on the NEXT LOGIN."
    warn "Log out and back in (or run: newgrp plugdev) for gphoto2 to work."
else
    info "${USER} is already in the plugdev group."
fi

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
# Disable GNOME VFS camera daemons
#
# Multiple GNOME VFS processes auto-mount cameras over MTP/PTP and hold
# the USB interface, preventing gphoto2 from accessing the device:
#
#   gvfs-gphoto2-volume-monitor / gvfsd-gphoto2
#       Used when the camera is detected as a PTP device via libgphoto2 udev rules.
#
#   gvfs-mtp-volume-monitor / gvfsd-mtp
#       Used when the camera enumerates as an MTP device.  Cameras like the
#       Nikon D780 use PTP/MTP as their *only* USB mode, so the OS always sees
#       them as MTP.  On Raspberry Pi OS Desktop, gvfs-mtp-volume-monitor starts
#       at login and immediately claims the camera – this is the primary cause of
#       the "PTP Access Denied" error reported by gphoto2.
#
#   gvfs-udisks2-volume-monitor
#       Monitors udisks2 volume events and can also claim USB camera devices,
#       continuing to cause "PTP Access Denied" even after the gphoto2/MTP
#       monitors are masked.
#
#   gvfs-goa-volume-monitor, gvfs-afc-volume-monitor
#       Additional volume monitors (GNOME Online Accounts, Apple File Connect)
#       that start automatically on desktop sessions.  Masking them prevents
#       unnecessary USB device scanning on a dedicated astrophotography system.
#
# Mask all volume monitors so they no longer auto-start, and kill all
# remaining gvfs daemon processes so the camera is immediately available.
# No sudo is required because all processes run as the current user (${USER}).
# ---------------------------------------------------------------------------
info "Masking GNOME VFS volume monitors and stopping their worker daemons…"
for _svc in \
    gvfs-gphoto2-volume-monitor \
    gvfs-mtp-volume-monitor \
    gvfs-udisks2-volume-monitor \
    gvfs-goa-volume-monitor \
    gvfs-afc-volume-monitor
do
    systemctl --user mask "$_svc" 2>/dev/null \
        && systemctl --user stop "$_svc" 2>/dev/null \
        || true   # service may not be present on this system – that is fine
done

# Kill all gvfs worker daemons that may currently hold the camera interface.
for _pat in \
    gvfsd-gphoto2 \
    gvfsd-mtp \
    gvfsd-fuse \
    gvfs-udisks2-volume-monitor \
    gvfs-goa-volume-monitor \
    gvfs-afc-volume-monitor
do
    pkill -f "$_pat" 2>/dev/null || true
done

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
ENV_FILE="/etc/gphoto2-astro-webui.env"

# Create the user-editable environment file only on first install so that
# any manual changes (e.g. LOG_LEVEL=debug) are preserved on re-runs.
if [[ ! -f "$ENV_FILE" ]]; then
    info "Creating environment configuration file ${ENV_FILE}…"
    sudo tee "$ENV_FILE" > /dev/null << 'EOF'
# gphoto2-astro-webui environment configuration
# Edit this file to customise the service, then restart:
#   sudo systemctl restart gphoto2-astro-webui

# Log level for the application and uvicorn (debug, info, warning, error, critical).
# Change to "debug" for verbose logging.
LOG_LEVEL=info
EOF
else
    info "Environment file ${ENV_FILE} already exists – preserving."
fi

# Always write (or update) the service file so that new directives such as
# EnvironmentFile are picked up on re-runs without requiring a manual edit.
info "Writing systemd service file…"
sudo tee "$SERVICE_FILE" > /dev/null << EOF
[Unit]
Description=gphoto2 Astro WebUI backend
After=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${REPO_DIR}/backend
Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="GALLERY_ROOT=${REPO_DIR}/galleries"
# Default log level; overridden by LOG_LEVEL in the environment file below.
Environment="LOG_LEVEL=info"
# User-configurable overrides (e.g. LOG_LEVEL=debug).
# Edit ${ENV_FILE} and restart the service to apply changes.
EnvironmentFile=-${ENV_FILE}
ExecStartPre=-/usr/bin/pkill -f gvfs-gphoto2-volume-monitor
ExecStartPre=-/usr/bin/pkill -f gvfsd-gphoto2
ExecStartPre=-/usr/bin/pkill -f gvfs-mtp-volume-monitor
ExecStartPre=-/usr/bin/pkill -f gvfsd-mtp
ExecStartPre=-/usr/bin/pkill -f gvfs-udisks2-volume-monitor
ExecStartPre=-/usr/bin/pkill -f gvfs-goa-volume-monitor
ExecStartPre=-/usr/bin/pkill -f gvfs-afc-volume-monitor
ExecStartPre=-/usr/bin/pkill -f gvfsd-fuse
ExecStart=${REPO_DIR}/backend/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --log-level \${LOG_LEVEL}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable gphoto2-astro-webui.service
info "Service file written and daemon reloaded."

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
    echo "  Enable debug logging:"
    echo "    sudo sed -i 's/^LOG_LEVEL=.*/LOG_LEVEL=debug/' ${ENV_FILE}"
    echo "    sudo systemctl restart gphoto2-astro-webui"
fi
