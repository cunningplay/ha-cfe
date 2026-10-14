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
        self.fail: dict[str, int | Exception] = {}  # url suffix -> status or error
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
            return err, None
        if path == "messages":
            assert params["q"] == "has:attachment filename:xml"
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


def fetch(fake, known):
    return asyncio.run(client(fake).fetch_new_xml(known))


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
    result = fetch(fake, [])
    assert result.processed_ids == ["m1", "m2"]  # oldest first
    assert result.attachments == [("m1", bill_xml), ("m2", b"<other/>")]
    assert downloads(fake) == [f"{API}/messages/m1/attachments/a1", f"{API}/messages/m2/attachments/a2"]


def test_known_ids_not_fetched(fake, bill_xml) -> None:
    fake.add("m1", [xml_part("a1")])
    fake.attachments[("m1", "a1")] = bill_xml
    result = fetch(fake, ["m1"])
    assert result.processed_ids == [] and result.attachments == []
    assert [u for u, _ in fake.calls] == [f"{API}/messages"]


def test_inline_data_and_padding(fake) -> None:
    fake.add("m1", [{"filename": "a.xml", "body": {"data": b64(b"<x/>12")}}])
    result = fetch(fake, [])
    assert result.attachments == [("m1", b"<x/>12")]
    assert downloads(fake) == []


def test_pagination(fake) -> None:
    fake.page_size = 2
    for i in range(5):
        fake.add(f"m{i}", [])
    result = fetch(fake, ["m0"])
    assert result.processed_ids == ["m1", "m2", "m3", "m4"]
    tokens = [p.get("pageToken") for u, p in fake.calls if u == f"{API}/messages"]
    assert tokens == [None, "2", "4"]


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors(fake, status) -> None:
    fake.fail["messages"] = status
    with pytest.raises(cfe_gmail.GmailAuthError):
        fetch(fake, [])


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
    assert result.attachments == [("m1", bill_xml)]


def test_failure_with_nothing_processed(fake) -> None:
    fake.add("m1", [xml_part("a1")])
    fake.fail["messages/m1"] = cfe_gmail.GmailError("timeout")
    with pytest.raises(cfe_gmail.GmailError):
        fetch(fake, [])


def test_undecodable_attachment_skipped(fake) -> None:
    fake.add("m1", [{"filename": "a.xml", "body": {"data": "@@@="}}])
    result = fetch(fake, [])
    assert result.processed_ids == ["m1"] and result.attachments == []
