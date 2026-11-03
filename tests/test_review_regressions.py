"""PR review regressions: ownership, real YAML failures and cancellation."""

from __future__ import annotations

import errno
import signal
import socket
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aqara_camera_2_way_audio import bridge, encoder, go2rtc
from custom_components.aqara_camera_2_way_audio.const import (
    DOMAIN,
    TYPE_START_VOICE,
    TYPE_STOP_VOICE,
)
from custom_components.aqara_camera_2_way_audio.protocol import parse_packet


@pytest.fixture(autouse=True)
def isolated_go2rtc_config(tmp_path, monkeypatch):
    """Never write to HA's shared testing_config directory."""
    monkeypatch.setattr(go2rtc, "GO2RTC_CONFIG_FILE", str(tmp_path / "go2rtc.yaml"))


def sources(directory: Path) -> list[str]:
    return [
        "rtsp://user:fixture-secret@192.0.2.10:8554/ch1",
        "ffmpeg:camera#audio=opus",
        go2rtc.build_exec_source(str(directory), "192.0.2.10"),
    ]


def reordered(values: list[str]) -> list[str]:
    command, options = values[-1].split("#", 1)
    return [*values[:-1], command + "#" + "#".join(reversed(options.split("#")))]


@pytest.mark.parametrize("change", ["channel", "password"])
def test_owned_reordered_sources_allow_ha_updates(tmp_path, change):
    original = sources(tmp_path)
    assert go2rtc.register_stream(str(tmp_path), "camera", original) == "wrote"
    digest = go2rtc.sources_digest(original)
    path = tmp_path / "go2rtc.yaml"
    path.write_text(yaml.safe_dump({"streams": {"camera": reordered(original)}}))
    desired = list(original)
    desired[0] = (
        desired[0].replace("ch1", "ch3")
        if change == "channel"
        else desired[0].replace("fixture-secret", "new-fixture")
    )
    assert (
        go2rtc.register_stream(str(tmp_path), "camera", desired, "192.0.2.10", digest, "camera")
        == "wrote"
    )
    assert yaml.safe_load(path.read_text())["streams"]["camera"] == desired


@pytest.mark.parametrize("change", ["rtsp", "ffmpeg", "exec", "duplicate", "foreign"])
def test_customized_owned_stream_is_not_updated_or_removed(tmp_path, change):
    original = sources(tmp_path)
    digest = go2rtc.sources_digest(original)
    existing = reordered(original)
    if change == "rtsp":
        existing[0] += "?custom=1"
    elif change == "ffmpeg":
        existing[1] += "#custom=1"
    elif change == "exec":
        existing[-1] += "#custom=1"
    elif change == "duplicate":
        existing.append(existing[0])
    else:
        existing = ["rtsp://another-owner"]
    path = tmp_path / "go2rtc.yaml"
    path.write_text(
        "# retain comment\n" + yaml.safe_dump({"streams": {"camera": existing}})
    )
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    assert (
        go2rtc.register_stream(str(tmp_path), "camera", original, "192.0.2.10", digest, "camera")
        == "skipped"
    )
    go2rtc.remove_stream(str(tmp_path), "camera", digest)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_remove_requires_saved_ownership_and_accepts_reordering(tmp_path):
    original = sources(tmp_path)
    path = tmp_path / "go2rtc.yaml"
    path.write_text(
        yaml.safe_dump(
            {"streams": {"camera": reordered(original), "other": ["rtsp://other"]}}
        )
    )
    before = path.read_bytes()
    go2rtc.remove_stream(str(tmp_path), "camera")
    assert path.read_bytes() == before
    go2rtc.remove_stream(str(tmp_path), "camera", go2rtc.sources_digest(original))
    assert yaml.safe_load(path.read_text())["streams"] == {"other": ["rtsp://other"]}


def test_legacy_with_changed_companions_requires_manual_reconciliation(tmp_path):
    desired = sources(tmp_path)
    legacy = desired[-1].partition("#")[0] + "#backchannel=1"
    path = tmp_path / "go2rtc.yaml"
    path.write_text(yaml.safe_dump({"streams": {"camera": ["rtsp://custom", legacy]}}))
    before = path.read_bytes()
    assert (
        go2rtc.register_stream(str(tmp_path), "camera", desired, "192.0.2.10")
        == "skipped"
    )
    assert path.read_bytes() == before


