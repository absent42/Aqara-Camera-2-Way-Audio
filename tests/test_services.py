"""Tests for the talk_audio_file service."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.aqara_camera_2_way_audio import _send_aac_file
from custom_components.aqara_camera_2_way_audio.const import (
    AAC_FRAME_SAMPLES,
    DOMAIN,
    FRAME_DURATION,
    MAX_AAC_FILE_BYTES,
)


@pytest.fixture
def mock_send_aac_file():
    """Patch _send_aac_file to capture the talk client it received."""
    with patch(
        "custom_components.aqara_camera_2_way_audio._send_aac_file",
        new=AsyncMock(),
    ) as m:
        yield m


@pytest.fixture
def allow_paths():
    """Patch hass.config.is_allowed_path to permit our test path."""
    with patch(
        "homeassistant.core.Config.is_allowed_path", return_value=True
    ):
        yield


async def _set_up_entry(hass: HomeAssistant, ip: str) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "camera_ip": ip,
            "rtsp_username": "u",
            "rtsp_password": "p",
        },
        unique_id=ip,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_targets_by_entity_id(
    hass: HomeAssistant,
    mock_talk_client,
    mock_send_aac_file,
    allow_paths,
) -> None:
    """Service call targeting one entity_id triggers send for that entry."""
    entry = await _set_up_entry(hass, "192.0.2.10")
    registry = er.async_get(hass)
    target_entity = registry.async_get_entity_id(
        "camera", DOMAIN, f"192.0.2.10_camera_ch1"
    )

    await hass.services.async_call(
        DOMAIN,
        "talk_audio_file",
        {"entity_id": target_entity, "file_path": "/tmp/x.aac"},
        blocking=True,
    )
    assert mock_send_aac_file.await_count == 1


async def test_targets_by_device_id(
    hass: HomeAssistant,
    mock_talk_client,
    mock_send_aac_file,
    allow_paths,
) -> None:
    """Service call targeting one device_id triggers send for that entry."""
    entry = await _set_up_entry(hass, "192.0.2.10")
    device_reg = dr.async_get(hass)
    devices = [
        d for d in device_reg.devices.values()
        if entry.entry_id in d.config_entries
    ]
    assert len(devices) == 1

    await hass.services.async_call(
        DOMAIN,
        "talk_audio_file",
        {"device_id": devices[0].id, "file_path": "/tmp/x.aac"},
        blocking=True,
    )
    assert mock_send_aac_file.await_count == 1


async def test_multiple_entity_ids_same_entry_dedup_to_one_send(
    hass: HomeAssistant,
    mock_talk_client,
    mock_send_aac_file,
    allow_paths,
) -> None:
    """Targeting all 3 camera entities of one camera sends once, not three times."""
    entry = await _set_up_entry(hass, "192.0.2.10")
    registry = er.async_get(hass)
    entity_ids = [
        registry.async_get_entity_id("camera", DOMAIN, f"192.0.2.10_camera_ch{ch}")
        for ch in (1, 2, 3)
    ]
    assert all(entity_ids)

    await hass.services.async_call(
        DOMAIN,
        "talk_audio_file",
        {"entity_id": entity_ids, "file_path": "/tmp/x.aac"},
        blocking=True,
    )
    assert mock_send_aac_file.await_count == 1


async def test_multi_target_two_entries_invokes_each(
    hass: HomeAssistant,
    mock_talk_client,
    mock_send_aac_file,
    allow_paths,
) -> None:
    """Targets across two cameras invoke the send function once per camera."""
    await _set_up_entry(hass, "192.0.2.10")
    await _set_up_entry(hass, "192.0.2.11")
    registry = er.async_get(hass)
    entity_a = registry.async_get_entity_id("camera", DOMAIN, "192.0.2.10_camera_ch1")
    entity_b = registry.async_get_entity_id("camera", DOMAIN, "192.0.2.11_camera_ch1")

    await hass.services.async_call(
        DOMAIN,
        "talk_audio_file",
        {"entity_id": [entity_a, entity_b], "file_path": "/tmp/x.aac"},
        blocking=True,
    )
    assert mock_send_aac_file.await_count == 2


async def test_disallowed_file_path_raises(
    hass: HomeAssistant,
    mock_talk_client,
    mock_send_aac_file,
) -> None:
    """A file_path outside the allowlist raises ServiceValidationError."""
    entry = await _set_up_entry(hass, "192.0.2.10")
    registry = er.async_get(hass)
    target_entity = registry.async_get_entity_id("camera", DOMAIN, "192.0.2.10_camera_ch1")

    with patch("homeassistant.core.Config.is_allowed_path", return_value=False):
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN,
                "talk_audio_file",
                {"entity_id": target_entity, "file_path": "/tmp/x.aac"},
                blocking=True,
            )


async def test_no_target_raises(
    hass: HomeAssistant,
    mock_talk_client,
    mock_send_aac_file,
    allow_paths,
) -> None:
    """Calling the service with no target raises ServiceValidationError."""
    await _set_up_entry(hass, "192.0.2.10")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "talk_audio_file",
            {"file_path": "/tmp/x.aac"},
            blocking=True,
        )


async def test_send_aac_file_rejects_oversize_file(hass: HomeAssistant) -> None:
    """SEC-003: files larger than MAX_AAC_FILE_BYTES raise ServiceValidationError."""
    client = MagicMock()
    client.is_connected = True

    big_stat = SimpleNamespace(st_size=MAX_AAC_FILE_BYTES + 1)
    with patch("pathlib.Path.stat", return_value=big_stat), patch(
        "pathlib.Path.read_bytes"
    ) as read_bytes:
        with pytest.raises(ServiceValidationError) as exc:
            await _send_aac_file(hass, client, "/tmp/huge.aac")
        # The raise uses translation_key so the visible message comes from
        # strings.json at runtime; assert on the key instead of the literal.
        assert exc.value.translation_key == "file_too_large"
        assert exc.value.translation_placeholders == {
            "size": str(MAX_AAC_FILE_BYTES + 1),
            "max_size": str(MAX_AAC_FILE_BYTES),
        }
        read_bytes.assert_not_called()
    client.send_audio_frame.assert_not_called()


async def test_send_aac_file_drift_corrected_pacing(hass: HomeAssistant) -> None:
    """OPT-003: with N frames, total wall time is bounded by N*FRAME_DURATION + small slack.

    Inject artificial 5ms-per-frame processing delay; without drift correction
    the total would be N*(FRAME_DURATION + 5ms). With correction, the total
    stays close to N*FRAME_DURATION because the per-frame deadline is anchored
    to the start time.
    """
    n_frames = 5
    fake_frames = [b"\xff\xf1FRAME" + bytes([i]) for i in range(n_frames)]

    client = MagicMock()
    client.is_connected = True

    def slow_send(frame: bytes, ts: int) -> None:
        time.sleep(0.005)  # simulate 5ms send overhead

    client.send_audio_frame.side_effect = slow_send

    small_stat = SimpleNamespace(st_size=1024)
    with patch("pathlib.Path.stat", return_value=small_stat), patch(
        "pathlib.Path.read_bytes", return_value=b"raw"
    ), patch(
        "custom_components.aqara_camera_2_way_audio.extract_adts_frames",
        return_value=fake_frames,
    ):
        start = asyncio.get_running_loop().time()
        await _send_aac_file(hass, client, "/tmp/x.aac")
        elapsed = asyncio.get_running_loop().time() - start

    expected = n_frames * FRAME_DURATION
    # Drift-corrected pacing should land within 50ms of the ideal duration.
    assert elapsed == pytest.approx(expected, abs=0.050), (
        f"drift exceeded budget: elapsed={elapsed:.3f}s expected={expected:.3f}s"
    )
    assert client.send_audio_frame.call_count == n_frames
    # Each frame should receive an incrementing AAC_FRAME_SAMPLES timestamp.
    seen_ts = [call.args[1] for call in client.send_audio_frame.call_args_list]
    assert seen_ts == [i * AAC_FRAME_SAMPLES for i in range(n_frames)]


async def test_send_aac_file_stops_on_disconnect(hass: HomeAssistant) -> None:
    """OPT-003: if client.is_connected goes False mid-loop, sends stop."""
    fake_frames = [b"\xff\xf1" + bytes([i]) for i in range(10)]
    client = MagicMock()
    # Connected for first 3 frames, then disconnects.
    client.is_connected = True
    type(client).is_connected = property(
        lambda self: self._call_count < 3
    )
    client._call_count = 0

    def count_calls(frame: bytes, ts: int) -> None:
        client._call_count += 1

    client.send_audio_frame.side_effect = count_calls

    small_stat = SimpleNamespace(st_size=1024)
    with patch("pathlib.Path.stat", return_value=small_stat), patch(
        "pathlib.Path.read_bytes", return_value=b"raw"
    ), patch(
        "custom_components.aqara_camera_2_way_audio.extract_adts_frames",
        return_value=fake_frames,
    ):
        await _send_aac_file(hass, client, "/tmp/x.aac")

    assert client.send_audio_frame.call_count == 3


async def test_connect_failure_raises_home_assistant_error(
    hass: HomeAssistant,
    mock_talk_client,
    mock_send_aac_file,
    allow_paths,
) -> None:
    """If the talk client cannot connect, the service raises HomeAssistantError."""
    entry = await _set_up_entry(hass, "192.0.2.10")
    mock_talk_client.connect = AsyncMock(return_value=False)
    registry = er.async_get(hass)
    target = registry.async_get_entity_id("camera", DOMAIN, "192.0.2.10_camera_ch1")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "talk_audio_file",
            {"entity_id": target, "file_path": "/tmp/x.aac"},
            blocking=True,
        )
