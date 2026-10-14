import base64
from datetime import date
from pathlib import Path
import time
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.application_credentials import ClientCredential, async_import_client_credential
from homeassistant.config_entries import ConfigEntryState, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from yarl import URL

from custom_components.cfe.const import DOMAIN
from custom_components.cfe.mail import FetchResult, MailAuthError, MailError

XML = (Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "bill_anon.xml").read_bytes()
RPU = "000111222333"
DATA = {"host": "imap.gmail.com", "port": 993, "username": "a@b.c", "password": "secret", "folder": "INBOX", "rpu": RPU}
FETCH = "custom_components.cfe.coordinator.fetch_new_xml"


def entry():
    return MockConfigEntry(domain=DOMAIN, data=DATA, unique_id=RPU, title=f"CFE {RPU}")


# A later bill for the same RPU: 23 SEP - 24 NOV 26, 200 kWh, CFDI Total 500.00.
NEXT = (
    XML.replace(b'Folio="000012345678"', b'Folio="000012399999"')
    .replace(b'Fecha="2026-09-25T09:41:25"', b'Fecha="2026-11-26T09:00:00"')
    .replace(b'Total="449.28"', b'Total="500.00"')
    .replace(b"<FECDESDE>28 AGO 26</FECDESDE>", b"<FECDESDE>23 SEP 26</FECDESDE>")
    .replace(b"<FECHASTA>23 SEP 26</FECHASTA>", b"<FECHASTA>24 NOV 26</FECHASTA>")
    .replace(b"<FECLIMITE>09 OCT 26</FECLIMITE>", b"<FECLIMITE>10 DIC 26</FECLIMITE>")
    .replace(b"<FECORTE>10 OCT 26</FECORTE>", b"<FECORTE>11 DIC 26</FECORTE>")
    .replace(b"<CONSUMO_R>174</CONSUMO_R>", b"<CONSUMO_R>200</CONSUMO_R>")
    .replace(b"<CargosCreditos>1128.28</CargosCreditos>", b"<CargosCreditos>500.00</CargosCreditos>")
)


def entity_ids(hass, e):
    ent = er.async_get(hass)
    return {x.translation_key: x.entity_id for x in er.async_entries_for_config_entry(ent, e.entry_id)}


async def get_stats(hass):
    from datetime import datetime, timezone

    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()
    return await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, datetime(2026, 1, 1, tzinfo=timezone.utc), None,
        {f"cfe:{RPU}_energy", f"cfe:{RPU}_cost"}, "hour", None, {"sum", "state"})


async def imap_form(hass):
    """Start setup and pick the IMAP method (v0.2.0 adds the method menu)."""
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert r["type"] is FlowResultType.MENU and r["menu_options"] == ["google", "imap"]
    return await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "imap"})


async def test_flow(hass: HomeAssistant):
    r = await imap_form(hass)
    assert r["type"] is FlowResultType.FORM
    r2 = await hass.config_entries.flow.async_configure(r["flow_id"], {**DATA, "rpu": "123"})
    assert r2["errors"] == {"rpu": "invalid_rpu"}
    with patch("custom_components.cfe.config_flow.check_login", side_effect=MailAuthError("x")):
        r3 = await hass.config_entries.flow.async_configure(r["flow_id"], DATA)
    assert r3["errors"] == {"base": "invalid_auth"}
    with patch("custom_components.cfe.config_flow.check_login"), patch(FETCH, return_value=FetchResult(1, 5, [], [])):
        r4 = await hass.config_entries.flow.async_configure(r["flow_id"], {**DATA, "rpu": "0001 1122 2333"})
    assert r4["type"] is FlowResultType.CREATE_ENTRY
    assert r4["data"]["rpu"] == RPU
    assert r4["data"]["auth_type"] == "imap"
    await hass.async_block_till_done()


async def test_flow_errors(hass: HomeAssistant):
    with patch(FETCH, return_value=FetchResult(1, 0, [], [])):
        e = entry(); e.add_to_hass(hass)
        await hass.config_entries.async_setup(e.entry_id); await hass.async_block_till_done()
    r = await imap_form(hass)
    r2 = await hass.config_entries.flow.async_configure(r["flow_id"], DATA)
    assert r2["type"] is FlowResultType.ABORT and r2["reason"] == "already_configured"
    r = await imap_form(hass)
    with patch("custom_components.cfe.config_flow.check_login", side_effect=MailError("x")):
        r3 = await hass.config_entries.flow.async_configure(r["flow_id"], {**DATA, "rpu": "999999999999"})
    assert r3["errors"] == {"base": "cannot_connect"}