def test_real_missing_parent_write_error_is_diagnostic(tmp_path, caplog):
    path = tmp_path / "missing" / "go2rtc.yaml"
    with patch.object(go2rtc, "GO2RTC_CONFIG_FILE", str(path)):
        assert (
            go2rtc.register_stream(str(tmp_path), "camera", sources(tmp_path))
            == "error"
        )
    assert not path.exists()
    assert str(path) in caplog.text and f"errno={errno.ENOENT}" in caplog.text
    assert "fixture-secret" not in caplog.text


@pytest.mark.parametrize(
    "raw",
    [
        "false",
        "[]",
        "1",
        "'fixture-secret'",
        "streams: []",
        "streams: [rtsp://user:fixture-secret@example.invalid\n",
    ],
)
def test_remove_yaml_failure_is_secret_safe_and_preserves_file(tmp_path, caplog, raw):
    path = tmp_path / "go2rtc.yaml"
    path.write_text(raw)
    go2rtc.remove_stream(str(tmp_path), "camera", "owned-digest")
    assert path.read_text() == raw
    assert "fixture-secret" not in caplog.text
    assert str(path) in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("operation", ["register", "remove"])
def test_real_io_error_has_path_errno_without_exception_payload(
    tmp_path, caplog, operation
):
    # EISDIR is real filesystem failure even when tests run as root.
    path = tmp_path / "go2rtc.yaml"
    path.mkdir()
    if operation == "register":
        assert (
            go2rtc.register_stream(str(tmp_path), "camera", sources(tmp_path))
            == "error"
        )
    else:
        go2rtc.remove_stream(str(tmp_path), "camera", "owned-digest")
    assert str(path) in caplog.text
    assert f"errno={errno.EISDIR}" in caplog.text
    assert "fixture-secret" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_permission_error_includes_safe_errno_not_message(tmp_path, caplog):
    with patch.object(
        Path,
        "read_text",
        side_effect=PermissionError(errno.EACCES, "fixture-secret", "fixture-secret"),
    ):
        assert (
            go2rtc.register_stream(str(tmp_path), "camera", sources(tmp_path))
            == "error"
        )
    assert "PermissionError" in caplog.text and "errno=13" in caplog.text
    assert str(tmp_path / "go2rtc.yaml") in caplog.text
    assert "fixture-secret" not in caplog.text


async def test_ha_reload_update_delete_real_config(
    hass, camera_config_data, mock_talk_client, tmp_path
):
    entry = MockConfigEntry(
        domain=DOMAIN, data=camera_config_data, unique_id="review-camera"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    stream_name = "aqara_camera_192_0_2_10"
    path = tmp_path / "go2rtc.yaml"
    digest = entry.data[go2rtc.CONF_GO2RTC_DIGEST]
    config = yaml.safe_load(path.read_text())
    config["streams"][stream_name] = reordered(config["streams"][stream_name])
    path.write_text(yaml.safe_dump(config))
    before = path.read_bytes()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert path.read_bytes() == before
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert path.read_bytes() == before
    assert entry.data[go2rtc.CONF_GO2RTC_DIGEST] == digest
    hass.config_entries.async_update_entry(entry, options={"backchannel_channel": 3})
    await hass.async_block_till_done()
    assert yaml.safe_load(path.read_text())["streams"][stream_name][0].endswith("/ch3")
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "rtsp_password": "replacement-fixture"}
    )
    await hass.async_block_till_done()
    assert (
        "replacement-fixture"
        in yaml.safe_load(path.read_text())["streams"][stream_name][0]
    )
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert stream_name not in yaml.safe_load(path.read_text())["streams"]


@pytest.mark.parametrize("foreign", [False, True])
async def test_ha_unload_and_remove_preserve_custom_or_foreign(
    hass, camera_config_data, mock_talk_client, foreign, tmp_path
):
    entry = MockConfigEntry(
        domain=DOMAIN, data=camera_config_data, unique_id="review-camera"
    )
    entry.add_to_hass(hass)
    path = tmp_path / "go2rtc.yaml"
    if foreign:
        path.write_text(
            yaml.safe_dump({"streams": {"aqara_camera_192_0_2_10": ["rtsp://foreign"]}})
        )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    if not foreign:
        config = yaml.safe_load(path.read_text())
        config["streams"]["aqara_camera_192_0_2_10"][0] += "?custom=1"
        path.write_text(yaml.safe_dump(config))
    before = path.read_bytes()
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert path.read_bytes() == before


