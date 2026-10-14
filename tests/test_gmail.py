"""Tests for the Gmail API client against a fake API."""

from __future__ import annotations

import asyncio
import base64

import pytest

from conftest import cfe_gmail

API = cfe_gmail.API


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class FakeGmail:
    """Answers get_json(url, params) like the Gmail REST API."""

    def __init__(self) -> None:
        self.messages: dict[str, dict] = {}  # id -> message (format=full)
        self.attachments: dict[tuple[str, str], bytes] = {}
        self.order: list[str] = []  # newest first, as Gmail lists them
        self.page_size = 500
        self.fail: dict[str, int | tuple | Exception] = {}  # url suffix -> status, (status, body) or error
        self.calls: list[tuple[str, dict | None]] = []

    def add(self, mid: str, parts: list[dict]) -> None:
        self.messages[mid] = {"id": mid, "payload": {"mimeType": "multipart/mixed", "parts": parts}}
        self.order.insert(0, mid)

    async def __call__(self, url: str, params: dict | None):
        self.calls.append((url, params))
        path = url.removeprefix(API + "/")
        if path in self.fail:
            err = self.fail[path]
            if isinstance(err, Exception):
                raise err
            if isinstance(err, tuple):
                return err
            return err, None
        if path == "messages":
            assert params["q"] == "has:attachment (filename:xml OR filename:pdf)"
            start = int(params.get("pageToken", 0))
            page = self.order[start : start + self.page_size]
            body = {"messages": [{"id": i, "threadId": i} for i in page]}
            if start + self.page_size < len(self.order):
                body["nextPageToken"] = str(start + self.page_size)
            return 200, body
        bits = path.split("/")
        if len(bits) == 2:
            assert params == {"format": "full"}
            return 200, self.messages[bits[1]]
        if len(bits) == 4 and bits[2] == "attachments":
            data = self.attachments[(bits[1], bits[3])]
            return 200, {"size": len(data), "data": b64(data)}
        raise AssertionError(url)


def xml_part(att_id: str, filename: str = "AA-1.xml") -> dict:
    return {"mimeType": "application/xml", "filename": filename, "body": {"attachmentId": att_id, "size": 1}}


@pytest.fixture
def fake() -> FakeGmail:
    return FakeGmail()


def client(fake):
    return cfe_gmail.GmailClient(fake)


def fetch(fake, known, **kwargs):
    return asyncio.run(client(fake).fetch_new_bills(known, **kwargs))


def google_error(status, reason, api_status="PERMISSION_DENIED"):
    return status, {"error": {"code": status, "message": "x", "status": api_status,
                              "errors": [{"reason": reason, "domain": "global"}]}}


def downloads(fake) -> list[str]:
    return [u for u, _ in fake.calls if "/attachments/" in u]


def test_new_messages(fake, bill_xml) -> None:
    fake.add("m1", [{"mimeType": "text/plain", "filename": "", "body": {"size": 3}}, xml_part("a1")])
    fake.attachments[("m1", "a1")] = bill_xml
    # nested (forwarded) message with an .XML part and a PDF
    fake.add("m2", [{"mimeType": "message/rfc822", "filename": "", "parts": [
        {"mimeType": "application/pdf", "filename": "recibo.pdf", "body": {"attachmentId": "p"}},
        xml_part("a2", " OTHER.XML "),
    ]}])
    fake.attachments[("m2", "a2")] = b"<other/>"
    fake.attachments[("m2", "p")] = b"%PDF-1.4"
    result = fetch(fake, [])
    assert result.processed_ids == ["m1", "m2"]  # oldest first
    # without xml_is_bill every PDF is downloaded; XML parts come first
    assert result.attachments == [("m1", "xml", bill_xml), ("m2", "xml", b"<other/>"), ("m2", "pdf", b"%PDF-1.4")]
    assert downloads(fake) == [f"{API}/messages/m1/attachments/a1", f"{API}/messages/m2/attachments/a2",
                               f"{API}/messages/m2/attachments/p"]


