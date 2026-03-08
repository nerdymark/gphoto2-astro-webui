"""
Tests for the FastAPI backend.

Run with:  cd backend && pytest tests/
"""

import io
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the backend package is importable from tests/ subdirectory
sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# camera module tests
# ---------------------------------------------------------------------------


class TestCameraModule:
    def test_is_camera_connected_no_binary(self):
        import camera as cam

        with patch.object(cam, "GPHOTO2_BIN", None):
            assert cam.is_camera_connected() is False

    def test_get_camera_summary_no_binary(self):
        import camera as cam

        with patch.object(cam, "GPHOTO2_BIN", None):
            result = cam.get_camera_summary()
        assert result["connected"] is False

    def test_set_exposure_no_binary(self):
        import camera as cam

        with patch.object(cam, "GPHOTO2_BIN", None):
            result = cam.set_exposure_settings(iso="800")
        assert result["ok"] is True

    def test_simulate_capture_creates_file(self, tmp_path):
        import camera as cam

        dst = cam._simulate_capture(tmp_path)
        assert dst.exists()
        assert dst.suffix.lower() == ".jpg"
        assert dst.stat().st_size > 0

    def test_capture_image_simulation_fallback(self, tmp_path):
        import camera as cam

        with patch.object(cam, "GPHOTO2_BIN", None):
            saved = cam.capture_image(tmp_path)
        assert saved.exists()

    def test_get_camera_summary_gphoto2_error_logs_stderr(self):
        """CalledProcessError from gphoto2 --summary should log stderr and return connected=False."""
        import camera as cam

        fake_exc = subprocess.CalledProcessError(
            returncode=1,
            cmd=["/usr/bin/gphoto2", "--summary"],
            stderr="*** Error (-53: 'Could not claim the USB device')",
            output="",
        )
        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "is_camera_connected", return_value=True),
            patch.object(cam, "_run", side_effect=fake_exc),
        ):
            result = cam.get_camera_summary()

        assert result["connected"] is False
        assert "Could not claim the USB device" in result["summary"]

    def test_get_config_value_usb_error_logs_stderr(self, caplog):
        """CalledProcessError from --get-config should be logged, not silently swallowed."""
        import camera as cam

        fake_exc = subprocess.CalledProcessError(
            returncode=1,
            cmd=["/usr/bin/gphoto2", "--get-config", "iso"],
            stderr="*** Error (-53: 'Could not claim the USB device')",
            output="",
        )
        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "_run", side_effect=fake_exc),
            caplog.at_level(logging.ERROR, logger="camera"),
        ):
            result = cam._get_config_value("iso")

        assert result is None
        assert any("Could not claim the USB device" in r.message for r in caplog.records)

    def test_get_config_choices_usb_error_logs_stderr(self, caplog):
        """CalledProcessError from --get-config (choices) should be logged, not silently swallowed."""
        import camera as cam

        fake_exc = subprocess.CalledProcessError(
            returncode=1,
            cmd=["/usr/bin/gphoto2", "--get-config", "shutterspeed"],
            stderr="*** Error (-53: 'Could not claim the USB device')",
            output="",
        )
        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "_run", side_effect=fake_exc),
            caplog.at_level(logging.ERROR, logger="camera"),
        ):
            result = cam._get_config_choices("shutterspeed")

        assert result == []
        assert any("Could not claim the USB device" in r.message for r in caplog.records)

    def test_run_retries_after_usb_claim_error(self, caplog):
        """When gphoto2 returns a USB claim error, _run kills gvfs and retries."""
        import camera as cam

        usb_error = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="",
            stderr="Could not claim the USB device",
        )
        ok_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Camera OK", stderr="",
        )
        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch("camera.subprocess.run", side_effect=[usb_error, ok_result]) as mock_run,
            patch.object(cam, "_kill_gvfs_monitor") as mock_kill,
            patch("camera.time.sleep"),
            caplog.at_level(logging.WARNING, logger="camera"),
        ):
            result = cam._run(["--summary"])

        assert mock_kill.call_count == 1
        assert mock_run.call_count == 2
        assert result.returncode == 0
        assert result.stdout == "Camera OK"

    def test_kill_gvfs_monitor_swallows_errors(self):
        """_kill_gvfs_monitor should not raise even if all sub-commands fail."""
        import camera as cam

        with (
            patch("camera.subprocess.run", side_effect=FileNotFoundError("not found")),
            patch("camera.time.sleep"),
        ):
            cam._kill_gvfs_monitor()  # must not raise


# ---------------------------------------------------------------------------
# stacking module tests
# ---------------------------------------------------------------------------


