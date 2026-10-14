"""Async Gmail API client that pulls .xml attachments from new messages.

Read-only (gmail.readonly scope): mail is never modified. The client works on a
``get_json(url, params) -> (status, body)`` callable so this module has no Home
Assistant imports at load time; :func:`session_get_json` builds that callable
from Home Assistant's ``OAuth2Session`` (aiohttp, token refresh).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

API = "https://gmail.googleapis.com/gmail/v1/users/me"
QUERY = "has:attachment filename:xml"
PAGE_SIZE = 500
MAX_PAGES = 40  # 20 000 messages: a safety stop, not a real limit
TIMEOUT = 30

type GetJson = Callable[[str, dict[str, str] | None], Awaitable[tuple[int, Any]]]


class GmailError(Exception):
    """Gmail API or network failure (retry next poll)."""


class GmailAuthError(GmailError):
    """Sign-in expired, revoked or refused (needs reauth)."""


@dataclass
class GmailFetchResult:
    """What one poll of the mailbox found."""

    # (message id, attachment bytes) for every .xml attachment found
    attachments: list[tuple[str, bytes]] = field(default_factory=list)
    # every new message id fully looked at in this poll (with or without XML)
    processed_ids: list[str] = field(default_factory=list)


def _b64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _xml_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every MIME part (nested ones included) whose filename ends in .xml."""
    found = []
    stack = [payload]
    while stack:
        part = stack.pop(0)
        if not isinstance(part, dict):
            continue
        stack.extend(part.get("parts") or [])
        filename = str(part.get("filename") or "").strip().lower()
        if filename.endswith(".xml"):
            found.append(part)
    return found


class GmailClient:
    """Lists messages with .xml attachments and downloads those attachments."""

    def __init__(self, get_json: GetJson) -> None:
        """Wrap a ``get_json`` callable (see :func:`session_get_json`)."""
        self._get_json = get_json

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        status, body = await self._get_json(f"{API}/{path}", params)
        if status in (401, 403):
            raise GmailAuthError(f"Gmail refused the request (HTTP {status})")
        if not 200 <= status < 300:
            raise GmailError(f"Gmail returned HTTP {status}")
        if not isinstance(body, dict):
            raise GmailError("Gmail returned an unexpected response")
        return body

    async def list_message_ids(self) -> list[str]:
        """Ids of every message matching the query, newest first."""
        ids: list[str] = []
        token: str | None = None
        for _ in range(MAX_PAGES):
            params = {"q": QUERY, "maxResults": str(PAGE_SIZE)}
            if token:
                params["pageToken"] = token
            body = await self._get("messages", params)
            ids.extend(
                m["id"] for m in body.get("messages") or [] if isinstance(m, dict) and m.get("id")
            )
            token = body.get("nextPageToken")
            if not token:
                break
        else:
            _LOGGER.warning("Gmail listing stopped after %d pages", MAX_PAGES)
        return ids

    async def _message_xml(self, message_id: str) -> list[bytes]:
        message = await self._get(f"messages/{message_id}", {"format": "full"})
        found = []
        for part in _xml_parts(message.get("payload") or {}):
            body = part.get("body") or {}
            data = body.get("data")
            if not data and body.get("attachmentId"):
                attachment = await self._get(
                    f"messages/{message_id}/attachments/{body['attachmentId']}"
                )
                data = attachment.get("data")
            if not data:
                continue
            try:
                payload = _b64url(str(data))
            except (binascii.Error, ValueError) as err:
                _LOGGER.warning("Skipping undecodable attachment in %s: %s", message_id, err)
                continue
            if payload:
                found.append(payload)
        return found

    async def fetch_new_xml(self, known_ids: Collection[str]) -> GmailFetchResult:
        """Fetch .xml attachments from messages not in ``known_ids``.

        Each message is fetched once: its id goes into ``processed_ids`` once
        looked at. If a fetch fails part way, the messages processed so far are
        returned; GmailError is raised only when nothing was processed.
        GmailAuthError is always raised.
        """
        known = set(known_ids)
        new_ids = [i for i in reversed(await self.list_message_ids()) if i not in known]
        result = GmailFetchResult()
        for message_id in new_ids:
            try:
                payloads = await self._message_xml(message_id)
            except GmailAuthError:
                raise
            except GmailError as err:
                if not result.processed_ids:
                    raise
                _LOGGER.warning(
                    "Gmail fetch stopped at message %s (%s); keeping %d processed",
                    message_id,
                    err,
                    len(result.processed_ids),
                )
                break
            result.attachments.extend((message_id, p) for p in payloads)
            result.processed_ids.append(message_id)
        return result


def session_get_json(session: Any) -> GetJson:
    """Build ``get_json`` over a Home Assistant ``OAuth2Session``.

    Token refresh refused (revoked or expired sign-in) -> GmailAuthError; other
    token, network and timeout failures -> GmailError.
    """
    # Imported here so the module loads without aiohttp / Home Assistant (unit tests).
    from aiohttp import ClientError

    from homeassistant.exceptions import OAuth2TokenRequestReauthError

    async def get_json(url: str, params: dict[str, str] | None) -> tuple[int, Any]:
        try:
            async with asyncio.timeout(TIMEOUT):
                # Refreshes the access token when it is about to expire.
                resp = await session.async_request("get", url, params=params)
                try:
                    status = resp.status
                    body = await resp.json(content_type=None) if status < 300 else None
                finally:
                    resp.release()
        except OAuth2TokenRequestReauthError as err:
            raise GmailAuthError("Google sign-in expired or was revoked") from err
        except (ClientError, TimeoutError, ValueError) as err:
            # Token request failures without a 4xx are ClientErrors too.
            raise GmailError(f"Gmail unavailable: {err}") from err
        return status, body

    return get_json
