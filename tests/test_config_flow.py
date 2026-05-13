"""Tests for the config and options flows."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

try:
    # HA 2024.x+ canonical location.
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
except ImportError:  # pragma: no cover
    from homeassistant.components.zeroconf import ZeroconfServiceInfo

from custom_components.aqara_camera_2_way_audio.const import (
    CONF_BACKCHANNEL_CHANNEL,
    CONF_CAMERA_IP,
    CONF_DEVICE_ID,
    CONF_INSTANCE_NAME,
    CONF_MODEL_CODE,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_USERNAME,
    DOMAIN,
)
from custom_components.aqara_camera_2_way_audio.discovery import DiscoveredCamera


@pytest.fixture
def mock_validate_ok():
    """Patch validate_connection to return True."""
    with patch(
        "custom_components.aqara_camera_2_way_audio.config_flow.validate_connection",
        new=AsyncMock(return_value=True),
    ):
        yield


async def test_manual_step_happy_path(
    hass: HomeAssistant, camera_config_data: dict, mock_validate_ok, mock_talk_client
) -> None:
    """The manual step (legacy IP form) creates an entry on submit."""
    with patch(
        "custom_components.aqara_camera_2_way_audio.config_flow."
        "async_scan_for_aqara_cameras",
        new=AsyncMock(return_value=[]),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
    # No cameras -> jumped to manual form.
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "manual"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], camera_config_data
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Aqara Camera ({camera_config_data[CONF_CAMERA_IP]})"
    assert result["data"][CONF_CAMERA_IP] == camera_config_data[CONF_CAMERA_IP]
    assert result["data"][CONF_MODEL_CODE] == ""


async def test_manual_step_connection_failure(
    hass: HomeAssistant, camera_config_data: dict
) -> None:
    """A failing TCP validate surfaces the cannot_connect error in the manual step."""
    with patch(
        "custom_components.aqara_camera_2_way_audio.config_flow."
        "async_scan_for_aqara_cameras",
        new=AsyncMock(return_value=[]),
    ), patch(
        "custom_components.aqara_camera_2_way_audio.config_flow.validate_connection",
        new=AsyncMock(return_value=False),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["step_id"] == "manual"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], camera_config_data
        )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "manual"
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.parametrize(
    "bad_ip",
    [
        "",
        "not-an-ip",
        "http://10.0.0.1",
        "10.0.0.1; rm -rf /",
        "10.0.0.1 --flag",
        "10.0.0.1@evil.com",
        "evil.com",
        "10.0.0.1/path",
    ],
)
async def test_manual_step_rejects_malformed_ip(
    hass: HomeAssistant, bad_ip: str
) -> None:
    """Non-IP strings are rejected before validate_connection is called."""
    with patch(
        "custom_components.aqara_camera_2_way_audio.config_flow."
        "async_scan_for_aqara_cameras",
        new=AsyncMock(return_value=[]),
    ), patch(
        "custom_components.aqara_camera_2_way_audio.config_flow.validate_connection",
        new=AsyncMock(return_value=True),
    ) as mock_validate:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["step_id"] == "manual"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_CAMERA_IP: bad_ip,
                CONF_RTSP_USERNAME: "user",
                CONF_RTSP_PASSWORD: "pass",
            },
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "manual"
        assert result["errors"] == {"base": "invalid_ip"}
        mock_validate.assert_not_called()


async def test_manual_step_duplicate_aborts(
    hass: HomeAssistant, camera_config_data: dict, mock_validate_ok, mock_talk_client
) -> None:
    """Adding a camera with an IP already configured aborts."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    existing = MockConfigEntry(
        domain=DOMAIN,
        data=camera_config_data,
        unique_id=camera_config_data[CONF_CAMERA_IP],
    )
    existing.add_to_hass(hass)

    with patch(
        "custom_components.aqara_camera_2_way_audio.config_flow."
        "async_scan_for_aqara_cameras",
        new=AsyncMock(return_value=[]),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["step_id"] == "manual"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], camera_config_data
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow_changes_backchannel_channel(
    hass: HomeAssistant, camera_config_data: dict, mock_talk_client
) -> None:
    """Submitting the options form updates entry.options and reloads."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=camera_config_data,
        unique_id=camera_config_data[CONF_CAMERA_IP],
        options={CONF_BACKCHANNEL_CHANNEL: 1},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_BACKCHANNEL_CHANNEL: 3}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_BACKCHANNEL_CHANNEL] == 3


def _zc_info(
    *,
    host: str = "192.0.2.10",
    md: str = "lumi.camera.agl013",
    did: str = "lumi3.0000000000000001",
    instance: str = "Doorbell-Camera-G400-0001",
) -> ZeroconfServiceInfo:
    return ZeroconfServiceInfo(
        ip_address=host,
        ip_addresses=[host],
        port=60334,
        hostname=f"{instance}.local.",
        type="_aqara._tcp.local.",
        name=f"{instance}._aqara._tcp.local.",
        properties={"md": md, "id": did, "fw": "4.5.20", "cl": "aiot"},
    )


async def test_zeroconf_happy_path(
    hass: HomeAssistant, mock_validate_ok, mock_talk_client
) -> None:
    """Zeroconf discovery + credentials submit creates an entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=_zc_info(),
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_RTSP_USERNAME: "u", CONF_RTSP_PASSWORD: "p"},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Doorbell-Camera-G400-0001"
    assert result["data"][CONF_CAMERA_IP] == "192.0.2.10"
    assert result["data"][CONF_MODEL_CODE] == "lumi.camera.agl013"
    assert result["data"][CONF_DEVICE_ID] == "lumi3.0000000000000001"
    assert result["data"][CONF_INSTANCE_NAME] == "Doorbell-Camera-G400-0001"