def test_known_ids_not_fetched(fake, bill_xml) -> None:
    fake.add("m1", [xml_part("a1")])
    fake.attachments[("m1", "a1")] = bill_xml
    result = fetch(fake, ["m1"])
    assert result.processed_ids == [] and result.attachments == []
    assert [u for u, _ in fake.calls] == [f"{API}/messages"]


def test_inline_data_and_padding(fake) -> None:
    fake.add("m1", [{"filename": "a.xml", "body": {"data": b64(b"<x/>12")}}])
    result = fetch(fake, [])
    assert result.attachments == [("m1", "xml", b"<x/>12")]
    assert downloads(fake) == []


def test_pagination(fake) -> None:
    fake.page_size = 2
    for i in range(5):
        fake.add(f"m{i}", [])
    result = fetch(fake, ["m0"])
    assert result.processed_ids == ["m1", "m2", "m3", "m4"]
    tokens = [p.get("pageToken") for u, p in fake.calls if u == f"{API}/messages"]
    assert tokens == [None, "2", "4"]


@pytest.mark.parametrize("failure", [
    401,
    google_error(401, "authError", "UNAUTHENTICATED"),
    google_error(403, "insufficientPermissions"),
    (403, {"error": {"code": 403, "status": "PERMISSION_DENIED",
                     "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}),
])
def test_auth_errors(fake, failure) -> None:
    fake.fail["messages"] = failure
    with pytest.raises(cfe_gmail.GmailAuthError):
        fetch(fake, [])


@pytest.mark.parametrize("failure", [
    google_error(403, "rateLimitExceeded"),
    google_error(403, "userRateLimitExceeded"),
    google_error(403, "dailyLimitExceeded"),
    google_error(429, "rateLimitExceeded", "RESOURCE_EXHAUSTED"),
    429,
    403,  # no body: not known to be an authorization problem
])
def test_quota_errors_are_not_auth(fake, failure) -> None:
    fake.fail["messages"] = failure
    with pytest.raises(cfe_gmail.GmailError) as err:
        fetch(fake, [])
    assert not isinstance(err.value, (cfe_gmail.GmailAuthError, cfe_gmail.GmailApiDisabledError))


@pytest.mark.parametrize("failure", [
    google_error(403, "accessNotConfigured"),
    (403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "details": [{"reason": "SERVICE_DISABLED"}]}}),
])
def test_api_disabled(fake, failure, caplog) -> None:
    fake.fail["messages"] = failure
    with pytest.raises(cfe_gmail.GmailApiDisabledError) as err:
        fetch(fake, [])
    assert not isinstance(err.value, cfe_gmail.GmailAuthError)
    assert "Gmail API must be enabled" in str(err.value)
    assert any(r.levelname == "ERROR" for r in caplog.records)


@pytest.mark.parametrize("path, failure", [
    ("messages/m1", 404),
    ("messages/m1", 400),
    ("messages/m1/attachments/a1", 404),
    ("messages/m1", (200, ["not", "a", "dict"])),
])
def test_unreadable_message_is_recorded(fake, bill_xml, path, failure) -> None:
    fake.add("m1", [xml_part("a1")])
    fake.add("m2", [xml_part("a2")])
    fake.attachments[("m2", "a2")] = bill_xml
    fake.fail[path] = failure
    result = fetch(fake, [])
    assert result.processed_ids == ["m1", "m2"]
    assert result.attachments == [("m2", "xml", bill_xml)]
    assert not result.more


def test_list_404_raises(fake) -> None:
    fake.fail["messages"] = 404
    with pytest.raises(cfe_gmail.GmailError):
        fetch(fake, [])


def test_per_poll_cap(fake) -> None:
    for i in range(5):
        fake.add(f"m{i}", [])
    first = fetch(fake, [], limit=2)
    assert first.processed_ids == ["m0", "m1"] and first.more
    second = fetch(fake, first.processed_ids, limit=2)
    assert second.processed_ids == ["m2", "m3"] and second.more
    third = fetch(fake, first.processed_ids + second.processed_ids, limit=2)
    assert third.processed_ids == ["m4"] and not third.more
    assert cfe_gmail.MAX_PER_POLL == 50


def test_auth_error_mid_poll_is_raised(fake, bill_xml) -> None:
    fake.add("m1", [xml_part("a1")])
    fake.add("m2", [xml_part("a2")])
    fake.attachments[("m1", "a1")] = bill_xml
    fake.fail["messages/m2"] = 401
    with pytest.raises(cfe_gmail.GmailAuthError):
        fetch(fake, [])


def test_server_error(fake) -> None:
    fake.fail["messages"] = 503
    with pytest.raises(cfe_gmail.GmailError) as err:
        fetch(fake, [])
    assert not isinstance(err.value, cfe_gmail.GmailAuthError)


def test_partial_progress_kept(fake, bill_xml) -> None:
    fake.add("m1", [xml_part("a1")])
    fake.add("m2", [xml_part("a2")])
    fake.attachments[("m1", "a1")] = bill_xml
    fake.fail["messages/m2/attachments/a2"] = 500
    result = fetch(fake, [])
    assert result.processed_ids == ["m1"]
    assert result.attachments == [("m1", "xml", bill_xml)]
    assert result.more


def test_failure_with_nothing_processed(fake) -> None:
    fake.add("m1", [xml_part("a1")])
    fake.fail["messages/m1"] = cfe_gmail.GmailError("timeout")
    with pytest.raises(cfe_gmail.GmailError):
        fetch(fake, [])


def test_undecodable_attachment_skipped(fake) -> None:
    fake.add("m1", [{"filename": "a.xml", "body": {"data": "@@@="}}])
    result = fetch(fake, [])
    assert result.processed_ids == ["m1"] and result.attachments == []


def test_pdf_only_message(fake) -> None:
    fake.add("m1", [{"mimeType": "application/pdf", "filename": "Recibo.PDF", "body": {"attachmentId": "p1"}},
                    {"mimeType": "image/png", "filename": "logo.png", "body": {"attachmentId": "i1"}}])
    fake.attachments[("m1", "p1")] = b"%PDF-1.7"
    result = fetch(fake, [])
    assert result.attachments == [("m1", "pdf", b"%PDF-1.7")]
    assert downloads(fake) == [f"{API}/messages/m1/attachments/p1"]


def pdf_part(att_id: str, size: int = 9) -> dict:
    return {"mimeType": "application/pdf", "filename": "Recibo.pdf", "body": {"attachmentId": att_id, "size": size}}


def test_pdf_downloaded_only_without_xml_bill(fake, bill_xml) -> None:
    fake.add("m1", [pdf_part("p1"), xml_part("a1")])  # XML bill: its PDF is not needed
    fake.add("m2", [pdf_part("p2"), xml_part("a2")])  # XML that is not a bill: PDF wanted
    fake.add("m3", [pdf_part("p3", size=cfe_gmail.MAX_PDF_BYTES + 1), pdf_part("p4")])
    fake.attachments.update({("m1", "a1"): bill_xml, ("m2", "a2"): b"<x/>", ("m2", "p2"): b"%PDF-2",
                             ("m3", "p4"): b"%PDF-4"})
    result = asyncio.run(client(fake).fetch_new_bills([], xml_is_bill=lambda p: p == bill_xml))
    assert result.attachments == [("m1", "xml", bill_xml), ("m2", "xml", b"<x/>"), ("m2", "pdf", b"%PDF-2"),
                                  ("m3", "pdf", b"%PDF-4")]
    assert not any(u.endswith(("/p1", "/p3")) for u in downloads(fake))
    assert result.processed_ids == ["m1", "m2", "m3"]
