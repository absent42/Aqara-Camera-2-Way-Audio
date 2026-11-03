"""Real-file regressions for stream ownership across discovery IP changes."""

from unittest.mock import patch

import pytest
import yaml
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aqara_camera_2_way_audio import go2rtc
from custom_components.aqara_camera_2_way_audio.const import DOMAIN

OLD_NAME = "aqara_camera_192_0_2_10"
NEW_NAME = "aqara_camera_192_0_2_11"
NAME_KEY = "go2rtc_stream_name"


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(go2rtc, "GO2RTC_CONFIG_FILE", str(tmp_path / "go2rtc.yaml"))


@pytest.mark.parametrize("old_state", ["unchanged", "reordered", "custom", "foreign"])
async def test_ip_change_then_remove(
    hass, camera_config_data, mock_talk_client, tmp_path, old_state
):
    entry = MockConfigEntry(domain=DOMAIN, data=camera_config_data, unique_id="ip-change")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    path = tmp_path / "go2rtc.yaml"
    config = yaml.safe_load(path.read_text())
    old = config["streams"][OLD_NAME]
    if old_state == "reordered":
        command, options = old[-1].split("#", 1)
        old[-1] = command + "#" + "#".join(reversed(options.split("#")))
    elif old_state == "custom":
        old[0] += "?manual=1"
    elif old_state == "foreign":
        old = ["rtsp://foreign-owner"]
    config["streams"][OLD_NAME] = old
    config["streams"]["unrelated"] = ["rtsp://other"]
    path.write_text(yaml.safe_dump(config))

    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "camera_ip": "192.0.2.11"}
    )
    await hass.async_block_till_done()
    streams = yaml.safe_load(path.read_text())["streams"]
    assert NEW_NAME in streams
    assert entry.data[NAME_KEY] == NEW_NAME
    assert entry.data[go2rtc.CONF_GO2RTC_DIGEST] == go2rtc.sources_digest(streams[NEW_NAME])
    if old_state in ("unchanged", "reordered"):
        assert OLD_NAME not in streams
    else:
        assert streams[OLD_NAME] == old
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert yaml.safe_load(path.read_text())["streams"] == {
        "unrelated": ["rtsp://other"],
        **({OLD_NAME: old} if old_state in ("custom", "foreign") else {}),
    }


@pytest.mark.parametrize("missing", ["digest", "name", "both", "legacy"])
async def test_ip_change_missing_snapshot_preserves_old(
    hass, camera_config_data, mock_talk_client, tmp_path, missing
):
    entry = MockConfigEntry(domain=DOMAIN, data=camera_config_data, unique_id="ip-change")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    path = tmp_path / "go2rtc.yaml"
    config = yaml.safe_load(path.read_text())
    if missing == "legacy":
        config["streams"][OLD_NAME][-1] = (
            config["streams"][OLD_NAME][-1].partition("#")[0] + "#backchannel=1"
        )
        path.write_text(yaml.safe_dump(config))
    old = config["streams"][OLD_NAME]
    data = {**entry.data, "camera_ip": "192.0.2.11"}
    if missing in ("digest", "both", "legacy"):
        data.pop(go2rtc.CONF_GO2RTC_DIGEST, None)
    if missing in ("name", "both", "legacy"):
        data.pop(NAME_KEY, None)
    hass.config_entries.async_update_entry(entry, data=data)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert yaml.safe_load(path.read_text())["streams"][OLD_NAME] == old
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert yaml.safe_load(path.read_text())["streams"] == {OLD_NAME: old}


@pytest.mark.parametrize("target_state", ["foreign", "write-error", "already-current"])
async def test_ip_change_registration_outcomes(
    hass, camera_config_data, mock_talk_client, tmp_path, target_state
):
    entry = MockConfigEntry(domain=DOMAIN, data=camera_config_data, unique_id="ip-change")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    path = tmp_path / "go2rtc.yaml"
    config = yaml.safe_load(path.read_text())
    previous_data = dict(entry.data)
    if target_state == "foreign":
        # Even a copy of the old owned sources under another name is not owned.
        config["streams"][NEW_NAME] = list(config["streams"][OLD_NAME])
    elif target_state == "already-current":
        config["streams"][NEW_NAME] = [
            source.replace("192.0.2.10", "192.0.2.11").replace(OLD_NAME, NEW_NAME)
            for source in config["streams"][OLD_NAME]
        ]
    path.write_text(yaml.safe_dump(config))
    before = path.read_bytes()
    with patch.object(go2rtc, "_write_config", side_effect=OSError("fixture")) if (
        target_state == "write-error"
    ) else patch.object(go2rtc, "_write_config", wraps=go2rtc._write_config):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "camera_ip": "192.0.2.11"}
        )
        await hass.async_block_till_done()
    if target_state == "already-current":
        assert OLD_NAME not in yaml.safe_load(path.read_text())["streams"]
        assert entry.data[NAME_KEY] == NEW_NAME
    else:
        assert path.read_bytes() == before
        assert entry.data[NAME_KEY] == previous_data[NAME_KEY]
        assert entry.data[go2rtc.CONF_GO2RTC_DIGEST] == previous_data[go2rtc.CONF_GO2RTC_DIGEST]
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert yaml.safe_load(path.read_text())["streams"] == (
        {NEW_NAME: config["streams"][NEW_NAME]} if target_state == "foreign" else {}
    )
