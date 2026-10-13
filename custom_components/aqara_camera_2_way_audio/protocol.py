"""Aqara camera LAN protocol: packet construction, CRC, and RTP headers.

Protocol reverse-engineered from the Aqara Android app (com.lumi.module.rtsp).
See README for the full port layout and packet format.
"""

from __future__ import annotations

import struct

import logging

from .const import MAGIC, TYPE_ACK

_LOGGER = logging.getLogger(__name__)

# =============================================================================
# CRC-16/KERMIT
# Poly=0x8408 (reflected 0x1021), Init=0xFFFF, FinalXOR=0xFFFF
# =============================================================================

CRC16_TABLE = [
    0x0000, 0x1189, 0x2312, 0x329B, 0x4624, 0x57AD, 0x6536, 0x74BF,
    0x8C48, 0x9DC1, 0xAF5A, 0xBED3, 0xCA6C, 0xDBE5, 0xE97E, 0xF8F7,
    0x1081, 0x0108, 0x3393, 0x221A, 0x56A5, 0x472C, 0x75B7, 0x643E,
    0x9CC9, 0x8D40, 0xBFDB, 0xAE52, 0xDAED, 0xCB64, 0xF9FF, 0xE876,
    0x2102, 0x308B, 0x0210, 0x1399, 0x6726, 0x76AF, 0x4434, 0x55BD,
    0xAD4A, 0xBCC3, 0x8E58, 0x9FD1, 0xEB6E, 0xFAE7, 0xC87C, 0xD9F5,
    0x3183, 0x200A, 0x1291, 0x0318, 0x77A7, 0x662E, 0x54B5, 0x453C,
    0xBDCB, 0xAC42, 0x9ED9, 0x8F50, 0xFBEF, 0xEA66, 0xD8FD, 0xC974,
    0x4204, 0x538D, 0x6116, 0x709F, 0x0420, 0x15A9, 0x2732, 0x36BB,
    0xCE4C, 0xDFC5, 0xED5E, 0xFCD7, 0x8868, 0x99E1, 0xAB7A, 0xBAF3,
    0x5285, 0x430C, 0x7197, 0x601E, 0x14A1, 0x0528, 0x37B3, 0x263A,
    0xDECD, 0xCF44, 0xFDDF, 0xEC56, 0x98E9, 0x8960, 0xBBFB, 0xAA72,
    0x6306, 0x728F, 0x4014, 0x519D, 0x2522, 0x34AB, 0x0630, 0x17B9,
    0xEF4E, 0xFEC7, 0xCC5C, 0xDDD5, 0xA96A, 0xB8E3, 0x8A78, 0x9BF1,
    0x7387, 0x620E, 0x5095, 0x411C, 0x35A3, 0x242A, 0x16B1, 0x0738,
    0xFFCF, 0xEE46, 0xDCDD, 0xCD54, 0xB9EB, 0xA862, 0x9AF9, 0x8B70,
    0x8408, 0x9581, 0xA71A, 0xB693, 0xC22C, 0xD3A5, 0xE13E, 0xF0B7,
    0x0840, 0x19C9, 0x2B52, 0x3ADB, 0x4E64, 0x5FED, 0x6D76, 0x7CFF,
    0x9489, 0x8500, 0xB79B, 0xA612, 0xD2AD, 0xC324, 0xF1BF, 0xE036,
    0x18C1, 0x0948, 0x3BD3, 0x2A5A, 0x5EE5, 0x4F6C, 0x7DF7, 0x6C7E,
    0xA50A, 0xB483, 0x8618, 0x9791, 0xE32E, 0xF2A7, 0xC03C, 0xD1B5,
    0x2942, 0x38CB, 0x0A50, 0x1BD9, 0x6F66, 0x7EEF, 0x4C74, 0x5DFD,
    0xB58B, 0xA402, 0x9699, 0x8710, 0xF3AF, 0xE226, 0xD0BD, 0xC134,
    0x39C3, 0x284A, 0x1AD1, 0x0B58, 0x7FE7, 0x6E6E, 0x5CF5, 0x4D7C,
    0xC60C, 0xD785, 0xE51E, 0xF497, 0x8028, 0x91A1, 0xA33A, 0xB2B3,
    0x4A44, 0x5BCD, 0x6956, 0x78DF, 0x0C60, 0x1DE9, 0x2F72, 0x3EFB,
    0xD68D, 0xC704, 0xF59F, 0xE416, 0x90A9, 0x8120, 0xB3BB, 0xA232,
    0x5AC5, 0x4B4C, 0x79D7, 0x685E, 0x1CE1, 0x0D68, 0x3FF3, 0x2E7A,
    0xE70E, 0xF687, 0xC41C, 0xD595, 0xA12A, 0xB0A3, 0x8238, 0x93B1,
    0x6B46, 0x7ACF, 0x4854, 0x59DD, 0x2D62, 0x3CEB, 0x0E70, 0x1FF9,
    0xF78F, 0xE606, 0xD49D, 0xC514, 0xB1AB, 0xA022, 0x92B9, 0x8330,
    0x7BC7, 0x6A4E, 0x58D5, 0x495C, 0x3DE3, 0x2C6A, 0x1EF1, 0x0F78,
]


