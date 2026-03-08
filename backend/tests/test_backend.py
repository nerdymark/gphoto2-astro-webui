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

    def test_kill_gvfs_monitor_kills_gvfsd_gphoto2(self):
        """_kill_gvfs_monitor must also kill gvfsd-gphoto2, which holds the USB interface."""
        import camera as cam

        with (
            patch("camera.subprocess.run") as mock_run,
            patch("camera.time.sleep"),
        ):
            cam._kill_gvfs_monitor()

        # Collect all commands that were attempted
        called_cmds = [call.args[0] for call in mock_run.call_args_list]
        command_strings = [" ".join(cmd) for cmd in called_cmds]
        assert any("gvfsd-gphoto2" in c for c in command_strings), (
            "gvfsd-gphoto2 was not targeted; it holds the USB interface and must be stopped"
        )

    def test_kill_gvfs_monitor_warning_mentions_gvfsd(self, caplog):
        """The warning log should mention gvfsd-gphoto2 so operators know what was stopped."""
        import camera as cam

        with (
            patch("camera.subprocess.run"),
            patch("camera.time.sleep"),
            caplog.at_level(logging.WARNING, logger="camera"),
        ):
            cam._kill_gvfs_monitor()

        log_messages = " ".join(r.message for r in caplog.records)
        assert "gvfsd-gphoto2" in log_messages

    def test_run_gives_up_after_max_usb_retries(self):
        """After _USB_MAX_ATTEMPTS failed attempts, _run stops retrying and raises."""
        import camera as cam

        usb_error = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="",
            stderr="Could not claim the USB device",
        )
        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch("camera.subprocess.run", return_value=usb_error) as mock_run,
            patch.object(cam, "_kill_gvfs_monitor") as mock_kill,
        ):
            with pytest.raises(subprocess.CalledProcessError):
                cam._run(["--summary"])

        # With _USB_MAX_ATTEMPTS=3: runs 3 times, kills after attempts 0 and 1
        assert mock_run.call_count == cam._USB_MAX_ATTEMPTS
        assert mock_kill.call_count == cam._USB_MAX_ATTEMPTS - 1

    def test_run_serializes_concurrent_calls(self):
        """Concurrent _run calls must be serialized so only one gphoto2 runs at a time."""
        import camera as cam
        import threading as thr

        active_count = [0]
        max_concurrent = [0]
        counter_lock = thr.Lock()

        def tracked_run(cmd, **kwargs):
            with counter_lock:
                active_count[0] += 1
                max_concurrent[0] = max(max_concurrent[0], active_count[0])
            time.sleep(0.01)
            with counter_lock:
                active_count[0] -= 1
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="OK", stderr="")

        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch("camera.subprocess.run", side_effect=tracked_run),
        ):
            threads = [thr.Thread(target=cam._run, args=(["--summary"],)) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # The lock ensures gphoto2 is never called concurrently
        assert max_concurrent[0] == 1

    def test_run_passes_cwd_to_subprocess(self, tmp_path):
        """_run must forward its cwd argument to subprocess.run."""
        import camera as cam

        received_cwd = []

        def tracking_run(cmd, **kwargs):
            received_cwd.append(kwargs.get("cwd"))
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="OK", stderr="")

        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch("camera.subprocess.run", side_effect=tracking_run),
        ):
            cam._run(["--summary"], cwd=str(tmp_path))

        assert received_cwd == [str(tmp_path)]

    def test_get_config_returns_value_and_choices(self):
        """_get_config parses both current value and choices in one gphoto2 call."""
        import camera as cam

        output = (
            "Label: ISO Speed\nType: RADIO\n"
            "Current: 800\n"
            "Choice: 0 100\nChoice: 1 200\nChoice: 2 400\nChoice: 3 800\n"
        )
        ok_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=output, stderr=""
        )
        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "_run", return_value=ok_result),
        ):
            value, choices = cam._get_config("iso")

        assert value == "800"
        assert choices == ["100", "200", "400", "800"]

    def test_get_config_success_logs_debug(self, caplog):
        """_get_config must log the parsed value and choices at DEBUG level on success."""
        import camera as cam

        output = (
            "Label: ISO Speed\nType: RADIO\n"
            "Current: 800\n"
            "Choice: 0 100\nChoice: 1 200\nChoice: 2 400\nChoice: 3 800\n"
        )
        ok_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=output, stderr=""
        )
        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "_run", return_value=ok_result),
            caplog.at_level(logging.DEBUG, logger="camera"),
        ):
            cam._get_config("iso")

        debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("iso" in m for m in debug_messages), (
            "Expected a DEBUG log from _get_config containing the key name"
        )
        assert any("800" in m for m in debug_messages), (
            "Expected a DEBUG log from _get_config containing the parsed value"
        )

    def test_get_config_error_returns_none_and_empty(self, caplog):
        """_get_config returns (None, []) and logs an error when gphoto2 fails
        with a non-'not found' error (e.g. a USB communication failure)."""
        import camera as cam

        fake_exc = subprocess.CalledProcessError(
            returncode=1,
            cmd=["/usr/bin/gphoto2", "--get-config", "iso"],
            stderr="*** Error (-53: 'Could not claim the USB device') ***",
            output="",
        )
        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "_run", side_effect=fake_exc),
            caplog.at_level(logging.ERROR, logger="camera"),
        ):
            value, choices = cam._get_config("iso")

        assert value is None
        assert choices == []
        assert any("iso" in r.message for r in caplog.records)

    def test_get_exposure_settings_falls_back_to_f_number(self):
        """get_exposure_settings uses f-number when aperture is not found."""
        import camera as cam

        aperture_exc = subprocess.CalledProcessError(
            returncode=1,
            cmd=["/usr/bin/gphoto2", "--get-config", "aperture"],
            stderr="aperture not found in configuration tree.",
            output="",
        )
        f_number_output = (
            "Label: F-Number\nType: RADIO\n"
            "Current: f/4\n"
            "Choice: 0 f/2.8\nChoice: 1 f/4\n"
        )
        f_number_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f_number_output, stderr=""
        )
        generic_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Current: 1/100\n", stderr=""
        )

        def mock_run(args, **kwargs):
            if "--get-config" in args:
                key = args[args.index("--get-config") + 1]
                if key == "aperture":
                    raise aperture_exc
                if key == "f-number":
                    return f_number_result
            return generic_result

        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "_run", side_effect=mock_run),
        ):
            settings = cam.get_exposure_settings()

        assert settings["aperture"] == "f/4"
        assert "f/2.8" in settings["aperture_choices"]

    def test_get_exposure_settings_no_duplicate_aperture_calls_on_failure(self):
        """When aperture is unsupported, _get_config must NOT be called a second time
        for the same key when collecting choices (single combined call per key)."""
        import camera as cam

        call_log: list[str] = []

        aperture_exc = subprocess.CalledProcessError(
            returncode=1,
            cmd=["/usr/bin/gphoto2", "--get-config", "aperture"],
            stderr="aperture not found in configuration tree.",
            output="",
        )
        generic_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Current: 1/100\n", stderr=""
        )

        def mock_run(args, **kwargs):
            if "--get-config" in args:
                key = args[args.index("--get-config") + 1]
                call_log.append(key)
                if key == "aperture":
                    raise aperture_exc
            return generic_result

        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "_run", side_effect=mock_run),
        ):
            cam.get_exposure_settings()

        # "aperture" must appear exactly once – the new _get_config() helper
        # retrieves value and choices in a single call.
        assert call_log.count("aperture") == 1, (
            f"aperture was queried {call_log.count('aperture')} time(s); expected 1. "
            f"Full call log: {call_log}"
        )

    def test_capture_image_uses_cwd_for_download(self, tmp_path):
        """capture_image must pass cwd=tmpdir to _run so the downloaded file lands
        in the temporary directory, not in the server's working directory."""
        import camera as cam

        captured_cwds: list = []

        def mock_run(args, check=True, cwd=None):
            captured_cwds.append(cwd)
            if "--capture-image-and-download" in args and cwd:
                # Simulate gphoto2 creating a file in cwd
                (Path(cwd) / "20240308-173422-00001.JPG").write_bytes(b"\xff\xd8fake")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "is_camera_connected", return_value=True),
            patch.object(cam, "_run", side_effect=mock_run),
        ):
            result = cam.capture_image(tmp_path)

        # File should have been moved from tmpdir into gallery_path
        assert result.exists()
        assert result.parent == tmp_path
        # At least one _run call must have received a non-None cwd
        assert any(cwd is not None for cwd in captured_cwds), (
            "capture_image did not pass cwd to _run; file may land in the wrong directory"
        )

    def test_capture_image_gphoto2_nothing_logs_error(self, tmp_path, caplog):
        """When gphoto2 exits 0 but downloads nothing, an error must be logged."""
        import camera as cam

        def mock_run(args, check=True, cwd=None):
            # Do NOT write any file – simulate gphoto2 succeeding but not downloading
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "is_camera_connected", return_value=True),
            patch.object(cam, "_run", side_effect=mock_run),
            caplog.at_level(logging.ERROR, logger="camera"),
        ):
            with pytest.raises(RuntimeError, match="gphoto2 captured nothing"):
                cam.capture_image(tmp_path)

        assert any("no file was downloaded" in r.message for r in caplog.records)

    def test_get_config_not_found_logs_warning_not_error(self, caplog):
        """'not found in configuration tree' should be a WARNING, not an ERROR.

        Cameras with manual lenses routinely lack the 'aperture' config key;
        flooding the log with ERROR-level messages would be misleading.
        """
        import camera as cam

        fake_exc = subprocess.CalledProcessError(
            returncode=1,
            cmd=["/usr/bin/gphoto2", "--get-config", "aperture"],
            stderr="*** Error ***\naperture not found in configuration tree.\n*** Error (-1: 'Unspecified error') ***",
            output="",
        )
        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "_run", side_effect=fake_exc),
            caplog.at_level(logging.WARNING, logger="camera"),
        ):
            value, choices = cam._get_config("aperture")

        assert value is None
        assert choices == []
        # Must be logged at WARNING, not ERROR
        aperture_records = [r for r in caplog.records if "aperture" in r.message]
        assert aperture_records, "Expected at least one log record mentioning 'aperture'"
        assert all(r.levelno < logging.ERROR for r in aperture_records), (
            "Expected WARNING (not ERROR) for 'not found in configuration tree'"
        )

    def test_capture_image_sets_capturetarget_before_download(self, tmp_path):
        """capture_image must set capturetarget=0 before --capture-image-and-download
        so images are downloaded to the host rather than saved only to the camera card."""
        import camera as cam

        call_log: list[list[str]] = []

        def mock_run(args, check=True, cwd=None):
            call_log.append(list(args))
            if "--capture-image-and-download" in args and cwd:
                (Path(cwd) / "20240308-173422-00001.JPG").write_bytes(b"\xff\xd8fake")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "is_camera_connected", return_value=True),
            patch.object(cam, "_run", side_effect=mock_run),
        ):
            cam.capture_image(tmp_path)

        # capturetarget=0 must appear in an argument list before the capture call
        capturetarget_indices = [
            i for i, args in enumerate(call_log)
            if any("capturetarget=0" in a for a in args)
        ]
        capture_indices = [
            i for i, args in enumerate(call_log)
            if "--capture-image-and-download" in args
        ]
        assert capturetarget_indices, "capturetarget=0 was never set before capture"
        assert capture_indices, "--capture-image-and-download was not called"
        assert min(capturetarget_indices) < min(capture_indices), (
            "capturetarget=0 must be set BEFORE --capture-image-and-download"
        )

    def test_capture_image_proceeds_when_capturetarget_unsupported(self, tmp_path, caplog):
        """capture_image must still succeed when capturetarget is not supported.

        Some cameras do not expose a capturetarget config key; the failure must
        be logged as a WARNING and capture must continue.
        """
        import camera as cam

        def mock_run(args, check=True, cwd=None):
            if any("capturetarget=0" in a for a in args):
                return subprocess.CompletedProcess(
                    args=args, returncode=1,
                    stdout="", stderr="capturetarget not found in configuration tree.",
                )
            if "--capture-image-and-download" in args and cwd:
                (Path(cwd) / "20240308-173422-00001.JPG").write_bytes(b"\xff\xd8fake")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "is_camera_connected", return_value=True),
            patch.object(cam, "_run", side_effect=mock_run),
            caplog.at_level(logging.WARNING, logger="camera"),
        ):
            result = cam.capture_image(tmp_path)

        assert result.exists(), "capture_image must return a valid file path"
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("capturetarget" in m for m in warning_messages), (
            "Expected a WARNING about capturetarget not being set"
        )

    def test_list_config_keys_no_binary(self):
        """list_config_keys returns [] when gphoto2 is not available."""
        import camera as cam

        with patch.object(cam, "GPHOTO2_BIN", None):
            assert cam.list_config_keys() == []

    def test_list_config_keys_returns_paths(self):
        """list_config_keys parses each non-empty line from --list-config output."""
        import camera as cam

        output = (
            "/main/imgsettings/iso\n"
            "/main/imgsettings/whitebalance\n"
            "/main/capturesettings/shutterspeed\n"
        )
        ok_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=output, stderr=""
        )
        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "_run", return_value=ok_result),
        ):
            keys = cam.list_config_keys()

        assert keys == [
            "/main/imgsettings/iso",
            "/main/imgsettings/whitebalance",
            "/main/capturesettings/shutterspeed",
        ]

    def test_list_config_keys_error_returns_empty(self, caplog):
        """list_config_keys logs an error and returns [] when gphoto2 fails."""
        import camera as cam

        fake_exc = subprocess.CalledProcessError(
            returncode=1,
            cmd=["/usr/bin/gphoto2", "--list-config"],
            stderr="*** Error (-53: 'Could not claim the USB device')",
            output="",
        )
        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "_run", side_effect=fake_exc),
            caplog.at_level(logging.ERROR, logger="camera"),
        ):
            keys = cam.list_config_keys()

        assert keys == []
        assert any("list_config_keys" in r.message for r in caplog.records)

    def test_run_logs_debug_command(self, caplog):
        """_run must log the command at DEBUG level before invoking subprocess."""
        import camera as cam

        ok_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Camera OK", stderr=""
        )
        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch("camera.subprocess.run", return_value=ok_result),
            caplog.at_level(logging.DEBUG, logger="camera"),
        ):
            cam._run(["--summary"])

        debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("--summary" in m for m in debug_messages), (
            "Expected a DEBUG log containing the gphoto2 command arguments"
        )

    def test_capture_image_logs_debug_output(self, tmp_path, caplog):
        """capture_image must log gphoto2 stdout/stderr at DEBUG level after capture."""
        import camera as cam

        def mock_run(args, check=True, cwd=None):
            if "--capture-image-and-download" in args and cwd:
                (Path(cwd) / "20240308-173422-00001.JPG").write_bytes(b"\xff\xd8fake")
            return subprocess.CompletedProcess(
                args=args, returncode=0,
                stdout="New file is in location /store_00010001/DCIM/100CANON/IMG_0001.CR3",
                stderr="",
            )

        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "is_camera_connected", return_value=True),
            patch.object(cam, "_run", side_effect=mock_run),
            caplog.at_level(logging.DEBUG, logger="camera"),
        ):
            cam.capture_image(tmp_path)

        debug_messages = " ".join(r.message for r in caplog.records if r.levelno == logging.DEBUG)
        assert "capture_image" in debug_messages, (
            "Expected DEBUG log entries from capture_image"
        )


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

    def test_list_config_keys_endpoint(self, client):
        import camera as cam

        with patch.object(cam, "GPHOTO2_BIN", None):
            resp = client.get("/api/camera/config-keys")
        assert resp.status_code == 200
        data = resp.json()
        assert "keys" in data
        assert isinstance(data["keys"], list)

    def test_list_config_keys_endpoint_returns_keys(self, client):
        import camera as cam

        ok_result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="/main/imgsettings/iso\n/main/capturesettings/shutterspeed\n",
            stderr="",
        )
        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "_run", return_value=ok_result),
        ):
            resp = client.get("/api/camera/config-keys")

        assert resp.status_code == 200
        data = resp.json()
        assert "/main/imgsettings/iso" in data["keys"]
        assert "/main/capturesettings/shutterspeed" in data["keys"]

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


# ---------------------------------------------------------------------------
# LOG_LEVEL env var tests
# ---------------------------------------------------------------------------


def test_log_level_env_var_defaults_to_info(monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    if "main" in sys.modules:
        del sys.modules["main"]
    import main  # noqa: PLC0415

    assert main._LOG_LEVEL == "INFO"


def test_log_level_env_var_debug(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    if "main" in sys.modules:
        del sys.modules["main"]
    import main  # noqa: PLC0415

    assert main._LOG_LEVEL == "DEBUG"


def test_log_level_env_var_lowercase_is_uppercased(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")
    if "main" in sys.modules:
        del sys.modules["main"]
    import main  # noqa: PLC0415

    assert main._LOG_LEVEL == "DEBUG"


def test_log_level_invalid_value_warns(monkeypatch, caplog):
    monkeypatch.setenv("LOG_LEVEL", "TRACE")
    if "main" in sys.modules:
        del sys.modules["main"]
    with caplog.at_level(logging.WARNING, logger="main"):
        import main  # noqa: PLC0415

    assert main._LOG_LEVEL == "TRACE"
    assert any("TRACE" in r.message for r in caplog.records)
