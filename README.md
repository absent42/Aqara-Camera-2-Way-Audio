# Aqara Camera 2-Way Audio - Home Assistant Integration

Local-only multi-quality streaming and two-way audio for Aqara LAN cameras. No cloud, no hub, no proprietary SDK.

Protocol reverse-engineered from the Aqara Android app. Built generically: any Aqara camera that exposes RTSP at `/ch1`, `/ch2`, `/ch3` on port 8554 plus the TCP 54324 LAN talk protocol should work.

See the [Aqara LANLink](https://github.com/absent42/Aqara-LANLink) integration for support for local detection events and more with Aqara cameras and doorbells. 

## Status

This is the v1.0 release of the integration, refactored from an earlier G400-doorbell proof of concept. It aims to be a useful, focused integration for one specific job (streaming + 2-way audio) rather than a full replacement for Aqara's cloud or HomeKit integrations. For motion events, recording, and device management, pair it with the Aqara Home app, HomeKit, or use the [Aqara LANLink](https://github.com/absent42/Aqara-LANLink) integration. 

## Features

- **Multi-quality video** - three RTSP streams per camera (high / medium / low quality), each as its own HA camera entity.
- **Snapshots** - via Home Assistant's bundled ffmpeg, on any of the three entities.
- **Two-way audio** - via go2rtc backchannel, exposed through the WebRTC custom card. Pick which RTSP channel carries the backchannel from the integration's options.
- **Audio file playback** - automation-friendly service plays AAC-ADTS files through the camera speaker.
- **Fully local** - all communication stays on your LAN.

## Requirements

- Aqara camera on the same LAN as Home Assistant, with RTSP enabled (Aqara app > Device > More Settings > RTSP LAN Preview).
- [AlexxIT's WebRTC](https://github.com/AlexxIT/WebRTC) custom component for the dashboard 2-way audio card.
- Home Assistant 2026.2+ with go2rtc.

## Installation

### HACS (custom repository)

[![Open HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=absent42&repository=Aqara-Camera-2-Way-Audio&category=Integration)

1. Click the **Open HACS** button above (Home Assistant prompts you to add the custom repository) and confirm. Alternatively, in HACS open the three-dot menu, choose **Custom repositories**, paste `https://github.com/absent42/Aqara-Camera-2-Way-Audio`, and select **Integration** as the type.
2. Restart Home Assistant.
3. Proceed to **Adding a camera** below.

### Manual install

1. Copy the `custom_components/aqara_camera_2_way_audio/` folder to your Home Assistant config directory.
2. Restart Home Assistant.
3. Proceed to **Adding a camera** below.

## Adding a camera

### Automatic (recommended)

Aqara cameras advertise themselves via mDNS. As soon as a camera is on the same LAN as Home Assistant:

1. Go to **Settings > Devices & Services**.
2. Look for a **Discovered** badge labelled "Aqara Camera 2-way Audio".
3. Click **Configure** -- the IP and model are already known. Enter the RTSP credentials from the Aqara app (Device > More Settings > RTSP LAN Preview) and submit.

If the badge doesn't appear, the camera and HA are probably not on the same multicast domain (a common issue with VLANs and isolated guest networks). Either move the camera to the same subnet or use the manual path below.

### Manual

1. Go to **Settings > Devices & Services > Add Integration > Aqara Camera 2-way Audio**.
2. The integration scans for cameras for 5 seconds:
   - **Cameras found:** Pick one from the dropdown, or pick "Other (enter IP manually)".
   - **No cameras found:** The form falls through to a manual IP entry directly.
3. Enter RTSP credentials and submit.

After install, restart Home Assistant once more so go2rtc reads the new stream config.

## Entities

Each configured camera creates one device with **three camera entities**:

| Entity | RTSP path | Notes |
|---|---|---|
| `camera.<name>_high_quality` | `/ch1` | Default channel for 2-way audio. |
| `camera.<name>_medium_quality` | `/ch2` | |
| `camera.<name>_low_quality` | `/ch3` | |

All three are enabled by default. Open **Settings > Devices & Services > Aqara Camera > [camera] > Entities** and disable the qualities you do not need.

## Two-way audio

### How it works

The Aqara camera uses a proprietary LAN protocol for two-way audio, separate from its RTSP video stream:

```
Browser microphone
  --> WebRTC (Opus/48kHz)
  --> go2rtc (transcodes to G.711 A-law / 8kHz)
  --> bridge.py (transcodes to AAC-LC / 16kHz via ffmpeg)
  --> RTP/UDP port 54323 --> camera speaker

Camera microphone
  --> RTSP (AAC / 16kHz) port 8554
  --> go2rtc (transcodes to Opus/48kHz via ffmpeg)
  --> WebRTC --> browser speaker
```

The integration writes a go2rtc stream containing three sources (RTSP, ffmpeg transcode, exec bridge) to `go2rtc.yaml`. After install or after changing the 2-way audio channel in options, restart Home Assistant so go2rtc rereads the file.

### Dashboard card

Add a [WebRTC Camera](https://github.com/AlexxIT/WebRTC) card to your dashboard:

```yaml
type: custom:webrtc-camera
streams:
  - url: aqara_camera_CAMERA_IP
    mode: webrtc
    media: video,audio,microphone
style: >
  video {aspect-ratio: 3/4; object-fit: fill;}
```

Replace `CAMERA_IP` with your camera's IP, dots replaced with underscores (e.g. `aqara_camera_192_168_1_100`).

Multi-stream card example with a listening/speaking toggle:

```yaml
type: custom:webrtc-camera
ui: true
streams:
  - url: aqara_camera_CAMERA_IP
    mode: webrtc
    media: video,audio
    name: Listening
  - url: aqara_camera_CAMERA_IP
    mode: webrtc
    media: video,audio,microphone
    name: Speaking
style: >
  video {aspect-ratio: 3/4; object-fit: fill;} .volume { padding: 4px 6px;
  display: flex } .stream { padding: 4px 8px; flex: 1 0 0; } .space, .header,
  .fullscreen, .screenshot, .pictureinpicture { display: none }
```

> **Note:** The card must use `url:` (go2rtc stream name), not `entity:` (HA entity ID). The go2rtc stream name is `aqara_camera_{ip_with_underscores}`.

> **Note:** The browser must be served over HTTPS for microphone access. The first connection will prompt for microphone permission.

### Changing the backchannel channel

By default, 2-way audio uses the high-quality stream (ch1). To change which RTSP channel carries the backchannel:

1. **Settings > Devices & Services > Aqara Camera 2-way Audio > Configure**
2. Pick the channel (1 = high, 2 = medium, 3 = low).
3. Restart Home Assistant for go2rtc to re-read its config.

### Remote access (outside your LAN)

On your home LAN the WebRTC card works out of the box. Accessing the same card from outside your network needs extra configuration -- not because of this integration, but because of how WebRTC media transport works. Two paths:

**Listening only (one-way, easy).** Switch the card to MSE, which is plain HTTPS through Home Assistant's existing reverse proxy. No NAT setup required, but **the microphone (backchannel) does not work in MSE mode**.

```yaml
type: custom:webrtc-camera
streams:
  - url: aqara_camera_CAMERA_IP
    mode: mse
    media: video,audio
```

**Full two-way audio (harder).** WebRTC media uses UDP/SRTP directly between the browser and go2rtc. For an external browser to reach your home-network go2rtc, ICE candidate exchange has to advertise a publicly-reachable address. Out of the box, go2rtc only knows its LAN address. You need one of:

1. **Public IP + port forward.** If your router has a public IPv4 address, forward UDP 8555 to the Home Assistant host and tell go2rtc its public address. In `<config>/go2rtc.yaml`:

   ```yaml
   webrtc:
     candidates:
       - stun:auto             # auto-detect public IP via STUN
       # or pin it explicitly:
       # - YOUR.PUBLIC.IP.HERE:8555
   ```

   Restart Home Assistant after editing.

2. **TURN relay (for CGNAT / cellular / restrictive networks).** Point go2rtc at a TURN server. Either run your own [coturn](https://github.com/coturn/coturn) or use a hosted service (Twilio, Cloudflare). In `<config>/go2rtc.yaml`:

   ```yaml
   webrtc:
     ice_servers:
       - urls: [turn:turn.example.com:3478]
         username: youruser
         credential: yourpass
   ```

   Adds 50-150 ms latency and bandwidth costs but works through any NAT.

#### Complete `go2rtc.yaml` example

The integration writes only its own `streams:` entries. You merge a `webrtc:` block at the top level. After both, the file looks like this (one camera shown):

```yaml
# webrtc: hand-written -- not touched by the integration
webrtc:
  candidates:
    - stun:auto            # public IP discovered via STUN
  ice_servers:
    - urls: [stun:stun.l.google.com:19302]
    # TURN entry, uncomment only if STUN alone is not enough:
    # - urls: [turn:turn.example.com:3478]
    #   username: youruser
    #   credential: yourpass

# streams: written by the Aqara Camera 2-Way Audio integration; do
# not edit by hand -- the integration rewrites this section on each
# setup or when you change the backchannel channel in options.
streams:
  aqara_camera_192_0_2_10:
    - rtsp://user:pass@192.0.2.10:8554/ch1
    - "ffmpeg:aqara_camera_192_0_2_10#video=copy#audio=opus#audio=copy"
    - "exec:/usr/local/bin/python3 /config/custom_components/aqara_camera_2_way_audio/bridge.py 192.0.2.10#backchannel=1"
```

Don't forget the router-side step: forward **UDP 8555** to the Home Assistant host (only required for the public-IP option, not for TURN). Restart Home Assistant after any edit to `go2rtc.yaml` so go2rtc reloads.

To test the WebRTC port is reachable from outside, from a phone on cellular run:

```bash
nc -uvz YOUR.PUBLIC.IP 8555 < /dev/null
```

You should see `Connection to YOUR.PUBLIC.IP 8555 port [udp/*] succeeded!`. If you don't, the port forward isn't working yet.

#### Diagnosing remote-access failures

If video appears externally but the microphone doesn't reach the camera, that's typically half-failed WebRTC (signaling worked, ICE partially worked). If neither direction works externally, that's full ICE failure. Open `chrome://webrtc-internals/` in a separate tab while the card is loading externally -- the peer connection's ICE state (`checking` -> `connected` vs. `failed`) and inbound RTP packet counts will tell you which side is failing.

go2rtc's `/api/streams?src=aqara_camera_<ip>` page also shows live producer/consumer state: a healthy external WebRTC connection appears as a producer with `format_name: "webrtc"` and non-empty sender/receiver byte counts.

## `talk_audio_file` service

Play a pre-recorded AAC-ADTS audio file through the camera speaker. Useful for automations that respond to triggers with canned audio.

```yaml
automation:
  trigger:
    platform: state
    entity_id: binary_sensor.front_door_motion
    to: "on"
  action:
    service: aqara_camera_2_way_audio.talk_audio_file
    target:
      device_id: 0123456789abcdef
    data:
      file_path: /media/doorbell_greeting.aac
```

You can target the camera by `device_id` or by any of the camera entity_ids on that device (all three quality entities map to the same physical speaker; multiple entity_ids for the same camera are deduplicated to a single playback).

Audio files must be in **AAC-ADTS** format, 16 kHz mono. Convert with ffmpeg:

```bash
ffmpeg -i input.mp3 -ar 16000 -ac 1 -c:a aac -b:a 32k -f adts output.aac
```

The file path must be inside one of HA's `allowlist_external_dirs`.

> **Note:** Browser two-way audio and `talk_audio_file` are mutually exclusive. The camera supports one voice session at a time. If the WebRTC card is connected, the service call will fail.

## Protocol details

Three independent network channels:

| Channel | Protocol | Port | Purpose |
|---|---|---|---|
| Video/Audio stream | RTSP over TCP | 8554 | H.264 video + AAC audio from camera, paths `/ch1`, `/ch2`, `/ch3` |
| Voice control | TCP | 54324 | Session management (start/stop/heartbeat) |
| Voice audio | UDP (RTP) | 54323 | AAC-LC ADTS frames to camera speaker |

### Control channel (TCP 54324)

Custom `LmLocalPacket` binary format:

```
Magic (0xFEEF) | Type (1B) | Payload Length (2B) | Payload (N) | CRC-16 (2B)
```

- **START_VOICE** (type 0): opens session, payload is epoch milliseconds.
- **STOP_VOICE** (type 1): closes session.
- **ACK** (type 2): response from camera (0 = success).
- **HEARTBEAT** (type 3): sent every 5 seconds to keep the session alive.

CRC is a CRC-16 variant with poly 0x8408 (KERMIT-style reflected polynomial) but with init 0xFFFF and final XOR 0xFFFF.

### Audio channel (UDP 54323)

Standard RTP (RFC 3550) with payload type 97 (dynamic AAC):

- **Codec:** AAC-LC ADTS, 16 kHz, mono, 32 kbps.
- **Frame size:** 1024 samples (64 ms per frame).
- **RTP timestamp clock:** 16 kHz.

## Compatibility

  | Model | Status |
  |---|---|
  | Aqara Camera G100 | Confirmed |
  | Aqara Doorbell G400 | Confirmed |
  | Other Aqara cameras | Unknown -- PRs welcome to update this table |

## Credential storage

go2rtc requires RTSP credentials in the source URL it consumes, so the integration writes the camera's RTSP username and password in cleartext into `<config>/go2rtc.yaml` when you add a camera. This file is created at Home Assistant's default file mode (the same `0644` you will see on `configuration.yaml` and `secrets.yaml`), and the credentials are also stored in Home Assistant's `.storage/core.config_entries` as plaintext JSON like every other integration's credentials. Any process or user with read access to your `/config` directory can therefore read the RTSP password: this includes the File Editor and Studio Code Server add-ons, the Samba add-on, the SSH add-on, and anyone with a backup of `/config`. If that is unacceptable for your threat model, restrict access to those add-ons and to `/config`.

## Known Limitations

- Two-way audio requires [AlexxIT's WebRTC](https://github.com/AlexxIT/WebRTC) custom component.
- The dashboard card must use `url:` (go2rtc stream name), not `entity:` (HA entity ID).
- A Home Assistant restart is required after initial install and after any change to the backchannel channel in options.
- Backchannel audio quality is limited to G.711 A-law at 8 kHz (a go2rtc limitation).
- The camera supports one voice session at a time.
- Only the models listed in the Compatibility table have been verified to work; other Aqara cameras may or may not.
- Out-of-the-box, WebRTC 2-way audio works only on the same LAN as Home Assistant. Remote access with 2-way audio requires go2rtc WebRTC ICE configuration (STUN/TURN or public port forward); see the "Remote access" section above. Remote viewing alone (no microphone) works without extra setup if you switch the dashboard card to `mode: mse`.

## Development

Run the test suite:

```bash
python3 -m pip install -r requirements_test.txt
python3 -m pytest tests/
```

Install the pre-push hook so tests run before every push:

```bash
git config core.hooksPath .githooks
```

GitHub Actions also runs the same `pytest tests/` on every push and pull request as a backstop.