def crc16_kermit(data: bytes) -> int:
    """Compute CRC-16/KERMIT (Aqara variant) of data.

    Parameters match the device: poly=0x8408, init=0xFFFF, final XOR 0xFFFF.
    """
    crc = 0xFFFF
    for b in data:
        crc = CRC16_TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return (~crc) & 0xFFFF


# =============================================================================
# LmLocalPacket -- TCP control channel (port 54324)
# =============================================================================


def build_packet(pkt_type: int, payload_value: int) -> bytes:
    """Build an LmLocalPacket for the TCP control channel.

    For ACK packets the payload is a single byte.
    For all other packet types the payload is an 8-byte big-endian uint64
    (typically the session epoch-ms timestamp).

    Wire format: Magic(2) | Type(1) | PayloadLen(2) | Payload(N) | CRC(2)
    CRC covers bytes from Type through end of Payload.
    """
    if pkt_type == TYPE_ACK:
        payload_bytes = struct.pack(">B", payload_value & 0xFF)
    else:
        payload_bytes = struct.pack(">Q", payload_value)

    header = MAGIC + struct.pack(">B", pkt_type) + struct.pack(">H", len(payload_bytes))
    crc_data = header[2:] + payload_bytes
    crc = crc16_kermit(crc_data)
    return header + payload_bytes + struct.pack(">H", crc)


def parse_packet(data: bytes) -> dict | None:
    """Parse an LmLocalPacket from raw bytes.

    Returns a dict with keys ``type`` and ``value``, or ``None`` if the
    data is invalid (wrong magic, bad length, bad CRC, or unknown type).
    """
    if len(data) < 8 or data[0:2] != MAGIC:
        return None
    pkt_type = data[2]
    if pkt_type > 3:
        return None
    payload_len = struct.unpack(">H", data[3:5])[0]
    if len(data) < 5 + payload_len + 2:
        return None

    crc_data = data[2 : 5 + payload_len]
    expected_crc = struct.unpack(">H", data[5 + payload_len : 7 + payload_len])[0]
    if crc16_kermit(crc_data) != expected_crc:
        return None

    payload_bytes = data[5 : 5 + payload_len]
    value = (
        payload_bytes[0]
        if pkt_type == TYPE_ACK
        else int.from_bytes(payload_bytes, "big")
    )
    return {"type": pkt_type, "value": value}


# Real control packets carry at most an 8-byte payload. A larger declared
# length means the reader locked onto a false header and must resync.
MAX_CONTROL_PAYLOAD = 64


