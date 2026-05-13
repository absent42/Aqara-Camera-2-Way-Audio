"""mDNS-based discovery of Aqara cameras on the LAN.

Aqara cameras advertise `_aqara._tcp.local.` with a TXT record that
includes `md` (model code, e.g. `lumi.camera.agl013`) and `id` (device
ID, e.g. `lumi3.1234567890123456`). This module exposes a single
coroutine that browses for ~5 seconds and returns one record per
reachable camera.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from zeroconf import ServiceStateChange
from zeroconf.asyncio import (
    AsyncServiceBrowser,
    AsyncServiceInfo,
    AsyncZeroconf,
)

try:
    from homeassistant.components import zeroconf as ha_zeroconf
except Exception:  # pragma: no cover -- standalone test path
    ha_zeroconf = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)

AQARA_SERVICE_TYPE = "_aqara._tcp.local."
_RESOLVE_TIMEOUT_MS = 2000
_MODEL_PREFIX = "lumi.camera."


@dataclass(frozen=True)
class DiscoveredCamera:
    """One Aqara camera resolved on the LAN."""

    ip: str                 # IPv4 dotted-quad
    device_id: str          # TXT `id`, e.g. `lumi3.0000000000000001`
    model_code: str         # TXT `md`, e.g. `lumi.camera.agl013`
    instance_name: str      # mDNS instance name, e.g. `Doorbell-Camera-G400-0001`
    port: int               # advertised port (rotates on reboot; informational only)
    firmware: str           # TXT `fw`, e.g. `4.5.20`


async def async_scan_for_aqara_cameras(
    hass: Any | None = None,
    *,
    timeout: float = 5.0,
) -> list[DiscoveredCamera]:
    """Browse `_aqara._tcp.local.` and return one record per camera with `md` set.

    Uses HA's shared `AsyncZeroconf` instance when called inside HA; falls
    back to a fresh local instance for tests / dev tools.

    Records are deduplicated by `device_id` (each physical camera produces
    exactly one row even if it answers on both IPv4 and IPv6, or on
    multiple service types).
    """
    aiozc, owns_aiozc = await _get_zeroconf(hass)

    found: dict[str, DiscoveredCamera] = {}
    resolve_tasks: list[asyncio.Task[None]] = []

    def _on_change(*, zeroconf, service_type, name, state_change):
        if state_change != ServiceStateChange.Added:
            return
        resolve_tasks.append(
            asyncio.create_task(_resolve(aiozc.zeroconf, service_type, name, found))
        )

    browser = AsyncServiceBrowser(
        aiozc.zeroconf, AQARA_SERVICE_TYPE, handlers=[_on_change]
    )
    try:
        await asyncio.sleep(timeout)
    finally:
        await browser.async_cancel()
        if resolve_tasks:
            await asyncio.gather(*resolve_tasks, return_exceptions=True)
        if owns_aiozc:
            await aiozc.async_close()

    return sorted(found.values(), key=lambda c: c.ip)


async def _get_zeroconf(hass: Any | None) -> tuple[AsyncZeroconf, bool]:
    """Return (AsyncZeroconf, owns_it). True `owns_it` means caller must close."""
    if hass is not None and ha_zeroconf is not None:
        try:
            return await ha_zeroconf.async_get_async_instance(hass), False
        except AttributeError:
            pass
    return AsyncZeroconf(), True


async def _resolve(
    zc,
    service_type: str,
    name: str,
    out: dict[str, DiscoveredCamera],
) -> None:
    """Resolve a discovered instance into a DiscoveredCamera if it's a camera."""
    info = AsyncServiceInfo(service_type, name)
    try:
        ok = await info.async_request(zc, _RESOLVE_TIMEOUT_MS)
    except Exception:
        return
    if not ok:
        return

    props = _decode_txt(getattr(info, "properties", None) or {})
    model_code = props.get("md", "")
    device_id = props.get("id", "")
    if not model_code.startswith(_MODEL_PREFIX):
        return

    # Prefer IPv4 for the IP we hand back -- HA's RTSP/stream plumbing
    # is happier with v4 and the selector UI is simpler if we don't
    # surface v6 dotted-quads.
    ipv4 = None
    for addr in info.parsed_addresses() if hasattr(info, "parsed_addresses") else []:
        if ":" not in addr:
            ipv4 = addr
            break
    if not ipv4:
        return

    instance = name.split("." + service_type, 1)[0] if service_type in name else name
    record = DiscoveredCamera(
        ip=ipv4,
        device_id=device_id,
        model_code=model_code,
        instance_name=instance,
        port=info.port or 0,
        firmware=props.get("fw", ""),
    )
    # Dedup on device_id so we keep one record per physical camera.
    out.setdefault(device_id, record)


def _decode_txt(raw: dict) -> dict[str, str]:
    """Decode zeroconf TXT properties (bytes -> str)."""
    out: dict[str, str] = {}
    for k, v in raw.items():
        ks = k.decode("utf-8", errors="replace") if isinstance(k, bytes) else str(k)
        if v is None:
            out[ks] = ""
        elif isinstance(v, bytes):
            out[ks] = v.decode("utf-8", errors="replace")
        else:
            out[ks] = str(v)
    return out
