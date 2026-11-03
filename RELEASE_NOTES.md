# v1.0.3 - Aqara Camera 2-Way Audio

Reliability and usability release. Talk sessions end cleanly, the integration no longer overwrites go2rtc streams you have edited by hand, and the `talk_audio_file` action is easier to use and no longer cuts off the end of a recording.

Contributions by [artemdanielov](https://github.com/artemdanielov) and [petrouv](https://github.com/petrouv).

## Highlights

- **Clean end of talk sessions** -- go2rtc now asks the bridge to stop instead of killing it, so the camera is always told the session is over, even if the browser disconnects while the session is starting.
- **Faster, bounded shutdown** -- the bridge finishes cleaning up within about seven seconds, inside go2rtc's ten-second limit, so no stray ffmpeg process is left running.
- **Your go2rtc edits are kept** -- the integration only updates or removes go2rtc streams it created. Streams you have customized in `go2rtc.yaml`, and streams from other integrations, are left alone.
- **No more cut-off recordings** -- `talk_audio_file` waits half a second after the last frame so the camera finishes playing before the session closes.
- **Simpler sound file setup** -- files in `/config/sounds` can be played without adding the folder to `allowlist_external_dirs`.
- **All cameras by default** -- calling `talk_audio_file` without a target plays on every loaded Aqara camera. If one camera cannot be reached, the others still play and the action then reports the failure. Area, floor and label targets are rejected rather than playing on every camera.
- **Safer logs** -- go2rtc config errors now include the file path and the line or OS error, without any text that could expose RTSP credentials.

See [CHANGELOG.md](CHANGELOG.md) for the full list of changes.

## Upgrading

Restart Home Assistant after updating. On first start the integration updates its go2rtc stream to the new bridge settings and shows a notification asking for one more restart so go2rtc picks them up.

- If you edited the integration's stream in `go2rtc.yaml` by hand, it is no longer updated automatically. Update the `exec:` line yourself to end with `#backchannel=1#audio=pcma/8000#killsignal=15#killtimeout=10`.
- Reloading, disabling, or changing options of the integration no longer removes its go2rtc stream. The stream is removed only when you delete the integration entry.

## Compatibility

  | Model | Status |
  |---|---|
  | Aqara Camera G100 | Confirmed |
  | Aqara Doorbell G400 | Confirmed |
  | Aqara Camera G410 | Confirmed |
  | Other Aqara cameras | Unknown -- PRs welcome to update this table |

## Requirements

- Home Assistant 2026.2 or newer with go2rtc.
- [AlexxIT's WebRTC](https://github.com/AlexxIT/WebRTC) custom component for the dashboard 2-way audio card.
- RTSP enabled in the Aqara app (Device > More Settings > RTSP LAN Preview).

See [README.md](README.md) for installation, configuration, and automation examples.
