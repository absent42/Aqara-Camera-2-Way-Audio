"""go2rtc exec bridge: stdin audio -> Aqara camera speaker.

This script is spawned by go2rtc as an exec source with backchannel enabled.
go2rtc pipes PCM audio (s16le, 16kHz, mono) from the browser's microphone
to this script's stdin. The script encodes it to AAC-LC ADTS, manages the
Aqara TCP control session, and sends frames to the camera via UDP RTP.

go2rtc invokes this as a script (not a module) since exec.Command does not
support shell syntax like PYTHONPATH=. The sys.path setup at the top of this
file ensures package imports resolve when run directly.

go2rtc stream config example (written automatically by the integration):
  streams:
    aqara_camera_192_0_2_10:
      - rtsp://user:pass@CAMERA_IP:8554/ch1
      - "ffmpeg:aqara_camera_192_0_2_10#video=copy#audio=opus#audio=copy"
      - "exec:/usr/local/bin/python3 /config/custom_components/aqara_camera_2_way_audio/bridge.py CAMERA_IP#backchannel=1"
"""

from __future__ import annotations

import logging
import os
import pathlib
import secrets
import signal
import socket
import sys
import threading
import time

# When go2rtc spawns this as a script (not a module), ensure the HA config
# directory is on sys.path so package imports resolve correctly.
# go2rtc uses exec.Command (not a shell), so PYTHONPATH can't be set via
# shell syntax in the exec URL.
_config_dir = str(pathlib.Path(__file__).resolve().parents[2])
if _config_dir not in sys.path:
    sys.path.insert(0, _config_dir)

from custom_components.aqara_camera_2_way_audio.const import (  # noqa: E402
    AAC_FRAME_SAMPLES,
    AUDIO_PORT,
    CONTROL_PORT,
    FRAME_DURATION,
    HEARTBEAT_INTERVAL,
    RTP_PAYLOAD_TYPE,
    TCP_CONNECT_TIMEOUT,
    TYPE_ACK,
    TYPE_HEARTBEAT,
    TYPE_START_VOICE,
    TYPE_STOP_VOICE,
)
from custom_components.aqara_camera_2_way_audio.protocol import build_packet, build_rtp_header, parse_packet  # noqa: E402

_LOGGER = logging.getLogger(__name__)

# PCM read chunk: go2rtc v1.9.14 sends PCML (s16le) at 16kHz (2 bytes/sample).
# 2048 bytes = 1024 samples = one AAC frame worth (~64ms).
STDIN_CHUNK_SIZE = 2048


class BridgeSession:
    """Synchronous Aqara talk session for the go2rtc exec bridge."""

    def __init__(self, camera_ip: str) -> None:
        self._camera_ip = camera_ip
        self._session_ts = int(time.time() * 1000)
        self._ssrc = secrets.randbelow(2**31 - 1) + 1
        self._seq_num = 0
        self._tcp_sock: socket.socket | None = None
        self._udp_sock: socket.socket | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._running = False
        self._stop_event = threading.Event()

    def connect(self) -> bool:
        """Open TCP control session and UDP audio socket."""
        try:
            self._tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._tcp_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._tcp_sock.settimeout(TCP_CONNECT_TIMEOUT)
            self._tcp_sock.connect((self._camera_ip, CONTROL_PORT))

            self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

            pkt = build_packet(TYPE_START_VOICE, self._session_ts)
            self._tcp_sock.sendall(pkt)

            resp = self._tcp_sock.recv(1024)
            parsed = parse_packet(resp) if resp else None
            if not parsed or parsed["type"] != TYPE_ACK or parsed["value"] != 0:
                _LOGGER.error("Voice session rejected: %s", parsed)
                self.close()
                return False

            self._running = True
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, daemon=True
            )
            self._heartbeat_thread.start()
            return True

        except (OSError, TimeoutError) as err:
            _LOGGER.error("Connect failed: %s", err)
            self.close()
            return False

    def send_audio_frame(self, aac_frame: bytes) -> None:
        """Send an AAC-ADTS frame via RTP UDP."""
        if not self._udp_sock:
            return
        ts = self._seq_num * AAC_FRAME_SAMPLES
        header = build_rtp_header(RTP_PAYLOAD_TYPE, ts, self._ssrc, self._seq_num)
        self._seq_num += 1
        self._udp_sock.sendto(header + aac_frame, (self._camera_ip, AUDIO_PORT))

    def close(self) -> None:
        """Send STOP_VOICE and close sockets."""
        self._running = False
        self._stop_event.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2)
            self._heartbeat_thread = None
        if self._tcp_sock:
            try:
                self._tcp_sock.sendall(build_packet(TYPE_STOP_VOICE, self._session_ts))
            except OSError:
                pass
            try:
                self._tcp_sock.close()
            except OSError:
                pass
            self._tcp_sock = None
        if self._udp_sock:
            try:
                self._udp_sock.close()
            except OSError:
                pass
            self._udp_sock = None

    def _heartbeat_loop(self) -> None:
        """Send heartbeats every 5s."""
        failures = 0
        while self._running:
            if self._stop_event.wait(timeout=HEARTBEAT_INTERVAL):
                break  # stop_event was set
            if not self._running or not self._tcp_sock:
                break
            try:
                self._tcp_sock.sendall(build_packet(TYPE_HEARTBEAT, self._session_ts))
                resp = self._tcp_sock.recv(1024)
                parsed = parse_packet(resp) if resp else None
                if parsed and parsed["type"] == TYPE_ACK and parsed["value"] == 0:
                    failures = 0
                    continue
                failures += 1
            except (OSError, TimeoutError):
                failures += 1
            if failures > 5:
                _LOGGER.error("Heartbeat failed %d times, closing", failures)
                self._running = False
                break


