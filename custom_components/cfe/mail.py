"""Blocking IMAP client that pulls .xml attachments from new messages.

No Home Assistant imports: the coordinator runs these functions in an executor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import email
from email import policy
import email.header
import imaplib
import logging
import ssl

_LOGGER = logging.getLogger(__name__)

TIMEOUT = 30


class MailError(Exception):
    """IMAP connection or protocol failure (retry next poll)."""


class MailAuthError(MailError):
    """Login rejected (needs reauth)."""


@dataclass
class FetchResult:
    """What one poll of the mailbox found."""

    uidvalidity: int | None
    last_uid: int
    # (message UID, attachment bytes) for every .xml attachment found
    attachments: list[tuple[int, bytes]] = field(default_factory=list)
    # every UID looked at in this poll (with or without attachments)
    seen_uids: list[int] = field(default_factory=list)


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _connect(host: str, port: int, user: str, password: str) -> imaplib.IMAP4_SSL:
    try:
        conn = imaplib.IMAP4_SSL(
            host, port, ssl_context=ssl.create_default_context(), timeout=TIMEOUT
        )
    except (OSError, imaplib.IMAP4.error) as err:
        raise MailError(f"Cannot connect to {host}:{port}: {err}") from err
    try:
        conn.login(user, password)
    except imaplib.IMAP4.error as err:
        _safe_logout(conn)
        # Never include the password; the server message is safe to show.
        raise MailAuthError(f"Login failed for {user}") from err
    except OSError as err:
        _safe_logout(conn)
        raise MailError(f"Connection lost during login: {err}") from err
    return conn


def _safe_logout(conn: imaplib.IMAP4) -> None:
    try:
        conn.logout()
    except (OSError, imaplib.IMAP4.error):
        pass


def _select(conn: imaplib.IMAP4, folder: str) -> int | None:
    typ, _ = conn.select(_quote(folder), readonly=True)
    if typ != "OK":
        raise MailError(f"Cannot open folder {folder!r}")
    _, values = conn.response("UIDVALIDITY")
    if values and values[0]:
        try:
            return int(values[0])
        except (TypeError, ValueError):
            return None
    return None


def check_login(host: str, port: int, user: str, password: str, folder: str) -> None:
    """Log in and open the folder; raise MailAuthError / MailError on failure."""
    conn = _connect(host, port, user, password)
    try:
        _select(conn, folder)
    except (OSError, imaplib.IMAP4.error) as err:
        raise MailError(str(err)) from err
    finally:
        _safe_logout(conn)


def _xml_attachments(raw: bytes) -> list[bytes]:
    """Every attachment whose filename ends in .xml (forwarded mail included)."""
    msg = email.message_from_bytes(raw, policy=policy.compat32)
    found = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not filename:
            continue
        filename = str(email.header.make_header(email.header.decode_header(filename)))
        if not filename.strip().lower().endswith(".xml"):
            continue
        payload = part.get_payload(decode=True)
        if payload:
            found.append(payload)
    return found


def fetch_new_xml(
    host: str,
    port: int,
    user: str,
    password: str,
    folder: str,
    uidvalidity: int | None,
    last_uid: int,
) -> FetchResult:
    """Fetch .xml attachments from messages newer than ``last_uid``.

    If the folder's UIDVALIDITY changed, every message is scanned again
    (bills are de-duplicated by folio later, so this is safe).
    """
    conn = _connect(host, port, user, password)
    try:
        current_validity = _select(conn, folder)
        if uidvalidity is not None and current_validity != uidvalidity:
            _LOGGER.info("IMAP UIDVALIDITY changed; rescanning %s", folder)
            last_uid = 0

        typ, data = conn.uid("SEARCH", None, f"UID {last_uid + 1}:*")
        if typ != "OK":
            raise MailError("UID SEARCH failed")
        uids = sorted(
            int(u) for u in (data[0] or b"").split() if u.isdigit() and int(u) > last_uid
        )

        result = FetchResult(uidvalidity=current_validity, last_uid=last_uid)
        for uid in uids:
            typ, fetched = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if typ != "OK":
                raise MailError(f"FETCH of UID {uid} failed")
            raw = next(
                (item[1] for item in fetched or [] if isinstance(item, tuple)), None
            )
            if raw is not None:
                for payload in _xml_attachments(raw):
                    result.attachments.append((uid, payload))
            result.seen_uids.append(uid)
            result.last_uid = uid
        return result
    except (OSError, imaplib.IMAP4.error) as err:
        raise MailError(str(err)) from err
    finally:
        _safe_logout(conn)
