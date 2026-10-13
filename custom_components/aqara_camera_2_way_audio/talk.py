"""Async LAN talk client for Aqara cameras.

Manages a TCP control session (port 54324) and a UDP audio stream
(port 54323). Used by the talk_audio_file service.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time

from .const import (
    AUDIO_PORT, CONTROL_PORT, HEARTBEAT_INTERVAL,
    RTP_PAYLOAD_TYPE, TCP_CONNECT_TIMEOUT,
    TYPE_ACK, TYPE_HEARTBEAT, TYPE_START_VOICE, TYPE_STOP_VOICE,
)
from .protocol import ControlPacketBuffer, build_packet, build_rtp_header

_LOGGER = logging.getLogger(__name__)

# StreamReader buffers incoming bytes as they arrive, so a late reply is
# already readable without waiting; this only bounds the check for one.
STALE_REPLY_WAIT = 0.01


class _UDPProtocol(asyncio.DatagramProtocol):
    """Minimal UDP protocol for sending audio."""

    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        pass


class AqaraLanTalkClient:
    """Async client for Aqara camera LAN talk protocol."""

    def __init__(self, camera_ip: str) -> None:
        self._camera_ip = camera_ip
        self._session_ts = int(time.time() * 1000)
        self._ssrc = secrets.randbelow(2**31 - 1) + 1
        self._seq_num = 0
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._udp_transport: asyncio.DatagramTransport | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._connected = False
        self._connect_lock = asyncio.Lock()
        self._packets = ControlPacketBuffer()

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        """Start a voice session with the camera."""
        async with self._connect_lock:
            if self._connected:
                return True
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self._camera_ip, CONTROL_PORT),
                    timeout=TCP_CONNECT_TIMEOUT,
                )
            except (OSError, asyncio.TimeoutError) as err:
                _LOGGER.error(
                    "Connect failed %s:%s: %s", self._camera_ip, CONTROL_PORT, err,
                )
                return False

            self._packets.clear()
            try:
                ack = await self._request_ack(TYPE_START_VOICE)
            except asyncio.TimeoutError:
                _LOGGER.error("No ACK from camera")
                await self._close_tcp()
                return False
            except EOFError:
                _LOGGER.error("Connection closed before ACK")
                await self._close_tcp()
                return False

            if ack != 0:
                _LOGGER.error("Voice session rejected: ACK value %d", ack)
                await self._close_tcp()
                return False

            loop = asyncio.get_running_loop()
            transport, _ = await loop.create_datagram_endpoint(
                _UDPProtocol, local_addr=("0.0.0.0", 0),
            )
            self._udp_transport = transport

            self._connected = True
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            self._heartbeat_task.add_done_callback(self._on_heartbeat_done)
            _LOGGER.debug("Voice session active with %s", self._camera_ip)
            return True

    @staticmethod
    def _on_heartbeat_done(task: asyncio.Task) -> None:
        """Surface unhandled exceptions from the heartbeat loop.

        Without this, a raise inside _heartbeat_loop is stashed on the Task
        and only emitted as a 'Task exception was never retrieved' warning
        when the Task is garbage-collected, by which point the session has
        already silently died.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _LOGGER.error("Heartbeat task ended unexpectedly: %r", exc)

    async def _read_packet(self, deadline: float) -> dict:
        """Return the next valid control packet, reading more bytes as needed.

        Raises EOFError if the camera closes the connection and
        asyncio.TimeoutError once the loop time passes deadline.
        """
        loop = asyncio.get_running_loop()
        while (packet := self._packets.pop()) is None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            data = await asyncio.wait_for(self._reader.read(1024), timeout=remaining)
            if not data:
                raise EOFError("Camera closed control connection")
            self._packets.feed(data)
        return packet

    async def _request_ack(self, packet_type: int) -> int:
        """Send a control packet and return the value of the ACK answering it."""
        self._writer.write(build_packet(packet_type, self._session_ts))
        await self._writer.drain()
        deadline = asyncio.get_running_loop().time() + TCP_CONNECT_TIMEOUT
        while True:
            packet = await self._read_packet(deadline)
            if packet["type"] == TYPE_ACK:
                return packet["value"]

    async def _discard_stale_replies(self) -> None:
        """Drop late replies to earlier heartbeats that already timed out.

        _request_ack takes the first ACK it sees, so a reply that arrived
        after its own request gave up would otherwise answer the next one.
        """
        self._packets.clear()
        while True:
            try:
                data = await asyncio.wait_for(self._reader.read(1024), timeout=STALE_REPLY_WAIT)
            except asyncio.TimeoutError:
                return
            if not data:
                raise EOFError("Camera closed control connection")

    def send_audio_frame(self, aac_frame: bytes, timestamp_samples: int) -> None:
        """Send an AAC-ADTS frame via RTP."""
        if not self._udp_transport:
            return
        header = build_rtp_header(
            RTP_PAYLOAD_TYPE, timestamp_samples, self._ssrc, self._seq_num
        )
        self._seq_num += 1
        self._udp_transport.sendto(header + aac_frame, (self._camera_ip, AUDIO_PORT))

    async def disconnect(self) -> None:
        """Stop the voice session and clean up."""
        self._connected = False

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._writer and not self._writer.is_closing():
            try:
                self._writer.write(build_packet(TYPE_STOP_VOICE, self._session_ts))
                await self._writer.drain()
            except (OSError, ConnectionError):
                pass

        await self._close_tcp()
        self._close_udp()

    async def _close_tcp(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except (OSError, ConnectionError):
                pass
            self._writer = None
            self._reader = None

    def _close_udp(self) -> None:
        if self._udp_transport:
            self._udp_transport.close()
            self._udp_transport = None

    async def _heartbeat_loop(self) -> None:
        """Send heartbeats every 5s to keep the session alive."""
        failures = 0
        while self._connected:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if not self._connected or not self._writer:
                break
            try:
                await self._discard_stale_replies()
                if await self._request_ack(TYPE_HEARTBEAT) == 0:
                    failures = 0
                    continue
                failures += 1
            except EOFError:
                _LOGGER.warning("Connection closed during heartbeat")
                self._connected = False
                await self._close_tcp()
                self._close_udp()
                break
            except (asyncio.TimeoutError, OSError, ConnectionError):
                failures += 1

            if failures > 5:
                _LOGGER.warning("Voice channel dead after %d heartbeat failures", failures)
                self._connected = False
                await self._close_tcp()
                self._close_udp()
                break
