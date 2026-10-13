"""Unit tests for AqaraLanTalkClient (talk.py)."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.aqara_camera_2_way_audio.const import (
    TCP_CONNECT_TIMEOUT,
    TYPE_ACK,
    TYPE_HEARTBEAT,
)
from custom_components.aqara_camera_2_way_audio.protocol import build_packet
from custom_components.aqara_camera_2_way_audio.talk import AqaraLanTalkClient


def _ack_bytes(value: int = 0) -> bytes:
    """Build a serialised TYPE_ACK packet the client will accept."""
    return build_packet(TYPE_ACK, value)


@pytest.fixture
def mock_reader_writer():
    """Return (reader, writer) AsyncMocks the client can talk to."""
    reader = AsyncMock()
    writer = MagicMock()
    writer.write = MagicMock()
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()
    writer.is_closing = MagicMock(return_value=False)
    return reader, writer


@pytest.fixture
def mock_udp_transport():
    """Return a MagicMock DatagramTransport."""
    transport = MagicMock()
    transport.sendto = MagicMock()
    transport.close = MagicMock()
    return transport


async def _connect_with_mocks(client, reader, writer, transport, ack: bytes | None = None):
    """Drive the client's connect() with provided mocks. Returns the result."""
    reader.read = AsyncMock(return_value=ack if ack is not None else _ack_bytes(0))
    loop = asyncio.get_running_loop()
    with patch(
        "asyncio.open_connection",
        new=AsyncMock(return_value=(reader, writer)),
    ), patch.object(
        loop, "create_datagram_endpoint", new=AsyncMock(return_value=(transport, None))
    ):
        return await client.connect()


async def test_connect_happy_path(mock_reader_writer, mock_udp_transport) -> None:
    """connect() opens TCP + UDP, sends HELLO, receives ACK, stores transports."""
    reader, writer = mock_reader_writer
    client = AqaraLanTalkClient("192.0.2.10")

    result = await _connect_with_mocks(client, reader, writer, mock_udp_transport)

    assert result is True
    assert client.is_connected is True
    assert client._writer is writer
    assert client._udp_transport is mock_udp_transport
    # The HELLO packet was written.
    assert writer.write.call_count >= 1
    writer.drain.assert_awaited()

    # Cleanup
    await client.disconnect()


async def test_connect_returns_true_on_second_call_without_reopen(
    mock_reader_writer, mock_udp_transport
) -> None:
    """A second connect() while already connected returns True and re-opens nothing."""
    reader, writer = mock_reader_writer
    client = AqaraLanTalkClient("192.0.2.10")

    await _connect_with_mocks(client, reader, writer, mock_udp_transport)
    assert client.is_connected

    with patch("asyncio.open_connection", new=AsyncMock()) as open_again:
        result = await client.connect()

    assert result is True
    open_again.assert_not_called()

    await client.disconnect()


async def test_connect_rejected_by_camera_returns_false(mock_reader_writer) -> None:
    """A non-zero ACK value means the camera rejected the session."""
    reader, writer = mock_reader_writer
    client = AqaraLanTalkClient("192.0.2.10")

    bad_ack = _ack_bytes(1)
    transport = MagicMock()
    transport.close = MagicMock()
    loop = asyncio.get_running_loop()
    reader.read = AsyncMock(return_value=bad_ack)
    with patch(
        "asyncio.open_connection",
        new=AsyncMock(return_value=(reader, writer)),
    ), patch.object(
        loop, "create_datagram_endpoint", new=AsyncMock(return_value=(transport, None))
    ):
        result = await client.connect()

    assert result is False
    assert client.is_connected is False
    # TCP writer was closed because connect aborted.
    writer.close.assert_called()


async def test_connect_open_connection_error_returns_false() -> None:
    """OSError from open_connection results in a clean False with no side effects."""
    client = AqaraLanTalkClient("192.0.2.10")
    with patch(
        "asyncio.open_connection",
        new=AsyncMock(side_effect=OSError("network unreachable")),
    ):
        result = await client.connect()

    assert result is False
    assert client.is_connected is False
    assert client._writer is None
    assert client._udp_transport is None


