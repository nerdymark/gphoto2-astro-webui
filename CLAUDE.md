# CLAUDE.md

## Project Overview

gphoto2-astro-webui is a web-based astrophotography camera control application. It wraps the `gphoto2` CLI tool with a FastAPI backend and React frontend, designed primarily for Raspberry Pi deployments. Features include live camera control, burst capture, image stacking (mean/median/sum), and a gallery viewer.

## Architecture

```
backend/          # FastAPI REST API (Python 3.10+)
├── main.py       # API endpoints, static file serving, Pydantic models
├── camera.py     # gphoto2 CLI wrapper, USB conflict resolution, simulation fallback
├── stacking.py   # NumPy-based image stacking (mean/median/sum)
├── requirements.txt
└── tests/
    └── test_backend.py   # pytest test suite with mocked gphoto2

frontend/         # React 19 + Vite SPA
├── src/
│   ├── api/client.js        # Fetch-based API client
│   ├── hooks/useCamera.js   # Custom hooks (useCamera, useExposure, useGalleries, useGallery)
│   ├── components/          # UI components (CapturePanel, ExposureControls, GalleryManager, etc.)
│   ├── App.jsx              # Root component
│   └── main.jsx             # Entry point
├── vite.config.js           # Dev proxy to localhost:8000
├── eslint.config.js         # Flat config, JSX enabled
└── tailwind.config.js

install.sh        # Raspberry Pi automated installer (systemd + nginx)
```

## Development Commands

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev      # Dev server on :5173, proxies /api/* to :8000
npm run build    # Production build to dist/
npm run lint     # ESLint
npm run preview  # Preview production build
```

### Testing

```bash
cd backend
pip install pytest httpx
pytest tests/ -v
```

Tests use `unittest.mock` to mock gphoto2 subprocess calls. Test file: `backend/tests/test_backend.py`.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GALLERY_ROOT` | `./galleries` | Image storage directory |
| `VITE_API_BASE` | `""` (same origin) | Frontend API base URL override |
| `LOG_LEVEL` | `INFO` | Backend logging verbosity |

## Key Conventions

### Backend (Python)

- **No gphoto2 library binding** — all camera interaction is via `subprocess.run` calling the `gphoto2` CLI
- **Simulation fallback**: When gphoto2 is not installed, camera.py falls back to a simulation mode generating placeholder images. This enables development without hardware.
- **Thread safety**: Camera access is serialized with `threading.RLock()` to prevent concurrent USB access
- **USB conflict resolution**: Automatically kills gvfs daemons (gvfsd, gvfs-gphoto2-volume-monitor, gvfs-mtp-volume-monitor, gvfsd-fuse) and unmounts FUSE mounts before camera access
- **Retry logic**: Auto-retries on USB claim errors (3 attempts) and PTP access denied errors (3 attempts)
- **Bulb mode**: Camera detects Bulb/Time shutter-speed modes and uses the two-phase `epress2` (Nikon) or `bulb` (generic) capture sequence instead of `--capture-image-and-download`
- **Config key fallbacks**: Nikon cameras use `f-number` instead of `aperture` and may use `shutterspeed2` instead of `shutterspeed`. The getter and setter both probe for the correct key.
- **Path sanitization**: Gallery names are validated to allow only `[a-zA-Z0-9._\- ]`
- **Pydantic models**: Used for request/response validation (`ExposureSettings`, `CaptureRequest`, `StackRequest`, `CreateGalleryRequest`)
- **Image formats**: Supports .jpg, .jpeg, .png, .tif, .cr2, .nef, .arw

### Frontend (React/JSX)

- **Styling**: Tailwind CSS with a dark theme (slate-900 base, indigo accents)
- **State management**: React hooks only (useState, useCallback, useEffect) — no external state library
- **Custom hooks** in `useCamera.js`: `useCamera`, `useExposure`, `useGalleries`, `useGallery`
- **Polling**: Camera status refreshes every 5 seconds
- **ESLint**: Flat config format, allows unused vars with uppercase or underscore prefix

### General

- No TypeScript — frontend is plain JavaScript (JSX)
- No CI/CD pipeline configured
- No frontend tests — only ESLint for code quality
- Production deployment serves frontend static files from FastAPI (mounted at `/`)

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/camera/status` | Camera connection status |
| GET | `/api/camera/config-keys` | List supported config keys |
| GET | `/api/camera/exposure` | Current exposure settings + choices |
| POST | `/api/camera/exposure` | Update aperture/shutter/ISO |
| POST | `/api/camera/capture` | Capture image to gallery |
| GET | `/api/galleries` | List all galleries |
| POST | `/api/galleries` | Create new gallery |
| GET | `/api/galleries/{name}` | List images in gallery |
| DELETE | `/api/galleries/{name}/{file}` | Delete image |
| POST | `/api/galleries/{name}/stack` | Stack selected images |
| GET | `/api/images/{gallery}/{file}` | Serve image file |

## Known Issues and Troubleshooting (Nikon D780)

### "PTP Access Denied" or "PTP Session Already Opened"
**Cause**: gvfs daemons (gvfs-mtp-volume-monitor, gvfsd-mtp) auto-claim the camera's PTP/MTP USB session. The D780 uses PTP/MTP as its only USB mode.
**Fix**: The installer masks gvfs services, removes gvfs-backends, adds a udev rule with `GVFS_IGNORE=1`, and renames dbus service files. The capture code also kills gvfs proactively before each capture with retry logic.

### "Invalid Status / Could not capture image"
**Cause**: Camera is in Bulb mode (shutter speed = `0xFFFFFFFD`). Standard `--capture-image-and-download` sends a single InitiateCapture PTP command which Bulb mode rejects.
**Fix**: `camera.py` detects Bulb mode via `is_bulb_mode()` and uses the `epress2=on` / `epress2=off` two-phase shutter sequence (Nikon-specific, falls back to `bulb=1`/`bulb=0` for other brands). Set a specific shutter speed (not Bulb) on the camera dial to avoid this entirely.

### Slow USB communication in shell mode
**Cause**: libgphoto2 PTP2 property cache timeout (5s default) causes excessive cache refreshes on cameras with many properties like the D780.
**Workaround**: Add `ptp2=cachetime=10` to `~/.gphoto/settings`.

### Config key differences
- Nikon uses `f-number` not `aperture`
- Nikon may use `shutterspeed2` not `shutterspeed`
- The code probes for the correct key automatically in both getters and setters
