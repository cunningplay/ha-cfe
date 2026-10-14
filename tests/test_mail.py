"""Tests for the IMAP client against a fake server."""

from __future__ import annotations

from email.message import EmailMessage
import imaplib

import pytest

from conftest import cfe_mail


def _message(xml: bytes, filename: str = "AA-1.xml") -> bytes:
    msg = EmailMessage()
    msg["Subject"] = "Recibo CFE"
    msg.set_content("adjunto")
    msg.add_attachment(xml, maintype="application", subtype="xml", filename=filename)
    return msg.as_bytes()


POISON = (
    b"Subject: x\r\nMIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
    b"--b\r\nContent-Type: application/xml\r\n"
    b"Content-Disposition: attachment; filename=\"=?x-no-such-charset?q?a.xml?=\"\r\n\r\n"
    b"<x/>\r\n--b--\r\n"
)


class FakeIMAP:
    """Just enough of imaplib.IMAP4_SSL for fetch_new_xml."""

    instances: list[FakeIMAP] = []

    def __init__(self, host, port, ssl_context=None, timeout=None):
        self.messages: dict[int, bytes] = dict(FakeIMAP.messages)
        self.uidvalidity = FakeIMAP.uidvalidity
        self.gmail = FakeIMAP.gmail
        self.login_error = FakeIMAP.login_error
        self.fail_fetch_uid = FakeIMAP.fail_fetch_uid
        self.fetched: list[int] = []
        self.searches: list[tuple] = []
        FakeIMAP.instances.append(self)

    def login(self, user, password):
        if self.login_error:
            raise self.login_error
        return "OK", [b"logged in"]

    def capability(self):
        caps = b"IMAP4rev1 IDLE" + (b" X-GM-EXT-1" if self.gmail else b"")
        return "OK", [caps]

    def select(self, mailbox, readonly=False):
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [str(self.uidvalidity).encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            self.searches.append(args)
            low = int(args[1].split()[1].split(":")[0])
            uids = [u for u in sorted(self.messages) if u >= low]
            if not uids and self.messages:
                uids = [max(self.messages)]  # RFC 3501: "N:*" includes the newest
            return "OK", [b" ".join(str(u).encode() for u in uids)]
        if command == "FETCH":
            uid = int(args[0])
            if uid == self.fail_fetch_uid:
                raise imaplib.IMAP4.abort("connection dropped")
            self.fetched.append(uid)
            return "OK", [(f"{uid} (UID {uid} BODY[] {{1}}".encode(), self.messages[uid]), b")"]
        raise AssertionError(command)

    def logout(self):
        return "BYE", [b""]


@pytest.fixture(autouse=True)
def fake_imap(monkeypatch):
    FakeIMAP.instances = []
    FakeIMAP.messages = {}
    FakeIMAP.uidvalidity = 7
    FakeIMAP.gmail = False
    FakeIMAP.login_error = None
    FakeIMAP.fail_fetch_uid = None
    monkeypatch.setattr(cfe_mail.imaplib, "IMAP4_SSL", FakeIMAP)
    return FakeIMAP


def _fetch(uidvalidity, last_uid):
    return cfe_mail.fetch_new_xml("h", 993, "u", "p", "INBOX", uidvalidity, last_uid)


def test_new_messages(fake_imap, bill_xml) -> None:
    fake_imap.messages = {3: _message(bill_xml), 4: b"Subject: hi\r\n\r\nno attachment"}
    result = _fetch(None, 0)
    assert result.uidvalidity == 7
    assert result.last_uid == 4
    assert result.seen_uids == [3, 4]
    assert result.attachments == [(3, "xml", bill_xml)]


def test_n_star_returns_last_uid(fake_imap, bill_xml) -> None:
    fake_imap.messages = {12: _message(bill_xml)}
    result = _fetch(7, 12)
    assert fake_imap.instances[0].fetched == []
    assert result.last_uid == 12
    assert result.attachments == []


def test_uidvalidity_reset_rescans(fake_imap, bill_xml) -> None:
    fake_imap.messages = {1: _message(bill_xml), 2: _message(bill_xml)}
    fake_imap.uidvalidity = 99
    result = _fetch(7, 50)
    assert result.uidvalidity == 99
    assert fake_imap.instances[0].fetched == [1, 2]
    assert result.last_uid == 2


def test_gmail_search_is_narrowed(fake_imap) -> None:
    fake_imap.gmail = True
    _fetch(None, 5)
    assert fake_imap.instances[0].searches == [
        (None, "UID 6:*", "X-GM-RAW", '"has:attachment (filename:xml OR filename:pdf)"')
    ]


def test_plain_search(fake_imap) -> None:
    _fetch(None, 5)
    assert fake_imap.instances[0].searches == [(None, "UID 6:*")]


def test_poison_message_is_skipped(fake_imap, bill_xml) -> None:
    fake_imap.messages = {5: POISON, 6: _message(bill_xml)}
    result = _fetch(7, 4)
    assert result.seen_uids == [5, 6]
    assert result.last_uid == 6
    assert result.attachments == [(6, "xml", bill_xml)]


def test_partial_progress_kept(fake_imap, bill_xml) -> None:
    fake_imap.messages = {5: _message(bill_xml), 6: _message(bill_xml)}
    fake_imap.fail_fetch_uid = 6
    result = _fetch(7, 4)
    assert result.last_uid == 5
    assert result.attachments == [(5, "xml", bill_xml)]


def test_fetch_failure_with_nothing_processed(fake_imap, bill_xml) -> None:
    fake_imap.messages = {5: _message(bill_xml)}
    fake_imap.fail_fetch_uid = 5
    with pytest.raises(cfe_mail.MailError):
        _fetch(7, 4)


def test_login_abort_is_not_auth_error(fake_imap) -> None:
    fake_imap.login_error = imaplib.IMAP4.abort("socket closed")
    with pytest.raises(cfe_mail.MailError) as err:
        _fetch(None, 0)
    assert not isinstance(err.value, cfe_mail.MailAuthError)


def test_login_rejected_is_auth_error(fake_imap) -> None:
    fake_imap.login_error = imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
    with pytest.raises(cfe_mail.MailAuthError):
        _fetch(None, 0)


def test_pdf_and_xml_attachments(fake_imap, bill_xml) -> None:
    msg = EmailMessage()
    msg["Subject"] = "Recibo CFE"
    msg.set_content("adjunto")
    msg.add_attachment(b"%PDF-1.7", maintype="application", subtype="pdf", filename="Recibo.PDF")
    msg.add_attachment(bill_xml, maintype="application", subtype="xml", filename="AA-1.xml")
    msg.add_attachment(b"png", maintype="image", subtype="png", filename="logo.png")
    fake_imap.messages = {3: msg.as_bytes()}
    result = _fetch(None, 0)
    assert result.attachments == [(3, "pdf", b"%PDF-1.7"), (3, "xml", bill_xml)]
