"""Unit tests for encoder.py."""

from __future__ import annotations

import shutil
import io
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from custom_components.aqara_camera_2_way_audio.encoder import (
    FFMPEG_CMD_ALAW,
    FFMPEG_CMD_PCM,
    AACEncoder,
)
from custom_components.aqara_camera_2_way_audio.protocol import extract_adts_frames_with_consumed


class TestFFmpegCommandSelection:
    """AACEncoder picks the right ffmpeg argv for each input format."""

    def test_pcm_default(self) -> None:
        enc = AACEncoder()
        assert enc._cmd == FFMPEG_CMD_PCM

    def test_pcm_explicit(self) -> None:
        enc = AACEncoder(input_format="pcm")
        assert enc._cmd == FFMPEG_CMD_PCM

    def test_alaw(self) -> None:
        enc = AACEncoder(input_format="alaw")
        assert enc._cmd == FFMPEG_CMD_ALAW


class TestFFmpegCommandShape:
    """Both ffmpeg command lines target the correct AAC profile and rate."""

    @pytest.mark.parametrize("cmd", [FFMPEG_CMD_PCM, FFMPEG_CMD_ALAW])
    def test_targets_aac_low_at_16khz_mono(self, cmd: list[str]) -> None:
        # Codec selection
        assert cmd[cmd.index("-c:a") + 1] == "aac"
        assert cmd[cmd.index("-profile:a") + 1] == "aac_low"
        # Both commands have two -ar pairs (input rate then output rate).
        # The OUTPUT (last) -ar must be 16000 -- that's the rate the camera expects.
        ar_indices = [i for i, x in enumerate(cmd) if x == "-ar"]
        assert len(ar_indices) == 2
        assert cmd[ar_indices[-1] + 1] == "16000"
        # Mono output: same pattern for -ac
        ac_indices = [i for i, x in enumerate(cmd) if x == "-ac"]
        assert cmd[ac_indices[-1] + 1] == "1"
        # ADTS muxer writing to stdout
        assert cmd[-3:] == ["-f", "adts", "pipe:1"]

    def test_alaw_input_rate_is_8khz(self) -> None:
        # Input section is the prefix up to "-i pipe:0"; the INPUT -ar
        # (first occurrence in that section) must be 8000 for the alaw command.
        i_index = FFMPEG_CMD_ALAW.index("-i")
        input_section = FFMPEG_CMD_ALAW[:i_index]
        assert input_section[input_section.index("-f") + 1] == "alaw"
        assert input_section[input_section.index("-ar") + 1] == "8000"

    @pytest.mark.parametrize("cmd", [FFMPEG_CMD_PCM, FFMPEG_CMD_ALAW])
    def test_raw_input_probe_is_bounded(self, cmd: list[str]) -> None:
        assert cmd[cmd.index("-probesize") + 1] == "32"
        assert cmd[cmd.index("-analyzeduration") + 1] == "0"
        assert cmd.index("-probesize") < cmd.index("-i")


class TestStartRequiresFFmpeg:
    """If ffmpeg is not installed we cannot exercise live encoding."""

    def test_write_pcm_without_start_raises(self) -> None:
        enc = AACEncoder()
        with pytest.raises(RuntimeError):
            enc.write_pcm(b"")

    @pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
    def test_start_and_stop_no_data(self) -> None:
        # Smoke test only -- start then immediately stop; should not raise.
        enc = AACEncoder()
        enc.start()
        enc.stop()

    @pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
    @pytest.mark.parametrize("input_format,chunk", [
        ("pcm", b"\x00" * 1024),
        ("alaw", b"\xd5" * 256),
    ])
    def test_real_encoder_preserves_aac_format_and_flush_api(self, input_format, chunk) -> None:
        enc = AACEncoder(input_format=input_format)
        enc.start()
        frames = []
        try:
            for _ in range(32):
                frames.extend(enc.write_pcm(chunk))
            frames.extend(enc.flush())
            assert frames
            for frame in frames:
                assert frame[:2] == b"\xff\xf1"
                assert (frame[2] >> 6) == 1  # AAC-LC
                assert ((frame[2] >> 2) & 15) == 8  # 16kHz
                channels = ((frame[2] & 1) << 2) | (frame[3] >> 6)
                assert channels == 1
            assert enc.flush() == []
        finally:
            enc.stop()


