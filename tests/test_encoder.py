"""Unit tests for encoder.py."""

from __future__ import annotations

import shutil
import subprocess
from unittest.mock import MagicMock

import pytest

from custom_components.aqara_camera_2_way_audio.encoder import (
    FFMPEG_CMD_ALAW,
    FFMPEG_CMD_PCM,
    AACEncoder,
)


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
