"""Audio-to-AAC-ADTS encoder using ffmpeg subprocess.

Accepts raw audio input and produces AAC-LC ADTS frames matching the
Aqara camera's requirements: 16kHz, mono, 32kbps.

Supports two input formats:
- PCM s16le 16kHz mono (for direct use / go2rtc v1.9.14+)
- G.711 A-law 8kHz mono (for go2rtc v1.9.9 backchannel)
"""

from __future__ import annotations

import logging
import select
import subprocess

from .protocol import extract_adts_frames_with_consumed

_LOGGER = logging.getLogger(__name__)

FFMPEG_CMD_PCM = [
    "ffmpeg",
    "-hide_banner",
    "-loglevel", "error",
    "-fflags", "nobuffer",
    "-flags", "low_delay",
    "-f", "s16le",
    "-ar", "16000",
    "-ac", "1",
    # Raw input has a known format; do not buffer it for stream discovery.
    "-probesize", "32",
    "-analyzeduration", "0",
    "-i", "pipe:0",
    "-c:a", "aac",
    "-profile:a", "aac_low",
    "-b:a", "32k",
    "-ar", "16000",
    "-ac", "1",
    "-f", "adts",
    "pipe:1",
]

FFMPEG_CMD_ALAW = [
    "ffmpeg",
    "-hide_banner",
    "-loglevel", "error",
    "-fflags", "nobuffer",
    "-flags", "low_delay",
    "-f", "alaw",
    "-ar", "8000",
    "-ac", "1",
    "-probesize", "32",
    "-analyzeduration", "0",
    "-i", "pipe:0",
    "-c:a", "aac",
    "-profile:a", "aac_low",
    "-b:a", "32k",
    "-ar", "16000",
    "-ac", "1",
    "-f", "adts",
    "pipe:1",
]


class AACEncoder:
    """Encode audio to AAC-ADTS via ffmpeg subprocess."""

    def __init__(self, input_format: str = "pcm") -> None:
        """Initialize encoder.

        Args:
            input_format: "pcm" for s16le 16kHz mono, "alaw" for G.711 A-law 8kHz.
        """
        self._proc: subprocess.Popen | None = None
        self._buffer = bytearray()
        self._flushed = False
        self._cmd = FFMPEG_CMD_ALAW if input_format == "alaw" else FFMPEG_CMD_PCM

    def start(self) -> None:
        """Start the ffmpeg encoder process."""
        self._flushed = False
        self._buffer.clear()
        self._proc = subprocess.Popen(
            self._cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
        )

    def write_pcm(self, pcm_data: bytes) -> list[bytes]:
        """Write audio data and return any available AAC-ADTS frames.

        Non-blocking read of ffmpeg stdout after writing data to stdin.
        Returns a list of complete ADTS frames (may be empty if ffmpeg
        hasn't produced output yet).
        """
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("Encoder not started")

        frames = self._read_available()
        # bufsize=0 gives us a raw pipe: writes may be partial. A broken pipe
        # must propagate so the bridge can release the voice session.
        remaining = memoryview(pcm_data)
        while remaining:
            written = self._proc.stdin.write(remaining)
            if not written:
                raise BrokenPipeError("ffmpeg input pipe closed")
            remaining = remaining[written:]
        self._proc.stdin.flush()

        frames.extend(self._read_available())
        return frames

    def _extract_frames(self) -> list[bytes]:
        """Consume both complete frames and bytes skipped during resync."""
        frames, consumed = extract_adts_frames_with_consumed(self._buffer)
        del self._buffer[:consumed]
        return frames

    def _read_available(self) -> list[bytes]:
        """Drain ready stdout before/after writing to avoid filling both pipes."""
        if not self._proc or not self._proc.stdout:
            return []

        # Non-blocking read of available output
        received = False
        while select.select([self._proc.stdout], [], [], 0)[0]:
            chunk = self._proc.stdout.read(4096)
            if not chunk:
                break
            self._buffer.extend(chunk)
            received = True

        # Unchanged data was already scanned; only an incomplete frame remains.
        return self._extract_frames() if received else []

    def flush(self) -> list[bytes]:
        """Close encoder input and return any remaining AAC-ADTS frames.

        Call this after the last write_pcm() to get frames still buffered
        inside the ffmpeg process.
        """
        if not self._proc or self._flushed:
            return []
        self._flushed = True

        if self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
            # communicate() otherwise tries to flush the already closed pipe
            # and raises ValueError before collecting the final AAC frames.
            self._proc.stdin = None

        # Read all remaining output (blocking, with timeout)
        try:
            remaining, _ = self._proc.communicate(timeout=5)
            if remaining:
                self._buffer.extend(remaining)
        except subprocess.TimeoutExpired:
            _LOGGER.warning("ffmpeg flush timed out; killing and reaping")
            self._proc.kill()
            try:
                remaining, _ = self._proc.communicate()
                if remaining:
                    self._buffer.extend(remaining)
            except OSError as err:
                _LOGGER.warning("ffmpeg reap after kill failed: %s", err)
        except OSError as err:
            _LOGGER.warning("ffmpeg flush error: %s", err)

        return self._extract_frames()

    def stop(self) -> None:
        """Stop the ffmpeg encoder process."""
        if not self._proc:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            try:
                self._proc.wait()
            except OSError as err:
                _LOGGER.warning("ffmpeg reap after kill failed: %s", err)
        if self._proc.stdout:
            self._proc.stdout.close()
        self._proc = None
        self._buffer.clear()