@pytest.mark.parametrize("stage", ["connect", "encoder-constructor", "encoder-start"])
def test_early_sigterm_cleans_resources(stage):
    session = MagicMock()
    session.connect.return_value = True
    enc = MagicMock()
    handler = None

    def install(sig, callback):
        nonlocal handler
        if callable(callback):
            handler = callback
        return signal.SIG_DFL

    def terminate(*args, **kwargs):
        handler(signal.SIGTERM, None)

    constructor = MagicMock(return_value=enc)
    if stage == "connect":
        session.connect.side_effect = terminate
    elif stage == "encoder-constructor":
        constructor.side_effect = terminate
    else:
        enc.start.side_effect = terminate
    with (
        patch.object(bridge.sys, "argv", ["bridge.py", "192.0.2.10"]),
        patch.object(bridge.signal, "signal", side_effect=install),
        patch.object(bridge, "BridgeSession", return_value=session),
        patch.object(encoder, "AACEncoder", constructor),
        pytest.raises(SystemExit) as exc,
    ):
        bridge.main()
    assert exc.value.code == 0
    session.close.assert_called_once()
    assert enc.stop.call_count == (1 if stage == "encoder-start" else 0)


def test_start_interruption_arms_stop_before_send():
    session = bridge.BridgeSession("192.0.2.10")
    sock = MagicMock(spec=socket.socket)
    session._tcp_sock = sock
    sock.sendall.side_effect = [SystemExit(0), None]
    with pytest.raises(SystemExit):
        session._request_ack(TYPE_START_VOICE)
    session.close()
    assert [
        parse_packet(call.args[0])["type"] for call in sock.sendall.call_args_list
    ] == [TYPE_START_VOICE, TYPE_STOP_VOICE]
    sock.settimeout.assert_called_with(1)


def test_real_sigterm_during_start_ack_sends_stop(socket_enabled):
    # Only a loopback fake camera is used. The child runs the real handshake
    # and OS signal handler, before an encoder has even been constructed.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(8)
    command = (
        "import sys; from custom_components.aqara_camera_2_way_audio import bridge; "
        f"bridge.CONTROL_PORT={listener.getsockname()[1]}; "
        "sys.argv=['bridge.py','127.0.0.1']; bridge.main()"
    )
    proc = subprocess.Popen(
        [bridge.sys.executable, "-c", command],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        with listener.accept()[0] as control:
            control.settimeout(8)
            assert parse_packet(control.recv(1024))["type"] == TYPE_START_VOICE
            started = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            assert parse_packet(control.recv(1024))["type"] == TYPE_STOP_VOICE
            proc.communicate(timeout=8)
            assert proc.returncode == 0
            assert time.monotonic() - started < 8
    finally:
        listener.close()
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=2)


def test_encoder_stop_kills_real_uncooperative_child():
    # A local process ignores EOF/TERM; stop must kill and reap it, not hang.
    proc = subprocess.Popen(
        [bridge.sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        bufsize=0,
    )
    enc = encoder.AACEncoder()
    enc._proc = proc
    started = time.monotonic()
    try:
        enc.stop()
        assert time.monotonic() - started < 4
        assert proc.poll() is not None
        assert enc._proc is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


def test_shutdown_waits_have_finite_total_budget():
    session = bridge.BridgeSession("192.0.2.10")
    thread = MagicMock()
    thread.is_alive.return_value = True
    session._heartbeat_thread = thread
    sock = MagicMock(spec=socket.socket)
    session._tcp_sock = sock
    session._stop_allowed = True
    session.close()
    enc = encoder.AACEncoder()
    proc = MagicMock()
    proc.wait.side_effect = subprocess.TimeoutExpired("fixture", 2)
    enc._proc = proc
    enc.stop()
    waits = [
        call.kwargs["timeout"]
        for call in thread.join.call_args_list + proc.wait.call_args_list
    ]
    assert sum(waits) + sock.settimeout.call_args.args[0] < 10
    assert all(timeout > 0 for timeout in waits)
    proc.kill.assert_called_once()
