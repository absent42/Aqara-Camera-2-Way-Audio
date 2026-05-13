"""Constants for the Aqara Camera 2-Way Audio integration."""

from homeassistant.const import Platform

DOMAIN = "aqara_camera_2_way_audio"

# Config keys
CONF_CAMERA_IP = "camera_ip"
CONF_RTSP_USERNAME = "rtsp_username"
CONF_RTSP_PASSWORD = "rtsp_password"

# Discovery-populated keys (set when the camera was added via mDNS
# auto-discovery or in-flow scan; empty string for manual-IP entries).
# Surfaced in DeviceInfo and the entry title.
CONF_MODEL_CODE = "model_code"        # e.g. "lumi.camera.agl013"
CONF_DEVICE_ID = "device_id"          # e.g. "lumi3.1234567890123456"
CONF_INSTANCE_NAME = "instance_name"  # e.g. "Doorbell-Camera-G400-1A12"

# Options keys
CONF_BACKCHANNEL_CHANNEL = "backchannel_channel"
DEFAULT_BACKCHANNEL_CHANNEL = 1

# RTSP channels exposed by Aqara cameras (high, medium, low quality)
CHANNELS = (1, 2, 3)

# Network ports
RTSP_PORT = 8554
CONTROL_PORT = 54324
AUDIO_PORT = 54323

# Protocol constants
MAGIC = b"\xFE\xEF"
TYPE_START_VOICE = 0
TYPE_STOP_VOICE = 1
TYPE_ACK = 2
TYPE_HEARTBEAT = 3
RTP_PAYLOAD_TYPE = 97  # Dynamic PT for AAC

# Timing
HEARTBEAT_INTERVAL = 5.0  # seconds
TCP_CONNECT_TIMEOUT = 3.0  # seconds

# AAC-LC framing for the camera's 16 kHz mono audio path.
AAC_SAMPLE_RATE = 16000
AAC_FRAME_SAMPLES = 1024
FRAME_DURATION = AAC_FRAME_SAMPLES / AAC_SAMPLE_RATE  # 0.064s

# Max size for files passed to talk_audio_file, to bound executor memory.
MAX_AAC_FILE_BYTES = 10 * 1024 * 1024  # 10 MB

# Platforms
PLATFORMS: list[Platform] = [Platform.CAMERA]
