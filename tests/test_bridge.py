"""Unit tests for the go2rtc exec bridge (bridge.py)."""

from __future__ import annotations

import itertools
import os
import socket
import struct
import threading
import time
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from custom_components.aqara_camera_2_way_audio import bridge
from custom_components.aqara_camera_2_way_audio.const import (
    MAGIC,
    TYPE_ACK,
    TYPE_HEARTBEAT,
    TYPE_START_VOICE,
    TYPE_STOP_VOICE,
)
from custom_components.aqara_camera_2_way_audio.protocol import build_packet, crc16_kermit, parse_packet


def _ack(value: int = 0) -> bytes:
    return build_packet(TYPE_ACK, value)


def _unknown_type_packet() -> bytes:
    """A well-formed packet with a CRC-valid but unrecognised type."""
    body = bytes([7]) + struct.pack(">H", 1) + b"\x00"
    return MAGIC + body + struct.pack(">H", crc16_kermit(body))


class FakeControlSocket:
    """TCP double that distinguishes blocking from non-blocking recv.

    ``pending`` is already waiting in the kernel buffer. Each sendall()
    releases the next entry of ``replies`` into the receive queue.
    """

    def __init__(self, pending: list[bytes] = (), replies: list[list[bytes]] = ()) -> None:
        self.incoming = list(pending)
        self.replies = [list(r) for r in replies]
        self.sent: list[bytes] = []
        self.blocking = True

    def setsockopt(self, *_args) -> None:
        pass

    def connect(self, _addr) -> None:
        pass

    def shutdown(self, _how: int) -> None:
        pass

    def close(self) -> None:
        pass

    def setblocking(self, flag: bool) -> None:
        self.blocking = flag

    def settimeout(self, timeout: float | None) -> None:
        self.blocking = timeout is None or timeout > 0

    def sendall(self, data: bytes) -> None:
        assert self.blocking, "sendall on a non-blocking socket can fail partway"
        self.sent.append(bytes(data))
        if self.replies:
            self.incoming.extend(self.replies.pop(0))

    def recv(self, _n: int) -> bytes:
        if self.incoming:
            return self.incoming.pop(0)
        if not self.blocking:
            raise BlockingIOError
        raise TimeoutError("timed out")


def _make_sock_factory(tcp_response: bytes):
    """Return (factory, tcp_sock, udp_sock).

    The factory yields the TCP socket on the first call and the UDP socket
    on the second, matching BridgeSession.connect()'s allocation order.
    Subsequent calls yield fresh MagicMocks so any extra socket()
    invocation does not break the test.
    """
    tcp_sock = MagicMock(spec=socket.socket)
    tcp_sock.recv.return_value = tcp_response
    udp_sock = MagicMock(spec=socket.socket)
    socks = [tcp_sock, udp_sock]

    def factory(*args, **kwargs):
        if socks:
            return socks.pop(0)
        return MagicMock(spec=socket.socket)

    return factory, tcp_sock, udp_sock


