"""Base entity for the Aqara Camera 2-Way Audio integration."""

from __future__ import annotations

import ipaddress

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .models import find_by_code


def _configuration_url_for(camera_ip: str) -> str | None:
    """Build the device-page URL, returning None for non-IP values.

    Defense in depth against a malformed camera_ip surviving in .storage
    from an entry created before config-flow IP validation existed: an
    invalid value would render as a clickable open-redirect in the HA UI.
    """
    try:
        ipaddress.ip_address(camera_ip)
    except ValueError:
        return None
    return f"http://{camera_ip}/"


def _device_name(camera_ip: str, instance_name: str) -> str:
    """Prefer the mDNS instance name (e.g. 'Doorbell-Camera-G400-0001') when
    available; fall back to the IP-suffixed form for manual entries."""
    if instance_name:
        return instance_name
    return f"Aqara Camera ({camera_ip})"


class AqaraCameraBaseEntity(Entity):
    """Base class for Aqara Camera entities.

    Provides shared DeviceInfo so all entities for one camera (the three
    quality streams) group under a single Home Assistant device.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        camera_ip: str,
        unique_id: str,
        *,
        model_code: str = "",
        instance_name: str = "",
    ) -> None:
        self._camera_ip = camera_ip
        model = find_by_code(model_code) if model_code else None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, unique_id)},
            manufacturer="Aqara",
            model=model.full_name if model else None,
            name=_device_name(camera_ip, instance_name),
            configuration_url=_configuration_url_for(camera_ip),
        )
