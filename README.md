# gphoto2-astro-webui

A Raspberry Pi-based camera controller web UI for astrophotography.

**Features**

- Camera detection via gphoto2 (MTP/PTP)
- Manual exposure controls: aperture, shutter speed, ISO
- Named gallery management – organize captures by session
- Burst capture with configurable interval
- Image stacking for star photography (mean / median / sum modes)
- Built-in gallery viewer with lightbox and image download
- SPA frontend (Vite + React + Tailwind CSS) served by FastAPI

---

## Requirements

- Raspberry Pi (3B+ or newer recommended) running Raspberry Pi OS (Bullseye/Bookworm)
- Camera connected via USB (any gphoto2-compatible DSLR or mirrorless)
- Wi-Fi or Ethernet connection
- Python 3.10+ and Node.js 18+

---

## Quick start – Raspberry Pi

```bash
# 1. Clone the repository
git clone https://github.com/nerdymark/gphoto2-astro-webui.git
cd gphoto2-astro-webui

# 2. Run the installer (installs deps, builds frontend, enables systemd service + nginx)
chmod +x install.sh
./install.sh

# 3. Open a browser on any device on the same network
#    The installer prints the URL at the end, e.g.:
#    http://192.168.1.42
```

### Development mode (no nginx, hot-reload)

```bash
./install.sh --dev

# Terminal 1 – backend
cd backend
source .venv/bin/activate
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Terminal 2 – frontend dev server (proxies /api to :8000)
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

---

## Project structure

```
gphoto2-astro-webui/
├── backend/
│   ├── main.py          # FastAPI application & REST endpoints
│   ├── camera.py        # gphoto2 wrapper (with simulation fallback)
│   ├── stacking.py      # Image stacking (mean/median/sum)
│   ├── requirements.txt
│   └── tests/
│       └── test_backend.py
├── frontend/
│   ├── src/
│   │   ├── api/client.js          # Fetch-based API client
│   │   ├── hooks/useCamera.js     # React hooks for camera/gallery state
│   │   ├── components/
│   │   │   ├── StatusBadge.jsx
│   │   │   ├── ExposureControls.jsx
│   │   │   ├── GalleryManager.jsx
│   │   │   ├── CapturePanel.jsx
│   │   │   ├── StackingPanel.jsx
│   │   │   └── GalleryViewer.jsx
│   │   └── App.jsx
│   ├── package.json
│   └── vite.config.js
├── galleries/           # Auto-created; stores captured images
├── install.sh           # Raspberry Pi installer
└── README.md
```

---

## API reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/camera/status` | Camera connection & summary |
| `GET` | `/api/camera/exposure` | Current aperture / shutter / ISO + available choices |
| `POST` | `/api/camera/exposure` | Set aperture / shutter / ISO |
| `POST` | `/api/camera/capture` | Capture image into a gallery |
| `GET` | `/api/galleries` | List all galleries |
| `POST` | `/api/galleries` | Create a gallery |
| `GET` | `/api/galleries/{gallery}` | List images in a gallery |
| `DELETE` | `/api/galleries/{gallery}/{filename}` | Delete an image |
| `POST` | `/api/galleries/{gallery}/stack` | Stack selected images |
| `GET` | `/api/images/{gallery}/{filename}` | Serve a gallery image |

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
| Camera busy/locked | Stop the GVFS gphoto2 volume monitor daemon |
| Permission denied on USB | `sudo adduser $USER plugdev` then re-login |
| Port 8000 in use | Change `--port` in the systemd service unit |