async def test_bill_flow(hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_storage):
    freezer.move_to("2026-09-30 12:00:00")
    other = XML.replace(RPU.encode(), b"999999999999").replace(b'Folio="000012345678"', b'Folio="2"')
    res = FetchResult(7, 12, [(10, XML), (11, XML), (11, other), (12, b"<bad")], [10, 11, 12])
    e = entry(); e.add_to_hass(hass)
    with patch(FETCH, return_value=res) as m:
        assert await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    assert m.call_args.args[5:] == (None, 0)
    ent = er.async_get(hass)
    ids = {x.translation_key: x.entity_id for x in er.async_entries_for_config_entry(ent, e.entry_id)}
    st = hass.states.get(ids["amount_due"])
    assert st.state == "1128.28" and st.attributes["unit_of_measurement"] == "MXN"
    assert hass.states.get(ids["due_date"]).state == "2026-10-09"
    assert hass.states.get(ids["energy"]).state == "174.0"
    assert hass.states.get(ids["tariff"]).state == "1A"
    assert hass.states.get(ids["meter_reading"]).state == "174"
    assert hass.states.get(ids["paid"]).state == "off"
    coord = e.runtime_data
    assert list(coord.data.bills) == ["000012345678"]
    await hass.services.async_call("button", "press", {"entity_id": ids["mark_paid"]}, blocking=True)
    assert hass.states.get(ids["paid"]).state == "on"
    await hass.async_block_till_done()
    freezer.tick(15)
    async_fire_time_changed(hass); await hass.async_block_till_done()
    stored = hass_storage[f"cfe.{e.entry_id}"]["data"]
    assert stored["paid"] == {"000012345678": True} and stored["last_uid"] == 12 and stored["uidvalidity"] == 7
    # refresh service with mail down: the call fails, the sensors stay available
    with patch(FETCH, side_effect=MailError("down")) as m2, pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "refresh", blocking=True)
    assert m2.call_args.args[5:] == (7, 12)
    assert hass.states.get(ids["amount_due"]).state == "1128.28"
    # refresh service success
    with patch(FETCH, return_value=FetchResult(7, 12, [], [])):
        await hass.services.async_call(DOMAIN, "refresh", blocking=True)
    # statistics: zero point at period start, then the bill at period end;
    # cost is the period charge (CFDI Total), not the amount due with deposit
    stats = await get_stats(hass)
    assert [r["sum"] for r in stats[f"cfe:{RPU}_energy"]] == [0.0, 174.0]
    assert [r["sum"] for r in stats[f"cfe:{RPU}_cost"]] == [0.0, 449.28]


async def test_restore_offline(hass: HomeAssistant, hass_storage, freezer):
    freezer.move_to("2026-10-13 12:00:00")
    e = entry(); e.add_to_hass(hass)
    import importlib
    parser = importlib.import_module("custom_components.cfe.parser")
    bill = parser.parse_bill(XML)
    hass_storage[f"cfe.{e.entry_id}"] = {"version": 1, "minor_version": 1, "key": f"cfe.{e.entry_id}",
        "data": {"bills": {bill.folio: bill.to_dict()}, "paid": {}, "uidvalidity": 7, "last_uid": 12, "initial_import_done": True}}
    with patch(FETCH, side_effect=MailError("no network")):
        assert await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    assert e.state is ConfigEntryState.LOADED
    ent = er.async_get(hass)
    ids = {x.translation_key: x.entity_id for x in er.async_entries_for_config_entry(ent, e.entry_id)}
    assert hass.states.get(ids["amount_due"]).state == "1128.28"
    assert hass.states.get(ids["paid"]).state == "off"
    assert hass.states.get(ids["period_end"]).state == "2026-09-23"


async def test_first_import_overdue_paid(hass: HomeAssistant, freezer):
    freezer.move_to("2026-10-13 12:00:00")
    e = entry(); e.add_to_hass(hass)
    with patch(FETCH, return_value=FetchResult(1, 3, [(3, XML)], [3])):
        assert await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    assert e.runtime_data.data.is_paid("000012345678")


