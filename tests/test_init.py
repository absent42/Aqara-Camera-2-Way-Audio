"""Tests for __init__.py setup/unload and go2rtc + notification behaviour."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant

from custom_components.aqara_camera_2_way_audio.const import (
    CONF_BACKCHANNEL_CHANNEL,
    DOMAIN,
)


@pytest.fixture
def captured_register_stream():
    """Patch go2rtc.register_stream to capture its call args.

    Returns "wrote" so the production code's persistent-notification gate
    (`if result == "wrote": ...`) triggers for every test setup.
    """
    calls = []

    def _capture(config_dir, stream_name, sources):
        calls.append((config_dir, stream_name, list(sources)))
        return "wrote"

    with patch(
        "custom_components.aqara_camera_2_way_audio.go2rtc.register_stream",
        side_effect=_capture,
    ), patch(
        "custom_components.aqara_camera_2_way_audio.go2rtc.remove_stream"
    ):
        yield calls


async def test_register_stream_called_with_default_channel(
    hass: HomeAssistant,
    camera_config_data: dict,
    mock_talk_client,
    captured_register_stream,
) -> None:
    """Setup with no options uses channel 1 in the RTSP source."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=camera_config_data,
        unique_id=camera_config_data["camera_ip"],
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert len(captured_register_stream) == 1
    _, stream_name, sources = captured_register_stream[0]
    assert stream_name == f"aqara_camera_{camera_config_data['camera_ip'].replace('.', '_')}"
    rtsp = sources[0]
    assert "/ch1" in rtsp


async def test_register_stream_uses_configured_channel(
    hass: HomeAssistant,
    camera_config_data: dict,
    mock_talk_client,
    captured_register_stream,
) -> None:
    """Setup with backchannel_channel=2 in options uses /ch2."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=camera_config_data,
        unique_id=camera_config_data["camera_ip"],
        options={CONF_BACKCHANNEL_CHANNEL: 2},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    _, _, sources = captured_register_stream[0]
    assert "/ch2" in sources[0]


async def test_persistent_notification_raised_on_setup(
    hass: HomeAssistant,
    camera_config_data: dict,
    mock_talk_client,
    captured_register_stream,
) -> None:
    """A persistent notification with a per-IP id is created on go2rtc write."""
    # Modern HA stores persistent notifications in an internal dict rather than
    # creating hass.states entries; query that dict via the public helper.
    from homeassistant.components.persistent_notification import (
        _async_get_or_create_notifications,
    )

    ip = camera_config_data["camera_ip"]
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=camera_config_data,
        unique_id=ip,
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    notifications = _async_get_or_create_notifications(hass)
    expected_id = (
        f"aqara_camera_2_way_audio_go2rtc_restart_{ip.replace('.', '_')}"
    )
    assert expected_id in notifications, (
        f"notification id missing: {set(notifications)}"
    )


async def test_options_change_reloads_entry(
    hass: HomeAssistant,
    camera_config_data: dict,
    mock_talk_client,
    captured_register_stream,
) -> None:
    """Updating entry.options triggers async_reload, re-invoking register_stream."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=camera_config_data,
        unique_id=camera_config_data["camera_ip"],
        options={CONF_BACKCHANNEL_CHANNEL: 1},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    first_call_count = len(captured_register_stream)

    hass.config_entries.async_update_entry(entry, options={CONF_BACKCHANNEL_CHANNEL: 3})
    await hass.async_block_till_done()

    assert len(captured_register_stream) > first_call_count
    _, _, sources = captured_register_stream[-1]
    assert "/ch3" in sources[0]


async def test_service_registered_once_across_entries(
    hass: HomeAssistant,
    camera_config_data: dict,
    mock_talk_client,
    captured_register_stream,
) -> None:
    """The talk_audio_file service registers once even with multiple entries."""
    e1 = MockConfigEntry(domain=DOMAIN, data=camera_config_data, unique_id="ip1")
    e1.add_to_hass(hass)
    data2 = {**camera_config_data, "camera_ip": "192.0.2.11"}
    e2 = MockConfigEntry(domain=DOMAIN, data=data2, unique_id="ip2")
    e2.add_to_hass(hass)

    # Setting up one entry triggers async_setup_component, which loads all
    # domain entries in one shot. Both entries end up LOADED after this call.
    await hass.config_entries.async_setup(e1.entry_id)
    await hass.async_block_till_done()

    assert hass.services.has_service(DOMAIN, "talk_audio_file")


async def test_service_removed_when_last_entry_unloads(
    hass: HomeAssistant,
    camera_config_data: dict,
    mock_talk_client,
    captured_register_stream,
) -> None:
    """Unloading the only entry removes the service; leaves it otherwise."""
    e1 = MockConfigEntry(domain=DOMAIN, data=camera_config_data, unique_id="ip1")
    e1.add_to_hass(hass)
    data2 = {**camera_config_data, "camera_ip": "192.0.2.11"}
    e2 = MockConfigEntry(domain=DOMAIN, data=data2, unique_id="ip2")
    e2.add_to_hass(hass)

    # Setting up one entry triggers async_setup_component, which loads both.
    await hass.config_entries.async_setup(e1.entry_id)
    await hass.async_block_till_done()

    await hass.config_entries.async_unload(e1.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, "talk_audio_file")

    await hass.config_entries.async_unload(e2.entry_id)
    await hass.async_block_till_done()
    assert not hass.services.has_service(DOMAIN, "talk_audio_file")
