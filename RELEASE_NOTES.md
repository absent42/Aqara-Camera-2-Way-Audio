# v1.0.0 - Aqara Camera 2-Way Audio

Initial release of Aqara Camera 2-way audio integration. Refactored from the earlier G400-doorbell proof of concept.

## Highlights

- **Three quality streams per camera** -- one HA camera entity per channel (high / medium / low). Pick the one you want in dashboards; disable the others in the entity registry.
- **Snapshots** -- now supported on all three entities via Home Assistant's bundled ffmpeg.
- **Options flow** -- pick which RTSP channel carries the WebRTC 2-way audio backchannel. Default is the high-quality stream; change it any time and the integration reloads automatically.
- **Multi-camera service targeting** -- the `talk_audio_file` action now takes `entity_id` or `device_id`, plays audio through the targeted camera's speaker, and deduplicates when you target multiple entities of the same camera.
- **Persistent restart notification** -- when `go2rtc.yaml` is updated, you get a clear prompt to restart Home Assistant. Restarts where the config is already current produce no notification.
- **Full test suite** -- 60 pytest tests run on every push (and locally via the included `.githooks/pre-push` hook). GitHub Actions also runs the suite as a backstop.

## Compatibility

  | Model | Status |
  |---|---|
  | Aqara Camera G100 | Confirmed |
  | Aqara Doorbell G400 | Confirmed |
  | Other Aqara cameras | Unknown -- PRs welcome to update this table |

## Requirements

- Home Assistant 2026.2 or newer with go2rtc.
- [AlexxIT's WebRTC](https://github.com/AlexxIT/WebRTC) custom component for the dashboard 2-way audio card.
- RTSP enabled in the Aqara app (Device > More Settings > RTSP LAN Preview).

See [README.md](README.md) for installation, configuration, and automation examples.
