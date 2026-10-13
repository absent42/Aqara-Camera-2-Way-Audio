"""Unit tests for protocol.py -- pure CRC / packet / RTP / ADTS logic."""

from __future__ import annotations

import struct

import pytest

from custom_components.aqara_camera_2_way_audio.const import (
    MAGIC,
    RTP_PAYLOAD_TYPE,
    TYPE_ACK,
    TYPE_HEARTBEAT,
    TYPE_START_VOICE,
    TYPE_STOP_VOICE,
)
from custom_components.aqara_camera_2_way_audio.protocol import (
    ControlPacketBuffer,
    build_packet,
    build_rtp_header,
    crc16_kermit,
    extract_adts_frames,
    extract_adts_frames_with_consumed,
    parse_packet,
)


class TestCRC16Kermit:
    """CRC-16/KERMIT with poly 0x8408, init 0xFFFF, final XOR 0xFFFF."""

    def test_known_vector_check_string(self) -> None:
        # The Aqara variant ("kermit" in the codebase, but with init=0xFFFF
        # and final XOR=0xFFFF -- which is actually closer to X-25) yields
        # 0x906E for the canonical check string "123456789". This pins the
        # implementation's actual output so any change to the table or the
        # init/finalisation constants will be detected.
        assert crc16_kermit(b"123456789") == 0x906E

    def test_empty_input(self) -> None:
        # CRC of empty input with init=0xFFFF and final XOR 0xFFFF is 0.
        assert crc16_kermit(b"") == 0x0000

    def test_single_byte_differs_per_input(self) -> None:
        assert crc16_kermit(b"\x00") != crc16_kermit(b"\x01")


class TestLmLocalPacketRoundtrip:
    """build_packet -> parse_packet should be lossless for valid inputs."""

    @pytest.mark.parametrize("pkt_type", [TYPE_START_VOICE, TYPE_STOP_VOICE, TYPE_HEARTBEAT])
    def test_uint64_payload_roundtrip(self, pkt_type: int) -> None:
        value = 1_700_000_000_000
        packet = build_packet(pkt_type, value)
        parsed = parse_packet(packet)
        assert parsed is not None
        assert parsed["type"] == pkt_type
        assert parsed["value"] == value

    def test_ack_byte_payload_roundtrip(self) -> None:
        packet = build_packet(TYPE_ACK, 0)
        parsed = parse_packet(packet)
        assert parsed is not None
        assert parsed["type"] == TYPE_ACK
        assert parsed["value"] == 0


class TestLmLocalPacketParseRejects:
    """parse_packet must return None for malformed inputs."""

    def test_short_input(self) -> None:
        assert parse_packet(b"") is None
        assert parse_packet(b"\xff\xff\x00\x00") is None

    def test_wrong_magic(self) -> None:
        packet = build_packet(TYPE_HEARTBEAT, 0)
        tampered = b"\x00\x00" + packet[2:]
        assert parse_packet(tampered) is None

    def test_unknown_packet_type(self) -> None:
        # Type 4 is not assigned -- parse should reject.
        payload = struct.pack(">Q", 0)
        header = MAGIC + bytes([4]) + struct.pack(">H", len(payload))
        # CRC will be invalid (we don't compute one), but parse should
        # short-circuit on the unknown type first.
        fake = header + payload + b"\x00\x00"
        assert parse_packet(fake) is None

    def test_bad_crc(self) -> None:
        packet = bytearray(build_packet(TYPE_HEARTBEAT, 0))
        # Flip the last byte of the CRC.
        packet[-1] ^= 0xFF
        assert parse_packet(bytes(packet)) is None

    def test_truncated_after_header(self) -> None:
        packet = build_packet(TYPE_HEARTBEAT, 0)
        # Cut the packet inside its payload region.
        assert parse_packet(packet[:7]) is None


class TestControlPacketBuffer:
    """Stream reassembly shared by the exec bridge and the async talk client."""

    def test_fragmented_packet_is_reassembled(self) -> None:
        ack = build_packet(TYPE_ACK, 0)
        buf = ControlPacketBuffer()
        for i in range(len(ack) - 1):
            buf.feed(ack[i : i + 1])
            assert buf.pop() is None
        buf.feed(ack[-1:])
        assert buf.pop() == {"type": TYPE_ACK, "value": 0}
        assert len(buf) == 0

    def test_coalesced_packets_are_returned_in_order(self) -> None:
        buf = ControlPacketBuffer()
        buf.feed(build_packet(TYPE_HEARTBEAT, 7) + build_packet(TYPE_ACK, 1))
        assert buf.pop() == {"type": TYPE_HEARTBEAT, "value": 7}
        assert buf.pop() == {"type": TYPE_ACK, "value": 1}
        assert buf.pop() is None

    def test_corrupt_packet_is_skipped(self) -> None:
        bad = bytearray(build_packet(TYPE_ACK, 0))
        bad[-1] ^= 1
        buf = ControlPacketBuffer()
        buf.feed(b"\x01\x02" + bytes(bad) + build_packet(TYPE_ACK, 3))
        assert buf.pop() == {"type": TYPE_ACK, "value": 3}

    def test_clear_drops_partial_packet(self) -> None:
        buf = ControlPacketBuffer()
        buf.feed(build_packet(TYPE_ACK, 0)[:4])
        buf.clear()
        assert len(buf) == 0


