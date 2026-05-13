"""Aqara Camera 2-Way Audio integration."""

from __future__ import annotations

import asyncio
import logging
import pathlib
from dataclasses import dataclass

import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from . import go2rtc as go2rtc_mod
from .const import (
    AAC_FRAME_SAMPLES,
    CONF_BACKCHANNEL_CHANNEL,
    CONF_CAMERA_IP,
    CONF_INSTANCE_NAME,
    CONF_MODEL_CODE,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_USERNAME,
    DEFAULT_BACKCHANNEL_CHANNEL,
    DOMAIN,
    FRAME_DURATION,
    MAX_AAC_FILE_BYTES,
    PLATFORMS,
)
from .protocol import extract_adts_frames
from .talk import AqaraLanTalkClient

_LOGGER = logging.getLogger(__name__)

type AqaraCameraConfigEntry = ConfigEntry[AqaraCameraRuntimeData]

TALK_AUDIO_SCHEMA = vol.Schema(
    {
        vol.Required("file_path"): str,
        vol.Optional("entity_id"): vol.Any(str, [str]),
        vol.Optional("device_id"): vol.Any(str, [str]),
    },
    extra=vol.ALLOW_EXTRA,
)


@dataclass
class AqaraCameraRuntimeData:
    """Runtime data attached to each AqaraCameraConfigEntry."""

    camera_ip: str
    rtsp_username: str
    rtsp_password: str
    talk_client: AqaraLanTalkClient
    go2rtc_stream_name: str | None = None
    # Discovery-populated (optional; empty string on v1.0-pre-discovery entries).
    model_code: str = ""
    instance_name: str = ""


def _resolve_target_entries(
    hass: HomeAssistant, call: ServiceCall
) -> list[AqaraCameraConfigEntry]:
    """Resolve service-call entity_id / device_id to this integration's entries.

    Returns a deduplicated list of config entries. Multiple entity_ids
    belonging to the same camera resolve to a single entry, so the caller
    sees one talk_client per camera, not one per entity.
    """
    entry_ids: set[str] = set()

    raw_entity_ids = call.data.get("entity_id")
    if raw_entity_ids:
        ent_reg = er.async_get(hass)
        ids = [raw_entity_ids] if isinstance(raw_entity_ids, str) else raw_entity_ids
        for eid in ids:
            ent = ent_reg.async_get(eid)
            if ent and ent.config_entry_id:
                entry_ids.add(ent.config_entry_id)

    raw_device_ids = call.data.get("device_id")
    if raw_device_ids:
        dev_reg = dr.async_get(hass)
        ids = [raw_device_ids] if isinstance(raw_device_ids, str) else raw_device_ids
        for did in ids:
            dev = dev_reg.async_get(did)
            if dev:
                for cid in dev.config_entries:
                    entry_ids.add(cid)

    entries: list[AqaraCameraConfigEntry] = []
    for entry_id in entry_ids:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry and entry.domain == DOMAIN and hasattr(entry, "runtime_data"):
            entries.append(entry)
    return entries


async def async_setup_entry(
    hass: HomeAssistant, entry: AqaraCameraConfigEntry
) -> bool:
    """Set up an Aqara Camera config entry."""
    talk_client = AqaraLanTalkClient(camera_ip=entry.data[CONF_CAMERA_IP])

    entry.runtime_data = AqaraCameraRuntimeData(
        camera_ip=entry.data[CONF_CAMERA_IP],
        rtsp_username=entry.data[CONF_RTSP_USERNAME],
        rtsp_password=entry.data[CONF_RTSP_PASSWORD],
        talk_client=talk_client,
        model_code=entry.data.get(CONF_MODEL_CODE, ""),
        instance_name=entry.data.get(CONF_INSTANCE_NAME, ""),
    )

    if not hass.services.has_service(DOMAIN, "talk_audio_file"):
        async def handle_talk_audio_file(call: ServiceCall) -> None:
            file_path = call.data["file_path"]
            if not hass.config.is_allowed_path(file_path):
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="path_not_allowed",
                    translation_placeholders={"file_path": file_path},
                )

            entries = _resolve_target_entries(hass, call)
            if not entries:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="no_camera_target",
                )

            for target_entry in entries:
                rt = target_entry.runtime_data
                if not rt.talk_client.is_connected:
                    if not await rt.talk_client.connect():
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="connect_failed",
                        )
                await _send_aac_file(hass, rt.talk_client, file_path)

        hass.services.async_register(
            DOMAIN, "talk_audio_file", handle_talk_audio_file, schema=TALK_AUDIO_SCHEMA,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await _setup_go2rtc_config(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_options_update_listener))
    return True


