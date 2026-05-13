"""HA-integration tests for the AqaraCameraStream entity."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.camera import Camera
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.aqara_camera_2_way_audio.camera import AqaraCameraStream
from custom_components.aqara_camera_2_way_audio.const import DOMAIN


@pytest.fixture
async def configured_entry(
    hass: HomeAssistant, camera_config_data: dict, mock_talk_client
) -> MockConfigEntry:
    """Create and set up a config entry for one camera."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=camera_config_data,
        unique_id=camera_config_data["camera_ip"],
        title=f"Aqara Camera ({camera_config_data['camera_ip']})",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_three_camera_entities_created(
    hass: HomeAssistant, configured_entry: MockConfigEntry
) -> None:
    """One entry produces exactly three camera entities."""
    registry = er.async_get(hass)
    entries = [
        e for e in registry.entities.values()
        if e.config_entry_id == configured_entry.entry_id and e.domain == Platform.CAMERA
    ]
    assert len(entries) == 3
    unique_ids = sorted(e.unique_id for e in entries)
    ip = configured_entry.data["camera_ip"]
    assert unique_ids == [
        f"{ip}_camera_ch1",
        f"{ip}_camera_ch2",
        f"{ip}_camera_ch3",
    ]


async def test_stream_source_per_channel(
    hass: HomeAssistant, configured_entry: MockConfigEntry
) -> None:
    """Each entity's stream_source() returns its own /chN URL."""
    registry = er.async_get(hass)
    ip = configured_entry.data["camera_ip"]
    for channel in (1, 2, 3):
        entity_id = registry.async_get_entity_id(
            "camera", DOMAIN, f"{ip}_camera_ch{channel}"
        )
        assert entity_id is not None, f"channel {channel} entity missing"
        camera = await _camera_entity(hass, entity_id)
        url = await camera.stream_source()
        assert url is not None
        assert f"/ch{channel}" in url


async def _camera_entity(hass: HomeAssistant, entity_id: str):
    """Return the live Camera entity instance for an entity_id.

    Uses the public EntityComponent registered under the "camera" key in
    hass.data. This avoids depending on the private
    `homeassistant.components.camera._get_camera_from_entity_id` helper.
    """
    from homeassistant.helpers.entity_component import EntityComponent
    component: EntityComponent = hass.data["camera"]
    entity = component.get_entity(entity_id)
    assert entity is not None, f"camera entity {entity_id} not found"
    return entity


async def test_all_three_share_one_device(
    hass: HomeAssistant, configured_entry: MockConfigEntry
) -> None:
    """The three entities are grouped under a single device entry."""
    device_reg = dr.async_get(hass)
    devices = [
        d for d in device_reg.devices.values()
        if configured_entry.entry_id in d.config_entries
    ]
    assert len(devices) == 1


async def test_async_camera_image_is_overridden() -> None:
    """Snapshot path is overridden -- HA's base Camera.async_camera_image
    falls through to camera_image (sync), which raises NotImplementedError
    for RTSP-only cameras. We override async_camera_image to use ffmpeg's
    async_get_image with the channel's RTSP URL. The actual ffmpeg call
    is only invoked at runtime in a live HA (it has a haffmpeg dep not
    present in test env), so we verify the override exists rather than
    exercising the full call.
    """
    assert AqaraCameraStream.async_camera_image is not Camera.async_camera_image


async def test_async_camera_image_returns_none_when_no_stream_source(
    hass: HomeAssistant, configured_entry: MockConfigEntry
) -> None:
    """If stream_source returns None, async_camera_image short-circuits to None
    without attempting to call ffmpeg. Verifies the defensive nil check.
    """
    from unittest.mock import AsyncMock, patch

    ip = configured_entry.data["camera_ip"]
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("camera", DOMAIN, f"{ip}_camera_ch1")
    camera = await _camera_entity(hass, entity_id)

    with patch.object(camera, "stream_source", new=AsyncMock(return_value=None)):
        result = await camera.async_camera_image()
        assert result is None


async def test_configuration_url_present_for_valid_ip() -> None:
    """SEC-005: a valid IP yields an http://IP/ configuration_url."""
    cam = AqaraCameraStream(
        camera_ip="192.0.2.10",
        rtsp_username="u",
        rtsp_password="p",
        unique_id="192.0.2.10",
        channel=1,
    )
    assert cam.device_info["configuration_url"] == "http://192.0.2.10/"


async def test_configuration_url_none_for_non_ip() -> None:
    """SEC-005: a non-IP camera_ip (smuggled into .storage) yields None URL."""
    cam = AqaraCameraStream(
        camera_ip="192.0.2.10@evil.com",
        rtsp_username="u",
        rtsp_password="p",
        unique_id="legacy",
        channel=1,
    )
    assert cam.device_info["configuration_url"] is None


async def test_brand_attribute_is_aqara() -> None:
    """_attr_brand is set to 'Aqara' on the class.

    HA's Entity base class converts the class-level ``_attr_brand``
    assignment into a descriptor that backs the public ``brand``
    property, so reading ``cls._attr_brand`` directly returns a
    property object rather than the string. Instead we instantiate an
    entity and read the resolved ``brand`` value.
    """
    cam = AqaraCameraStream(
        camera_ip="192.0.2.10",
        rtsp_username="user",
        rtsp_password="pass",
        unique_id="192.0.2.10",
        channel=1,
    )
    assert cam.brand == "Aqara"


async def test_device_info_model_set_from_registry() -> None:
    """When the entity is built with a known model_code, DeviceInfo.model
    is the registry's full_name."""
    cam = AqaraCameraStream(
        camera_ip="192.0.2.10",
        rtsp_username="u",
        rtsp_password="p",
        unique_id="192.0.2.10",
        channel=1,
        model_code="lumi.camera.agl013",
        instance_name="Doorbell-Camera-G400-0001",
    )
    assert cam.device_info["model"] == "G400 Video Doorbell"
    assert cam.device_info["name"] == "Doorbell-Camera-G400-0001"


async def test_device_info_model_none_when_code_unknown() -> None:
    """Unknown model_code leaves DeviceInfo.model as None."""
    cam = AqaraCameraStream(
        camera_ip="192.0.2.10",
        rtsp_username="u",
        rtsp_password="p",
        unique_id="192.0.2.10",
        channel=1,
        model_code="lumi.camera.unknown999",
        instance_name="",
    )
    assert cam.device_info["model"] is None


async def test_device_info_falls_back_to_ip_name_when_no_instance() -> None:
    """Without instance_name, DeviceInfo.name uses the legacy IP-suffixed form."""
    cam = AqaraCameraStream(
        camera_ip="192.0.2.10",
        rtsp_username="u",
        rtsp_password="p",
        unique_id="192.0.2.10",
        channel=1,
        model_code="",
        instance_name="",
    )
    assert cam.device_info["name"] == "Aqara Camera (192.0.2.10)"
    assert cam.device_info["model"] is None
