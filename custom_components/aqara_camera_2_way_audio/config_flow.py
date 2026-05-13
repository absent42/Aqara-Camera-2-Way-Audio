"""Config flow for the Aqara Camera 2-Way Audio integration."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import Any

import voluptuous as vol

try:
    # HA 2024.x+ canonical location.
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
except ImportError:  # pragma: no cover -- compatibility shim for older HA
    from homeassistant.components.zeroconf import ZeroconfServiceInfo
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_BACKCHANNEL_CHANNEL,
    CONF_CAMERA_IP,
    CONF_DEVICE_ID,
    CONF_INSTANCE_NAME,
    CONF_MODEL_CODE,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_USERNAME,
    CONTROL_PORT,
    DEFAULT_BACKCHANNEL_CHANNEL,
    DOMAIN,
    TCP_CONNECT_TIMEOUT,
)
from .discovery import DiscoveredCamera, async_scan_for_aqara_cameras
from .models import find_by_code

_LOGGER = logging.getLogger(__name__)

USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CAMERA_IP): str,
        vol.Required(CONF_RTSP_USERNAME): str,
        vol.Required(CONF_RTSP_PASSWORD): str,
    }
)


def _is_valid_ip(value: str) -> bool:
    """Return True iff value parses as an IPv4 or IPv6 address."""
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


async def validate_connection(ip: str) -> bool:
    """TCP-connect to the camera control port to verify reachability."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, CONTROL_PORT),
            timeout=TCP_CONNECT_TIMEOUT,
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


def _entry_title(data: dict[str, Any]) -> str:
    """Build a config-entry title from the resolved metadata.

    Prefers the mDNS instance name (e.g. "Doorbell-Camera-G400-0001") when
    available, falls back to "Aqara Camera (<ip>)" for manual entries.
    """
    instance = data.get(CONF_INSTANCE_NAME)
    ip = data.get(CONF_CAMERA_IP)
    if instance:
        return instance
    return f"Aqara Camera ({ip})"


class AqaraCameraConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Aqara Camera 2-Way Audio integration."""

    VERSION = 1

    def __init__(self) -> None:
        # Set across steps when discovery or manual selection populates them.
        self._candidate: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Entry point for `Add Integration`. Scan and either show a picker
        or fall through to manual entry."""
        # First entry only: run the scan and stash candidates.
        if not self._candidate.get("_scan_done"):
            candidates = await self._scan_filtered()
            self._candidate["_scan_done"] = True
            self._candidate["_candidates"] = candidates
            if not candidates:
                # No new cameras visible -- jump straight to manual entry.
                return await self.async_step_manual()

        candidates: list[DiscoveredCamera] = self._candidate["_candidates"]

        if user_input is not None:
            choice = user_input["camera_choice"]
            if choice == "manual":
                return await self.async_step_manual()
            chosen = next((c for c in candidates if c.device_id == choice), None)
            if chosen is None:
                # Defensive: an option that no longer matches a candidate
                # (race with rescan, or stale state). Re-scan.
                self._candidate.pop("_scan_done", None)
                return await self.async_step_user()
            await self.async_set_unique_id(chosen.device_id)
            self._abort_if_unique_id_configured(
                updates={CONF_CAMERA_IP: chosen.ip},
            )
            self._candidate[CONF_CAMERA_IP] = chosen.ip
            self._candidate[CONF_MODEL_CODE] = chosen.model_code
            self._candidate[CONF_DEVICE_ID] = chosen.device_id
            self._candidate[CONF_INSTANCE_NAME] = chosen.instance_name
            return await self.async_step_credentials()

        options = [
            {
                "value": c.device_id,
                "label": f"{c.instance_name} ({c.ip}) -- {self._friendly_for(c.model_code)}",
            }
            for c in candidates
        ]
        options.append({"value": "manual", "label": "Other (enter IP manually)"})

        schema = vol.Schema({
            vol.Required("camera_choice"): SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        })
        return self.async_show_form(step_id="user", data_schema=schema)

    async def _scan_filtered(self) -> list[DiscoveredCamera]:
        """Scan and remove cameras whose device_id is already configured."""
        discovered = await async_scan_for_aqara_cameras(self.hass, timeout=5.0)
        already = {
            e.unique_id
            for e in self.hass.config_entries.async_entries(DOMAIN)
        }
        return [c for c in discovered if c.device_id not in already]

    @staticmethod
    def _friendly_for(model_code: str) -> str:
        """Return registry friendly name, or the raw code if unknown."""
        m = find_by_code(model_code)
        return m.friendly if m else model_code

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manual IP entry -- the fallback when no cameras are discovered
        or the user picks 'Other'."""
        errors: dict[str, str] = {}

        if user_input is not None:
            ip = user_input[CONF_CAMERA_IP]
            if not _is_valid_ip(ip):
                errors["base"] = "invalid_ip"
            else:
                await self.async_set_unique_id(ip)
                self._abort_if_unique_id_configured()
                if await validate_connection(ip):
                    data = {
                        CONF_CAMERA_IP: ip,
                        CONF_RTSP_USERNAME: user_input[CONF_RTSP_USERNAME],
                        CONF_RTSP_PASSWORD: user_input[CONF_RTSP_PASSWORD],
                        # No model auto-detected on the manual path.
                        CONF_MODEL_CODE: "",
                        CONF_DEVICE_ID: "",
                        CONF_INSTANCE_NAME: "",
                    }
                    return self.async_create_entry(
                        title=_entry_title(data), data=data,
                    )
                errors["base"] = "cannot_connect"

        schema = vol.Schema({
            vol.Required(CONF_CAMERA_IP): str,
            vol.Required(CONF_RTSP_USERNAME): str,
            vol.Required(CONF_RTSP_PASSWORD): str,
        })
        # Signal to the UI whether we landed here because no cameras were
        # discovered (so we can render a helpful notice in strings).
        placeholders = {
            "notice": "no_cameras_found" if self._candidate.get("_scan_done") else "",
        }
        return self.async_show_form(
            step_id="manual",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """RTSP creds step for the discovered-and-picked path."""
        errors: dict[str, str] = {}
        if user_input is not None:
            ip = self._candidate[CONF_CAMERA_IP]
            if await validate_connection(ip):
                data = {
                    CONF_CAMERA_IP: ip,
                    CONF_RTSP_USERNAME: user_input[CONF_RTSP_USERNAME],
                    CONF_RTSP_PASSWORD: user_input[CONF_RTSP_PASSWORD],
                    CONF_MODEL_CODE: self._candidate.get(CONF_MODEL_CODE, ""),
                    CONF_DEVICE_ID: self._candidate.get(CONF_DEVICE_ID, ""),
                    CONF_INSTANCE_NAME: self._candidate.get(CONF_INSTANCE_NAME, ""),
                }
                return self.async_create_entry(
                    title=_entry_title(data), data=data,
                )
            errors["base"] = "cannot_connect"

        schema = vol.Schema({
            vol.Required(CONF_RTSP_USERNAME): str,
            vol.Required(CONF_RTSP_PASSWORD): str,
        })
        placeholders = {
            "name": self._candidate.get(CONF_INSTANCE_NAME, "")
                     or self._candidate.get(CONF_CAMERA_IP, ""),
            "model": self._friendly_for(self._candidate.get(CONF_MODEL_CODE, "")),
        }
        return self.async_show_form(
            step_id="credentials",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a camera discovered via HA's bundled zeroconf component."""
        props = discovery_info.properties or {}
        model_code = str(props.get("md", ""))
        device_id = str(props.get("id", ""))
        # info.name is the full mDNS name; the instance is everything before
        # the service-type suffix.
        full = discovery_info.name or ""
        instance = full.split("._aqara._tcp.", 1)[0] if "._aqara._tcp." in full else full

        if not model_code.startswith("lumi.camera."):
            # Manifest filter should make this unreachable; defensive.
            return self.async_abort(reason="not_a_camera")

        unique = device_id or str(discovery_info.host)
        await self.async_set_unique_id(unique)
        self._abort_if_unique_id_configured(
            updates={CONF_CAMERA_IP: str(discovery_info.host)},
        )

        self._candidate = {
            CONF_CAMERA_IP: str(discovery_info.host),
            CONF_MODEL_CODE: model_code,
            CONF_DEVICE_ID: device_id,
            CONF_INSTANCE_NAME: instance,
        }
        model = find_by_code(model_code)
        self.context["title_placeholders"] = {
            "name": instance or f"Aqara Camera ({discovery_info.host})",
            "model": model.friendly if model else model_code,
        }
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user for RTSP credentials for a zeroconf-discovered camera."""
        errors: dict[str, str] = {}
        if user_input is not None:
            ip = self._candidate[CONF_CAMERA_IP]
            if await validate_connection(ip):
                data = {
                    CONF_CAMERA_IP: ip,
                    CONF_RTSP_USERNAME: user_input[CONF_RTSP_USERNAME],
                    CONF_RTSP_PASSWORD: user_input[CONF_RTSP_PASSWORD],
                    CONF_MODEL_CODE: self._candidate.get(CONF_MODEL_CODE, ""),
                    CONF_DEVICE_ID: self._candidate.get(CONF_DEVICE_ID, ""),
                    CONF_INSTANCE_NAME: self._candidate.get(CONF_INSTANCE_NAME, ""),
                }
                return self.async_create_entry(
                    title=_entry_title(data), data=data,
                )
            errors["base"] = "cannot_connect"

        schema = vol.Schema({
            vol.Required(CONF_RTSP_USERNAME): str,
            vol.Required(CONF_RTSP_PASSWORD): str,
        })
        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders=self.context.get("title_placeholders", {}),
        )

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> "AqaraCameraOptionsFlow":
        return AqaraCameraOptionsFlow(config_entry)


class AqaraCameraOptionsFlow(OptionsFlow):
    """Options flow for an Aqara Camera entry."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # NumberSelector returns floats; cast to int at storage time so
            # entry.options[CONF_BACKCHANNEL_CHANNEL] is always a clean int.
            user_input[CONF_BACKCHANNEL_CHANNEL] = int(user_input[CONF_BACKCHANNEL_CHANNEL])
            return self.async_create_entry(title="", data=user_input)

        current = self._entry.options.get(
            CONF_BACKCHANNEL_CHANNEL, DEFAULT_BACKCHANNEL_CHANNEL
        )

        schema = vol.Schema({
            vol.Required(CONF_BACKCHANNEL_CHANNEL, default=current): NumberSelector(
                NumberSelectorConfig(min=1, max=3, step=1, mode=NumberSelectorMode.BOX)
            )
        })

        return self.async_show_form(step_id="init", data_schema=schema)