class TestRTPHeader:
    """build_rtp_header produces a 12-byte RFC 3550 header."""

    def test_length_is_12_bytes(self) -> None:
        assert len(build_rtp_header(RTP_PAYLOAD_TYPE, 0, 0, 0)) == 12

    def test_field_layout(self) -> None:
        header = build_rtp_header(RTP_PAYLOAD_TYPE, 0xABCD, 0x12345678, 0x4242)
        # Byte 0: V=2, P=0, X=0, CC=0 -> 0b1000_0000 = 0x80
        assert header[0] == 0x80
        # Byte 1: M=0, PT=97
        assert header[1] == RTP_PAYLOAD_TYPE
        # Bytes 2-3: sequence number
        assert struct.unpack(">H", header[2:4])[0] == 0x4242
        # Bytes 4-7: timestamp
        assert struct.unpack(">I", header[4:8])[0] == 0xABCD
        # Bytes 8-11: SSRC
        assert struct.unpack(">I", header[8:12])[0] == 0x12345678

    def test_field_masks_are_applied(self) -> None:
        # Values larger than the field width should wrap, not raise.
        header = build_rtp_header(RTP_PAYLOAD_TYPE, 0x1_FFFF_FFFF, 0xDEAD_BEEF, 0x1_FFFF)
        assert struct.unpack(">H", header[2:4])[0] == 0xFFFF
        assert struct.unpack(">I", header[4:8])[0] == 0xFFFF_FFFF


class TestExtractADTSFrames:
    """extract_adts_frames scans for the 0xFFF sync word and yields frames."""

    @staticmethod
    def _adts_header(frame_len: int) -> bytes:
        """Build a minimal valid ADTS header for a frame of total length frame_len.

        Layout encodes the 13-bit frame_length across bytes 3-5; we leave
        the audio-specific bits at sensible defaults that the extractor
        does not look at.
        """
        header = bytearray(7)
        header[0] = 0xFF
        header[1] = 0xF1  # 0xF for sync hi, 0 for MPEG-4 + layer/protection
        # Bytes 3-5 encode frame_length (13 bits split across them).
        header[3] = (frame_len >> 11) & 0x03
        header[4] = (frame_len >> 3) & 0xFF
        header[5] = ((frame_len & 0x07) << 5) | 0x1F  # low 3 bits of length, then buffer fullness top
        header[6] = 0xFC
        return bytes(header)

    def test_extracts_multiple_frames(self) -> None:
        frame1 = self._adts_header(20) + b"\xAA" * 13  # 20 total
        frame2 = self._adts_header(30) + b"\xBB" * 23  # 30 total
        combined = frame1 + frame2
        frames = extract_adts_frames(combined)
        assert frames == [frame1, frame2]

    def test_streaming_consumes_garbage_and_preserves_incomplete_tail(self) -> None:
        frame1 = self._adts_header(20) + b"\xAA" * 13
        frame2 = self._adts_header(30) + b"\xBB" * 23
        prefix = b"\x00" * 40
        data = prefix + frame1 + frame2[:10]
        frames, consumed = extract_adts_frames_with_consumed(data)
        assert frames == [frame1]
        assert consumed == len(prefix) + len(frame1)
        tail = data[consumed:] + frame2[10:]
        frames, consumed = extract_adts_frames_with_consumed(tail)
        assert frames == [frame2]
        assert consumed == len(frame2)

    def test_complete_seven_byte_frame_is_consumed(self) -> None:
        frame = self._adts_header(7)
        assert extract_adts_frames(frame) == [frame]
        assert extract_adts_frames_with_consumed(frame) == ([frame], 7)

    def test_skips_garbage_before_sync(self) -> None:
        garbage = b"\x00\x01\x02"
        frame = self._adts_header(15) + b"\xCC" * 8
        frames = extract_adts_frames(garbage + frame)
        assert frames == [frame]

    def test_truncated_final_frame_dropped(self) -> None:
        # A frame whose declared length runs past the end of the buffer
        # should not be returned.
        frame = self._adts_header(40) + b"\xDD" * 5  # only 12 bytes total
        frames = extract_adts_frames(frame)
        assert frames == []

    def test_malformed_frame_len_does_not_silence_buffer(self) -> None:
        """SEC-004: a single bad frame_len must not drop subsequent valid frames.

        Previously a frame_len < 7 would break out of the parser, losing every
        frame that followed in the buffer. Now we skip one byte and rescan.
        """
        good_before = self._adts_header(20) + b"\xAA" * 13
        # Build a frame with malformed frame_len = 0 in the header bits.
        bad = bytearray(7)
        bad[0] = 0xFF
        bad[1] = 0xF1
        bad[3] = 0  # high bits
        bad[4] = 0  # mid bits
        bad[5] = 0  # low bits all zero -> frame_len = 0
        bad[6] = 0xFC
        good_after = self._adts_header(25) + b"\xCC" * 18
        combined = good_before + bytes(bad) + good_after
        frames = extract_adts_frames(combined)
        # Both legitimate frames must survive the malformed one in the middle.
        assert good_before in frames
        assert good_after in frames
