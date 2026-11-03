"""go2rtc stream registration for the Aqara camera backchannel.

Registers a go2rtc stream with three producers: an RTSP source for the
camera video/audio, an ffmpeg source that re-exposes the stream with
Opus audio for WebRTC compatibility, and an exec source that runs
bridge.py to ferry the WebRTC mic audio back to the camera speaker.

Registration is done by writing to go2rtc.yaml because go2rtc blocks
exec sources via REST API for security. The integration continues to
work without go2rtc -- only the WebRTC backchannel is affected.

Ownership is tracked by a sources digest saved in the HA config entry.
Only an unmodified owned stream (or the exact legacy output of this
integration) is ever rewritten or removed; customized and foreign
entries are left untouched. Errors are logged without exception text,
since YAML errors can embed credential-bearing source lines.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
from collections import Counter
from pathlib import Path
from typing import TypeGuard
from urllib.parse import quote

import yaml

from .const import RTSP_PORT

_LOGGER = logging.getLogger(__name__)
GO2RTC_CONFIG_FILE = "go2rtc.yaml"
_LEGACY_EXEC_SUFFIX = "#backchannel=1"
_EXEC_SUFFIX = "#backchannel=1#audio=pcma/8000#killsignal=15#killtimeout=10"
# Stored in the HA config entry, not go2rtc.yaml; contains no source credentials.
CONF_GO2RTC_DIGEST = "go2rtc_sources_digest"
CONF_GO2RTC_STREAM_NAME = "go2rtc_stream_name"

# Serialises read-modify-write on go2rtc.yaml across threads (the integration
# dispatches register_stream/remove_stream via hass.async_add_executor_job, so
# concurrent setup of two camera entries would otherwise race).
_CONFIG_LOCK = threading.Lock()


class _QuotedDumper(yaml.SafeDumper):
    # Empty subclass so add_representer below does not mutate the global
    # SafeDumper state shared with the rest of the process.
    pass


def _quoted_str_representer(dumper: yaml.Dumper, data: str) -> yaml.ScalarNode:
    return dumper.represent_scalar(
        "tag:yaml.org,2002:str", data, style='"' if "#" in data else None
    )


_QuotedDumper.add_representer(str, _quoted_str_representer)


def _dump_config(config: dict) -> str:
    """Dump go2rtc config to YAML, quoting strings that contain #."""
    return yaml.dump(config, Dumper=_QuotedDumper, default_flow_style=False, width=4096)


def _read_config(config_path: Path) -> dict:
    """Return a mapping, rejecting non-mapping YAML roots without writing."""
    try:
        raw = config_path.read_text()
    except FileNotFoundError:
        return {}
    config = yaml.safe_load(raw)
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise TypeError("Expected a YAML mapping at the config root")
    return config


def _write_config(config_path: Path, config: dict) -> None:
    config_path.write_text(_dump_config(config))


def _log_config_error(action: str, path: Path, err: Exception) -> None:
    # Never render exception text/tracebacks: YAML errors include source lines,
    # and even OSError.strerror/filename can contain arbitrary caller content.
    detail = type(err).__name__
    if isinstance(err, OSError) and err.errno is not None:
        detail += f", errno={err.errno} ({os.strerror(err.errno)})"
    elif isinstance(err, yaml.MarkedYAMLError) and err.problem_mark is not None:
        detail += (
            f", line={err.problem_mark.line + 1}, column={err.problem_mark.column + 1}"
        )
    elif isinstance(err, (TypeError, ValueError)):
        detail += ", expected mapping config/streams and string-list sources"
    _LOGGER.warning("Failed to %s go2rtc config at %s (%s)", action, path, detail)


def build_exec_source(config_dir: str, camera_ip: str) -> str:
    """Build the go2rtc exec source URL for the backchannel bridge.

    The exec source tells go2rtc to spawn the bridge script when a browser
    microphone connects. Explicit PCMA/8kHz matches the bridge's A-law
    decoder. SIGTERM lets its cleanup send STOP_VOICE, with a ten-second
    graceful-stop wait limit on go2rtc builds supporting it.

    go2rtc uses exec.Command (not a shell), so we pass the absolute script
    path. go2rtc's ParseQuery splits on # (not &) for multiple parameters.
    """
    # Defense in depth: the exec source is space-delimited, so any whitespace
    # in camera_ip would inject extra argv elements into bridge.py.
    assert not any(c.isspace() for c in camera_ip), (
        "camera_ip must not contain whitespace"
    )
    bridge_script = (
        Path(config_dir)
        / "custom_components"
        / "aqara_camera_2_way_audio"
        / "bridge.py"
    )
    return f"exec:{sys.executable} {bridge_script} {camera_ip}{_EXEC_SUFFIX}"


def build_rtsp_source(
    camera_ip: str, username: str, password: str, channel: int = 1
) -> str:
    """Build an RTSP URL with encoded credentials."""
    return f"rtsp://{quote(username, safe='')}:{quote(password, safe='')}@{camera_ip}:{RTSP_PORT}/ch{channel}"


def _valid_sources(sources: object) -> TypeGuard[list[str]]:
    return isinstance(sources, list) and all(isinstance(s, str) for s in sources)


def sources_digest(sources: list[str]) -> str:
    """Fingerprint a source multiset; exec option ordering is immaterial.

    Counts (including duplicate options) are preserved. Unknown/changed options
    change the digest. No credentials are persisted in this ownership marker.
    """
    normalized = []
    for source in sources:
        if source.startswith("exec:"):
            command, separator, options = source.partition("#")
            source = command + (
                "#" + "#".join(sorted(options.split("#"))) if separator else ""
            )
        normalized.append(source)
    return hashlib.sha256(json.dumps(sorted(normalized)).encode()).hexdigest()