async def _setup_go2rtc_config(
    hass: HomeAssistant, entry: AqaraCameraConfigEntry
) -> None:
    """Write the go2rtc stream config for this camera.

    A persistent notification fires only when the file actually changed,
    so HA restarts where the config is already current do not nag the user.
    """
    data = entry.runtime_data
    stream_name = f"aqara_camera_{data.camera_ip.replace('.', '_')}"

    channel = int(entry.options.get(
        CONF_BACKCHANNEL_CHANNEL, DEFAULT_BACKCHANNEL_CHANNEL
    ))

    rtsp_source = go2rtc_mod.build_rtsp_source(
        data.camera_ip, data.rtsp_username, data.rtsp_password, channel=channel,
    )
    exec_source = go2rtc_mod.build_exec_source(hass.config.config_dir, data.camera_ip)
    ffmpeg_source = f"ffmpeg:{stream_name}#video=copy#audio=opus#audio=copy"

    entry.runtime_data.go2rtc_stream_name = stream_name

    result = await hass.async_add_executor_job(
        go2rtc_mod.register_stream,
        hass.config.config_dir,
        stream_name,
        [rtsp_source, ffmpeg_source, exec_source],
    )

    if result == "wrote":
        notif_id = (
            f"aqara_camera_2_way_audio_go2rtc_restart_"
            f"{data.camera_ip.replace('.', '_')}"
        )
        persistent_notification.async_create(
            hass,
            (
                f"Aqara camera {data.camera_ip}: go2rtc stream config updated. "
                "Restart Home Assistant for the 2-way audio backchannel to take effect."
            ),
            title="Aqara Camera",
            notification_id=notif_id,
        )


async def async_unload_entry(
    hass: HomeAssistant, entry: AqaraCameraConfigEntry
) -> bool:
    """Unload an Aqara Camera config entry."""
    await entry.runtime_data.talk_client.disconnect()

    if entry.runtime_data.go2rtc_stream_name:
        await hass.async_add_executor_job(
            go2rtc_mod.remove_stream,
            hass.config.config_dir,
            entry.runtime_data.go2rtc_stream_name,
        )

    result = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Only count entries that are still loaded; the entry being unloaded is
    # already in the UNLOAD_IN_PROGRESS / NOT_LOADED state by the time we get
    # here, so we filter on loaded-state rather than entry_id identity.
    remaining_loaded = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id and e.state is ConfigEntryState.LOADED
    ]
    if not remaining_loaded:
        hass.services.async_remove(DOMAIN, "talk_audio_file")

    return result


async def _options_update_listener(
    hass: HomeAssistant, entry: AqaraCameraConfigEntry
) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _send_aac_file(
    hass: HomeAssistant, client: AqaraLanTalkClient, file_path: str
) -> None:
    """Send an AAC-ADTS file through the talk client."""
    path = pathlib.Path(file_path)
    stat = await hass.async_add_executor_job(path.stat)
    if stat.st_size > MAX_AAC_FILE_BYTES:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="file_too_large",
            translation_placeholders={
                "size": str(stat.st_size),
                "max_size": str(MAX_AAC_FILE_BYTES),
            },
        )
    audio_data = await hass.async_add_executor_job(path.read_bytes)

    frames = extract_adts_frames(audio_data)
    if not frames:
        return

    loop = asyncio.get_running_loop()
    start = loop.time()
    for i, frame in enumerate(frames):
        if not client.is_connected:
            break
        client.send_audio_frame(frame, i * AAC_FRAME_SAMPLES)
        # Drift-corrected pacing: each frame's deadline is anchored to start,
        # so per-frame send overhead does not accumulate over a long file.
        target = start + (i + 1) * FRAME_DURATION
        delay = target - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