class TestBridgeConnect:
    """BridgeSession.connect() opens TCP+UDP and handshakes with the camera."""

    def test_connect_happy_path(self) -> None:
        factory, tcp_sock, udp_sock = _make_sock_factory(_ack(0))
        with patch("socket.socket", side_effect=factory):
            session = bridge.BridgeSession("192.0.2.10")
            ok = session.connect()
        try:
            assert ok is True
            tcp_sock.connect.assert_called_once_with(("192.0.2.10", 54324))
            assert tcp_sock.sendall.called
        finally:
            session.close()

    def test_connect_rejected_ack_returns_false_and_closes(self) -> None:
        """Non-zero ACK value means the camera said no. Sockets must be cleaned."""
        factory, tcp_sock, udp_sock = _make_sock_factory(_ack(1))
        with patch("socket.socket", side_effect=factory):
            session = bridge.BridgeSession("192.0.2.10")
            ok = session.connect()

        assert ok is False
        # close() should have been invoked on the TCP socket as part of cleanup.
        tcp_sock.close.assert_called()
        sent_types = [parse_packet(call.args[0])["type"] for call in tcp_sock.sendall.call_args_list]
        assert sent_types == [TYPE_START_VOICE]

    def test_connect_accepts_fragmented_ack(self) -> None:
        factory, tcp_sock, _ = _make_sock_factory(_ack())
        tcp_sock.recv.side_effect = [_ack()[:2], _ack()[2:5], _ack()[5:]]
        with patch("socket.socket", side_effect=factory), patch.object(bridge.threading, "Thread"):
            session = bridge.BridgeSession("192.0.2.10")
            assert session.connect()
            assert session.active
            session.close()
        assert tcp_sock.recv.call_count == 3

    def test_reader_preserves_coalesced_packets(self) -> None:
        session = bridge.BridgeSession("192.0.2.10")
        session._tcp_sock = MagicMock(spec=socket.socket)
        session._tcp_sock.recv.return_value = _ack() + _ack(1)
        deadline = time.monotonic() + 3
        assert session._receive_packet(deadline)["value"] == 0
        assert session._receive_packet(deadline)["value"] == 1
        session._tcp_sock.recv.assert_called_once()

    def test_reader_raises_on_eof(self) -> None:
        session = bridge.BridgeSession("192.0.2.10")
        session._tcp_sock = MagicMock(spec=socket.socket)
        session._tcp_sock.recv.return_value = b""
        with pytest.raises(ConnectionError):
            session._receive_packet(time.monotonic() + 3)

    @staticmethod
    def _corrupt_ack() -> bytes:
        packet = bytearray(_ack())
        packet[-1] ^= 1
        return bytes(packet)

    @pytest.mark.parametrize(
        "noise",
        [
            pytest.param(b"\xfe\xef\x02\xff\xff", id="oversized-length"),
            pytest.param(b"\x00\x13\x37", id="leading-garbage"),
            pytest.param(_corrupt_ack.__func__(), id="bad-crc"),
            pytest.param(_unknown_type_packet(), id="unknown-type"),
        ],
    )
    def test_reader_skips_unparseable_bytes(self, noise: bytes) -> None:
        """One bad packet must not end the session: resync to the next one."""
        session = bridge.BridgeSession("192.0.2.10")
        session._tcp_sock = FakeControlSocket(pending=[noise, _ack(1)])
        assert session._receive_packet(time.monotonic() + 3)["value"] == 1
        assert not session._received

    def test_reader_keeps_magic_split_across_reads(self) -> None:
        ack = _ack(1)
        session = bridge.BridgeSession("192.0.2.10")
        session._tcp_sock = FakeControlSocket(pending=[b"\x00" * 4 + ack[:1], ack[1:]])
        assert session._receive_packet(time.monotonic() + 3)["value"] == 1

    def test_reader_times_out_on_only_garbage(self) -> None:
        session = bridge.BridgeSession("192.0.2.10")
        session._tcp_sock = FakeControlSocket(pending=[self._corrupt_ack()])
        with pytest.raises(TimeoutError):
            session._receive_packet(time.monotonic() + 3)

    def test_connect_oserror_returns_false(self) -> None:
        """Network failure during connect cleans up without raising."""
        bad_sock = MagicMock(spec=socket.socket)
        bad_sock.connect.side_effect = OSError("unreachable")
        with patch("socket.socket", return_value=bad_sock):
            session = bridge.BridgeSession("192.0.2.10")
            ok = session.connect()
        assert ok is False


class TestBridgeClose:
    """close() reaps heartbeat thread and closes both sockets."""

    def test_close_joins_thread_and_closes_sockets(self) -> None:
        factory, tcp_sock, udp_sock = _make_sock_factory(_ack(0))
        with patch("socket.socket", side_effect=factory):
            session = bridge.BridgeSession("192.0.2.10")
            assert session.connect()
            session.close()

        tcp_sock.close.assert_called()
        udp_sock.close.assert_called()
        assert session._tcp_sock is None
        assert session._udp_sock is None

    def test_accepted_session_sends_stop_and_close_is_idempotent(self) -> None:
        factory, tcp_sock, _ = _make_sock_factory(_ack())
        with patch("socket.socket", side_effect=factory), \
             patch.object(bridge.threading, "Thread") as thread_cls:
            thread_cls.return_value.is_alive.return_value = False
            session = bridge.BridgeSession("192.0.2.10")
            assert session.connect()
            session.close()
            session.close()
        sent_types = [parse_packet(call.args[0])["type"] for call in tcp_sock.sendall.call_args_list]
        assert sent_types == [TYPE_START_VOICE, TYPE_STOP_VOICE]
        tcp_sock.shutdown.assert_called_once_with(socket.SHUT_RD)


