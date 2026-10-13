# v1.0.2 - Aqara Camera 2-Way Audio

Reliability release. Talk sessions now survive network quirks and bad messages from the camera, recover cleanly when the camera drops the connection, and start with less delay.

Contribution by [alikorkmaz](https://github.com/alikorkmaz).

## Highlights

- **More reliable session start** -- talk no longer fails when the camera's reply arrives split across network reads or bundled with another reply. Applies to both browser talk and the `talk_audio_file` action.
- **Sessions survive bad messages** -- a single corrupted or unrecognised control message from the camera no longer ends an active talk session.
- **Clean recovery after a dropped connection** -- if the camera drops the talk connection while nobody is speaking, the bridge exits promptly, so the next push-to-talk starts a fresh session instead of failing.
- **Accurate camera health checks** -- a late heartbeat reply is no longer counted as the answer to the next heartbeat, so a camera that has stopped responding is detected.
- **Smoother audio** -- audio queued during a pause in speech is no longer sent in a burst when speech resumes. Duplicate frames and dropped microphone audio in the encoder are also fixed.
- **Lower latency** -- the bridge reads microphone input in 32 ms chunks instead of 256 ms, and ffmpeg no longer probes the known input format at startup.
- **Safer cleanup** -- a refused talk session no longer sends a stop command that could end another caller's session, and the camera is released if the encoder fails to start.

See [CHANGELOG.md](CHANGELOG.md) for the full list of fixes.

## Upgrading

No configuration changes. Restart Home Assistant after updating.

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
