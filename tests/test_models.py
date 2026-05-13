"""Unit tests for the static model registry."""

from __future__ import annotations

import pytest

from custom_components.aqara_camera_2_way_audio.models import (
    MODELS,
    AqaraCameraModel,
    find_by_code,
    find_by_substring,
)


class TestRegistryShape:
    """The registry must contain a baseline of known codes."""

    def test_g400_present(self) -> None:
        m = find_by_code("lumi.camera.agl013")
        assert m is not None
        assert m.friendly == "G400"
        assert "agl013" in m.aliases

    def test_g100_present(self) -> None:
        m = find_by_code("lumi.camera.agl005")
        assert m is not None
        assert m.friendly == "G100"

    def test_g410_global_and_china_codes_both_resolve(self) -> None:
        """Both region prefixes for G410 (acn017 + agl006) resolve to the same friendly name."""
        glob = find_by_code("lumi.camera.agl006")
        cn = find_by_code("lumi.camera.acn017")
        assert glob is not None and glob.friendly == "G410"
        assert cn is not None and cn.friendly == "G410"
        # The full_name disambiguates when needed.
        assert "China" in cn.full_name

    def test_unknown_code_returns_none(self) -> None:
        assert find_by_code("lumi.camera.nonexistent999") is None

    def test_all_entries_have_required_fields(self) -> None:
        for code, entry in MODELS.items():
            assert isinstance(entry, AqaraCameraModel)
            assert entry.code == code
            assert entry.friendly
            assert entry.full_name


class TestSubstringMatch:
    """find_by_substring uses longest-alias-wins."""

    def test_matches_full_code(self) -> None:
        m = find_by_substring("got lumi.camera.agl013 from probe")
        assert m is not None and m.friendly == "G400"

    def test_matches_hostname_uppercase(self) -> None:
        m = find_by_substring("Doorbell-Camera-G400-0001")
        assert m is not None and m.friendly == "G400"

    def test_matches_hostname_lowercase(self) -> None:
        m = find_by_substring("camera-g100-0002")
        assert m is not None and m.friendly == "G100"

    def test_returns_none_when_no_alias_matches(self) -> None:
        assert find_by_substring("random unrelated string xyz") is None

    def test_longest_alias_wins(self) -> None:
        """If 'g4' would match both G4 and G400, the longer alias must win."""
        m = find_by_substring("G400")
        assert m is not None
        assert m.friendly == "G400"