def main() -> None:
    """Entry point for go2rtc exec bridge."""
    level_name = os.environ.get("AQARA_BRIDGE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if len(sys.argv) < 2:
        print(
            "Usage: python3 -m custom_components.aqara_camera_2_way_audio.bridge CAMERA_IP",
            file=sys.stderr,
        )
        sys.exit(1)

    camera_ip = sys.argv[1]
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    _LOGGER.info("Bridge starting for %s", camera_ip)

    from custom_components.aqara_camera_2_way_audio.encoder import AACEncoder

    session = BridgeSession(camera_ip)
    if not session.connect():
        sys.exit(1)

    # go2rtc sends G.711 A-law (PCMA) at 8kHz to exec backchannel stdin.
    # Despite version reporting as 1.9.14, HA's bundled go2rtc uses stdin.NewClient
    # which hardcodes PCMA/8000 and ignores the #audio= parameter.
    encoder = AACEncoder(input_format="alaw")
    encoder.start()
    _LOGGER.info("Encoding pipeline active, reading PCMA/8kHz from stdin")

    total_frames = 0
    start_time = 0.0  # set on first audio data, not on bridge start

    def _send_paced(frame: bytes) -> None:
        """Send a frame, sleeping if ahead of real-time schedule."""
        nonlocal total_frames, start_time
        if start_time == 0.0:
            start_time = time.monotonic()
        expected_time = start_time + total_frames * FRAME_DURATION
        now = time.monotonic()
        if now < expected_time:
            time.sleep(expected_time - now)
        session.send_audio_frame(frame)
        total_frames += 1

    first_stdin = True
    first_frame = True
    try:
        while True:
            pcm_data = sys.stdin.buffer.read(STDIN_CHUNK_SIZE)
            if not pcm_data:
                _LOGGER.info("stdin EOF, flushing encoder")
                break
            if first_stdin:
                _LOGGER.info("First stdin data received (%d bytes)", len(pcm_data))
                first_stdin = False
            frames = encoder.write_pcm(pcm_data)
            for frame in frames:
                if first_frame:
                    _LOGGER.info("First AAC frame produced (encoder latency: %.0fms)",
                                 (time.monotonic() - start_time) * 1000 if start_time else 0)
                    first_frame = False
                _send_paced(frame)

        # Flush remaining frames from encoder buffer
        remaining = encoder.flush()
        for frame in remaining:
            _send_paced(frame)
        _LOGGER.info("Sent %d audio frames total", total_frames)

    except KeyboardInterrupt:
        _LOGGER.info("Interrupted, stopping")
    finally:
        encoder.stop()
        session.close()
        _LOGGER.info("Bridge stopped")


if __name__ == "__main__":
    main()
