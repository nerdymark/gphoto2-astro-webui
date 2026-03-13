#!/usr/bin/env bash
# =============================================================================
# install.sh – Raspberry Pi installer for gphoto2-astro-webui
#
# Usage:
#   chmod +x install.sh
#   ./install.sh [--dev] [--server]
#
# Modes:
#   (default)  Install the Raspberry Pi camera controller (backend + frontend)
#   --server   Install the remote GPU processing server on port 8069
#
# What the default (Pi) mode does:
#   1. Installs gphoto2, libgphoto2-6(t64), and libgphoto2-port12(t64) from
#      the distro package repository.  The correct package name variant is
#      detected automatically: Bookworm uses the 't64' suffix for the 64-bit
#      time_t ABI transition; Bullseye uses the plain names.  Using the
#      packaged version avoids source-build link mismatches against
#      libgphoto2-port that cause "PTP Access Denied" / "PTP Session Already
#      Opened" errors with some cameras.
#   2. Detects any locally-built gphoto2 under /usr/local (installed by a
#      previous version of this script) and offers to remove it so there is
#      only one gphoto2 binary on the system.
#   3. Removes gvfs camera/MTP backends to prevent PTP session conflicts
#   4. Creates a Python virtual environment in ./backend/.venv
#   5. Installs Python dependencies
#   6. Installs Node.js dependencies and builds the frontend
#   7. Writes a systemd service file so the backend starts on boot
#   8. Optionally configures nginx as a reverse proxy
#
# Options:
#   --dev     Development mode: skips nginx and does not start the service.
#   --server  Install remote processing server instead of Pi camera controller.
#   --yes     Non-interactive: automatically remove a locally-built gphoto2
#             without prompting (useful when running the script unattended).
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
YES_MODE=false
SERVER_MODE=false

for arg in "$@"; do
  case "$arg" in
    --dev) DEV_MODE=true ;;
    --yes|-y) YES_MODE=true ;;
    --server) SERVER_MODE=true ;;
  esac
done

# ---------------------------------------------------------------------------
# Server mode: install the remote processing server and exit
# ---------------------------------------------------------------------------
if $SERVER_MODE; then
    info "Installing remote processing server…"

    # System dependencies
    sudo apt-get update -qq
    info "Installing system packages (python3, ffmpeg)…"
    sudo apt-get install -y -qq \
        python3 \
        python3-pip \
        python3-venv \
        ffmpeg

    # Python virtual environment
    info "Setting up Python virtual environment in server/.venv…"
    python3 -m venv "${REPO_DIR}/server/.venv"
    source "${REPO_DIR}/server/.venv/bin/activate"

    info "Installing Python dependencies…"
    pip install --upgrade pip
    pip install --progress-bar on -r "${REPO_DIR}/server/requirements.txt"

    # Try to install CUDA support (optional – won't fail if no GPU)
    info "Attempting to install cupy for CUDA acceleration (optional)…"
    pip install cupy-cuda12x 2>/dev/null \
        && info "cupy installed – CUDA GPU acceleration enabled" \
        || warn "cupy not installed (no CUDA GPU or drivers) – falling back to CPU"

    deactivate

    # Systemd service for the processing server
    SERVER_SERVICE_FILE="/etc/systemd/system/astro-processing-server.service"
    SERVER_ENV_FILE="/etc/astro-processing-server.env"

    if [[ ! -f "$SERVER_ENV_FILE" ]]; then
        info "Creating server environment configuration…"
        sudo tee "$SERVER_ENV_FILE" > /dev/null << 'EOF'
# Astro Processing Server configuration
LOG_LEVEL=info
WORK_DIR=/tmp/astro-server-jobs
EOF
    fi

    info "Writing systemd service file…"
    sudo tee "$SERVER_SERVICE_FILE" > /dev/null << EOF
[Unit]
Description=Astro Remote Processing Server
After=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${REPO_DIR}/server
Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
EnvironmentFile=-${SERVER_ENV_FILE}
ExecStart=${REPO_DIR}/server/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8069 --log-level \${LOG_LEVEL}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable astro-processing-server.service

    if ! $DEV_MODE; then
        sudo systemctl start astro-processing-server.service
        IP=$(hostname -I | awk '{print $1}')
        info "Remote processing server installed and running!"
        echo
        echo "  Server URL: http://${IP}:8069"
        echo "  Health check: curl http://${IP}:8069/api/health"
        echo "  Service status: sudo systemctl status astro-processing-server"
        echo "  View logs: journalctl -u astro-processing-server -f"
        echo
        echo "  On the Raspberry Pi, set this in /etc/gphoto2-astro-webui.env:"
        echo "    REMOTE_SERVER=http://${IP}:8069"
        echo "  Then restart the Pi service:"
        echo "    sudo systemctl restart gphoto2-astro-webui"
    else
        info "Server installed (dev mode – not started)."
        echo
        echo "  Start manually:"
        echo "    cd ${REPO_DIR}/server && source .venv/bin/activate"
        echo "    uvicorn main:app --host 0.0.0.0 --port 8069 --reload"
    fi

    exit 0
