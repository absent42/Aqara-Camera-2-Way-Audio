"""Aqara camera model registry."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AqaraCameraModel:
    """One known Aqara camera model code and its human-readable metadata."""

    code: str
    friendly: str
    full_name: str
    aliases: tuple[str, ...] = ()


MODELS: dict[str, AqaraCameraModel] = {
    "lumi.camera.acn003": AqaraCameraModel(
        code="lumi.camera.acn003",
        friendly="G2H Pro",
        full_name="Camera Hub G2H Pro",
        aliases=("acn003", "g2hpro"),
    ),
    # CSV-derived.
    "lumi.camera.acn005": AqaraCameraModel(
        code="lumi.camera.acn005",
        friendly="G4",
        full_name="G4 Doorbell (China)",
        aliases=("acn005", "g4"),
    ),
    "lumi.camera.acn007": AqaraCameraModel(
        code="lumi.camera.acn007",
        friendly="E1",
        full_name="Camera E1",
        aliases=("acn007", "e1"),
    ),
    "lumi.camera.acn010": AqaraCameraModel(
        code="lumi.camera.acn010",
        friendly="G5 Pro",
        full_name="Camera Hub G5 Pro (PoE)",
        aliases=("acn010", "g5pro"),
    ),
    # CSV-derived; not maintainer-probed.
    "lumi.camera.acn017": AqaraCameraModel(
        code="lumi.camera.acn017",
        friendly="G410",
        full_name="G410 Doorbell Camera Hub (China)",
        aliases=("acn017", "g410"),
    ),
    "lumi.camera.agl001": AqaraCameraModel(
        code="lumi.camera.agl001",
        friendly="G2H Pro",
        full_name="Camera Hub G2H Pro",
        aliases=("agl001", "g2hpro"),
    ),
    # CSV-derived. Note G4 != G400 -- separate products.
    "lumi.camera.agl002": AqaraCameraModel(
        code="lumi.camera.agl002",
        friendly="G4",
        full_name="G4 Doorbell",
        aliases=("agl002", "g4"),
    ),
    "lumi.camera.agl003": AqaraCameraModel(
        code="lumi.camera.agl003",
        friendly="G5 Pro",
        full_name="Camera Hub G5 Pro (Wi-Fi)",
        aliases=("agl003", "g5pro"),
    ),
    "lumi.camera.agl004": AqaraCameraModel(
        code="lumi.camera.agl004",
        friendly="G5 Pro",
        full_name="Camera Hub G5 Pro (Wi-Fi)",
        aliases=("agl004", "g5pro"),
    ),
    # Maintainer-probed 2026-05-13, firmware 4.3.8.
    "lumi.camera.agl005": AqaraCameraModel(
        code="lumi.camera.agl005",
        friendly="G100",
        full_name="G100 Camera",
        aliases=("agl005", "g100"),
    ),
    # CSV-derived; not maintainer-probed (no G410 hardware available).
    "lumi.camera.agl006": AqaraCameraModel(
        code="lumi.camera.agl006",
        friendly="G410",
        full_name="G410 Doorbell Camera Hub",
        aliases=("agl006", "g410"),
    ),
    # Maintainer-probed 2026-05-13, firmware 4.5.20.
    "lumi.camera.agl013": AqaraCameraModel(
        code="lumi.camera.agl013",
        friendly="G400",
        full_name="G400 Video Doorbell",
        aliases=("agl013", "g400"),
    ),
    "lumi.camera.gwag03": AqaraCameraModel(
        code="lumi.camera.gwag03",
        friendly="G2H",
        full_name="Camera Hub G2H",
        aliases=("gwag03", "g2h"),
    ),
    "lumi.camera.gwpagl01": AqaraCameraModel(
        code="lumi.camera.gwpagl01",
        friendly="G3",
        full_name="Camera Hub G3",
        aliases=("gwpagl01", "g3"),
    ),
    "lumi.camera.gwpgl1": AqaraCameraModel(
        code="lumi.camera.gwpgl1",
        friendly="G3",
        full_name="Camera Hub G3",
        aliases=("gwpgl1", "g3"),
    ),
}


def find_by_code(code: str) -> AqaraCameraModel | None:
    """Exact-match lookup against the canonical `md` value."""
    return MODELS.get(code)


def find_by_substring(haystack: str) -> AqaraCameraModel | None:
    """Case-insensitive substring search; longest alias wins."""
    haystack = haystack.lower()
    best: tuple[int, AqaraCameraModel] | None = None
    for model in MODELS.values():
        for tag in (model.code.lower(), model.friendly.lower(), *model.aliases):
            if tag and tag in haystack:
                if best is None or len(tag) > best[0]:
                    best = (len(tag), model)
    return best[1] if best else None