async def test_connect_ack_timeout_returns_false(mock_reader_writer) -> None:
    """If the camera never sends an ACK, connect returns False."""
    reader, writer = mock_reader_writer
    reader.read = AsyncMock(side_effect=asyncio.TimeoutError)
    client = AqaraLanTalkClient("192.0.2.10")
    with patch(
        "asyncio.open_connection",
        new=AsyncMock(return_value=(reader, writer)),
    ):
        result = await client.connect()
    assert result is False
    writer.close.assert_called()


async def test_connect_empty_resp_returns_false(mock_reader_writer) -> None:
    """A 0-byte response from the camera means the connection died pre-ACK."""
    reader, writer = mock_reader_writer
    reader.read = AsyncMock(return_value=b"")
    client = AqaraLanTalkClient("192.0.2.10")
    with patch(
        "asyncio.open_connection",
        new=AsyncMock(return_value=(reader, writer)),
    ):
        result = await client.connect()
    assert result is False
    writer.close.assert_called()


async def test_connect_accepts_fragmented_ack(mock_reader_writer, mock_udp_transport) -> None:
    """A START ACK split across TCP segments is reassembled, not rejected."""
    reader, writer = mock_reader_writer
    client = AqaraLanTalkClient("192.0.2.10")
    ack = _ack_bytes(0)
    loop = asyncio.get_running_loop()
    reader.read = AsyncMock(side_effect=[ack[:3], ack[3:]])
    with patch("asyncio.open_connection", new=AsyncMock(return_value=(reader, writer))), \
         patch.object(loop, "create_datagram_endpoint",
                      new=AsyncMock(return_value=(mock_udp_transport, None))):
        assert await client.connect() is True
    await client.disconnect()


async def test_connect_skips_other_packet_coalesced_before_ack(
    mock_reader_writer, mock_udp_transport
) -> None:
    reader, writer = mock_reader_writer
    client = AqaraLanTalkClient("192.0.2.10")
    coalesced = build_packet(TYPE_HEARTBEAT, 1) + _ack_bytes(0)
    assert await _connect_with_mocks(client, reader, writer, mock_udp_transport, ack=coalesced)
    await client.disconnect()


async def test_late_reply_does_not_answer_next_heartbeat(mock_reader_writer) -> None:
    """A reply to a timed-out heartbeat is discarded, not credited to the next."""
    reader, writer = mock_reader_writer
    chunks = [_ack_bytes(0), None, _ack_bytes(1)]

    async def read(_n: int) -> bytes:
        item = chunks.pop(0)
        if item is None:
            await asyncio.sleep(1)  # nothing else pending; the discard times out
        return item

    reader.read = read
    client = AqaraLanTalkClient("192.0.2.10")
    client._reader, client._writer = reader, writer
    client._packets.feed(_ack_bytes(0)[:3])  # tail of an earlier partial read

    await client._discard_stale_replies()
    assert await client._request_ack(TYPE_HEARTBEAT) == 1
    assert not chunks


async def test_heartbeat_eof_closes_session(mock_reader_writer, mock_udp_transport) -> None:
    reader, writer = mock_reader_writer
    client = AqaraLanTalkClient("192.0.2.10")
    await _connect_with_mocks(client, reader, writer, mock_udp_transport)
    reader.read = AsyncMock(return_value=b"")
    with patch("custom_components.aqara_camera_2_way_audio.talk.HEARTBEAT_INTERVAL", 0):
        await asyncio.wait_for(client._heartbeat_task, timeout=2.0)
    assert client.is_connected is False
    writer.close.assert_called()
    mock_udp_transport.close.assert_called()