class ControlPacketBuffer:
    """Reassemble control packets from a TCP byte stream.

    TCP may split one packet across reads or coalesce several into one.
    Garbage, corrupt packets and unknown types are skipped by resyncing to
    the next MAGIC, so one bad packet cannot desynchronise the stream.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def __len__(self) -> int:
        return len(self._buf)

    def feed(self, data: bytes) -> None:
        """Append bytes received from the socket."""
        self._buf.extend(data)

    def clear(self) -> None:
        """Discard everything buffered, including partial packets."""
        self._buf.clear()

    def pop(self) -> dict | None:
        """Remove and return the first valid packet, or None if incomplete."""
        buf = self._buf
        while len(buf) >= 5:
            if buf[:2] != MAGIC:
                self._resync(1)
                continue
            length = int.from_bytes(buf[3:5], "big")
            if length > MAX_CONTROL_PAYLOAD:
                self._resync(2)
                continue
            total = 7 + length
            if len(buf) < total:
                return None
            parsed = parse_packet(bytes(buf[:total]))
            if parsed is None:
                # Bad CRC or unknown type. Drop only the header: if it was a
                # false match, a real packet may start inside these bytes.
                _LOGGER.debug("Skipping unparseable control packet %s", buf[:total].hex())
                self._resync(2)
                continue
            del buf[:total]
            return parsed
        return None

    def _resync(self, start: int) -> None:
        """Discard buffered bytes up to the next MAGIC at or after start."""
        buf = self._buf
        index = buf.find(MAGIC, start)
        if index < 0:
            # Keep a trailing first MAGIC byte; its partner may still be in flight.
            index = len(buf) - 1 if buf.endswith(MAGIC[:1]) else len(buf)
        _LOGGER.debug("Discarding %d control bytes to resync", index)
        del buf[:index]


# =============================================================================
# RTP header -- UDP audio channel (port 54323)
# =============================================================================


def build_rtp_header(payload_type: int, timestamp: int, ssrc: int, seq_num: int) -> bytes:
    """Build a minimal 12-byte RTP header (RFC 3550).

    Version=2, no padding, no extension, no CSRC, no marker.
    All integer fields are masked to their respective bit widths.
    """
    return struct.pack(
        ">BBHII",
        0x80,
        payload_type,
        seq_num & 0xFFFF,
        timestamp & 0xFFFFFFFF,
        ssrc & 0xFFFFFFFF,
    )


# =============================================================================
# AAC-ADTS frame extraction
# =============================================================================


def extract_adts_frames(data: bytes) -> list[bytes]:
    """Extract individual AAC-ADTS frames from a byte buffer.

    Scans for the 12-bit sync word 0xFFF, reads the frame length from the
    ADTS header, and collects each complete frame. Bytes before the first
    sync word are silently skipped. Malformed frame_len values (less than
    the 7-byte header itself) advance the cursor by one byte and keep
    scanning, so one bad frame from a malfunctioning or hostile source
    cannot suppress the rest of the buffer.
    """
    frames, _ = extract_adts_frames_with_consumed(data)
    return frames


def extract_adts_frames_with_consumed(data: bytes | bytearray) -> tuple[list[bytes], int]:
    """Return frames and the number of bytes consumed, including resync bytes.

    Streaming callers must remove skipped garbage as well as complete frames,
    and may pass their bytearray directly: returned frames are always copies.
    The incomplete trailing header/frame is retained for the next read.
    ``extract_adts_frames`` keeps its existing list-only API for file callers.
    """
    frames: list[bytes] = []
    offset = 0
    malformed_logged = False
    while offset <= len(data) - 7:
        if data[offset] != 0xFF or (data[offset + 1] & 0xF0) != 0xF0:
            offset += 1
            continue
        frame_len = (
            (data[offset + 3] & 0x03) << 11
            | data[offset + 4] << 3
            | (data[offset + 5] >> 5)
        )
        if frame_len < 7:
            if not malformed_logged:
                _LOGGER.warning(
                    "ADTS frame_len %d < 7 at offset %d; skipping byte and rescanning",
                    frame_len, offset,
                )
                malformed_logged = True
            offset += 1
            continue
        if offset + frame_len > len(data):
            # Declared frame extends past end of buffer: incomplete trailing
            # frame. Caller is expected to buffer the tail across calls.
            break
        frames.append(bytes(data[offset : offset + frame_len]))
        offset += frame_len
    return frames, offset
