# gphoto2-astro-webui

A Raspberry Pi-based camera controller web UI for astrophotography.

**Features**

- Camera detection via gphoto2 (MTP/PTP)
- Manual exposure controls: aperture, shutter speed, ISO
- Named gallery management -- organize captures by session
- Burst capture with configurable interval
- Image stacking for star photography (mean / sum modes)
- Background job system with live progress and log streaming
- Built-in gallery viewer with lightbox and image download
- SPA frontend (Vite + React + Tailwind CSS) served by FastAPI

---

## License

This is a personal art project. **No open-source license is granted.**

You are free to clone and run this software for personal use, but this
repository is not open to outside contributions. Pull requests will not
be accepted. This policy exists to prevent conflicts with the author's
professional work.

Copyright (c) 2024-2026 nerdymark. All rights reserved.

---

## Requirements

- Raspberry Pi (3B+ or newer recommended) running Raspberry Pi OS (Bullseye/Bookworm)
- Camera connected via USB (any gphoto2-compatible DSLR or mirrorless)
- Wi-Fi or Ethernet connection
- Python 3.10+ and Node.js 18+

---

## Quick start -- Raspberry Pi

```bash
# 1. Clone the repository
git clone https://github.com/nerdymark/gphoto2-astro-webui.git
cd gphoto2-astro-webui

# 2. Run the installer (installs deps, builds frontend, enables systemd service + nginx)
chmod +x install.sh
./install.sh

# If a previous version of this repo built gphoto2 from source you will be
# prompted to remove it.  Pass --yes to skip the prompt and remove automatically.
#   ./install.sh --yes

# 3. Open a browser on any device on the same network
#    The installer prints the URL at the end, e.g.:
#    http://192.168.1.42
```

### Development mode (no nginx, hot-reload)

```bash
./install.sh --dev

# Terminal 1 -- backend
cd backend
source .venv/bin/activate
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Terminal 2 -- frontend dev server (proxies /api to :8000)
cd frontend
npm run dev
```

Visit `http://localhost:5173`.

---

## Manual installation

### Backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run
GALLERY_ROOT=../galleries uvicorn main:app --host 0.0.0.0 --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run build          # production build -> dist/
# OR
npm run dev            # Vite dev server with API proxy
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `GALLERY_ROOT` | `./galleries` | Directory where captured images are stored |
| `VITE_API_BASE` | `""` (same origin) | Override API base URL for frontend builds |
| `LOG_LEVEL` | `INFO` | Backend logging verbosity |

---

## Project structure

```
gphoto2-astro-webui/
├── backend/
│   ├── main.py          # FastAPI application & REST endpoints
│   ├── camera.py        # gphoto2 wrapper (with simulation fallback)
│   ├── stacking.py      # Image stacking (mean/sum)
│   ├── jobs.py          # Background job manager
│   ├── requirements.txt
│   └── tests/
│       └── test_backend.py
├── frontend/
│   ├── src/
│   │   ├── api/client.js          # Fetch-based API client
│   │   ├── hooks/
│   │   │   ├── useCamera.js       # Camera/gallery state hooks
│   │   │   └── useJobs.js         # Job polling hook
│   │   ├── components/
│   │   │   ├── StatusBadge.jsx
│   │   │   ├── ExposureControls.jsx
│   │   │   ├── GalleryManager.jsx
│   │   │   ├── CapturePanel.jsx
│   │   │   ├── StackingPanel.jsx
│   │   │   ├── GalleryViewer.jsx
│   │   │   └── JobsPanel.jsx
│   │   └── App.jsx
│   ├── package.json
│   └── vite.config.js
├── galleries/           # Auto-created; stores captured images
├── install.sh           # Raspberry Pi installer
├── CLAUDE.md            # AI assistant instructions
└── README.md
```

---

## API reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/camera/status` | Camera connection & summary |
| `GET` | `/api/camera/config-keys` | List supported config keys |
| `GET` | `/api/camera/exposure` | Current aperture / shutter / ISO + available choices |
| `POST` | `/api/camera/exposure` | Set aperture / shutter / ISO |
| `POST` | `/api/camera/capture` | Capture image into a gallery |
| `POST` | `/api/camera/burst` | Start burst capture (returns job ID) |
| `GET` | `/api/galleries` | List all galleries |
| `POST` | `/api/galleries` | Create a gallery |
| `GET` | `/api/galleries/{gallery}` | List images in a gallery |
| `DELETE` | `/api/galleries/{gallery}/{filename}` | Delete an image |
| `POST` | `/api/galleries/{gallery}/stack` | Start image stacking (returns job ID) |
| `GET` | `/api/images/{gallery}/{filename}` | Serve a gallery image |
| `GET` | `/api/jobs` | List all jobs |
| `GET` | `/api/jobs/{id}` | Get job status, progress, and log |
| `POST` | `/api/jobs/{id}/cancel` | Cancel a running job |

---

## Running tests

```bash
cd backend
pip install pytest httpx
pytest tests/ -v
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "No camera" shown | Check USB cable; run `gphoto2 --auto-detect` |
| Camera busy/locked | The app handles this automatically; for manual recovery run `pkill -f gvfsd-gphoto2` |
| "PTP Access Denied" or "PTP Session Already Opened" on capture | A gvfs daemon is holding the camera's PTP session, or a source-built gphoto2 is mis-linked against the port library. Re-run `./install.sh` (add `--yes` to automatically remove any source-built version). For manual recovery: `sudo apt-get remove -y gvfs-backends gvfs-fuse && pkill -f gvfsd` |
| Two gphoto2 binaries (`/usr/bin` and `/usr/local/bin`) | A previous install built gphoto2 from source. Run `./install.sh --yes` to remove the source build and switch to the distro package (`libgphoto2-port12t64` on Bookworm / `libgphoto2-port12` on Bullseye). |
| Permission denied on USB | `sudo adduser $USER plugdev` then re-login |
| Port 8000 in use | Change `--port` in the systemd service unit |
