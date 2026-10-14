"""Async Gmail API client that pulls .xml and .pdf attachments from new messages.

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
QUERY = "has:attachment (filename:xml OR filename:pdf)"
PAGE_SIZE = 500
MAX_PAGES = 40  # 20 000 messages: a safety stop, not a real limit
MAX_PER_POLL = 50  # new messages read per poll; the rest wait for the next one
TIMEOUT = 30

# Google error reasons (error.errors[].reason, error.status, error.details[].reason)
_AUTH_REASONS = {
    "authError",
    "forbidden",
    "insufficientPermissions",
    "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
    "UNAUTHENTICATED",
}
_QUOTA_REASONS = {
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "dailyLimitExceeded",
    "quotaExceeded",
    "RATE_LIMIT_EXCEEDED",
    "RESOURCE_EXHAUSTED",
}
_DISABLED_REASONS = {"accessNotConfigured", "SERVICE_DISABLED"}
_KINDS = {".xml": "xml", ".pdf": "pdf"}

type GetJson = Callable[[str, dict[str, str] | None], Awaitable[tuple[int, Any]]]


class GmailError(Exception):
    """Gmail API or network failure (retry next poll)."""


class GmailAuthError(GmailError):
    """Sign-in expired, revoked or refused (needs reauth)."""


class GmailApiDisabledError(GmailError):
    """The Gmail API is not enabled in the Google Cloud project."""


class GmailSkipError(GmailError):
    """One message or attachment cannot be read (400/404, bad body): skip it."""


@dataclass
class GmailFetchResult:
    """What one poll of the mailbox found."""

    # (message id, "xml" or "pdf", attachment bytes) for every bill attachment
    attachments: list[tuple[str, str, bytes]] = field(default_factory=list)
    # every new message id fully looked at in this poll (with or without a bill)
    processed_ids: list[str] = field(default_factory=list)
    # more new messages are waiting (per-poll cap or a stop part way)
    more: bool = False


def _b64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _reasons(body: Any) -> set[str]:
    """Reasons from a Google API error body."""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return set()
    found = {str(error["status"])} if error.get("status") else set()
    for key in ("errors", "details"):
        for item in error.get(key) or []:
            if isinstance(item, dict) and item.get("reason"):
                found.add(str(item["reason"]))
    return found


def _bill_parts(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """(kind, part) for every MIME part (nested too) named *.xml or *.pdf."""
    found = []
    stack = [payload]
    while stack:
        part = stack.pop(0)
        if not isinstance(part, dict):
            continue
        stack.extend(part.get("parts") or [])
        filename = str(part.get("filename") or "").strip().lower()
        kind = next((k for ext, k in _KINDS.items() if filename.endswith(ext)), None)
        if kind is not None:
            found.append((kind, part))
    return found


class GmailClient:
    """Lists messages with .xml/.pdf attachments and downloads those attachments."""

    def __init__(self, get_json: GetJson) -> None:
        """Wrap a ``get_json`` callable (see :func:`session_get_json`)."""
        self._get_json = get_json

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        status, body = await self._get_json(f"{API}/{path}", params)
        if 200 <= status < 300:
            if not isinstance(body, dict):
                raise GmailSkipError("Gmail returned an unexpected response")
            return body
        reasons = _reasons(body)
        if status == 401:
            raise GmailAuthError("Gmail rejected the sign-in (HTTP 401)")
        if reasons & _DISABLED_REASONS:
            _LOGGER.error(
                "The Gmail API is not enabled in the Google Cloud project of the "
                "OAuth client; enable it in Google Cloud Console (APIs & Services)"
            )
            raise GmailApiDisabledError(
                "The Gmail API must be enabled in the Google Cloud project"
            )
        if status == 403 and reasons & _AUTH_REASONS and not reasons & _QUOTA_REASONS:
            raise GmailAuthError(
                f"Gmail refused access (HTTP 403: {', '.join(sorted(reasons))})"
            )
        if status in (400, 404):
            raise GmailSkipError(f"Gmail returned HTTP {status}")
        detail = f": {', '.join(sorted(reasons))}" if reasons else ""
        raise GmailError(f"Gmail returned HTTP {status}{detail}")

    async def get_email(self) -> str:
        """The signed-in account's email address."""
        body = await self._get("profile")
        if not body.get("emailAddress"):
            raise GmailError("Gmail profile has no email address")
        return str(body["emailAddress"])

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

    async def _message_bills(self, message_id: str) -> list[tuple[str, bytes]]:
        message = await self._get(f"messages/{message_id}", {"format": "full"})
        found = []
        for kind, part in _bill_parts(message.get("payload") or {}):
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
                found.append((kind, payload))
        return found

    async def fetch_new_xml(
        self, known_ids: Collection[str], limit: int | None = None
    ) -> GmailFetchResult:
        """Fetch .xml/.pdf attachments from up to ``limit`` messages not in ``known_ids``.

        Oldest first. Each message is fetched once: its id goes into
        ``processed_ids`` once looked at; a message or attachment Gmail cannot
        serve (400/404, bad body) is recorded as processed with nothing found.
        If a fetch fails part way, the messages processed so far are returned
        (``more`` set); GmailError is raised only when nothing was processed.
        GmailAuthError and GmailApiDisabledError are always raised.
        """
        limit = MAX_PER_POLL if limit is None else limit
        known = set(known_ids)
        new_ids = [i for i in reversed(await self.list_message_ids()) if i not in known]
        result = GmailFetchResult(more=len(new_ids) > limit)
        for message_id in new_ids[:limit]:
            try:
                payloads = await self._message_bills(message_id)
            except GmailSkipError as err:
                _LOGGER.warning("Skipping Gmail message %s: %s", message_id, err)
                payloads = []
            except (GmailAuthError, GmailApiDisabledError):
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
                result.more = True
                break
            result.attachments.extend((message_id, k, p) for k, p in payloads)
            result.processed_ids.append(message_id)
        return result


def session_get_json(session: Any) -> GetJson:
    """Build ``get_json`` over a Home Assistant ``OAuth2Session`` (refreshes the token)."""
    return request_get_json(session.async_request)


def request_get_json(request: Callable[..., Awaitable[Any]]) -> GetJson:
    """Build ``get_json`` over ``request(method, url, params=...)`` (aiohttp).

    Token refresh refused (revoked or expired sign-in) -> GmailAuthError; other
    token, network and timeout failures -> GmailError. Error bodies are kept so
    the reason can be read.
    """
    # Imported here so the module loads without aiohttp / Home Assistant (unit tests).
    from aiohttp import ClientError

    from homeassistant.exceptions import OAuth2TokenRequestReauthError

    async def get_json(url: str, params: dict[str, str] | None) -> tuple[int, Any]:
        try:
            async with asyncio.timeout(TIMEOUT):
                resp = await request("get", url, params=params)
                try:
                    status = resp.status
                    try:
                        body = await resp.json(content_type=None)
                    except ValueError:
                        if status < 300:
                            raise
                        body = None
                finally:
                    resp.release()
        except OAuth2TokenRequestReauthError as err:
            raise GmailAuthError("Google sign-in expired or was revoked") from err
        except (ClientError, TimeoutError, ValueError) as err:
            # Token request failures without a 4xx are ClientErrors too.
            raise GmailError(f"Gmail unavailable: {err}") from err
        return status, body

    return get_json
