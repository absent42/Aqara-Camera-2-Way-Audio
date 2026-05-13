"""Shared fixtures for the aqara_camera_2_way_audio test suite."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.aqara_camera_2_way_audio.const import (
    CONF_CAMERA_IP,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_USERNAME,
    DOMAIN,
)

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow the custom_components/ tree to be loaded by HA in tests."""
    yield


@pytest.fixture
def camera_config_data() -> dict:
    """Standard config-entry data for a test camera."""
    return {
        CONF_CAMERA_IP: "192.0.2.10",
        CONF_RTSP_USERNAME: "user",
        CONF_RTSP_PASSWORD: "pass",
    }


@pytest.fixture
def mock_talk_client():
    """Patch AqaraLanTalkClient with an AsyncMock.

    Yields the AsyncMock so tests can inspect calls or assert on
    connect()/disconnect()/send_audio_frame() behaviour.
    """
    mock = AsyncMock()
    mock.is_connected = False
    mock.connect = AsyncMock(return_value=True)
    mock.disconnect = AsyncMock()
    with patch(
        "custom_components.aqara_camera_2_way_audio.AqaraLanTalkClient",
        return_value=mock,
    ):
        yield mock
