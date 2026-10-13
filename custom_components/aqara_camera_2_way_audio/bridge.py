"""go2rtc exec bridge: stdin audio -> Aqara camera speaker.

This script is spawned by go2rtc as an exec source with backchannel enabled.
go2rtc pipes G.711 A-law audio (8kHz, mono) from the browser's microphone
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
import select
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
from custom_components.aqara_camera_2_way_audio.protocol import (  # noqa: E402
    ControlPacketBuffer,
    build_packet,
    build_rtp_header,
)

_LOGGER = logging.getLogger(__name__)

# The entry point below selects PCMA/8kHz: 256 bytes is 32ms of input.
# Output AAC frames still contain 1024 samples at 16kHz (64ms).
STDIN_CHUNK_SIZE = 256

# How often the stdin wait wakes up to notice a control session that died
# while go2rtc was sending no audio.
STDIN_POLL_INTERVAL = 0.5

# Pacing may send late frames back to back to absorb encoder jitter, but a
# larger lag means input paused; restart the schedule instead of bursting.
MAX_PACING_LAG = 0.25


class FramePacer:
    """Hold frames to real-time rate, restarting the schedule after a pause."""

    def __init__(self) -> None:
        self._next_send = 0.0  # the schedule starts at the first frame

    def wait(self) -> None:
        """Block until the next frame is due."""
        now = time.monotonic()
        if now < self._next_send:
            time.sleep(self._next_send - now)
        elif now - self._next_send > MAX_PACING_LAG:
            # First frame, or input resumed after a pause: start a new
            # schedule rather than bursting the backlog to catch up.
            self._next_send = now
        self._next_send += FRAME_DURATION


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
        self._received = ControlPacketBuffer()
        self._stop_allowed = False

    @property
    def active(self) -> bool:
        """Read the current session state, including heartbeat failures."""
        return bool(self._running and self._tcp_sock and self._udp_sock)

    def _receive_packet(self, deadline: float) -> dict:
        """Return the next valid control packet, accumulating TCP fragments.

        Coalesced packets stay buffered for the next call. Garbage, corrupt
        packets and unknown types are skipped, so only a closed socket
        (ConnectionError) or the deadline (TimeoutError) ends the wait.
        """
        while True:
            parsed = self._received.pop()
            if parsed is not None:
                return parsed
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Control ACK timeout")
            # close() may clear the attribute from another thread; a local
            # reference turns that into an OSError instead of AttributeError.
            sock = self._tcp_sock
            if sock is None:
                raise ConnectionError("Control socket closed")
            sock.settimeout(remaining)
            data = sock.recv(1024)
            if not data:
                raise ConnectionError("Camera closed control connection")
            self._received.feed(data)

    def _discard_stale_replies(self) -> None:
        """Drop late replies to earlier requests that already timed out.

        _request_ack takes the first ACK it sees, so a reply that arrived
        after its own request gave up would otherwise answer the next one.
        """
        sock = self._tcp_sock
        if sock is None:
            raise ConnectionError("Control socket closed")
        self._received.clear()
        sock.setblocking(False)
        try:
            while True:
                if not sock.recv(1024):
                    raise ConnectionError("Camera closed control connection")
        except BlockingIOError:
            pass
        finally:
            sock.settimeout(TCP_CONNECT_TIMEOUT)

    def _send_heartbeat(self) -> bool:
        """Send one heartbeat and match it only against its own ACK."""
        self._discard_stale_replies()
        return self._request_ack(TYPE_HEARTBEAT)

    def _request_ack(self, packet_type: int) -> bool:
        """Wait for one complete ACK within a single bounded deadline."""
        sock = self._tcp_sock
        if sock is None:
            raise ConnectionError("Control socket closed")
        sock.sendall(build_packet(packet_type, self._session_ts))
        if packet_type == TYPE_START_VOICE:
            # A lost ACK may still mean START succeeded; clean that up on exit.
            self._stop_allowed = True
        deadline = time.monotonic() + TCP_CONNECT_TIMEOUT
        while True:
            parsed = self._receive_packet(deadline)
            if parsed["type"] != TYPE_ACK:
                continue
            if parsed["value"] != 0:
                if packet_type == TYPE_START_VOICE:
                    # Explicit refusal must not stop another user's call.
                    self._stop_allowed = False
                return False
            return True

    def connect(self) -> bool:
        """Open TCP control session and UDP audio socket."""
        self.close()
        self._stop_event = threading.Event()
        self._received.clear()
        self._session_ts = int(time.time() * 1000)
        self._seq_num = 0
        self._ssrc = secrets.randbelow(2**31 - 1) + 1
        try:
            self._tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._tcp_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._tcp_sock.settimeout(TCP_CONNECT_TIMEOUT)
            self._tcp_sock.connect((self._camera_ip, CONTROL_PORT))

            self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

            if not self._request_ack(TYPE_START_VOICE):
                _LOGGER.error("Voice session rejected")
                self.close()
                return False

            self._running = True
            # The thread keeps this session's event, so a later connect()
            # cannot hand a straggling thread a fresh, unset one.
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, args=(self._stop_event,), daemon=True
            )
            self._heartbeat_thread.start()
            return True

        except (OSError, TimeoutError) as err:
            _LOGGER.error("Connect failed: %s", err)
            self.close()
            return False

    def send_audio_frame(self, aac_frame: bytes) -> None:
        """Send an AAC-ADTS frame via RTP UDP.

        Raises ConnectionError once the session has ended, so no caller can
        silently drop audio into a dead session.
        """
        if not self.active:
            raise ConnectionError("Voice session is no longer active")
        ts = self._seq_num * AAC_FRAME_SAMPLES
        header = build_rtp_header(RTP_PAYLOAD_TYPE, ts, self._ssrc, self._seq_num)
        self._seq_num += 1
        self._udp_sock.sendto(header + aac_frame, (self._camera_ip, AUDIO_PORT))

    def close(self) -> None:
        """Send STOP_VOICE and close sockets."""
        self._running = False
        self._stop_event.set()
        if self._tcp_sock:
            try:
                # Wake recv() before joining, keeping writes available for STOP.
                self._tcp_sock.shutdown(socket.SHUT_RD)
            except OSError:
                pass
        thread, self._heartbeat_thread = self._heartbeat_thread, None
        if thread and thread.is_alive():
            thread.join(timeout=2)
            if thread.is_alive() and self._tcp_sock:
                # Still blocked in sendall: the send buffer is full, so STOP
                # could not get out either. Shut down writes to unblock it
                # before the socket is torn down underneath the thread.
                _LOGGER.warning("Heartbeat thread did not stop, forcing socket shutdown")
                try:
                    self._tcp_sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                thread.join(timeout=1)
        if self._tcp_sock:
            try:
                if self._stop_allowed:
                    self._tcp_sock.settimeout(TCP_CONNECT_TIMEOUT)
                    self._tcp_sock.sendall(build_packet(TYPE_STOP_VOICE, self._session_ts))
            except OSError:
                pass
            try:
                self._tcp_sock.close()
            except OSError:
                pass
            self._tcp_sock = None
        self._stop_allowed = False
        if self._udp_sock:
            try:
                self._udp_sock.close()
            except OSError:
                pass
            self._udp_sock = None

    def _heartbeat_loop(self, stop_event: threading.Event) -> None:
        """Send heartbeats every 5s until stop_event is set or the camera fails."""
        failures = 0
        while not stop_event.wait(timeout=HEARTBEAT_INTERVAL):
            try:
                if self._send_heartbeat():
                    failures = 0
                    continue
                failures += 1
            except TimeoutError:
                failures += 1
            except OSError as err:
                # Errors after close() are expected and must not touch the
                # state of whatever session replaced this one.
                if not stop_event.is_set():
                    _LOGGER.error("Control connection lost: %s", err)
                    self._running = False
                return
            if failures > 5:
                if not stop_event.is_set():
                    _LOGGER.error("Heartbeat failed %d times, closing", failures)
                    self._running = False
                return


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
    pacer = FramePacer()
    total_frames = 0

    def _send_paced(frame: bytes) -> None:
        """Send a frame, sleeping if ahead of real-time schedule."""
        nonlocal total_frames
        pacer.wait()
        session.send_audio_frame(frame)
        total_frames += 1

    first_stdin = True
    first_frame = True
    first_input_time = 0.0
    try:
        encoder.start()
        _LOGGER.info("Encoding pipeline active, reading PCMA/8kHz from stdin")
        stdin_fd = sys.stdin.fileno()
        while True:
            # A blocking read would never notice the heartbeat thread ending
            # the session while go2rtc sends no audio, leaving the bridge
            # alive with a dead session, so wake up periodically to check.
            ready, _, _ = select.select([stdin_fd], [], [], STDIN_POLL_INTERVAL)
            if not session.active:
                raise ConnectionError("Voice session is no longer active")
            if not ready:
                continue
            pcm_data = os.read(stdin_fd, STDIN_CHUNK_SIZE)
            if not pcm_data:
                _LOGGER.info("stdin EOF, flushing encoder")
                break
            if first_stdin:
                first_input_time = time.monotonic()
                _LOGGER.info("First stdin data received (%d bytes)", len(pcm_data))
                first_stdin = False
            frames = encoder.write_pcm(pcm_data)
            for frame in frames:
                if first_frame:
                    _LOGGER.info("First AAC frame produced (encoder latency: %.0fms)",
                                 (time.monotonic() - first_input_time) * 1000)
                    first_frame = False
                _send_paced(frame)

        # Flush remaining frames from encoder buffer
        remaining = encoder.flush()
        for frame in remaining:
            _send_paced(frame)
        _LOGGER.info("Sent %d audio frames total", total_frames)

    except KeyboardInterrupt:
        _LOGGER.info("Interrupted, stopping")
    except (OSError, RuntimeError) as err:
        _LOGGER.error("Bridge stopped after audio/control failure: %s", err)
        sys.exit(1)
    finally:
        # Release the camera before waiting for the encoder subprocess.
        session.close()
        encoder.stop()
        _LOGGER.info("Bridge stopped")


if __name__ == "__main__":
    main()
