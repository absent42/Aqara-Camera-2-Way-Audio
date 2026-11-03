"""Partial ownership snapshots must not authorize adoption or deletion."""

import pytest
import yaml
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aqara_camera_2_way_audio import async_remove_entry, go2rtc
from custom_components.aqara_camera_2_way_audio.const import DOMAIN


@pytest.mark.parametrize("missing", ["name", "digest", "empty-name", "empty-digest", "invalid-digest"])
@pytest.mark.parametrize("target", ["old-copy", "exact", "legacy"])
def test_partial_snapshot_preserves_existing_target(tmp_path, missing, target):
    old = [go2rtc.build_exec_source(str(tmp_path), "192.0.2.10")]
    desired = [go2rtc.build_exec_source(str(tmp_path), "192.0.2.11")]
    existing = {
        "old-copy": old,
        "exact": desired,
        "legacy": [desired[0].partition("#")[0] + "#backchannel=1"],
    }[target]
    path = tmp_path / "go2rtc.yaml"
    path.write_text("# preserve\n" + yaml.safe_dump({"streams": {"new": existing}}))
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    name = None if missing == "name" else "" if missing == "empty-name" else "new"
    digest = (
        None if missing == "digest"
        else "" if missing == "empty-digest"
        else "invalid" if missing == "invalid-digest"
        else go2rtc.sources_digest(old)
    )
    assert go2rtc.register_stream(
        str(tmp_path), "new", desired, "192.0.2.11", digest, name
    ) == "skipped"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("missing", ["name", "digest", "empty-name", "empty-digest"])
async def test_partial_snapshot_removal_preserves_ip_derived_foreign_target(
    hass, camera_config_data, tmp_path, monkeypatch, missing
):
    path = tmp_path / "go2rtc.yaml"
    monkeypatch.setattr(go2rtc, "GO2RTC_CONFIG_FILE", str(path))
    name = "aqara_camera_192_0_2_10"
    existing = ["rtsp://copied-old-sources"]
    path.write_text("# preserve\n" + yaml.safe_dump({"streams": {name: existing}}))
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    data = {
        **camera_config_data,
        go2rtc.CONF_GO2RTC_STREAM_NAME: name,
        go2rtc.CONF_GO2RTC_DIGEST: go2rtc.sources_digest(existing),
    }
    key = (
        go2rtc.CONF_GO2RTC_STREAM_NAME if "name" in missing
        else go2rtc.CONF_GO2RTC_DIGEST
    )
    if missing.startswith("empty-"):
        data[key] = ""
    else:
        data.pop(key)
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    await async_remove_entry(hass, entry)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