async def test_auth_fail_reauth(hass: HomeAssistant, hass_storage):
    e = entry(); e.add_to_hass(hass)
    with patch(FETCH, side_effect=MailAuthError("bad")):
        await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)
    flow = next(f for f in flows if f["context"]["source"] == "reauth")
    with patch("custom_components.cfe.config_flow.check_login"), patch(FETCH, return_value=FetchResult(1, 0, [], [])):
        r = await hass.config_entries.flow.async_configure(flow["flow_id"], {"password": "new"})
        await hass.async_block_till_done()
    assert r["reason"] == "reauth_successful"
    assert e.data["password"] == "new"


async def test_options(hass: HomeAssistant):
    e = entry(); e.add_to_hass(hass)
    with patch(FETCH, return_value=FetchResult(1, 0, [], [])):
        await hass.config_entries.async_setup(e.entry_id); await hass.async_block_till_done()
        r = await hass.config_entries.options.async_init(e.entry_id)
        r = await hass.config_entries.options.async_configure(r["flow_id"], {"scan_interval": 12})
        await hass.async_block_till_done()
    assert e.options == {"scan_interval": 12}
    from datetime import timedelta
    assert e.runtime_data.update_interval == timedelta(hours=12)


async def test_overdue_after_initial_import_stays_unpaid(hass: HomeAssistant, hass_storage, freezer):
    freezer.move_to("2026-10-13 12:00:00")
    e = entry(); e.add_to_hass(hass)
    hass_storage[f"cfe.{e.entry_id}"] = {"version": 1, "minor_version": 1, "key": f"cfe.{e.entry_id}",
        "data": {"bills": {}, "paid": {}, "uidvalidity": 7, "last_uid": 2, "initial_import_done": True}}
    with patch(FETCH, return_value=FetchResult(7, 3, [(3, XML)], [3])):
        assert await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    assert list(e.runtime_data.data.bills) == ["000012345678"]
    assert not e.runtime_data.data.is_paid("000012345678")


async def test_two_bills_reverse_order(hass: HomeAssistant, freezer):
    freezer.move_to("2026-11-27 12:00:00")
    e = entry(); e.add_to_hass(hass)
    with patch(FETCH, return_value=FetchResult(7, 2, [(1, NEXT), (2, XML)], [1, 2])):
        assert await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    ids = entity_ids(hass, e)
    assert hass.states.get(ids["period_end"]).state == "2026-11-24"
    assert hass.states.get(ids["energy"]).state == "200.0"
    assert hass.states.get(ids["amount_due"]).state == "500.0"
    stats = await get_stats(hass)
    energy = stats[f"cfe:{RPU}_energy"]
    cost = stats[f"cfe:{RPU}_cost"]
    assert [r["sum"] for r in energy] == [0.0, 174.0, 374.0]
    assert [r["state"] for r in energy] == [0.0, 174.0, 200.0]
    assert [r["sum"] for r in cost] == [0.0, 449.28, 949.28]


async def test_remove_entry_deletes_store(hass: HomeAssistant, hass_storage):
    e = entry(); e.add_to_hass(hass)
    with patch(FETCH, return_value=FetchResult(7, 3, [(3, XML)], [3])):
        assert await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    assert f"cfe.{e.entry_id}" in hass_storage
    await hass.config_entries.async_remove(e.entry_id)
    await hass.async_block_till_done()
    assert f"cfe.{e.entry_id}" not in hass_storage


# ---------------------------------------------------------------- Google sign-in


TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
OTHER = XML.replace(RPU.encode(), b"999999999999").replace(b'Folio="000012345678"', b'Folio="2"')
KEYS = ("amount_due", "due_date", "cutoff_date", "period_start", "period_end", "bill_date", "energy",
        "daily_energy", "daily_cost", "meter_reading", "tariff", "subsidy", "real_cost", "paid")


EMAIL = "bills@example.com"


def token(expires_in=3600, access="at-1", scope=SCOPE):
    return {"access_token": access, "refresh_token": "rt-1", "token_type": "Bearer", "scope": scope,
            "expires_in": expires_in, "expires_at": time.time() + expires_in}


def google_entry(expires_in=3600, email=EMAIL):
    data = {"auth_type": "google", "auth_implementation": DOMAIN, "token": token(expires_in), "rpu": RPU}
    if email:
        data["email"] = email
    return MockConfigEntry(domain=DOMAIN, unique_id=RPU, title=f"CFE {RPU}", data=data)


