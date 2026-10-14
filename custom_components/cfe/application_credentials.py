"""Application credentials (Google OAuth client) for the Gmail sign-in."""

from __future__ import annotations

from homeassistant.components.application_credentials import AuthorizationServer
from homeassistant.core import HomeAssistant

from .const import GOOGLE_AUTHORIZE_URL, GOOGLE_TOKEN_URL


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    """Google's authorization and token servers."""
    return AuthorizationServer(GOOGLE_AUTHORIZE_URL, GOOGLE_TOKEN_URL)


async def async_get_description_placeholders(hass: HomeAssistant) -> dict[str, str]:
    """Links shown in the add-credentials dialog."""
    return {
        "console_url": "https://console.cloud.google.com/",
        "gmail_api_url": "https://console.cloud.google.com/apis/library/gmail.googleapis.com",
        "oauth_consent_url": "https://console.cloud.google.com/apis/credentials/consent",
        "oauth_creds_url": "https://console.cloud.google.com/apis/credentials",
        "more_info_url": "https://github.com/cunningplay/ha-cfe#google-account-sign-in",
        "redirect_url": "https://my.home-assistant.io/redirect/oauth",
    }