fi

# ---------------------------------------------------------------------------
# Detect and offer to remove a locally-built (source-installed) gphoto2.
#
# A previous version of this installer compiled gphoto2 and libgphoto2 from
# source and installed them under /usr/local.  Leaving those files in place
# while also installing the distro package creates two competing gphoto2
# binaries and two sets of libgphoto2 shared libraries on the linker search
# path, which produces unpredictable USB port library mismatches and is a
# root cause of persistent "PTP Access Denied" / "PTP Session Already Opened"
# errors.
#
# This function detects the source-installed files and, with the user's
# permission (or automatically with --yes / in non-interactive mode), removes
# them so the distro package is the sole gphoto2 on the system.
# ---------------------------------------------------------------------------
remove_source_build() {
    # The source build installs the gphoto2 binary to /usr/local/bin.
    # The distro package installs it to /usr/bin.  The presence of
    # /usr/local/bin/gphoto2 is therefore a reliable indicator that a
    # source build was previously installed by this script.
    if [[ ! -f /usr/local/bin/gphoto2 ]]; then
        return 0
    fi

    local src_ver
    src_ver=$(/usr/local/bin/gphoto2 --version 2>&1 | head -1 || true)
    warn "Locally-built gphoto2 detected: ${src_ver:-unknown version}"
    warn "The following files will be removed:"
    warn "  /usr/local/bin/gphoto2"
    warn "  /usr/local/lib/libgphoto2*.so*     (shared libraries)"
    warn "  /usr/local/lib/libgphoto2/          (camera driver plugins)"
    warn "  /usr/local/lib/libgphoto2_port/     (port driver plugins)"
    warn "  /usr/local/share/libgphoto2/        (camera definitions)"
    warn "  /usr/local/lib/pkgconfig/libgphoto2*.pc"
    echo

    local do_remove=false
    if $YES_MODE; then
        do_remove=true
    elif [[ -t 0 ]]; then
        # Interactive terminal – prompt the user.
        read -r -p "$(echo -e "${YELLOW}[WARN]${NC}  Remove locally-built gphoto2 and use the distro package instead? [Y/n] ")" _reply
        case "${_reply:-Y}" in
            [Yy]*|"") do_remove=true ;;
            *)
                warn "Skipping removal of locally-built gphoto2."
                warn "Both versions will be present; the distro package may not take effect."
                return 0
                ;;
        esac
    else
        # Non-interactive (piped or redirected stdin) – default to removing so
        # the script is safe to run unattended (e.g. from another script or CI).
        info "Non-interactive mode: removing locally-built gphoto2 automatically."
        do_remove=true
    fi

    if $do_remove; then
        info "Removing locally-built gphoto2 and libgphoto2 from /usr/local…"
        sudo rm -f  /usr/local/bin/gphoto2
        sudo rm -f  /usr/local/lib/libgphoto2.so*
        sudo rm -f  /usr/local/lib/libgphoto2_port.so*
        sudo rm -rf /usr/local/lib/libgphoto2
        sudo rm -rf /usr/local/lib/libgphoto2_port
        sudo rm -rf /usr/local/share/libgphoto2
        sudo rm -f  /usr/local/lib/pkgconfig/libgphoto2.pc
        sudo rm -f  /usr/local/lib/pkgconfig/libgphoto2_port.pc
        # Remove the update-alternatives entry registered by the old installer;
        # ignore the error if it was never registered or has already been removed.
        sudo update-alternatives --remove gphoto2 /usr/local/bin/gphoto2 2>/dev/null || true
        sudo ldconfig
        info "Locally-built gphoto2 removed."
    fi
}

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
info "Updating package list…"
sudo apt-get update -qq

# Remove any locally-built (source-installed) gphoto2 before installing the
# distro package to avoid having two competing versions on the system.
remove_source_build

# ---------------------------------------------------------------------------
# Determine the correct libgphoto2 port-library package name for this distro.
#
# Debian Bookworm (and Raspberry Pi OS Bookworm) added a 't64' suffix to
# packages that underwent the 64-bit time_t ABI transition; the package is
# therefore named 'libgphoto2-port12t64' on Bookworm and 'libgphoto2-port12'
# on Bullseye.  Likewise the main library is 'libgphoto2-6t64' on Bookworm.
# apt-cache show probes the local package database without downloading
# anything – it is fast and works offline.
# ---------------------------------------------------------------------------
if apt-cache show libgphoto2-port12t64 &>/dev/null; then
    _GPHOTO2_PORT_PKG="libgphoto2-port12t64"