async def test_zeroconf_aborts_for_non_camera_md(
    hass: HomeAssistant, mock_talk_client
) -> None:
    """If md doesn't start with lumi.camera., the flow aborts cleanly."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=_zc_info(md="lumi.gateway.acn008"),
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "not_a_camera"


async def test_zeroconf_aborts_if_already_configured(
    hass: HomeAssistant, camera_config_data: dict, mock_talk_client
) -> None:
    """A second zeroconf discovery for an already-configured device aborts."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    existing = MockConfigEntry(
        domain=DOMAIN,
        data=camera_config_data,
        unique_id="lumi3.0000000000000001",  # device_id
    )
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=_zc_info(did="lumi3.0000000000000001"),
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_zeroconf_cannot_connect_resurfaces_form(
    hass: HomeAssistant, mock_talk_client
) -> None:
    """If validate_connection fails, the confirm form shows cannot_connect."""
    with patch(
        "custom_components.aqara_camera_2_way_audio.config_flow.validate_connection",
        new=AsyncMock(return_value=False),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=_zc_info(),
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_RTSP_USERNAME: "u", CONF_RTSP_PASSWORD: "p"},
        )
        assert result["type"] == FlowResultType.FORM
        assert result["errors"] == {"base": "cannot_connect"}


async def test_zeroconf_falls_back_to_host_when_device_id_missing(
    hass: HomeAssistant, mock_validate_ok, mock_talk_client
) -> None:
    """A firmware that omits the `id` TXT field still produces a working entry,
    using the discovered host as the unique_id (parity with v1.0 manual entries)."""
    info = _zc_info(did="")
    # Strip `id` from the TXT properties entirely.
    info.properties.pop("id", None)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=info,
    )
    assert result["type"] == FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_RTSP_USERNAME: "u", CONF_RTSP_PASSWORD: "p"},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEVICE_ID] == ""
    # unique_id falls back to the host string.
    assert result["context"]["unique_id"] == "192.0.2.10"


def _fake_discovered(**overrides) -> DiscoveredCamera:
    defaults = dict(
        ip="192.0.2.10",
        device_id="lumi3.0000000000000001",
        model_code="lumi.camera.agl013",
        instance_name="Doorbell-Camera-G400-0001",
        port=60334,
        firmware="4.5.20",
    )
    defaults.update(overrides)
    return DiscoveredCamera(**defaults)


async def test_user_step_offers_discovered_dropdown(
    hass: HomeAssistant, mock_validate_ok, mock_talk_client
) -> None:
    """When the scan finds a camera, the user sees a dropdown selector."""
    camera = _fake_discovered()
    with patch(
        "custom_components.aqara_camera_2_way_audio.config_flow."
        "async_scan_for_aqara_cameras",
        new=AsyncMock(return_value=[camera]),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    # The schema's only key is camera_choice; verify it's there.
    schema_keys = {str(k) for k in result["data_schema"].schema}
    assert "camera_choice" in schema_keys


async def test_user_step_pick_routes_to_credentials(
    hass: HomeAssistant, mock_validate_ok, mock_talk_client
) -> None:
    """Picking a discovered camera routes to the credentials form."""
    camera = _fake_discovered()
    with patch(
        "custom_components.aqara_camera_2_way_audio.config_flow."
        "async_scan_for_aqara_cameras",
        new=AsyncMock(return_value=[camera]),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"camera_choice": camera.device_id}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "credentials"

    # Submit credentials.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_RTSP_USERNAME: "u", CONF_RTSP_PASSWORD: "p"},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CAMERA_IP] == camera.ip
    assert result["data"][CONF_MODEL_CODE] == camera.model_code
    assert result["data"][CONF_DEVICE_ID] == camera.device_id
    assert result["title"] == camera.instance_name


async def test_user_step_pick_manual_routes_to_manual(
    hass: HomeAssistant, mock_validate_ok, mock_talk_client
) -> None:
    """Picking 'Other' routes to the manual IP entry form."""
    camera = _fake_discovered()
    with patch(
        "custom_components.aqara_camera_2_way_audio.config_flow."
        "async_scan_for_aqara_cameras",
        new=AsyncMock(return_value=[camera]),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"camera_choice": "manual"}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "manual"


async def test_user_step_filters_already_configured(
    hass: HomeAssistant, camera_config_data: dict, mock_talk_client
) -> None:
    """Already-configured cameras are filtered out of the dropdown."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    existing = MockConfigEntry(
        domain=DOMAIN,
        data=camera_config_data,
        unique_id="lumi3.0000000000000001",
    )
    existing.add_to_hass(hass)

    camera = _fake_discovered()  # device_id matches the existing entry
    with patch(
        "custom_components.aqara_camera_2_way_audio.config_flow."
        "async_scan_for_aqara_cameras",
        new=AsyncMock(return_value=[camera]),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
    # After filtering, no cameras remain -> jumped to manual.
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "manual"