async def test_disconnect_closes_both_transports_and_cancels_heartbeat(
    mock_reader_writer, mock_udp_transport
) -> None:
    """disconnect() releases the TCP writer, UDP transport, and heartbeat task."""
    reader, writer = mock_reader_writer
    client = AqaraLanTalkClient("192.0.2.10")
    await _connect_with_mocks(client, reader, writer, mock_udp_transport)
    heartbeat_task = client._heartbeat_task
    assert heartbeat_task is not None

    await client.disconnect()

    assert client.is_connected is False
    writer.close.assert_called()
    mock_udp_transport.close.assert_called()
    assert client._heartbeat_task is None
    assert client._udp_transport is None
    assert client._writer is None
    assert heartbeat_task.cancelled() or heartbeat_task.done()


async def test_send_audio_frame_before_connect_is_silent() -> None:
    """Calling send_audio_frame before connect() returns silently (no raise)."""
    client = AqaraLanTalkClient("192.0.2.10")
    # Must not raise even though there is no UDP transport.
    client.send_audio_frame(b"\xff\xf1FRAME", timestamp_samples=0)


async def test_send_audio_frame_uses_udp_transport(
    mock_reader_writer, mock_udp_transport
) -> None:
    """After connect, send_audio_frame writes a 12-byte RTP header plus payload."""
    reader, writer = mock_reader_writer
    client = AqaraLanTalkClient("192.0.2.10")
    await _connect_with_mocks(client, reader, writer, mock_udp_transport)

    frame = b"\xff\xf1FRAME_PAYLOAD"
    client.send_audio_frame(frame, timestamp_samples=1024)

    assert mock_udp_transport.sendto.called
    payload, addr = mock_udp_transport.sendto.call_args.args
    assert addr[0] == "192.0.2.10"
    # 12-byte RTP header + the frame body.
    assert len(payload) == 12 + len(frame)
    assert payload.endswith(frame)

    await client.disconnect()


async def test_heartbeat_failure_escalation_closes_session(
    mock_reader_writer, mock_udp_transport
) -> None:
    """Six consecutive heartbeat failures close TCP + UDP and flip is_connected to False."""
    reader, writer = mock_reader_writer
    client = AqaraLanTalkClient("192.0.2.10")
    await _connect_with_mocks(client, reader, writer, mock_udp_transport)
    assert client.is_connected

    # Force every heartbeat read to error.
    reader.read = AsyncMock(side_effect=OSError("RST"))
    # Shrink the sleep to keep the test fast.
    with patch(
        "custom_components.aqara_camera_2_way_audio.talk.HEARTBEAT_INTERVAL", 0
    ):
        # Wait for the heartbeat task to exit on its own.
        try:
            await asyncio.wait_for(client._heartbeat_task, timeout=2.0)
        except asyncio.CancelledError:
            pass

    assert client.is_connected is False
    writer.close.assert_called()
    mock_udp_transport.close.assert_called()


async def test_heartbeat_done_callback_logs_unexpected_exception(
    caplog, mock_reader_writer, mock_udp_transport
) -> None:
    """SEC-007: a raise inside the heartbeat task is surfaced via ERROR log."""
    reader, writer = mock_reader_writer
    client = AqaraLanTalkClient("192.0.2.10")
    await _connect_with_mocks(client, reader, writer, mock_udp_transport)
    task = client._heartbeat_task
    assert task is not None

    # Cancel the running task so we can replace it with one that raises a
    # non-CancelledError exception.
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    async def boom() -> None:
        raise RuntimeError("synthetic heartbeat failure")

    new_task = asyncio.create_task(boom())
    new_task.add_done_callback(AqaraLanTalkClient._on_heartbeat_done)

    with caplog.at_level(logging.ERROR, logger="custom_components.aqara_camera_2_way_audio.talk"):
        try:
            await new_task
        except RuntimeError:
            pass
        # Let the done callback run.
        await asyncio.sleep(0)

    messages = [r.getMessage() for r in caplog.records]
    assert any("Heartbeat task ended unexpectedly" in m for m in messages), messages

    await client.disconnect()
