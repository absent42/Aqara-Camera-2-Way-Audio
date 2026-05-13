"""go2rtc stream registration for the Aqara camera backchannel.

Registers a go2rtc stream with three producers: an RTSP source for the
camera video/audio, an ffmpeg source that re-exposes the stream with
Opus audio for WebRTC compatibility, and an exec source that runs
bridge.py to ferry the WebRTC mic audio back to the camera speaker.

Registration is done by writing to go2rtc.yaml because go2rtc blocks
exec sources via REST API for security. The integration continues to
work without go2rtc -- only the WebRTC backchannel is affected.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from urllib.parse import quote

import yaml

from .const import RTSP_PORT

_LOGGER = logging.getLogger(__name__)

GO2RTC_CONFIG_FILE = "go2rtc.yaml"

# Serialises read-modify-write on go2rtc.yaml across threads (the integration
# dispatches register_stream/remove_stream via hass.async_add_executor_job, so
# concurrent setup of two camera entries would otherwise race).
_CONFIG_LOCK = threading.Lock()


class _QuotedDumper(yaml.SafeDumper):
    # Empty subclass so add_representer below does not mutate the global
    # SafeDumper state shared with the rest of the process.
    pass


def _quoted_str_representer(dumper: yaml.Dumper, data: str) -> yaml.ScalarNode:
    if "#" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_QuotedDumper.add_representer(str, _quoted_str_representer)


def _dump_config(config: dict) -> str:
    """Dump go2rtc config to YAML, quoting strings that contain #."""
    return yaml.dump(config, Dumper=_QuotedDumper, default_flow_style=False, width=4096)


def _read_config(config_path: Path) -> dict:
    """Read go2rtc.yaml, returning {} if absent. Atomic (no exists() probe)."""
    try:
        raw = config_path.read_text()
    except FileNotFoundError:
        return {}
    return yaml.safe_load(raw) or {}


def _write_config(config_path: Path, config: dict) -> None:
    config_path.write_text(_dump_config(config))


def build_exec_source(config_dir: str, camera_ip: str) -> str:
    """Build the go2rtc exec source URL for the backchannel bridge.

    The exec source tells go2rtc to spawn the bridge script when a browser
    microphone connects. HA's bundled go2rtc uses stdin.NewClient which
    hardcodes PCMA/8kHz for backchannel audio (the #audio= parameter is
    ignored despite the binary reporting as v1.9.14).

    go2rtc uses exec.Command (not a shell), so we pass the absolute script
    path. go2rtc's ParseQuery splits on # (not &) for multiple parameters.
    """
    # Defense in depth: the exec source is space-delimited, so any whitespace
    # in camera_ip would inject extra argv elements into bridge.py.
    assert not any(c.isspace() for c in camera_ip), "camera_ip must not contain whitespace"
    python = sys.executable
    bridge_script = str(
        Path(config_dir)
        / "custom_components"
        / "aqara_camera_2_way_audio"
        / "bridge.py"
    )
    return f"exec:{python} {bridge_script} {camera_ip}#backchannel=1"


def build_rtsp_source(
    camera_ip: str, username: str, password: str, channel: int = 1
) -> str:
    """Build the RTSP source URL for the camera stream on a given channel."""
    user = quote(username, safe="")
    pwd = quote(password, safe="")
    return f"rtsp://{user}:{pwd}@{camera_ip}:{RTSP_PORT}/ch{channel}"


def register_stream(
    config_dir: str,
    stream_name: str,
    sources: list[str],
) -> str:
    """Add a stream to go2rtc.yaml.

    Returns one of:
      - "wrote": new content written
      - "noop": this exact stream + sources already in the file
      - "skipped": a stream by this name exists with different sources NOT
        matching ours -- treated as a collision with another integration
        or another camera entry; do not overwrite
      - "error": IO or parse failure (logged, never raised)
    """
    config_path = Path(config_dir) / GO2RTC_CONFIG_FILE
    try:
        with _CONFIG_LOCK:
            config = _read_config(config_path)
            streams = config.setdefault("streams", {})
            existing = streams.get(stream_name)

            if existing is not None:
                if set(existing) == set(sources):
                    _LOGGER.debug("go2rtc stream %s already current", stream_name)
                    return "noop"
                # Refuse to clobber if the existing entry is plausibly not
                # ours. An entry whose exec source does not match ours
                # belongs to another camera (or another integration).
                if not _stream_belongs_to_us(existing, sources):
                    _LOGGER.warning(
                        "go2rtc stream %s already exists with different sources "
                        "(possible collision). Not overwriting.",
                        stream_name,
                    )
                    return "skipped"

            streams[stream_name] = sources
            _write_config(config_path, config)
        _LOGGER.info(
            "go2rtc stream '%s' written to %s. Restart HA for backchannel to take effect.",
            stream_name,
            config_path,
        )
        return "wrote"

    except Exception:
        _LOGGER.warning("Failed to write go2rtc config", exc_info=True)
        return "error"


def _stream_belongs_to_us(existing_sources: list[str], our_sources: list[str]) -> bool:
    """Heuristic: same stream name belongs to us if our exec source matches.

    The exec source encodes the bridge.py absolute path and the camera IP,
    making it a strong ownership signature.
    """
    our_exec = next((s for s in our_sources if s.startswith("exec:")), None)
    if our_exec is None:
        return False
    return our_exec in existing_sources


def remove_stream(config_dir: str, stream_name: str) -> None:
    """Remove a stream from go2rtc.yaml. Best-effort, never raises."""
    config_path = Path(config_dir) / GO2RTC_CONFIG_FILE

    try:
        with _CONFIG_LOCK:
            config = _read_config(config_path)
            streams = config.get("streams", {})
            if stream_name not in streams:
                return
            del streams[stream_name]
            _write_config(config_path, config)
        _LOGGER.info("go2rtc stream '%s' removed from %s", stream_name, config_path)
    except Exception:
        _LOGGER.warning("Failed to update go2rtc config", exc_info=True)
