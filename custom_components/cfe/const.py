"""Constants for the CFE bill integration."""

from __future__ import annotations

DOMAIN = "cfe"

CONF_FOLDER = "folder"
CONF_RPU = "rpu"
CONF_SCAN_INTERVAL = "scan_interval"  # hours

DEFAULT_HOST = "imap.gmail.com"
DEFAULT_PORT = 993
DEFAULT_FOLDER = "INBOX"
DEFAULT_SCAN_INTERVAL = 6  # hours
MIN_SCAN_INTERVAL = 1
MAX_SCAN_INTERVAL = 168

CURRENCY = "MXN"
MANUFACTURER = "Comisión Federal de Electricidad"

STORAGE_VERSION = 1

SERVICE_REFRESH = "refresh"
