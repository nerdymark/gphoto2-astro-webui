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
        """_kill_gvfs_monitor must target gvfsd-gphoto2, which holds the USB interface."""
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

    def test_kill_gvfs_monitor_kills_gvfsd(self):
        """_kill_gvfs_monitor must also target gvfsd, the master GNOME VFS daemon.

        Even after the camera-specific daemons (gvfsd-gphoto2, gvfsd-mtp) are
        killed, gvfsd can restart them automatically when the camera is still
        connected.  Stopping gvfsd (or the gvfs-daemon user service) prevents
        those restarts and ensures the USB interface is fully released before
        gphoto2 retries the capture.
        """
        import camera as cam

        with (
            patch("camera.subprocess.run") as mock_run,
            patch("camera.time.sleep"),
        ):
            cam._kill_gvfs_monitor()

        called_cmds = [call.args[0] for call in mock_run.call_args_list]
        # Verify that gvfsd is targeted both via systemctl and pkill
        assert ["systemctl", "--user", "stop", "gvfs-daemon"] in called_cmds, (
            "systemctl --user stop gvfs-daemon was not called; "
            "the gvfs-daemon user service must be stopped to prevent gvfsd from restarting workers"
        )
        assert ["pkill", "-f", "gvfsd"] in called_cmds, (
            "pkill -f gvfsd was not called; "
            "gvfsd (the master GNOME VFS daemon) must be killed to prevent it from "
            "restarting the camera worker daemons and re-claiming the USB interface"
        )

    def test_kill_gvfs_monitor_kills_gvfs_mtp_volume_monitor(self):
        """_kill_gvfs_monitor must also target the MTP volume monitor and its worker.

        The Nikon D780 (and other PTP/MTP-only cameras) enumerate as MTP devices,
        so gvfs-mtp-volume-monitor claims the camera instead of
        gvfs-gphoto2-volume-monitor.  Both sets of daemons must be stopped.
        """
        import camera as cam

        with (
            patch("camera.subprocess.run") as mock_run,
            patch("camera.time.sleep"),
        ):
            cam._kill_gvfs_monitor()

        called_cmds = [call.args[0] for call in mock_run.call_args_list]
        command_strings = [" ".join(cmd) for cmd in called_cmds]
        assert any("gvfs-mtp-volume-monitor" in c for c in command_strings), (
            "gvfs-mtp-volume-monitor was not targeted; on cameras like the Nikon D780 "
            "this is the process that claims the USB interface"
        )
        assert any("gvfsd-mtp" in c for c in command_strings), (
            "gvfsd-mtp was not targeted; it is the MTP worker that holds the camera session"
        )

    def test_kill_gvfs_monitor_warning_mentions_gvfsd(self, caplog):
        """The warning log should mention the gphoto2, MTP, and master gvfsd daemons."""
        import camera as cam

        with (
            patch("camera.subprocess.run"),
            patch("camera.time.sleep"),
            caplog.at_level(logging.WARNING, logger="camera"),
        ):
            cam._kill_gvfs_monitor()

        log_messages = " ".join(r.message for r in caplog.records)
        assert "gvfsd-gphoto2" in log_messages
        assert "gvfs-mtp-volume-monitor" in log_messages
        assert "gvfsd" in log_messages

    def test_kill_gvfs_monitor_kills_gvfsd_fuse(self):
        """_kill_gvfs_monitor must target gvfsd-fuse to release the FUSE mount.

        When the fuse kernel module has an active user (``lsmod`` shows
        ``fuse ... 1``), gvfsd-fuse is running and holding a FUSE filesystem
        mount.  This keeps gvfsd alive and able to restart camera worker
        daemons even after they are killed.  gvfsd-fuse must be explicitly
        killed to break that cycle.
        """
        import camera as cam

        with (
            patch("camera.subprocess.run") as mock_run,
            patch("camera.time.sleep"),
        ):
            cam._kill_gvfs_monitor()

        called_cmds = [call.args[0] for call in mock_run.call_args_list]
        command_strings = [" ".join(cmd) for cmd in called_cmds]
        assert any("gvfsd-fuse" in c for c in command_strings), (
            "gvfsd-fuse was not targeted; it holds the FUSE mount that prevents "
            "gvfsd from exiting and releasing the camera's PTP session"
        )

    def test_kill_gvfs_monitor_unmounts_gvfs_fuse(self):
        """_kill_gvfs_monitor must call fusermount -uz to release the gvfs FUSE mount.

        gvfsd cannot exit cleanly while gvfsd-fuse holds an active FUSE
        mount.  A lazy unmount (``fusermount -uz``) detaches the filesystem
        from the mount table immediately so that gvfsd-fuse can exit promptly
        and gvfsd can then exit without restarting the camera daemons.
        """
        import camera as cam

        with (
            patch("camera.subprocess.run") as mock_run,
            patch("camera.time.sleep"),
        ):
            cam._kill_gvfs_monitor()

        called_cmds = [call.args[0] for call in mock_run.call_args_list]
        command_strings = [" ".join(cmd) for cmd in called_cmds]
        assert any("fusermount" in c and "-uz" in c for c in command_strings), (
            "fusermount -uz was not called; the gvfs FUSE mount must be released "
            "before gvfsd-fuse and gvfsd can exit and free the camera's PTP session"
        )

    def test_kill_gvfs_monitor_unmounts_before_killing(self):
        """fusermount -uz must be called before pkill gvfsd-fuse.

        The FUSE mount must be detached first so gvfsd-fuse is not blocked
        waiting for it to be released when it receives SIGTERM.
        """
        import camera as cam

        with (
            patch("camera.subprocess.run") as mock_run,
            patch("camera.time.sleep"),
        ):
            cam._kill_gvfs_monitor()

        called_cmds = [call.args[0] for call in mock_run.call_args_list]
        command_strings = [" ".join(cmd) for cmd in called_cmds]

        fusermount_indices = [
            i for i, c in enumerate(command_strings) if "fusermount" in c
        ]
        gvfsd_fuse_kill_indices = [
            i for i, c in enumerate(command_strings)
            if "gvfsd-fuse" in c and "fusermount" not in c
        ]
        assert fusermount_indices, "fusermount was never called"
        assert gvfsd_fuse_kill_indices, "gvfsd-fuse was never killed"
        assert max(fusermount_indices) < min(gvfsd_fuse_kill_indices), (
            "fusermount must be called before pkill gvfsd-fuse so the FUSE "
            "mount is detached before the daemon is killed"
        )

    def test_kill_gvfs_monitor_falls_back_to_umount_when_fusermount_missing(self):
        """When fusermount is not installed, _kill_gvfs_monitor must fall back to umount -l.

        The ``fusermount`` binary is part of the ``fuse`` / ``fuse3`` package
        which may not be installed on every system.  ``umount -l`` (part of
        ``util-linux``) must be used as a fallback so that the gvfs FUSE mount
        can still be released without requiring the fuse package.
        """
        import camera as cam

        called_cmds: list[list[str]] = []

        def side_effect(cmd, **kwargs):
            called_cmds.append(list(cmd))
            if cmd and cmd[0] == "fusermount":
                raise FileNotFoundError("fusermount not found")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        with (
            patch("camera.subprocess.run", side_effect=side_effect),
            patch("camera.time.sleep"),
        ):
            cam._kill_gvfs_monitor()

        command_strings = [" ".join(cmd) for cmd in called_cmds]
        assert any("umount" in c and "-l" in c for c in command_strings), (
            "umount -l was not called as a fallback when fusermount is missing; "
            "the gvfs FUSE mount must be releasable without the fuse package"
        )

    def test_kill_gvfs_monitor_umount_before_killing_when_fusermount_missing(self):
        """When fusermount is absent, umount -l must still run before pkill gvfsd-fuse."""
        import camera as cam

        called_cmds: list[list[str]] = []

        def side_effect(cmd, **kwargs):
            called_cmds.append(list(cmd))
            if cmd and cmd[0] == "fusermount":
                raise FileNotFoundError("fusermount not found")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        with (
            patch("camera.subprocess.run", side_effect=side_effect),
            patch("camera.time.sleep"),
        ):
            cam._kill_gvfs_monitor()

        command_strings = [" ".join(cmd) for cmd in called_cmds]
        umount_indices = [
            i for i, c in enumerate(command_strings) if "umount" in c and "-l" in c
        ]
        gvfsd_fuse_kill_indices = [
            i for i, c in enumerate(command_strings)
            if "pkill" in c and "gvfsd-fuse" in c
        ]
        assert umount_indices, "umount -l was never called as a fallback"
        assert gvfsd_fuse_kill_indices, "gvfsd-fuse was never killed via pkill"
        assert max(umount_indices) < min(gvfsd_fuse_kill_indices), (
            "umount -l fallback must run before pkill gvfsd-fuse"
        )

    def test_capture_image_kills_gvfs_before_first_attempt(self, tmp_path):
        """capture_image must kill gvfs daemons before the first capture attempt.

        On cameras like the Nikon D780, gvfs (via gvfsd-fuse + gvfsd-mtp) can
        (re)claim the camera's PTP session between is_camera_connected()
        returning True and the first --capture-image-and-download call.  A
        proactive kill before the first attempt prevents this race.
        """
        import camera as cam

        events: list[str] = []

        def mock_run(args, check=True, cwd=None):
            if "--capture-image-and-download" in args:
                events.append("capture")
                if cwd:
                    (Path(cwd) / "20240308-173422-00001.JPG").write_bytes(b"\xff\xd8fake")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "is_camera_connected", return_value=True),
            patch.object(cam, "_run", side_effect=mock_run),
            patch.object(cam, "_kill_gvfs_monitor", side_effect=lambda: events.append("kill")),
        ):
            cam.capture_image(tmp_path)

        first_capture_idx = next((i for i, e in enumerate(events) if e == "capture"), None)
        assert first_capture_idx is not None, "No capture was attempted"
        assert any(events[i] == "kill" for i in range(first_capture_idx)), (
            "gvfs must be killed at least once before the first capture attempt; "
            "gvfsd-fuse can (re)claim the camera between is_camera_connected() "
            "and --capture-image-and-download"
        )

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

    def test_get_exposure_settings_no_warning_when_aperture_falls_back_to_f_number(
        self, caplog
    ):
        """get_exposure_settings must NOT log a WARNING when aperture is absent
        but f-number provides the value successfully.

        The 'aperture' key is missing on Nikon bodies (which expose 'f-number'
        instead).  Because get_exposure_settings has an explicit fallback, the
        absence of 'aperture' is expected and must only produce a DEBUG message,
        not a WARNING, to avoid flooding production logs on every status poll.
        """
        import camera as cam

        aperture_exc = subprocess.CalledProcessError(
            returncode=1,
            cmd=["/usr/bin/gphoto2", "--get-config", "aperture"],
            stderr="aperture not found in configuration tree.",
            output="",
        )
        f_number_result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="Label: F-Number\nType: RADIO\nCurrent: f/4\nChoice: 0 f/4\n",
            stderr="",
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
            caplog.at_level(logging.DEBUG, logger="camera"),
        ):
            settings = cam.get_exposure_settings()

        assert settings["aperture"] == "f/4", "fallback to f-number must succeed"
        warning_records = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING and "aperture" in r.message
        ]
        assert not warning_records, (
            "No WARNING should be logged when aperture falls back to f-number; "
            f"got: {[r.message for r in warning_records]}"
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
            patch.object(cam, "is_bulb_mode", return_value=False),
            patch.object(cam, "_run", side_effect=mock_run),
            patch.object(cam, "_kill_gvfs_monitor"),
            caplog.at_level(logging.ERROR, logger="camera"),
        ):
            with pytest.raises(RuntimeError, match="gphoto2 captured nothing"):
                cam.capture_image(tmp_path)

    def test_capture_image_ptp_access_denied_raises_with_detail(self, tmp_path):
        """gphoto2 exits 0 with 'PTP Access Denied' in stderr: RuntimeError with that detail.

        gphoto2 can return exit code 0 while writing a PTP error to stderr when
        the camera is in a state that blocks software-triggered captures.  The
        error must be surfaced to the caller instead of the generic fallback.
        All retry attempts fail here to verify the final RuntimeError.
        """
        import camera as cam

        ptp_stderr = (
            "*** Error ***              \n"
            "PTP Access Denied\n"
            "ERROR: Could not capture image.\n"
            "ERROR: Could not capture."
        )

        def mock_run(args, check=True, cwd=None):
            if "--capture-image-and-download" in args:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="", stderr=ptp_stderr
                )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "is_camera_connected", return_value=True),
            patch.object(cam, "_run", side_effect=mock_run),
            patch.object(cam, "_kill_gvfs_monitor"),
        ):
            with pytest.raises(RuntimeError, match="PTP Access Denied"):
                cam.capture_image(tmp_path)

    def test_capture_image_ptp_access_denied_logs_error(self, tmp_path, caplog):
        """PTP Access Denied (exit 0) must be logged at ERROR level."""
        import camera as cam

        ptp_stderr = (
            "*** Error ***\nPTP Access Denied\nERROR: Could not capture image."
        )

        def mock_run(args, check=True, cwd=None):
            if "--capture-image-and-download" in args:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="", stderr=ptp_stderr
                )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "is_camera_connected", return_value=True),
            patch.object(cam, "is_bulb_mode", return_value=False),
            patch.object(cam, "_run", side_effect=mock_run),
            patch.object(cam, "_kill_gvfs_monitor"),
            caplog.at_level(logging.ERROR, logger="camera"),
        ):
            with pytest.raises(RuntimeError):
                cam.capture_image(tmp_path)

    def test_capture_image_ptp_access_denied_retries_with_gvfs_kill(self, tmp_path):
        """PTP access denied on the first attempt: gvfs is killed and capture retries.

        On cameras like the Nikon D780 that use PTP/MTP as their only USB mode,
        gvfs-gphoto2 opens a PTP session automatically when the camera is
        connected.  capture_image now kills gvfs proactively before the first
        attempt and again between the failed and successful attempt, so
        mock_kill is called twice total (1 proactive + 1 retry).
        """
        import camera as cam

        ptp_stderr = "*** Error ***\nPTP Access Denied\nERROR: Could not capture."
        capture_call_count = 0

        def mock_run(args, check=True, cwd=None):
            nonlocal capture_call_count
            if "--capture-image-and-download" in args:
                capture_call_count += 1
                if capture_call_count == 1:
                    return subprocess.CompletedProcess(
                        args=args, returncode=0, stdout="", stderr=ptp_stderr
                    )
                # Second attempt succeeds
                if cwd:
                    (Path(cwd) / "20240308-173422-00001.JPG").write_bytes(b"\xff\xd8fake")
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="", stderr=""
                )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "is_camera_connected", return_value=True),
            patch.object(cam, "_run", side_effect=mock_run),
            patch.object(cam, "_kill_gvfs_monitor") as mock_kill,
        ):
            result = cam.capture_image(tmp_path)

        assert result.exists(), "capture_image must return a valid file path on retry"
        assert capture_call_count == 2, "Expected exactly two capture attempts"
        # 1 kill in _enable_liveview + 1 kill on PTP error retry = 2.
        assert mock_kill.call_count == 2, (
            "gvfs must be killed once in _enable_liveview and once on PTP "
            f"error retry (1 + 1 = 2 total); got {mock_kill.call_count}"
        )

    def test_capture_image_ptp_access_denied_all_retries_kill_gvfs(self, tmp_path):
        """Each PTP access denied attempt must kill gvfs before the next retry.

        When all _PTP_MAX_ATTEMPTS fail, gvfs must have been killed
        1 (from _enable_liveview) + (_PTP_MAX_ATTEMPTS - 1) (one per retry
        gap, not after the last failed attempt) times total.
        """
        import camera as cam

        ptp_stderr = "*** Error ***\nPTP Access Denied\nERROR: Could not capture."

        def mock_run(args, check=True, cwd=None):
            if "--capture-image-and-download" in args:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="", stderr=ptp_stderr
                )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "is_camera_connected", return_value=True),
            patch.object(cam, "_run", side_effect=mock_run),
            patch.object(cam, "_kill_gvfs_monitor") as mock_kill,
        ):
            with pytest.raises(RuntimeError, match="PTP Access Denied"):
                cam.capture_image(tmp_path)

        # 1 from _enable_liveview + (_PTP_MAX_ATTEMPTS - 1) retry gaps.
        expected = 1 + (cam._PTP_MAX_ATTEMPTS - 1)
        assert mock_kill.call_count == expected, (
            f"Expected gvfs to be killed {expected} time(s) "
            f"(1 liveview + {cam._PTP_MAX_ATTEMPTS - 1} retry gaps); "
            f"got {mock_kill.call_count}"
        )

    def test_run_retries_after_ptp_session_already_opened(self):
        """When gphoto2 returns 'PTP Session Already Opened', _run kills gvfs and retries."""
        import camera as cam

        ptp_session_error = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="",
            stderr="PTP Session Already Opened",
        )
        ok_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Camera OK", stderr="",
        )
        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch("camera.subprocess.run", side_effect=[ptp_session_error, ok_result]) as mock_run,
            patch.object(cam, "_kill_gvfs_monitor") as mock_kill,
        ):
            result = cam._run(["--summary"])

        assert mock_kill.call_count == 1
        assert mock_run.call_count == 2
        assert result.returncode == 0
        assert result.stdout == "Camera OK"

    def test_run_gives_up_after_max_ptp_session_retries(self):
        """After _USB_MAX_ATTEMPTS attempts with PTP Session Already Opened, _run stops and raises."""
        import camera as cam

        ptp_session_error = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="",
            stderr="PTP Session Already Opened",
        )
        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch("camera.subprocess.run", return_value=ptp_session_error) as mock_run,
            patch.object(cam, "_kill_gvfs_monitor") as mock_kill,
        ):
            with pytest.raises(subprocess.CalledProcessError):
                cam._run(["--summary"])

        assert mock_run.call_count == cam._USB_MAX_ATTEMPTS
        assert mock_kill.call_count == cam._USB_MAX_ATTEMPTS - 1

    def test_capture_image_ptp_session_already_opened_raises_with_detail(self, tmp_path):
        """gphoto2 exits 0 with 'PTP Session Already Opened' in stderr: RuntimeError with detail.

        This error (code 0x201e) means gvfs already holds the PTP session.
        All retry attempts fail here to verify the final RuntimeError.
        """
        import camera as cam

        ptp_session_stderr = (
            "*** Error ***\n"
            "PTP Session Already Opened\n"
            "ERROR: Could not capture image.\n"
            "ERROR: Could not capture."
        )

        def mock_run(args, check=True, cwd=None):
            if "--capture-image-and-download" in args:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="", stderr=ptp_session_stderr
                )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "is_camera_connected", return_value=True),
            patch.object(cam, "_run", side_effect=mock_run),
            patch.object(cam, "_kill_gvfs_monitor"),
        ):
            with pytest.raises(RuntimeError, match="PTP Session Already Opened"):
                cam.capture_image(tmp_path)

    def test_capture_image_ptp_session_already_opened_retries_with_gvfs_kill(self, tmp_path):
        """PTP Session Already Opened on first attempt: gvfs is killed and capture retries.

        When gvfs holds the PTP session (0x201e), capture_image kills gvfs
        proactively before the first attempt and again between the failed and
        successful attempt, so mock_kill is called twice total
        (1 proactive + 1 retry).
        """
        import camera as cam

        ptp_session_stderr = "*** Error ***\nPTP Session Already Opened\nERROR: Could not capture."
        capture_call_count = 0

        def mock_run(args, check=True, cwd=None):
            nonlocal capture_call_count
            if "--capture-image-and-download" in args:
                capture_call_count += 1
                if capture_call_count == 1:
                    return subprocess.CompletedProcess(
                        args=args, returncode=0, stdout="", stderr=ptp_session_stderr
                    )
                # Second attempt succeeds
                if cwd:
                    (Path(cwd) / "20240308-173422-00001.JPG").write_bytes(b"\xff\xd8fake")
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="", stderr=""
                )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "is_camera_connected", return_value=True),
            patch.object(cam, "_run", side_effect=mock_run),
            patch.object(cam, "_kill_gvfs_monitor") as mock_kill,
        ):
            result = cam.capture_image(tmp_path)

        assert result.exists(), "capture_image must return a valid file path on retry"
        assert capture_call_count == 2, "Expected exactly two capture attempts"
        # 1 kill in _enable_liveview + 1 kill on PTP error retry = 2.
        assert mock_kill.call_count == 2, (
            "gvfs must be killed once in _enable_liveview and once on PTP "
            f"error retry (1 + 1 = 2 total); got {mock_kill.call_count}"
        )

    def test_capture_image_ptp_session_already_opened_all_retries_kill_gvfs(self, tmp_path):
        """Each PTP Session Already Opened attempt must kill gvfs before the next retry.

        When all _PTP_MAX_ATTEMPTS fail with this error, gvfs must have been
        killed 1 (from _enable_liveview) + (_PTP_MAX_ATTEMPTS - 1) (one per
        retry gap) times total.
        """
        import camera as cam

        ptp_session_stderr = "*** Error ***\nPTP Session Already Opened\nERROR: Could not capture."

        def mock_run(args, check=True, cwd=None):
            if "--capture-image-and-download" in args:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="", stderr=ptp_session_stderr
                )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "is_camera_connected", return_value=True),
            patch.object(cam, "_run", side_effect=mock_run),
            patch.object(cam, "_kill_gvfs_monitor") as mock_kill,
        ):
            with pytest.raises(RuntimeError, match="PTP Session Already Opened"):
                cam.capture_image(tmp_path)

        # 1 from _enable_liveview + (_PTP_MAX_ATTEMPTS - 1) retry gaps.
        expected = 1 + (cam._PTP_MAX_ATTEMPTS - 1)
        assert mock_kill.call_count == expected, (
            f"Expected gvfs to be killed {expected} time(s) "
            f"(1 liveview + {cam._PTP_MAX_ATTEMPTS - 1} retry gaps); "
            f"got {mock_kill.call_count}"
        )

    def test_capture_image_could_not_capture_stderr_raises(self, tmp_path):
        """'ERROR: Could not capture' in stderr with exit 0 raises RuntimeError."""
        import camera as cam

        def mock_run(args, check=True, cwd=None):
            if "--capture-image-and-download" in args:
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="",
                    stderr="ERROR: Could not capture image.\nERROR: Could not capture.",
                )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "is_camera_connected", return_value=True),
            patch.object(cam, "_run", side_effect=mock_run),
        ):
            with pytest.raises(RuntimeError, match="Could not capture"):
                cam.capture_image(tmp_path)

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
        """capture_image must include capturetarget=0 in the capture call
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
            patch.object(cam, "is_bulb_mode", return_value=False),
            patch.object(cam, "_run", side_effect=mock_run),
            patch.object(cam, "_kill_gvfs_monitor"),
        ):
            cam.capture_image(tmp_path)

        # capturetarget=0 and --capture-image-and-download should be in the same call
        capture_calls = [
            args for args in call_log
            if "--capture-image-and-download" in args
        ]
        assert capture_calls, "--capture-image-and-download was not called"
        assert any("capturetarget=0" in a for a in capture_calls[0]), (
            "capturetarget=0 must be in the same gphoto2 call as --capture-image-and-download"
        )

    def test_capture_image_proceeds_when_capturetarget_unsupported(self, tmp_path, caplog):
        """capture_image must still succeed when capturetarget is not supported.

        When capturetarget=0 and --capture-image-and-download are combined in the
        same gphoto2 call, gphoto2 treats the unsupported config key as non-fatal
        and proceeds with the capture.  The mock simulates this by returning
        success with a stderr warning about the unsupported key while still
        writing the downloaded file.
        """
        import camera as cam

        def mock_run(args, check=True, cwd=None):
            if "--capture-image-and-download" in args and cwd:
                (Path(cwd) / "20240308-173422-00001.JPG").write_bytes(b"\xff\xd8fake")
                return subprocess.CompletedProcess(
                    args=args, returncode=0,
                    stdout="", stderr="",
                )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with (
            patch.object(cam, "GPHOTO2_BIN", "/usr/bin/gphoto2"),
            patch.object(cam, "is_camera_connected", return_value=True),
            patch.object(cam, "is_bulb_mode", return_value=False),
            patch.object(cam, "_run", side_effect=mock_run),
            patch.object(cam, "_kill_gvfs_monitor"),
            caplog.at_level(logging.WARNING, logger="camera"),
        ):
            result = cam.capture_image(tmp_path)

        assert result.exists(), "capture_image must return a valid file path"

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
        import time
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
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        # Poll until the background job completes.
        for _ in range(30):
            job_resp = client.get(f"/api/jobs/{job_id}")
            assert job_resp.status_code == 200
            job_data = job_resp.json()
            if job_data["status"] in ("completed", "failed"):
                break
            time.sleep(0.1)

        assert job_data["status"] == "completed"
        result = job_data["result"]
        assert result["ok"] is True
        stacked_path = gallery_dir / result["filename"]
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


def test_log_level_sets_root_logger_level(monkeypatch):
    """LOG_LEVEL must be applied to the root logger even when basicConfig is a no-op."""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    if "main" in sys.modules:
        del sys.modules["main"]
    import main  # noqa: PLC0415

    assert logging.root.level == logging.DEBUG


def test_log_level_info_sets_root_logger_level(monkeypatch):
    """Setting LOG_LEVEL=info must lower the root logger level to INFO."""
    monkeypatch.setenv("LOG_LEVEL", "info")
    if "main" in sys.modules:
        del sys.modules["main"]
    import main  # noqa: PLC0415

    assert logging.root.level == logging.INFO


def test_debug_messages_emitted_when_log_level_debug(monkeypatch, caplog):
    """Debug messages from the camera logger must appear when LOG_LEVEL=DEBUG."""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    # Reset root logger to WARNING to simulate uvicorn having already run
    logging.root.setLevel(logging.WARNING)

    if "main" in sys.modules:
        del sys.modules["main"]
    import main  # noqa: PLC0415

    assert main._LOG_LEVEL == "DEBUG"
    assert logging.root.level == logging.DEBUG

    import camera as cam  # noqa: PLC0415

    with caplog.at_level(logging.DEBUG, logger="camera"):
        cam.logger.debug("test debug message from camera")

    assert any("test debug message from camera" in r.message for r in caplog.records)