class TestBridgeHeartbeatLoop:
    """The heartbeat thread is the primary leak risk: ensure it terminates."""

    def test_late_ack_does_not_answer_next_heartbeat(self) -> None:
        """A reply to a timed-out heartbeat is discarded, not credited to the next."""
        session = bridge.BridgeSession("192.0.2.10")
        sock = FakeControlSocket(pending=[_ack(0)], replies=[[_ack(1)]])
        session._tcp_sock = sock
        session._received.extend(_ack(0)[:3])  # tail of an earlier partial read
        assert session._send_heartbeat() is False
        assert [parse_packet(p)["type"] for p in sock.sent] == [TYPE_HEARTBEAT]
        assert sock.blocking

    def test_stale_reply_cannot_mask_silent_camera(self) -> None:
        session = bridge.BridgeSession("192.0.2.10")
        session._tcp_sock = FakeControlSocket(pending=[_ack(0)])
        with pytest.raises(TimeoutError):
            session._send_heartbeat()

    def test_discard_detects_closed_connection(self) -> None:
        session = bridge.BridgeSession("192.0.2.10")
        session._tcp_sock = FakeControlSocket(pending=[b""])
        with pytest.raises(ConnectionError):
            session._send_heartbeat()

    def test_corrupt_heartbeat_reply_counts_as_failure_not_loss(self) -> None:
        """Before resync, a corrupt packet raised ConnectionError and ended the session."""
        session = bridge.BridgeSession("192.0.2.10")
        packet = bytearray(_ack())
        packet[-1] ^= 1
        session._tcp_sock = FakeControlSocket(replies=[[bytes(packet)], [_ack()]])
        session._running = True
        session._udp_sock = MagicMock(spec=socket.socket)
        stop_event = threading.Event()
        calls = itertools.count()

        def wait(timeout=None):
            # Two heartbeats, then stop.
            return next(calls) >= 2

        with patch.object(stop_event, "wait", side_effect=wait):
            session._heartbeat_loop(stop_event)
        assert session.active
        assert len(session._tcp_sock.sent) == 2

    def test_straggling_thread_keeps_its_own_stop_event(self) -> None:
        """Errors from a thread whose session was closed must not kill a new session."""
        session = bridge.BridgeSession("192.0.2.10")
        old_event = threading.Event()
        old_event.set()
        session._running = True
        # The thread was mid-heartbeat when close() set its event, so the
        # OSError it then sees must not clear the replacement session's state.
        with patch.object(old_event, "wait", return_value=False), \
             patch.object(session, "_send_heartbeat", side_effect=OSError("closed")):
            session._heartbeat_loop(old_event)
        assert session._running is True

    def test_close_forces_shutdown_when_heartbeat_is_stuck(self) -> None:
        session = bridge.BridgeSession("192.0.2.10")
        tcp_sock = MagicMock(spec=socket.socket)
        session._tcp_sock = tcp_sock
        session._stop_allowed = False
        thread = MagicMock()
        thread.is_alive.side_effect = [True, True]
        session._heartbeat_thread = thread
        session.close()
        assert [c.args[0] for c in tcp_sock.shutdown.call_args_list] == [socket.SHUT_RD, socket.SHUT_RDWR]
        assert thread.join.call_count == 2
        tcp_sock.close.assert_called_once()
        assert session._heartbeat_thread is None

    def test_heartbeat_failure_threshold_terminates_loop(self) -> None:
        """After six heartbeat failures the loop exits and _running goes False."""
        factory, tcp_sock, udp_sock = _make_sock_factory(_ack(0))

        # Once the heartbeat loop starts, every recv raises.
        recv_calls = {"n": 0}

        def recv_side_effect(_n: int) -> bytes:
            recv_calls["n"] += 1
            if recv_calls["n"] == 1:
                return _ack(0)  # initial connect handshake
            raise OSError("RST")

        tcp_sock.recv.side_effect = recv_side_effect

        # Drop HEARTBEAT_INTERVAL to 0 so the thread spins.
        with patch("socket.socket", side_effect=factory), patch.object(
            bridge, "HEARTBEAT_INTERVAL", 0
        ):
            session = bridge.BridgeSession("192.0.2.10")
            assert session.connect()
            # Wait for heartbeat to escalate and exit.
            session._heartbeat_thread.join(timeout=3)

        assert session._running is False
        assert not session.active
        assert not session._heartbeat_thread.is_alive()
        session.close()

    def test_timeout_retry_threshold_is_unchanged(self) -> None:
        tcp_sock = FakeControlSocket(replies=[[_ack()]])
        with patch("socket.socket", side_effect=[tcp_sock, MagicMock(spec=socket.socket)]), \
             patch.object(bridge, "HEARTBEAT_INTERVAL", 0):
            session = bridge.BridgeSession("192.0.2.10")
            assert session.connect()
            session._heartbeat_thread.join(timeout=3)
        assert not session.active
        sent_types = [parse_packet(p)["type"] for p in tcp_sock.sent]
        assert sent_types == [TYPE_START_VOICE] + [TYPE_HEARTBEAT] * 6
        session.close()