class TestStackingModule:
    def _make_image(self, tmp_path, name: str, color=(100, 100, 100)):
        from PIL import Image

        img = Image.new("RGB", (64, 64), color=color)
        path = tmp_path / name
        img.save(str(path), format="JPEG")
        return path

    def test_mean_stack(self, tmp_path):
        import stacking

        paths = [
            self._make_image(tmp_path, "a.jpg", (100, 100, 100)),
            self._make_image(tmp_path, "b.jpg", (200, 200, 200)),
        ]
        result = stacking.stack_images(paths, mode="mean")
        import numpy as np

        arr = np.array(result)
        # Mean of 100 and 200 is 150 (JPEG compression adds ~±10 tolerance)
        assert abs(int(arr[0, 0, 0]) - 150) <= 15

    def test_median_stack(self, tmp_path):
        import stacking

        paths = [
            self._make_image(tmp_path, "a.jpg", (50, 50, 50)),
            self._make_image(tmp_path, "b.jpg", (100, 100, 100)),
            self._make_image(tmp_path, "c.jpg", (200, 200, 200)),
        ]
        result = stacking.stack_images(paths, mode="median")
        import numpy as np

        arr = np.array(result)
        assert abs(int(arr[0, 0, 0]) - 100) <= 15

    def test_sum_stack_normalized(self, tmp_path):
        import stacking

        paths = [
            self._make_image(tmp_path, "a.jpg", (100, 100, 100)),
            self._make_image(tmp_path, "b.jpg", (100, 100, 100)),
        ]
        result = stacking.stack_images(paths, mode="sum")
        import numpy as np

        arr = np.array(result)
        # Sum mode normalises to 255 max
        assert arr.max() == 255

    def test_requires_at_least_two_images(self, tmp_path):
        import stacking

        path = self._make_image(tmp_path, "only.jpg")
        with pytest.raises(ValueError, match="At least 2"):
            stacking.stack_images([path], mode="mean")

    def test_unknown_mode_raises(self, tmp_path):
        import stacking

        paths = [
            self._make_image(tmp_path, "a.jpg"),
            self._make_image(tmp_path, "b.jpg"),
        ]
        with pytest.raises(ValueError, match="Unknown stacking mode"):
            stacking.stack_images(paths, mode="magic")  # type: ignore

    def test_mismatched_sizes_get_resized(self, tmp_path):
        """Images with different sizes should be resized and not raise."""
        from PIL import Image
        import stacking

        img_a = Image.new("RGB", (64, 64), (100, 100, 100))
        img_a.save(str(tmp_path / "a.jpg"), format="JPEG")
        img_b = Image.new("RGB", (128, 128), (200, 200, 200))
        img_b.save(str(tmp_path / "b.jpg"), format="JPEG")
        result = stacking.stack_images(
            [tmp_path / "a.jpg", tmp_path / "b.jpg"], mode="mean"
        )
        assert result.size == (64, 64)


# ---------------------------------------------------------------------------
# FastAPI endpoint tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Return a TestClient with the gallery root pointing at tmp_path."""
    monkeypatch.setenv("GALLERY_ROOT", str(tmp_path))

    # Re-import main so GALLERY_ROOT is picked up
    if "main" in sys.modules:
        del sys.modules["main"]

    import main  # noqa: PLC0415

    main.GALLERY_ROOT = tmp_path

    from fastapi.testclient import TestClient

    return TestClient(main.app)


class TestAPI:
    def test_camera_status(self, client):
        resp = client.get("/api/camera/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "connected" in data

    def test_get_exposure(self, client):
        resp = client.get("/api/camera/exposure")
        assert resp.status_code == 200

    def test_set_exposure(self, client):
        import camera as cam

        with patch.object(cam, "GPHOTO2_BIN", None):
            resp = client.post(
                "/api/camera/exposure",
                json={"aperture": "f/4", "shutter": "1/100", "iso": "800"},
            )
        assert resp.status_code == 200

    def test_create_gallery(self, client, tmp_path):
        import main

        main.GALLERY_ROOT = tmp_path
        resp = client.post("/api/galleries", json={"name": "My Stars"})
        assert resp.status_code == 201
        assert resp.json()["name"] == "My Stars"

    def test_create_gallery_empty_name(self, client):
        resp = client.post("/api/galleries", json={"name": "   "})
        assert resp.status_code == 400

    def test_list_galleries_empty(self, client):
        resp = client.get("/api/galleries")
        assert resp.status_code == 200
        assert resp.json()["galleries"] == []

    def test_capture_and_list(self, client, tmp_path):
        import main
        import camera as cam

        main.GALLERY_ROOT = tmp_path
        # Create gallery first
        client.post("/api/galleries", json={"name": "orion"})
        # Capture (simulated)
        with patch.object(cam, "GPHOTO2_BIN", None):
            resp = client.post("/api/camera/capture", json={"gallery": "orion"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["filename"].endswith(".jpg")

        # List images
        resp = client.get("/api/galleries/orion")
        assert resp.status_code == 200
        assert len(resp.json()["images"]) == 1

    def test_stack_images(self, client, tmp_path):
        from PIL import Image
        import main
        import camera as cam

        main.GALLERY_ROOT = tmp_path
        client.post("/api/galleries", json={"name": "stack_test"})
        gallery_dir = tmp_path / "stack_test"

        # Create two dummy JPEG images
        for name, color in [("img1.jpg", (100, 100, 100)), ("img2.jpg", (200, 200, 200))]:
            img = Image.new("RGB", (64, 64), color=color)
            img.save(str(gallery_dir / name), format="JPEG")

        resp = client.post(
            "/api/galleries/stack_test/stack",
            json={"images": ["img1.jpg", "img2.jpg"], "mode": "mean"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        stacked_path = gallery_dir / data["filename"]
        assert stacked_path.exists()

    def test_delete_image(self, client, tmp_path):
        from PIL import Image
        import main

        main.GALLERY_ROOT = tmp_path
        client.post("/api/galleries", json={"name": "del_test"})
        gallery_dir = tmp_path / "del_test"
        img = Image.new("RGB", (8, 8), (0, 0, 0))
        img.save(str(gallery_dir / "to_delete.jpg"), format="JPEG")

        resp = client.delete("/api/galleries/del_test/to_delete.jpg")
        assert resp.status_code == 200
        assert not (gallery_dir / "to_delete.jpg").exists()

    def test_serve_image(self, client, tmp_path):
        from PIL import Image
        import main

        main.GALLERY_ROOT = tmp_path
        client.post("/api/galleries", json={"name": "serve_test"})
        gallery_dir = tmp_path / "serve_test"
        img = Image.new("RGB", (8, 8), (255, 0, 0))
        img.save(str(gallery_dir / "red.jpg"), format="JPEG")

        resp = client.get("/api/images/serve_test/red.jpg")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/")
