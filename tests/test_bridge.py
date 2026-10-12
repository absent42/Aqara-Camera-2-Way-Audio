"""Unit tests for the go2rtc exec bridge (bridge.py)."""

from __future__ import annotations

import socket
import io
import time
from unittest.mock import MagicMock, patch

import pytest

from custom_components.aqara_camera_2_way_audio import bridge
from custom_components.aqara_camera_2_way_audio.const import TYPE_ACK, TYPE_START_VOICE, TYPE_STOP_VOICE
from custom_components.aqara_camera_2_way_audio.protocol import build_packet, parse_packet


def _ack(value: int = 0) -> bytes:
    return build_packet(TYPE_ACK, value)


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

    @pytest.mark.parametrize("data", [b"", b"\xfe\xef\x02\xff\xff"])
    def test_reader_rejects_eof_or_oversized_packet(self, data: bytes) -> None:
        session = bridge.BridgeSession("192.0.2.10")
        session._tcp_sock = MagicMock(spec=socket.socket)
        session._tcp_sock.recv.return_value = data
        with pytest.raises(ConnectionError):
            session._receive_packet(time.monotonic() + 3)

    def test_reader_checks_crc(self) -> None:
        session = bridge.BridgeSession("192.0.2.10")
        session._tcp_sock = MagicMock(spec=socket.socket)
        packet = bytearray(_ack())
        packet[-1] ^= 1
        session._tcp_sock.recv.return_value = bytes(packet)
        with pytest.raises(ConnectionError):
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
        with patch("socket.socket", side_effect=factory), patch.object(bridge.threading, "Thread"):
            session = bridge.BridgeSession("192.0.2.10")
            assert session.connect()
            session.close()
            session.close()
        sent_types = [parse_packet(call.args[0])["type"] for call in tcp_sock.sendall.call_args_list]
        assert sent_types == [TYPE_START_VOICE, TYPE_STOP_VOICE]
        tcp_sock.shutdown.assert_called_once_with(socket.SHUT_RD)


class TestBridgeHeartbeatLoop:
    """The heartbeat thread is the primary leak risk: ensure it terminates."""

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
        factory, tcp_sock, _ = _make_sock_factory(_ack())
        tcp_sock.recv.side_effect = [_ack()] + [TimeoutError("ACK timeout")] * 6
        with patch("socket.socket", side_effect=factory), patch.object(bridge, "HEARTBEAT_INTERVAL", 0):
            session = bridge.BridgeSession("192.0.2.10")
            assert session.connect()
            session._heartbeat_thread.join(timeout=3)
        assert not session.active
        assert tcp_sock.recv.call_count == 7
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
        stdin = MagicMock()
        stdin.buffer = io.BytesIO(b"\xd5" * bridge.STDIN_CHUNK_SIZE)
        with patch.object(bridge.sys, "argv", ["bridge.py", "192.0.2.10"]), \
             patch.object(bridge.sys, "stdin", stdin), \
             patch.object(bridge.signal, "signal"), \
             patch.object(bridge, "BridgeSession", return_value=session), \
             patch("custom_components.aqara_camera_2_way_audio.encoder.AACEncoder", return_value=encoder):
            with pytest.raises(SystemExit) as err:
                bridge.main()
        assert err.value.code == 1
        session.send_audio_frame.assert_not_called()
        assert [call[0] for call in order.mock_calls] == ["close_session", "stop_encoder"]

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
