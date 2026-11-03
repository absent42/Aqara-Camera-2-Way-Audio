"""Unit tests for go2rtc.py -- config-file read/write/merge."""

from __future__ import annotations

from collections.abc import Iterator
from itertools import permutations
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
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

    def test_explicit_audio_and_graceful_stop_preserve_command(self) -> None:
        with patch.object(go2rtc_mod.sys, "executable", "/usr/bin/python3"):
            src = build_exec_source("/config", "10.0.0.1")
        command, options = src.split("#", 1)
        assert command == (
            "exec:/usr/bin/python3 "
            "/config/custom_components/aqara_camera_2_way_audio/bridge.py 10.0.0.1"
        )
        assert options == "backchannel=1#audio=pcma/8000#killsignal=15#killtimeout=10"


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


class TestLegacyMigration:
    """Only the exact previously generated command is eligible for upgrade."""

    @pytest.fixture
    def generated_sources(self, tmp_path: Path) -> Iterator[tuple[str, str]]:
        with patch.object(go2rtc_mod.sys, "executable", "/usr/bin/python3"):
            command = (
                "exec:/usr/bin/python3 "
                f"{tmp_path}/custom_components/aqara_camera_2_way_audio/bridge.py 10.0.0.1"
            )
            yield (
                command + "#backchannel=1",
                command + "#backchannel=1#audio=pcma/8000#killsignal=15#killtimeout=10",
            )

    def test_upgrades_legacy_and_then_noops(
        self, tmp_path: Path, generated_sources: tuple[str, str]
    ) -> None:
        legacy, current = generated_sources
        path = tmp_path / "go2rtc.yaml"
        config = {
            "api": {"listen": "127.0.0.1:1984"},
            "webrtc": {"candidates": ["stun:auto"]},
            "streams": {
                "camera": ["rtsp://new-channel", "ffmpeg:camera#audio=opus", legacy],
                "other": ["rtsp://unrelated"],
            },
        }
        path.write_text(yaml.safe_dump(config))
        sources = ["rtsp://new-channel", "ffmpeg:camera#audio=opus", current]
        assert register_stream(str(tmp_path), "camera", sources, "10.0.0.1") == "wrote"
        config["streams"]["camera"] = sources
        assert yaml.safe_load(path.read_text()) == config
        first = path.read_bytes()
        with patch.object(go2rtc_mod, "_write_config") as write:
            assert register_stream(str(tmp_path), "camera", sources) == "noop"
        write.assert_not_called()
        assert path.read_bytes() == first

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("10.0.0.1#", "10.0.0.2#"),
            ("/bridge.py", "/different.py"),
            ("/aqara_camera_2_way_audio/", "/another_component/"),
            ("/usr/bin/python3", "/usr/bin/python"),
            ("#backchannel=1", "#backchannel=1#custom=1"),
            ("#backchannel=1", "#backchannel=1#audio=pcma/8000"),
            ("#backchannel=1", "#backchannel=1#audio=pcm/16000"),
            ("#backchannel=1", "#audio=pcma/8000#backchannel=1"),
            ("#backchannel=1", "#backchannel=1#killsignal=9"),
        ],
    )
    def test_foreign_or_customized_entry_is_untouched(
        self, tmp_path: Path, generated_sources: tuple[str, str], old: str, new: str
    ) -> None:
        legacy, current = generated_sources
        self._assert_skipped(tmp_path, legacy.replace(old, new), current)

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("/usr/bin/python3", "/usr/bin/python"),
            ("/bridge.py", "/custom/bridge.py"),
            ("#backchannel=1", "#backchannel=1#custom=1"),
        ],
    )
    def test_custom_requested_command_cannot_claim_legacy_entry(
        self, tmp_path: Path, generated_sources: tuple[str, str], old: str, new: str
    ) -> None:
        legacy, current = generated_sources
        self._assert_skipped(tmp_path, legacy.replace(old, new), current.replace(old, new))

    @pytest.mark.parametrize(
        "command",
        [
            "exec:/usr/bin/python3 /arbitrary/bridge.py 10.0.0.1",
            "exec:/usr/bin/python3 /config/custom_components/aqara_camera_2_way_audio/bridge.py 10.0.0.1",
            "exec:arbitrary-command argument",
        ],
    )
    def test_generic_callers_do_not_gain_legacy_ownership(
        self, tmp_path: Path, command: str
    ) -> None:
        self._assert_skipped(
            tmp_path,
            command + "#backchannel=1",
            command + "#backchannel=1#audio=pcma/8000#killsignal=15#killtimeout=10",
        )

    def test_exec_match_alone_does_not_allow_update(self, tmp_path: Path) -> None:
        path = tmp_path / "go2rtc.yaml"
        path.write_text(yaml.safe_dump({"streams": {"camera": ["rtsp://old", "exec:custom"]}}))
        sources = ["rtsp://new", "exec:custom"]
        assert register_stream(str(tmp_path), "camera", sources) == "skipped"
        assert yaml.safe_load(path.read_text())["streams"]["camera"] == ["rtsp://old", "exec:custom"]

    @staticmethod
    def _assert_skipped(tmp_path: Path, existing: str, desired: str) -> None:
        path = tmp_path / "go2rtc.yaml"
        path.write_text(yaml.safe_dump({"streams": {"camera": ["rtsp://old", existing]}}))
        before = path.read_bytes()
        with patch.object(go2rtc_mod, "_write_config") as write:
            assert register_stream(str(tmp_path), "camera", ["rtsp://new", desired]) == "skipped"
        write.assert_not_called()
        assert path.read_bytes() == before