class TestSubprocessCleanup:
    """OPT-006/007: encoder must reap subprocesses on timeout, not leak zombies."""

    def test_flush_kills_and_reaps_on_timeout(self) -> None:
        enc = AACEncoder()
        proc = MagicMock()
        proc.stdin = MagicMock()
        first_call = {"done": False}

        def communicate(timeout: float | None = None) -> tuple[bytes, bytes]:
            if not first_call["done"]:
                first_call["done"] = True
                raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)
            return (b"", b"")

        proc.communicate.side_effect = communicate
        enc._proc = proc

        enc.flush()

        proc.kill.assert_called_once()
        assert proc.communicate.call_count == 2

    def test_stop_kills_and_waits_on_timeout(self) -> None:
        enc = AACEncoder()
        proc = MagicMock()
        proc.stdin = MagicMock()
        first_call = {"done": False}

        def wait(timeout: float | None = None) -> int:
            if not first_call["done"]:
                first_call["done"] = True
                raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)
            return 0

        proc.wait.side_effect = wait
        enc._proc = proc

        enc.stop()

        proc.kill.assert_called_once()
        assert proc.wait.call_count == 2
        assert enc._proc is None

    def test_flush_real_pipe_does_not_flush_closed_stdin(self) -> None:
        """Popen.communicate must not raise ValueError after explicit close."""
        frame = bytes.fromhex("fff16040017ffc") + b"data"
        enc = AACEncoder()
        enc._proc = subprocess.Popen(
            [sys.executable, "-c",
             "import sys; sys.stdin.buffer.read(); "
             f"sys.stdout.buffer.write(bytes.fromhex('{frame.hex()}'))"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0,
        )
        try:
            assert enc.flush() == [frame]
            assert enc._proc.stdin is None
            assert enc.flush() == []
        finally:
            enc.stop()


class TestStreamingWrites:
    def test_closed_pipe_is_reported_instead_of_silently_dropping_input(self) -> None:
        enc = AACEncoder()
        enc._proc = MagicMock()
        enc._proc.stdin.write.return_value = 0
        with patch.object(enc, "_read_available", return_value=[]):
            with pytest.raises(BrokenPipeError):
                enc.write_pcm(b"data")

    def test_partial_raw_pipe_writes_are_completed(self) -> None:
        class ShortWriter(io.BytesIO):
            def write(self, data) -> int:
                return super().write(bytes(data[:2]))

        enc = AACEncoder()
        enc._proc = MagicMock()
        enc._proc.stdin = ShortWriter()
        with patch.object(enc, "_read_available", return_value=[]) as read:
            assert enc.write_pcm(b"1234567") == []
        assert enc._proc.stdin.getvalue() == b"1234567"
        assert read.call_count == 2

    def test_resync_does_not_emit_a_frame_twice(self) -> None:
        frame = bytes.fromhex("fff16040017ffc") + b"data"
        enc = AACEncoder()
        enc._buffer.extend(b"\x00" * 40 + frame)
        assert enc._extract_frames() == [frame]
        assert enc._extract_frames() == []

    def test_extraction_accepts_buffer_without_copy_and_returns_bytes(self) -> None:
        frame = bytes.fromhex("fff16040017ffc") + b"data"
        enc = AACEncoder()
        enc._buffer.extend(frame)
        with patch("custom_components.aqara_camera_2_way_audio.encoder.extract_adts_frames_with_consumed",
                   wraps=extract_adts_frames_with_consumed) as extract:
            frames = enc._extract_frames()
        assert extract.call_args.args[0] is enc._buffer
        assert frames == [frame] and type(frames[0]) is bytes

    def test_read_without_new_output_skips_rescan(self) -> None:
        enc = AACEncoder()
        enc._proc = MagicMock()
        enc._buffer.extend(bytes.fromhex("fff16040017ffc"))  # incomplete frame
        with patch("custom_components.aqara_camera_2_way_audio.encoder.select.select",
                   return_value=([], [], [])), \
             patch.object(enc, "_extract_frames") as extract:
            assert enc._read_available() == []
        extract.assert_not_called()

    def test_write_drains_output_before_input_and_afterwards(self) -> None:
        enc = AACEncoder()
        enc._proc = MagicMock()
        enc._proc.stdin.write.return_value = 4
        calls = []
        enc._proc.stdin.write.side_effect = lambda data: calls.append("write") or len(data)
        with patch.object(enc, "_read_available", side_effect=lambda: calls.append("read") or []):
            enc.write_pcm(b"data")
        assert calls == ["read", "write", "read"]
