"""Camera entities for the Aqara Camera 2-Way Audio integration.

Each configured camera produces three Camera entities, one per RTSP
channel (high / medium / low quality). All three share one DeviceInfo
so they appear under a single device in Home Assistant.
"""

from __future__ import annotations

import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CHANNELS
from .entity import AqaraCameraBaseEntity
from .go2rtc import build_rtsp_source

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one Camera entity per RTSP channel."""
    data = entry.runtime_data
    async_add_entities(
        AqaraCameraStream(
            camera_ip=data.camera_ip,
            rtsp_username=data.rtsp_username,
            rtsp_password=data.rtsp_password,
            unique_id=entry.unique_id,
            channel=channel,
            model_code=data.model_code,
            instance_name=data.instance_name,
        )
        for channel in CHANNELS
    )


class AqaraCameraStream(AqaraCameraBaseEntity, Camera):
    """One quality stream from an Aqara camera."""

    _attr_brand = "Aqara"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        camera_ip: str,
        rtsp_username: str,
        rtsp_password: str,
        unique_id: str,
        channel: int,
        *,
        model_code: str = "",
        instance_name: str = "",
    ) -> None:
        AqaraCameraBaseEntity.__init__(
            self,
            camera_ip,
            unique_id,
            model_code=model_code,
            instance_name=instance_name,
        )
        Camera.__init__(self)
        self._rtsp_username = rtsp_username
        self._rtsp_password = rtsp_password
        self._channel = channel
        self._attr_unique_id = f"{unique_id}_camera_ch{channel}"
        self._attr_translation_key = f"camera_ch{channel}"

    async def stream_source(self) -> str | None:
        return build_rtsp_source(
            self._camera_ip,
            self._rtsp_username,
            self._rtsp_password,
            channel=self._channel,
        )

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a JPEG snapshot by pulling a frame from the RTSP stream.

        HA's base Camera class delegates async_camera_image to the sync
        camera_image method by default, which raises NotImplementedError
        for RTSP-only cameras. We override here to use ffmpeg's
        async_get_image, the canonical pattern for RTSP cameras.

        The import is deferred so module-load doesn't fail in environments
        where the ffmpeg integration's transitive haffmpeg dependency
        isn't installed (e.g. test envs).
        """
        from homeassistant.components.ffmpeg import async_get_image

        stream_url = await self.stream_source()
        if not stream_url:
            return None
        return await async_get_image(
            self.hass, stream_url, width=width, height=height,
        )