def _stream_belongs_to_us(
    existing: object,
    desired: list[str],
    config_dir: str,
    camera_ip: str | None,
    owned_digest: str | None,
) -> bool:
    """Require an unmodified ownership snapshot, or a narrow legacy upgrade.

    Without a saved snapshot, only the old generated exec command with all
    companion sources unchanged is migrated. Never infer the camera from argv.
    """
    if not _valid_sources(existing):
        return False
    if owned_digest is not None:
        return sources_digest(existing) == owned_digest
    if camera_ip is None:
        return False
    our_exec = [s for s in desired if s.startswith("exec:")]
    old_exec = [s for s in existing if s.startswith("exec:")]
    if len(our_exec) != 1 or len(old_exec) != 1:
        return False
    expected = build_exec_source(config_dir, camera_ip)
    if our_exec[0] != expected:
        return False
    legacy = expected.removesuffix(_EXEC_SUFFIX) + _LEGACY_EXEC_SUFFIX
    return old_exec[0] == legacy and Counter(
        s for s in existing if not s.startswith("exec:")
    ) == Counter(s for s in desired if not s.startswith("exec:"))


def register_stream(
    config_dir: str,
    stream_name: str,
    sources: list[str],
    camera_ip: str | None = None,
    owned_digest: str | None = None,
    previous_stream_name: str | None = None,
) -> str:
    """Add or update a stream in go2rtc.yaml. Never raises.

    Returns one of:
      - "wrote": new content written
      - "noop": sources already current (source and exec option order are
        ignored); nothing written
      - "skipped": a stream by this name exists but is unowned, customized,
        or has an incomplete ownership snapshot; not overwritten
      - "error": IO, parse or unexpected failure (logged without exception
        text, since it can contain RTSP credentials)

    The caller persists the stream name and sources_digest(sources) on
    wrote/noop. A later update must match that snapshot, allowing changed HA
    credentials/channel while preserving edits made directly to go2rtc.yaml.
    On a name change, remove the unchanged predecessor in the same locked
    read/modify/write as registration; never infer a missing previous name.
    """
    config_path = Path(config_dir) / GO2RTC_CONFIG_FILE
    try:
        if not _valid_sources(sources):
            raise ValueError("Invalid requested sources")
        with _CONFIG_LOCK:
            config = _read_config(config_path)
            streams = config.setdefault("streams", {})
            if not isinstance(streams, dict):
                raise TypeError("Invalid streams mapping")
            # Partial snapshots cannot authorize updates or adoption (including
            # exact/legacy matches). A missing snapshot can use migration rules.
            snapshot_present = owned_digest is not None or previous_stream_name is not None
            snapshot_complete = (
                isinstance(previous_stream_name, str) and bool(previous_stream_name)
                and isinstance(owned_digest, str) and bool(owned_digest)
                and len(owned_digest) == 64
                and all(c in "0123456789abcdef" for c in owned_digest)
            )
            if stream_name in streams and snapshot_present and not snapshot_complete:
                _LOGGER.warning(
                    "go2rtc stream %s has incomplete ownership; not adopting",
                    stream_name,
                )
                return "skipped"
            # A digest belongs to its saved name, never to a different target.
            transitioning = snapshot_complete and previous_stream_name != stream_name
            target_digest = owned_digest if snapshot_complete and not transitioning else None
            current = False
            if stream_name in streams:
                existing = streams[stream_name]
                current = _valid_sources(existing) and sources_digest(
                    existing
                ) == sources_digest(sources)
                if not current and not _stream_belongs_to_us(
                    existing, sources, config_dir, camera_ip, target_digest
                ):
                    _LOGGER.warning(
                        "go2rtc stream %s is unowned or customized; not overwriting",
                        stream_name,
                    )
                    return "skipped"
            # Clean an IP-transition predecessor only with both parts of its
            # ownership snapshot. Commit target and cleanup in the same write,
            # before the caller replaces that snapshot. Missing/edited/foreign
            # predecessors are deliberately preserved.
            removed_previous = False
            if transitioning and owned_digest is not None:
                previous = streams.get(previous_stream_name)
                if _valid_sources(previous) and sources_digest(previous) == owned_digest:
                    del streams[previous_stream_name]
                    removed_previous = True
            if current and not removed_previous:
                return "noop"
            streams[stream_name] = sources
            _write_config(config_path, config)
        _LOGGER.info(
            "go2rtc stream '%s' written to %s; restart HA to apply",
            stream_name,
            config_path,
        )
        return "wrote"
    except Exception as err:
        # Best-effort: a go2rtc config problem must not fail entry setup.
        _log_config_error("write", config_path, err)
        return "error"


def remove_stream(
    config_dir: str, stream_name: str, owned_digest: str | None = None
) -> None:
    """Remove only an explicitly owned, unmodified stream on entry deletion.

    Missing ownership is deliberately a no-op. Reload/unload must not call this.
    Best-effort, never raises.
    """
    if owned_digest is None:
        return
    config_path = Path(config_dir) / GO2RTC_CONFIG_FILE
    try:
        with _CONFIG_LOCK:
            config = _read_config(config_path)
            streams = config.get("streams", {})
            if not isinstance(streams, dict):
                raise TypeError("Invalid streams mapping")
            existing = streams.get(stream_name)
            if not _valid_sources(existing) or sources_digest(existing) != owned_digest:
                return
            del streams[stream_name]
            _write_config(config_path, config)
        _LOGGER.info("go2rtc stream '%s' removed from %s", stream_name, config_path)
    except Exception as err:
        _log_config_error("remove from", config_path, err)