class TestBridgeMainEntryPoint:
    """Smoke-test main() does not crash on missing argv."""

    def test_main_exits_on_missing_argv(self) -> None:
        with patch.object(bridge.sys, "argv", ["bridge.py"]):
            with pytest.raises(SystemExit):
                bridge.main()

    def test_main_exits_and_cleans_up_when_control_session_dies(self) -> None:
        session = MagicMock()
        session.connect.return_value = True
        session.active = True
        encoder = MagicMock()

        def encode(_data):
            session.active = False
            return [b"frame"]

        encoder.write_pcm.side_effect = encode
        order = MagicMock()
        order.attach_mock(session.close, "close_session")
        order.attach_mock(encoder.stop, "stop_encoder")
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"\xd5" * bridge.STDIN_CHUNK_SIZE)
        stdin = MagicMock()
        stdin.fileno.return_value = read_fd
        try:
            with patch.object(bridge.sys, "argv", ["bridge.py", "192.0.2.10"]), \
                 patch.object(bridge.sys, "stdin", stdin), \
                 patch.object(bridge.signal, "signal"), \
                 patch.object(bridge, "BridgeSession", return_value=session), \
                 patch("custom_components.aqara_camera_2_way_audio.encoder.AACEncoder", return_value=encoder):
                with pytest.raises(SystemExit) as err:
                    bridge.main()
        finally:
            os.close(read_fd)
            os.close(write_fd)
        assert err.value.code == 1
        session.send_audio_frame.assert_not_called()
        assert [call[0] for call in order.mock_calls] == ["close_session", "stop_encoder"]

    def test_main_exits_when_session_dies_while_stdin_is_idle(self) -> None:
        """No audio arrives, so only the poll can notice the dead session."""
        session = MagicMock()
        session.connect.return_value = True
        type(session).active = PropertyMock(side_effect=itertools.chain([True], itertools.repeat(False)))
        encoder = MagicMock()
        read_fd, write_fd = os.pipe()  # write end stays open: no data, no EOF
        stdin = MagicMock()
        stdin.fileno.return_value = read_fd
        try:
            with patch.object(bridge.sys, "argv", ["bridge.py", "192.0.2.10"]), \
                 patch.object(bridge.sys, "stdin", stdin), \
                 patch.object(bridge.signal, "signal"), \
                 patch.object(bridge, "STDIN_POLL_INTERVAL", 0.01), \
                 patch.object(bridge, "BridgeSession", return_value=session), \
                 patch("custom_components.aqara_camera_2_way_audio.encoder.AACEncoder", return_value=encoder):
                with pytest.raises(SystemExit) as err:
                    bridge.main()
        finally:
            os.close(read_fd)
            os.close(write_fd)
        assert err.value.code == 1
        encoder.write_pcm.assert_not_called()
        session.close.assert_called_once()
        encoder.stop.assert_called_once()

    def test_main_releases_session_if_encoder_start_fails(self) -> None:
        session = MagicMock()
        session.connect.return_value = True
        encoder = MagicMock()
        encoder.start.side_effect = OSError("ffmpeg unavailable")
        with patch.object(bridge.sys, "argv", ["bridge.py", "192.0.2.10"]), \
             patch.object(bridge.signal, "signal"), \
             patch.object(bridge, "BridgeSession", return_value=session), \
             patch("custom_components.aqara_camera_2_way_audio.encoder.AACEncoder", return_value=encoder):
            with pytest.raises(SystemExit):
                bridge.main()
        session.close.assert_called_once()
        encoder.stop.assert_called_once()


class TestBridgeSendAudioFrame:
    """send_audio_frame attaches an RTP header and uses sendto."""

    def test_send_audio_frame_uses_udp_sock(self) -> None:
        factory, tcp_sock, udp_sock = _make_sock_factory(_ack(0))
        with patch("socket.socket", side_effect=factory):
            session = bridge.BridgeSession("192.0.2.10")
            assert session.connect()

            frame = b"\xff\xf1FRAME_BODY"
            session.send_audio_frame(frame)

            assert udp_sock.sendto.called
            payload, addr = udp_sock.sendto.call_args.args
            assert addr[0] == "192.0.2.10"
            assert len(payload) == 12 + len(frame)
            assert payload.endswith(frame)

            session.close()

    def test_send_audio_frame_silent_before_connect(self) -> None:
        """Calling send_audio_frame before connect is a no-op (no raise)."""
        session = bridge.BridgeSession("192.0.2.10")
        session.send_audio_frame(b"\xff\xf1FRAME")
