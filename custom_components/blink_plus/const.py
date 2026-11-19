"""Constants for Blink."""

from homeassistant.const import Platform

DOMAIN = "blink_plus"
# Legacy globally-shared OAuth identifier every install used to send. Blink's
# authorize endpoint now rejects it (HTTP 406); kept only to detect and drop
# it on migration/reauth so blinkpy can mint a per-install UUID instead.
LEGACY_HARDWARE_ID = "Home Assistant"

CONF_MIGRATE = "migrate"
CONF_CAMERA = "camera"
CONF_ALARM_CONTROL_PANEL = "alarm_control_panel"
DEFAULT_BRAND = "Blink"
DEFAULT_ATTRIBUTION = "Data provided by immedia-semi.com"
DEFAULT_SCAN_INTERVAL = 300
DEFAULT_OFFSET = 1
SIGNAL_UPDATE_BLINK = "blink_update"

TYPE_CAMERA_ARMED = "motion_enabled"
TYPE_MOTION_DETECTED = "motion_detected"
TYPE_TEMPERATURE = "temperature"
TYPE_BATTERY = "battery"
TYPE_WIFI_STRENGTH = "wifi_strength"


PLATFORMS = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.SENSOR,
    Platform.SWITCH,
]

# Live view options
CONF_STREAM_HOST = "stream_host"
CONF_STREAM_BASE_PORT = "stream_base_port"
CONF_STREAM_MAX_MINUTES = "stream_max_minutes"
CONF_STREAM_LINGER = "stream_linger"
CONF_STREAM_TOKEN_MODE = "stream_token_mode"
DEFAULT_STREAM_HOST = "127.0.0.1"
DEFAULT_STREAM_BASE_PORT = 0  # 0 = random free port per camera
DEFAULT_STREAM_MAX_MINUTES = 5
DEFAULT_STREAM_LINGER = 5
DEFAULT_STREAM_TOKEN_MODE = "auto"
CONF_USB_MOTION_MINUTES = "usb_motion_minutes"
DEFAULT_USB_MOTION_MINUTES = 0  # 0 = never read the USB clip list (app keeps working)

# Clip archive (copy USB clips to a folder, e.g. a PC share mounted under /media)
CONF_ARCHIVE_FOLDER = "archive_folder"
CONF_ARCHIVE_INTERVAL = "archive_interval_minutes"
CONF_ARCHIVE_HOURS = "archive_lookback_hours"
DEFAULT_ARCHIVE_FOLDER = ""  # empty = archive off
DEFAULT_ARCHIVE_INTERVAL = 60  # minutes, 0 = only via button/action
DEFAULT_ARCHIVE_HOURS = 24
# Delete USB clips older than N days once they are safely archived (0 = off)
CONF_USB_DELETE_DAYS = "usb_delete_after_days"
DEFAULT_USB_DELETE_DAYS = 0
