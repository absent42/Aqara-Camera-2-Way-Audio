# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-05-13

Initial release of Aqara Camera 2-way audio integration. Refactored from the earlier G400-doorbell proof of concept.

### Added

- Three camera entities per configured camera, one per RTSP channel (`/ch1`, `/ch2`, `/ch3`), labelled "High quality", "Medium quality", "Low quality".
- Snapshot support via Home Assistant's bundled ffmpeg on all three camera entities.
- Options flow to pick which RTSP channel carries the WebRTC backchannel (default: channel 1).
- `talk_audio_file` service now accepts `entity_id` and `device_id` targets, with multi-camera fan-out and per-camera deduplication.
- Persistent notification when the `go2rtc.yaml` stream config is updated, prompting the user to restart Home Assistant.
- Stream-name collision detection in `go2rtc.yaml`: existing streams owned by another integration or camera are not overwritten.
- Full translation key coverage for the config flow, options flow, camera entity names, and service.
- pytest test suite using `pytest-homeassistant-custom-component`: 60 tests covering protocol code, encoder selection, go2rtc config IO, config and options flows, setup/unload, camera entities, and service targeting.
- Git `pre-push` hook (`.githooks/pre-push`) and GitHub Actions workflow that both run `pytest tests/`.
- HA auto-discovery: the integration appears as a "Discovered" badge in Settings > Devices & Services as soon as an Aqara camera advertises itself via mDNS (`_aqara._tcp.local.` with `cl=aiot` and `md=lumi.camera.*`).
- In-flow LAN scan: the "Add Integration" path runs a 5-second mDNS browse and presents detected cameras as a dropdown. Picking one auto-fills the IP and model; only RTSP credentials are needed.
- Manual IP entry remains as a fallback for cameras on a non-multicast-reachable network segment, or when all discovered cameras are already configured.
- Static model registry (`models.py`) populated from `logs/aqara_device_resources_all_regions.csv` plus `scripts/models_extras.yaml`. Includes G400 (`lumi.camera.agl013`), G100 (`lumi.camera.agl005`), G410 (both `agl006` and `acn017`), G2H, G2H Pro, G3, G4, G5 Pro, and E1.
- `DeviceInfo.model` now displays the friendly product name (e.g. "G400 Video Doorbell") when the camera is identified via discovery. Falls back to `None` for manual entries (no model auto-detected).

### Confirmed compatible

- Aqara G400
- Aqara G410
- Aqara G100