def google_error(status, reason, api_status="PERMISSION_DENIED"):
    return {"status": status, "json": {"error": {"code": status, "message": "x", "status": api_status,
                                                 "errors": [{"reason": reason, "domain": "global"}]}}}


def reauth_flows(hass):
    return [f for f in hass.config_entries.flow.async_progress() if f["context"]["source"] == "reauth"]


@pytest.fixture
async def credentials(hass: HomeAssistant):
    assert await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(hass, DOMAIN, ClientCredential("client-id", "client-secret"), DOMAIN)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def mock_gmail(aioclient_mock, messages: dict[str, bytes]):
    """Gmail with one message (and one .xml attachment) per entry; listed newest first."""
    for mid, xml in messages.items():
        aioclient_mock.get(f"{GMAIL}/messages/{mid}/attachments/att-{mid}", json={"size": len(xml), "data": b64(xml)})
        aioclient_mock.get(f"{GMAIL}/messages/{mid}", json={"id": mid, "payload": {"mimeType": "multipart/mixed", "parts": [
            {"partId": "0", "mimeType": "text/html", "filename": "", "body": {"size": 10}},
            {"partId": "1", "mimeType": "application/pdf", "filename": "recibo.pdf", "body": {"attachmentId": "pdf", "size": 9}},
            {"partId": "2", "mimeType": "text/xml", "filename": f"{mid}.xml", "body": {"attachmentId": f"att-{mid}", "size": len(xml)}},
        ]}})
    aioclient_mock.get(f"{GMAIL}/messages", json={
        "messages": [{"id": m, "threadId": m} for m in reversed(list(messages))], "resultSizeEstimate": len(messages)})


def gmail_calls(aioclient_mock, kind):
    urls = [str(c[1]) for c in aioclient_mock.mock_calls if str(c[1]).startswith(GMAIL)]
    if kind == "attachments":
        return [u for u in urls if "/attachments/" in u]
    return urls


async def sign_in(hass, hass_client_no_auth, aioclient_mock, flow_id, result, email=EMAIL, scope=SCOPE):
    """Follow the external Google step: check the authorize URL, call back, swap the code."""
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    url = URL(result["url"])
    assert str(url.with_query(None)) == "https://accounts.google.com/o/oauth2/v2/auth"
    assert url.query["scope"] == SCOPE
    assert url.query["access_type"] == "offline" and url.query["prompt"] == "consent"
    assert url.query["client_id"] == "client-id"
    client = await hass_client_no_auth()
    resp = await client.get(f"/auth/external/callback?code=abcd&state={url.query['state']}")
    assert resp.status == 200
    aioclient_mock.post(TOKEN_URL, json=token(access="at-new", scope=scope))
    aioclient_mock.get(f"{GMAIL}/profile", json={"emailAddress": email, "messagesTotal": 3})
    return await hass.config_entries.flow.async_configure(flow_id)


async def test_google_flow(hass: HomeAssistant, credentials, hass_client_no_auth, aioclient_mock, current_request_with_host):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert r["type"] is FlowResultType.MENU and r["menu_options"] == ["google", "imap"]
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "google"})
    r = await sign_in(hass, hass_client_no_auth, aioclient_mock, r["flow_id"], r)
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "rpu"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"rpu": "12"})
    assert r["errors"] == {"rpu": "invalid_rpu"}
    mock_gmail(aioclient_mock, {})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"rpu": "0001 1122 2333"})
    await hass.async_block_till_done()
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["title"] == f"CFE {RPU} ({EMAIL})" and r["result"].unique_id == RPU
    data = r["data"]
    assert data["auth_type"] == "google" and data["rpu"] == RPU and data["auth_implementation"] == DOMAIN
    assert data["email"] == EMAIL
    assert data["token"]["access_token"] == "at-new" and data["token"]["refresh_token"] == "rt-1"
    assert "password" not in data and "username" not in data
    assert r["result"].state is ConfigEntryState.LOADED
    # a second Google entry for the same RPU is refused after sign-in
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "google"})
    r = await sign_in(hass, hass_client_no_auth, aioclient_mock, r["flow_id"], r)
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"rpu": RPU})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "already_configured"


async def test_google_missing_credentials(hass: HomeAssistant, current_request_with_host):
    assert await async_setup_component(hass, "application_credentials", {})
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "google"})
    # HA's standard abort: the frontend opens "Add application credentials"
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "missing_credentials"


