"""Constants for the CFE bill integration."""

from __future__ import annotations

DOMAIN = "cfe"

CONF_AUTH_TYPE = "auth_type"
AUTH_IMAP = "imap"  # default: entries from v0.1.0 have no auth_type
AUTH_GOOGLE = "google"

CONF_FOLDER = "folder"
CONF_RPU = "rpu"
CONF_SCAN_INTERVAL = "scan_interval"  # hours

DEFAULT_HOST = "imap.gmail.com"
DEFAULT_PORT = 993
DEFAULT_FOLDER = "INBOX"
DEFAULT_SCAN_INTERVAL = 6  # hours
MIN_SCAN_INTERVAL = 1
MAX_SCAN_INTERVAL = 168

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

CURRENCY = "MXN"
MANUFACTURER = "Comisión Federal de Electricidad"

STORAGE_VERSION = 1

SERVICE_REFRESH = "refresh"
