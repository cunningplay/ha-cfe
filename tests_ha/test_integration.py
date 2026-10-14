from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

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


async def test_flow(hass: HomeAssistant):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
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
    await hass.async_block_till_done()


async def test_flow_errors(hass: HomeAssistant):
    with patch(FETCH, return_value=FetchResult(1, 0, [], [])):
        e = entry(); e.add_to_hass(hass)
        await hass.config_entries.async_setup(e.entry_id); await hass.async_block_till_done()
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    r2 = await hass.config_entries.flow.async_configure(r["flow_id"], DATA)
    assert r2["type"] is FlowResultType.ABORT and r2["reason"] == "already_configured"
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
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