async def states(hass, e):
    ids = entity_ids(hass, e)
    return {k: hass.states.get(ids[k]).state for k in KEYS}


async def test_google_poll_matches_imap(hass: HomeAssistant, credentials, aioclient_mock, hass_storage, freezer):
    freezer.move_to("2026-09-30 12:00:00")
    mock_gmail(aioclient_mock, {"m1": XML, "m2": OTHER})
    e = google_entry(); e.add_to_hass(hass)
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    assert e.state is ConfigEntryState.LOADED
    assert list(e.runtime_data.data.bills) == ["000012345678"]  # the other RPU's XML is ignored
    assert len(gmail_calls(aioclient_mock, "attachments")) == 2  # the PDF is never downloaded
    for call in aioclient_mock.mock_calls:
        if str(call[1]).startswith(GMAIL):
            assert call[3]["Authorization"] == "Bearer at-1"
    stored = hass_storage[f"cfe.{e.entry_id}"]["data"]
    assert stored["gmail_ids"] == ["m1", "m2"]
    google_states = await states(hass, e)
    # later poll, no new messages: only the listing, no message or attachment fetch
    before = len(gmail_calls(aioclient_mock, "all"))
    await hass.services.async_call(DOMAIN, "refresh", blocking=True)
    assert gmail_calls(aioclient_mock, "all")[before:] == [f"{GMAIL}/messages?q=has:attachment+filename:xml&maxResults=500"]
    # same XML over IMAP: same sensor values
    assert await hass.config_entries.async_remove(e.entry_id)
    await hass.async_block_till_done()
    i = entry(); i.add_to_hass(hass)
    with patch(FETCH, return_value=FetchResult(7, 2, [(1, XML), (2, OTHER)], [1, 2])):
        assert await hass.config_entries.async_setup(i.entry_id)
        await hass.async_block_till_done()
    assert await states(hass, i) == google_states
    assert google_states["amount_due"] == "1128.28"


def stored_bill(hass_storage, e):
    import importlib
    bill = importlib.import_module("custom_components.cfe.parser").parse_bill(XML)
    hass_storage[f"cfe.{e.entry_id}"] = {"version": 1, "minor_version": 1, "key": f"cfe.{e.entry_id}",
        "data": {"bills": {bill.folio: bill.to_dict()}, "paid": {}, "uidvalidity": None, "last_uid": 0,
                 "gmail_ids": ["m1"], "initial_import_done": True}}


async def test_google_token_revoked_reauth(hass: HomeAssistant, credentials, aioclient_mock, hass_storage,
                                           hass_client_no_auth, current_request_with_host, freezer):
    freezer.move_to("2026-10-13 12:00:00")
    e = google_entry(expires_in=-60, email=None); e.add_to_hass(hass)  # v0.2.0 dev entry: no email
    stored_bill(hass_storage, e)
    aioclient_mock.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    assert e.state is ConfigEntryState.LOADED
    assert hass.states.get(entity_ids(hass, e)["amount_due"]).state == "1128.28"  # stored bills stay visible
    assert gmail_calls(aioclient_mock, "all") == []
    flows = [f for f in hass.config_entries.flow.async_progress() if f["context"]["source"] == "reauth"]
    assert len(flows) == 1 and flows[0]["step_id"] == "reauth_google"
    aioclient_mock.clear_requests()
    mock_gmail(aioclient_mock, {"m1": XML})
    r = await hass.config_entries.flow.async_configure(flows[0]["flow_id"], {})
    r = await sign_in(hass, hass_client_no_auth, aioclient_mock, flows[0]["flow_id"], r)
    await hass.async_block_till_done()
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "reauth_successful"
    assert e.data["token"]["access_token"] == "at-new" and e.data["rpu"] == RPU and e.data["auth_type"] == "google"
    assert e.data["email"] == EMAIL  # stored on the first reauth
    assert e.state is ConfigEntryState.LOADED
    assert gmail_calls(aioclient_mock, "attachments") == []  # m1 was already processed


async def test_google_gmail_401_reauth(hass: HomeAssistant, credentials, aioclient_mock, hass_storage, freezer):
    freezer.move_to("2026-10-13 12:00:00")
    e = google_entry(); e.add_to_hass(hass)
    stored_bill(hass_storage, e)
    aioclient_mock.get(f"{GMAIL}/messages", status=401)
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(entity_ids(hass, e)["amount_due"]).state == "1128.28"
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