class TestRetainedBridgeOptions:
    """Equivalent options are retained, never used to claim changed sources."""

    OPTIONS = ("backchannel=1", "audio=pcma/8000", "killsignal=15", "killtimeout=10")

    @pytest.fixture
    def sources(self, tmp_path: Path) -> list[str]:
        return [
            "rtsp://example.invalid/ch1",
            "ffmpeg:camera#video=copy#audio=opus",
            build_exec_source(str(tmp_path), "192.0.2.1"),
        ]

    @pytest.mark.parametrize("options", list(permutations(OPTIONS)))
    def test_all_option_orders_preserve_bytes_and_mtime(
        self, tmp_path: Path, sources: list[str], options: tuple[str, ...]
    ) -> None:
        command = sources[-1].partition("#")[0]
        existing = [command + "#" + "#".join(options), *reversed(sources[:-1])]
        self.assert_preserved(tmp_path, existing, sources, "noop")

    @pytest.mark.parametrize(
        "option_suffix",
        [
            "#backchannel=1#audio=pcma/8000#killsignal=15#killtimeout=10#custom=1",
            "#backchannel=1#audio=pcma/8000#killsignal=15#killtimeout=10#backchannel=1",
            "#backchannel=1#audio=pcma/8000#killsignal=15",
            "#backchannel=1#audio=pcm/16000#killsignal=15#killtimeout=10",
            "#backchannel=1#audio=pcma/8000#killsignal=9#killtimeout=10",
            "#backchannel=1#audio=pcma/8000#killsignal=15#killtimeout=11",
            "#backchannel=1#audio=pcma/8000#killsignal=15#killtimeout=10#",
        ],
    )
    def test_altered_options_are_not_owned(
        self, tmp_path: Path, sources: list[str], option_suffix: str
    ) -> None:
        existing = [*sources[:-1], sources[-1].partition("#")[0] + option_suffix]
        self.assert_preserved(tmp_path, existing, sources, "skipped")

    @pytest.mark.parametrize("change", ["camera", "python", "script", "rtsp", "ffmpeg", "duplicate"])
    def test_reordered_options_do_not_claim_changed_sources(
        self, tmp_path: Path, sources: list[str], change: str
    ) -> None:
        command = sources[-1].partition("#")[0]
        existing = [*sources[:-1], command + "#" + "#".join(reversed(self.OPTIONS))]
        if change == "camera":
            existing[-1] = existing[-1].replace("192.0.2.1", "192.0.2.2")
        elif change == "python":
            existing[-1] = existing[-1].replace("exec:", "exec:/different-python ", 1)
        elif change == "script":
            existing[-1] = existing[-1].replace("/bridge.py", "/different.py")
        elif change == "rtsp":
            existing[0] = "rtsp://example.invalid/ch2"
        elif change == "ffmpeg":
            existing[1] = "ffmpeg:camera#video=copy#audio=pcma"
        else:
            existing.append(existing[0])
        self.assert_preserved(tmp_path, existing, sources, "skipped")

    @pytest.mark.parametrize("legacy", [False, True])
    @pytest.mark.parametrize("extra", ["exec:foreign", "duplicate"])
    def test_multiple_exec_producers_cannot_grant_update_ownership(
        self, tmp_path: Path, sources: list[str], legacy: bool, extra: str
    ) -> None:
        producer = sources[-1]
        if legacy:
            producer = producer.partition("#")[0] + "#backchannel=1"
        existing = [*sources[:-1], producer, producer if extra == "duplicate" else extra]
        self.assert_preserved(tmp_path, existing, sources, "skipped")

    @pytest.mark.parametrize("existing", [None, 1, "exec:foreign", {}, [None], [{}], [1]])
    def test_malformed_existing_sources_are_not_overwritten(
        self, tmp_path: Path, sources: list[str], existing: object
    ) -> None:
        self.assert_preserved(tmp_path, existing, sources, "skipped")

    @pytest.mark.parametrize("requested", [None, 1, "exec:foreign", {}, [None], [{}]])
    def test_invalid_requested_sources_fail_without_writing(
        self, tmp_path: Path, sources: list[str], requested: object
    ) -> None:
        self.assert_preserved(tmp_path, sources, requested, "error")

    @pytest.mark.parametrize("config", [False, [], [1], 1, "invalid", {"streams": None}, {"streams": []}])
    def test_malformed_config_is_not_overwritten(
        self, tmp_path: Path, sources: list[str], config: object
    ) -> None:
        path = tmp_path / "go2rtc.yaml"
        path.write_text(yaml.safe_dump(config))
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        with patch.object(go2rtc_mod, "_write_config") as write:
            assert register_stream(str(tmp_path), "camera", sources) == "error"
        write.assert_not_called()
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before

    def test_duplicate_sources_are_not_mistaken_for_exact_match(self, tmp_path: Path) -> None:
        self.assert_preserved(tmp_path, ["rtsp://example.invalid"] * 2, ["rtsp://example.invalid"], "skipped")

    def test_parse_error_does_not_log_source_credentials(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "go2rtc.yaml"
        path.write_text('streams: [rtsp://user:private-fixture@example.invalid/ch1\n')
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        assert register_stream(str(tmp_path), "camera", ["rtsp://example.invalid"]) == "error"
        assert "ParserError" in caplog.text
        assert "private-fixture" not in caplog.text
        assert all(record.exc_info is None for record in caplog.records)
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before

    def test_matching_duplicate_companions_are_retained(
        self, tmp_path: Path, sources: list[str]
    ) -> None:
        desired = [sources[0], *sources]
        command = sources[-1].partition("#")[0]
        existing = [command + "#" + "#".join(reversed(self.OPTIONS)), *desired[:-1]]
        self.assert_preserved(tmp_path, existing, desired, "noop")

    def test_write_error_does_not_log_exception_text(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with patch.object(go2rtc_mod, "_write_config", side_effect=OSError("private-fixture")):
            assert register_stream(str(tmp_path), "camera", ["rtsp://example.invalid"]) == "error"
        assert "OSError" in caplog.text
        assert "private-fixture" not in caplog.text
        assert all(record.exc_info is None for record in caplog.records)

    @staticmethod
    def assert_preserved(tmp_path: Path, existing: object, desired: object, result: str) -> None:
        path = tmp_path / "go2rtc.yaml"
        path.write_text("# Preserve this comment and formatting.\n" + yaml.safe_dump({"streams": {"camera": existing}}))
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        with patch.object(go2rtc_mod, "_write_config") as write:
            # Deliberately exercise malformed caller values as well as lists.
            assert register_stream(str(tmp_path), "camera", cast(list[str], desired)) == result
        write.assert_not_called()
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before


class TestRemoveStream:
    """remove_stream takes only the named stream out of go2rtc.yaml."""

    def test_removes_named_stream_only(self, tmp_path: Path) -> None:
        existing = {"streams": {
            "aqara_camera_10_0_0_1": ["rtsp://x"],
            "other_stream": ["rtsp://existing"],
        }}
        (tmp_path / "go2rtc.yaml").write_text(yaml.dump(existing))
        remove_stream(str(tmp_path), "aqara_camera_10_0_0_1", go2rtc_mod.sources_digest(["rtsp://x"]))
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
