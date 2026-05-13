"""Unit tests for go2rtc.py -- config-file read/write/merge."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml

from custom_components.aqara_camera_2_way_audio import go2rtc as go2rtc_mod
from custom_components.aqara_camera_2_way_audio.go2rtc import (
    build_exec_source,
    build_rtsp_source,
    register_stream,
    remove_stream,
)


class TestBuildRtspSource:
    """build_rtsp_source URL-encodes credentials and inserts the channel."""

    def test_default_channel_is_1(self) -> None:
        url = build_rtsp_source("10.0.0.1", "user", "pw")
        assert url == "rtsp://user:pw@10.0.0.1:8554/ch1"

    def test_channel_in_path(self) -> None:
        url = build_rtsp_source("10.0.0.1", "user", "pw", channel=3)
        assert url == "rtsp://user:pw@10.0.0.1:8554/ch3"

    def test_credentials_url_encoded(self) -> None:
        url = build_rtsp_source("10.0.0.1", "a@b", "p:w", channel=2)
        assert url == "rtsp://a%40b:p%3Aw@10.0.0.1:8554/ch2"


class TestBuildExecSource:
    """build_exec_source points at bridge.py in the new package path."""

    def test_contains_new_package_path(self) -> None:
        src = build_exec_source("/config", "10.0.0.1")
        assert "aqara_camera_2_way_audio/bridge.py" in src
        assert "aqara_doorbell" not in src
        assert "10.0.0.1#backchannel=1" in src


class TestRegisterStream:
    """register_stream creates or merges into go2rtc.yaml."""

    def test_creates_file_when_absent(self, tmp_path: Path) -> None:
        ok = register_stream(str(tmp_path), "aqara_camera_10_0_0_1", ["rtsp://x", "exec:y"])
        assert ok == "wrote"
        cfg_path = tmp_path / "go2rtc.yaml"
        assert cfg_path.exists()
        parsed = yaml.safe_load(cfg_path.read_text())
        assert parsed["streams"]["aqara_camera_10_0_0_1"] == ["rtsp://x", "exec:y"]

    def test_merges_with_existing_streams(self, tmp_path: Path) -> None:
        existing = {"streams": {"other_stream": ["rtsp://existing"]}}
        (tmp_path / "go2rtc.yaml").write_text(yaml.dump(existing))
        register_stream(str(tmp_path), "aqara_camera_10_0_0_1", ["rtsp://x"])
        parsed = yaml.safe_load((tmp_path / "go2rtc.yaml").read_text())
        assert parsed["streams"]["other_stream"] == ["rtsp://existing"]
        assert parsed["streams"]["aqara_camera_10_0_0_1"] == ["rtsp://x"]

    def test_is_idempotent(self, tmp_path: Path) -> None:
        sources = ["rtsp://x", "exec:y"]
        register_stream(str(tmp_path), "aqara_camera_10_0_0_1", sources)
        first = (tmp_path / "go2rtc.yaml").read_text()
        register_stream(str(tmp_path), "aqara_camera_10_0_0_1", sources)
        second = (tmp_path / "go2rtc.yaml").read_text()
        assert first == second

    def test_quotes_strings_containing_hash(self, tmp_path: Path) -> None:
        # The ffmpeg source contains '#' separators; without quoting, Go
        # yaml would treat them as comments.
        register_stream(
            str(tmp_path),
            "aqara_camera_10_0_0_1",
            ["ffmpeg:aqara_camera_10_0_0_1#video=copy#audio=opus"],
        )
        raw = (tmp_path / "go2rtc.yaml").read_text()
        assert '"ffmpeg:aqara_camera_10_0_0_1#video=copy#audio=opus"' in raw

    def test_returns_noop_when_unchanged(self, tmp_path: Path) -> None:
        sources = ["rtsp://x", "exec:/path/bridge.py 10.0.0.1#backchannel=1"]
        register_stream(str(tmp_path), "aqara_camera_10_0_0_1", sources)
        assert register_stream(str(tmp_path), "aqara_camera_10_0_0_1", sources) == "noop"

    def test_collision_skips_write(self, tmp_path: Path) -> None:
        existing = {"streams": {"aqara_camera_10_0_0_1": ["rtsp://other-owner"]}}
        (tmp_path / "go2rtc.yaml").write_text(yaml.dump(existing))
        result = register_stream(
            str(tmp_path),
            "aqara_camera_10_0_0_1",
            ["rtsp://x", "exec:/path/bridge.py 10.0.0.1#backchannel=1"],
        )
        assert result == "skipped"
        parsed = yaml.safe_load((tmp_path / "go2rtc.yaml").read_text())
        assert parsed["streams"]["aqara_camera_10_0_0_1"] == ["rtsp://other-owner"]


class TestRemoveStream:
    """remove_stream takes only the named stream out of go2rtc.yaml."""

    def test_removes_named_stream_only(self, tmp_path: Path) -> None:
        existing = {"streams": {
            "aqara_camera_10_0_0_1": ["rtsp://x"],
            "other_stream": ["rtsp://existing"],
        }}
        (tmp_path / "go2rtc.yaml").write_text(yaml.dump(existing))
        remove_stream(str(tmp_path), "aqara_camera_10_0_0_1")
        parsed = yaml.safe_load((tmp_path / "go2rtc.yaml").read_text())
        assert "aqara_camera_10_0_0_1" not in parsed["streams"]
        assert parsed["streams"]["other_stream"] == ["rtsp://existing"]

    def test_no_op_when_file_missing(self, tmp_path: Path) -> None:
        # Must not raise.
        remove_stream(str(tmp_path), "nonexistent")

    def test_no_op_when_stream_missing(self, tmp_path: Path) -> None:
        existing = {"streams": {"other": ["rtsp://existing"]}}
        (tmp_path / "go2rtc.yaml").write_text(yaml.dump(existing))
        remove_stream(str(tmp_path), "not_present")
        parsed = yaml.safe_load((tmp_path / "go2rtc.yaml").read_text())
        assert parsed["streams"] == {"other": ["rtsp://existing"]}


class TestPermissionsAndLocking:
    """SEC-006/OPT-015 lock-protected read-modify-write."""

    def test_register_stream_holds_lock_during_io(self, tmp_path: Path) -> None:
        # The lock must surround the read-modify-write block.
        events: list[str] = []

        class TracingLock:
            def __enter__(self) -> "TracingLock":
                events.append("lock-acquired")
                return self

            def __exit__(self, *exc_info: object) -> None:
                events.append("lock-released")

        original_read = go2rtc_mod._read_config
        original_write = go2rtc_mod._write_config

        def trace_read(path: Path) -> dict:
            events.append("read")
            return original_read(path)

        def trace_write(path: Path, config: dict) -> None:
            events.append("write")
            return original_write(path, config)

        with patch.object(go2rtc_mod, "_CONFIG_LOCK", TracingLock()), patch.object(
            go2rtc_mod, "_read_config", trace_read
        ), patch.object(go2rtc_mod, "_write_config", trace_write):
            register_stream(str(tmp_path), "aqara_camera_10_0_0_1", ["rtsp://x"])

        assert events[0] == "lock-acquired"
        assert events[-1] == "lock-released"
        assert "read" in events and "write" in events
        assert events.index("read") < events.index("write")