async def test_google_auth_fail_on_first_setup(hass: HomeAssistant, credentials, aioclient_mock):
    e = google_entry(); e.add_to_hass(hass)
    aioclient_mock.get(f"{GMAIL}/messages", **google_error(403, "insufficientPermissions"))
    await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    assert e.state is ConfigEntryState.SETUP_ERROR
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


@pytest.mark.parametrize("failure", [{"status": 503}, {"exc": TimeoutError()}])
async def test_google_gmail_down(hass: HomeAssistant, credentials, aioclient_mock, hass_storage, freezer, failure):
    freezer.move_to("2026-10-13 12:00:00")
    e = google_entry(); e.add_to_hass(hass)
    stored_bill(hass_storage, e)
    aioclient_mock.get(f"{GMAIL}/messages", **failure)
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "refresh", blocking=True)
    assert e.state is ConfigEntryState.LOADED
    assert hass.states.get(entity_ids(hass, e)["amount_due"]).state == "1128.28"
    assert not any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())
    assert hass_storage[f"cfe.{e.entry_id}"]["data"]["gmail_ids"] == ["m1"]


async def test_google_options(hass: HomeAssistant, credentials, aioclient_mock):
    mock_gmail(aioclient_mock, {})
    e = google_entry(); e.add_to_hass(hass)
    await hass.config_entries.async_setup(e.entry_id); await hass.async_block_till_done()
    r = await hass.config_entries.options.async_init(e.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"scan_interval": 24})
    await hass.async_block_till_done()
    from datetime import timedelta
    assert e.runtime_data.update_interval == timedelta(hours=24)


async def test_google_missing_scope(hass: HomeAssistant, credentials, hass_client_no_auth, aioclient_mock,
                                    current_request_with_host):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "google"})
    # Gmail unticked on Google's consent screen: only the basic scopes come back
    r = await sign_in(hass, hass_client_no_auth, aioclient_mock, r["flow_id"], r, scope="openid")
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "missing_scope"
    assert gmail_calls(aioclient_mock, "all") == []


async def test_google_flow_api_disabled(hass: HomeAssistant, credentials, hass_client_no_auth, aioclient_mock,
                                        current_request_with_host):
    aioclient_mock.get(f"{GMAIL}/profile", **google_error(403, "accessNotConfigured"))
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "google"})
    r = await sign_in(hass, hass_client_no_auth, aioclient_mock, r["flow_id"], r)
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "api_disabled"


async def test_google_reauth_account_check(hass: HomeAssistant, credentials, hass_client_no_auth, aioclient_mock,
                                           current_request_with_host):
    mock_gmail(aioclient_mock, {})
    e = google_entry(); e.add_to_hass(hass)
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    r = await e.start_reauth_flow(hass)
    assert r["step_id"] == "reauth_google"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    r = await sign_in(hass, hass_client_no_auth, aioclient_mock, r["flow_id"], r, email="someone@else.com")
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "wrong_account"
    assert r["description_placeholders"] == {"email": EMAIL}
    assert e.data["token"]["access_token"] == "at-1"
    aioclient_mock.clear_requests()
    mock_gmail(aioclient_mock, {})
    r = await e.start_reauth_flow(hass)
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    r = await sign_in(hass, hass_client_no_auth, aioclient_mock, r["flow_id"], r, email="Bills@Example.com")
    await hass.async_block_till_done()
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "reauth_successful"
    assert e.data["token"]["access_token"] == "at-new"


async def test_google_new_message_without_bill_is_saved(hass: HomeAssistant, credentials, aioclient_mock,
                                                        hass_storage, freezer):
    freezer.move_to("2026-10-13 12:00:00")
    e = google_entry(); e.add_to_hass(hass)
    stored_bill(hass_storage, e)
    mock_gmail(aioclient_mock, {"m1": XML, "m3": OTHER})
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    assert list(e.runtime_data.data.bills) == ["000012345678"]
    assert gmail_calls(aioclient_mock, "attachments") == [f"{GMAIL}/messages/m3/attachments/att-m3"]
    assert hass_storage[f"cfe.{e.entry_id}"]["data"]["gmail_ids"] == ["m1", "m3"]