else
    _GPHOTO2_PORT_PKG="libgphoto2-port12"
fi
info "libgphoto2 port library package: ${_GPHOTO2_PORT_PKG}"

if apt-cache show libgphoto2-6t64 &>/dev/null; then
    _LIBGPHOTO2_PKG="libgphoto2-6t64"
else
    _LIBGPHOTO2_PKG="libgphoto2-6"
fi

info "Installing system dependencies and gphoto2 from distro packages…"
sudo apt-get install -y -qq \
    python3 \
    python3-pip \
    python3-venv \
    libturbojpeg0 \
    libjpeg-dev \
    zlib1g-dev \
    libopenblas0 \
    ffmpeg \
    git \
    curl \
    gphoto2 \
    "${_LIBGPHOTO2_PKG}" \
    "${_GPHOTO2_PORT_PKG}"

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

# Extra backstop: rename gvfs dbus/mount service files so they cannot auto-start
# even if gvfs-backends could not be removed (e.g. due to desktop dependencies).
for _gvfs_file in \
    /usr/share/dbus-1/services/org.gtk.Private.GPhoto2VolumeMonitor.service \
    /usr/share/dbus-1/services/org.gtk.Private.MTPVolumeMonitor.service \
    /usr/share/gvfs/mounts/gphoto2.mount \
    /usr/share/gvfs/mounts/mtp.mount \
    /usr/share/gvfs/remote-volume-monitors/gphoto2.monitor \
    /usr/share/gvfs/remote-volume-monitors/mtp.monitor
do
    if [[ -f "$_gvfs_file" ]]; then
        info "Disabling ${_gvfs_file}…"
        sudo mv "$_gvfs_file" "${_gvfs_file}.disabled" 2>/dev/null || true
    fi
done

# Refresh the dynamic-linker cache; the apt post-install scripts already do
# this, but an explicit call ensures the new libraries are visible to every
# subsequent command in this script.
sudo ldconfig

_installed_ver=$(gphoto2 --version 2>&1 | head -1 || true)
info "gphoto2 package ready: ${_installed_ver}"

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

# Release the gvfs FUSE filesystem before killing gvfsd-fuse and gvfsd.
# When gvfsd-fuse holds an active FUSE mount (visible as 'fuse ... 1' in
# lsmod), gvfsd cannot exit cleanly until the mount is released.
# fusermount -uz does a lazy unmount: the filesystem is detached from the
# mount table immediately.  We try both common mount-point locations.
fusermount -uz ~/.gvfs 2>/dev/null || true
fusermount -uz "/run/user/$(id -u)/gvfs" 2>/dev/null || true

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

# On low-RAM Pis, /tmp is a tmpfs that can fill up during pip builds
# (e.g. opencv-python-headless needs ~300 MiB temp space).  Use a
# disk-backed temp directory under GALLERY_ROOT if /tmp is a tmpfs
# with less than 512 MiB free.
_PIP_ENV=()
if findmnt -n -o FSTYPE /tmp 2>/dev/null | grep -q tmpfs; then
    _tmp_avail=$(df --output=avail -BM /tmp 2>/dev/null | tail -1 | tr -dc '0-9')
    if [[ -n "$_tmp_avail" ]] && (( _tmp_avail < 512 )); then
        _pip_tmp="${REPO_DIR}/.pip-tmp"
        mkdir -p "$_pip_tmp"
        warn "/tmp is tmpfs with only ${_tmp_avail}M free – using ${_pip_tmp} for pip builds"
        _PIP_ENV=(env "TMPDIR=${_pip_tmp}")
    fi
fi

info "Installing Python dependencies (this may take a few minutes on Pi)…"
"${_PIP_ENV[@]}" pip install --upgrade pip
"${_PIP_ENV[@]}" pip install --progress-bar on -r "${REPO_DIR}/backend/requirements.txt"

# Clean up disk-backed temp directory
[[ -d "${REPO_DIR}/.pip-tmp" ]] && rm -rf "${REPO_DIR}/.pip-tmp"

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

# Remote processing server URL (optional).
# When set, stacking and timelapse panels show a "Process on remote server"
# checkbox that offloads heavy computation to a GPU-equipped machine.
# Example: REMOTE_SERVER=http://10.0.1.20:8069
#REMOTE_SERVER=
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
# Release the gvfs FUSE mount before killing gvfsd-fuse so gvfsd can exit.
# fusermount -uz is a lazy unmount (detaches immediately without waiting).
# %U is the systemd specifier for the service user's numeric UID;
# %h is the specifier for the service user's home directory.
# Both common mount-point paths are tried; failures are silently ignored.
ExecStartPre=-/bin/fusermount -uz /run/user/%U/gvfs
ExecStartPre=-/bin/fusermount -uz %h/.gvfs
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
        proxy_read_timeout 600;
        proxy_send_timeout 600;
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
