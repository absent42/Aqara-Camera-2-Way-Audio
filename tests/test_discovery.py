"""Tests for the LAN discovery helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.aqara_camera_2_way_audio.discovery import (
    AQARA_SERVICE_TYPE,
    DiscoveredCamera,
    async_scan_for_aqara_cameras,
)


def _make_service_info(
    *,
    addresses_v4: list[str],
    txt: dict[str, str],
    port: int = 60334,
    addresses_v6: list[str] | None = None,
) -> MagicMock:
    """Build a MagicMock that quacks like AsyncServiceInfo."""
    info = MagicMock()
    info.async_request = AsyncMock(return_value=True)
    info.parsed_addresses = MagicMock(
        return_value=list(addresses_v4) + list(addresses_v6 or [])
    )
    info.properties = {k.encode(): v.encode() for k, v in txt.items()}
    info.port = port
    return info


class _FakeBrowser:
    """Fires `_on_change` callbacks for a fixed list of (service_type, name)."""

    def __init__(self, _zc, service_type, handlers):
        self._handlers = handlers
        self._service_type = service_type

    def fire(self, name: str) -> None:
        from zeroconf import ServiceStateChange
        for h in self._handlers:
            h(
                zeroconf=None,
                service_type=self._service_type,
                name=name,
                state_change=ServiceStateChange.Added,
            )

    async def async_cancel(self) -> None:
        return


@pytest.fixture
def fake_zeroconf():
    """Patch AsyncZeroconf + AsyncServiceBrowser + AsyncServiceInfo."""
    browsers: list[_FakeBrowser] = []
    info_responses: dict[str, MagicMock] = {}

    class FakeAsyncZeroconf:
        def __init__(self):
            self.zeroconf = MagicMock()

        async def async_close(self):
            return

    def fake_browser(zc, service_type, handlers):
        b = _FakeBrowser(zc, service_type, handlers)
        browsers.append(b)
        return b

    def fake_service_info(service_type, name):
        # The keys we register are by name (instance name) only.
        info = info_responses.get(name, MagicMock())
        info.async_request = AsyncMock(return_value=name in info_responses)
        return info

    with patch(
        "custom_components.aqara_camera_2_way_audio.discovery.AsyncZeroconf",
        FakeAsyncZeroconf,
    ), patch(
        "custom_components.aqara_camera_2_way_audio.discovery.AsyncServiceBrowser",
        fake_browser,
    ), patch(
        "custom_components.aqara_camera_2_way_audio.discovery.AsyncServiceInfo",
        fake_service_info,
    ):
        yield (browsers, info_responses)


@pytest.mark.asyncio
async def test_scan_finds_a_camera(fake_zeroconf, monkeypatch):
    browsers, info_responses = fake_zeroconf
    # Stage a fake service.
    instance = "Doorbell-Camera-G400-0001"
    full_name = f"{instance}.{AQARA_SERVICE_TYPE}"
    info_responses[full_name] = _make_service_info(
        addresses_v4=["192.0.2.10"],
        addresses_v6=["fe80::1"],
        txt={
            "md": "lumi.camera.agl013",
            "id": "lumi3.0000000000000001",
            "fw": "4.5.20",
        },
    )

    # Speed up the await asyncio.sleep(timeout) inside the helper.
    async def fast_sleep(secs):
        # Fire browser events on the first call to give resolution a chance.
        if browsers:
            browsers[0].fire(full_name)
        return

    monkeypatch.setattr(
        "custom_components.aqara_camera_2_way_audio.discovery.asyncio.sleep",
        fast_sleep,
    )

    result = await async_scan_for_aqara_cameras(timeout=0.0)
    assert len(result) == 1
    assert result[0] == DiscoveredCamera(
        ip="192.0.2.10",
        device_id="lumi3.0000000000000001",
        model_code="lumi.camera.agl013",
        instance_name="Doorbell-Camera-G400-0001",
        port=60334,
        firmware="4.5.20",
    )


@pytest.mark.asyncio
async def test_scan_skips_non_camera_md(fake_zeroconf, monkeypatch):
    browsers, info_responses = fake_zeroconf
    full_name = f"Hub-M3-aaaa.{AQARA_SERVICE_TYPE}"
    info_responses[full_name] = _make_service_info(
        addresses_v4=["10.1.20.20"],
        txt={"md": "lumi.gateway.acn008", "id": "lumi1.xxx"},
    )

    async def fast_sleep(secs):
        if browsers:
            browsers[0].fire(full_name)
        return

    monkeypatch.setattr(
        "custom_components.aqara_camera_2_way_audio.discovery.asyncio.sleep",
        fast_sleep,
    )

    result = await async_scan_for_aqara_cameras(timeout=0.0)
    assert result == []


@pytest.mark.asyncio
async def test_scan_dedups_by_device_id(fake_zeroconf, monkeypatch):
    """Same device_id seen twice (e.g. v4 and v6 records) produces one row."""
    browsers, info_responses = fake_zeroconf
    shared_did = "lumi3.0000000000000002"
    name_a = f"Camera-G100-0002.{AQARA_SERVICE_TYPE}"
    name_b = f"Camera-G100-0002.duplicate.{AQARA_SERVICE_TYPE}"
    info_responses[name_a] = _make_service_info(
        addresses_v4=["192.0.2.11"],
        txt={"md": "lumi.camera.agl005", "id": shared_did},
    )
    info_responses[name_b] = _make_service_info(
        addresses_v4=["192.0.2.11"],
        txt={"md": "lumi.camera.agl005", "id": shared_did},
    )

    async def fast_sleep(secs):
        if browsers:
            browsers[0].fire(name_a)
            browsers[0].fire(name_b)
        return

    monkeypatch.setattr(
        "custom_components.aqara_camera_2_way_audio.discovery.asyncio.sleep",
        fast_sleep,
    )

    result = await async_scan_for_aqara_cameras(timeout=0.0)
    assert len(result) == 1
    assert result[0].device_id == shared_did


@pytest.mark.asyncio
async def test_scan_returns_empty_when_no_services(fake_zeroconf, monkeypatch):
    async def fast_sleep(secs):
        return
    monkeypatch.setattr(
        "custom_components.aqara_camera_2_way_audio.discovery.asyncio.sleep",
        fast_sleep,
    )
    result = await async_scan_for_aqara_cameras(timeout=0.0)
    assert result == []