async def test_imap_cursor_saved_without_bill(hass: HomeAssistant, hass_storage):
    e = entry(); e.add_to_hass(hass)
    hass_storage[f"cfe.{e.entry_id}"] = {"version": 1, "minor_version": 1, "key": f"cfe.{e.entry_id}",
        "data": {"bills": {}, "paid": {}, "uidvalidity": 7, "last_uid": 2, "initial_import_done": True}}
    with patch(FETCH, return_value=FetchResult(7, 5, [], [])):
        assert await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    assert hass_storage[f"cfe.{e.entry_id}"]["data"]["last_uid"] == 5


async def test_google_token_server_down(hass: HomeAssistant, credentials, aioclient_mock, hass_storage, freezer):
    freezer.move_to("2026-10-13 12:00:00")
    e = google_entry(expires_in=-60); e.add_to_hass(hass)
    stored_bill(hass_storage, e)
    aioclient_mock.post(TOKEN_URL, status=503)
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    assert e.state is ConfigEntryState.LOADED
    assert hass.states.get(entity_ids(hass, e)["amount_due"]).state == "1128.28"
    assert not e.runtime_data.last_update_success
    assert reauth_flows(hass) == []


@pytest.mark.parametrize(("failure", "reauth"), [
    (google_error(403, "rateLimitExceeded"), False),
    (google_error(403, "accessNotConfigured"), False),
    (google_error(403, "insufficientPermissions"), True),
    (google_error(401, "authError", "UNAUTHENTICATED"), True),
])
async def test_google_403_kinds(hass: HomeAssistant, credentials, aioclient_mock, hass_storage, freezer,
                                caplog, failure, reauth):
    freezer.move_to("2026-10-13 12:00:00")
    e = google_entry(); e.add_to_hass(hass)
    stored_bill(hass_storage, e)
    aioclient_mock.get(f"{GMAIL}/messages", **failure)
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    assert e.state is ConfigEntryState.LOADED
    assert hass.states.get(entity_ids(hass, e)["amount_due"]).state == "1128.28"
    assert bool(reauth_flows(hass)) is reauth
    if failure["json"]["error"]["errors"][0]["reason"] == "accessNotConfigured":
        assert "Gmail API is not enabled" in caplog.text


async def test_google_missing_message_is_recorded(hass: HomeAssistant, credentials, aioclient_mock,
                                                  hass_storage, freezer):
    freezer.move_to("2026-09-30 12:00:00")
    e = google_entry(); e.add_to_hass(hass)
    aioclient_mock.get(f"{GMAIL}/messages/m1", status=404, json={"error": {"code": 404, "status": "NOT_FOUND"}})
    mock_gmail(aioclient_mock, {"m1": OTHER, "m2": XML})
    assert await hass.config_entries.async_setup(e.entry_id)
    await hass.async_block_till_done()
    assert list(e.runtime_data.data.bills) == ["000012345678"]
    assert hass_storage[f"cfe.{e.entry_id}"]["data"]["gmail_ids"] == ["m1", "m2"]


async def test_google_per_poll_cap(hass: HomeAssistant, credentials, aioclient_mock, hass_storage, freezer):
    freezer.move_to("2026-10-13 12:00:00")
    e = google_entry(); e.add_to_hass(hass)
    mock_gmail(aioclient_mock, {"m1": OTHER, "m2": NEXT.replace(b"<FECORTE>11 DIC 26</FECORTE>", b"<FECORTE>11 OCT 26</FECORTE>"), "m3": XML})
    with patch("custom_components.cfe.gmail.MAX_PER_POLL", 2):
        assert await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
        stored = hass_storage[f"cfe.{e.entry_id}"]["data"]
        assert stored["gmail_ids"] == ["m1", "m2"]  # oldest first; m3 waits
        assert stored["initial_import_done"] is False
        assert gmail_calls(aioclient_mock, "attachments")[-1].endswith("/m2/attachments/att-m2")
        freezer.tick(61)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    stored = hass_storage[f"cfe.{e.entry_id}"]["data"]
    assert stored["gmail_ids"] == ["m1", "m2", "m3"]
    assert stored["initial_import_done"] is True
    # bills read in the follow-up poll still count as the first import (auto-paid)
    assert e.runtime_data.data.is_paid("000012345678")
    assert sorted(e.runtime_data.data.bills) == ["000012345678", "000012399999"]
