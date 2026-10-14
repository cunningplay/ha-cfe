"""Coordinator: polls the mailbox (IMAP or Gmail), keeps every bill in a local Store."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import logging
from typing import Any

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    UnitOfEnergy,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .const import (
    AUTH_GOOGLE,
    AUTH_IMAP,
    CONF_AUTH_TYPE,
    CONF_FOLDER,
    CONF_RPU,
    CONF_SCAN_INTERVAL,
    CURRENCY,
    DEFAULT_FOLDER,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    STORAGE_VERSION,
)
from .gmail import GmailAuthError, GmailClient, GmailError
from .mail import MailAuthError, MailError, fetch_new_bills
from .parser import Bill, BillParseError, parse_bill
from .pdfbill import parse_pdf_bill

_LOGGER = logging.getLogger(__name__)

MORE_DELAY = 60  # seconds before the next poll when Gmail has more new messages

type CfeConfigEntry = ConfigEntry[CfeCoordinator]


@dataclass
class CfeData:
    """Everything the entities read."""

    bills: dict[str, Bill] = field(default_factory=dict)
    paid: dict[str, bool] = field(default_factory=dict)

    @property
    def latest(self) -> Bill | None:
        """The most recent bill by period end (issue date as a fallback)."""
        if not self.bills:
            return None
        return max(self.bills.values(), key=_sort_key)

    def is_paid(self, folio: str) -> bool:
        """Local paid flag for a folio."""
        return self.paid.get(folio, False)


@dataclass
class _Fetched:
    """One poll of a mail source: attachments, plus the cursor update to keep."""

    attachments: list[tuple[Any, str, bytes]]  # (message UID or id, kind, bytes)
    moved: bool  # the source cursor changed (worth saving)
    commit: Callable[[], None]  # applies the new cursor once bills are handled
    more: bool = False  # more new messages wait for the next poll


def _is_cfe_xml(payload: bytes) -> bool:
    """The XML is a CFE bill (any service): the message's PDFs are not needed."""
    try:
        return parse_bill(payload) is not None
    except BillParseError:
        return False


def _sort_key(bill: Bill) -> tuple[date, datetime]:
    return (
        bill.period_end or date.min,
        bill.issued.replace(tzinfo=None) if bill.issued else datetime.min,
    )


class CfeCoordinator(DataUpdateCoordinator[CfeData]):
    """Polls the mailbox for new CFE bills; serves stored data when mail is down."""

    config_entry: CfeConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: CfeConfigEntry,
        gmail: GmailClient | None = None,
    ) -> None:
        """Set up the coordinator for one RPU (``gmail`` for Google entries)."""
        hours = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_RPU]}",
            update_interval=timedelta(hours=hours),
        )
        self.rpu: str = entry.data[CONF_RPU]
        self.auth_type: str = entry.data.get(CONF_AUTH_TYPE, AUTH_IMAP)
        if self.auth_type == AUTH_GOOGLE and gmail is None:
            raise ValueError("A Google entry needs a Gmail client")
        self._gmail = gmail
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._uidvalidity: int | None = None
        self._last_uid = 0
        self._gmail_ids: list[str] = []  # Gmail message ids already processed
        self._initial_import_done = False
        self._unsub_more: CALLBACK_TYPE | None = None
        self._state = CfeData()

    async def async_load_store(self) -> bool:
        """Load stored bills; publish them right away. True if any were stored."""
        stored = await self._store.async_load()
        if not stored:
            return False
        bills: dict[str, Bill] = {}
        for folio, raw in (stored.get("bills") or {}).items():
            try:
                bills[folio] = Bill.from_dict(raw)
            except (TypeError, ValueError) as err:
                _LOGGER.warning("Dropping unreadable stored bill %s: %s", folio, err)
        self._state = CfeData(
            bills=bills,
            paid={k: bool(v) for k, v in (stored.get("paid") or {}).items()},
        )
        self._uidvalidity = stored.get("uidvalidity")
        self._last_uid = int(stored.get("last_uid") or 0)
        self._gmail_ids = [str(i) for i in stored.get("gmail_ids") or []]
        self._initial_import_done = bool(stored.get("initial_import_done"))
        self.async_set_updated_data(self._snapshot())
        return True

    def _snapshot(self) -> CfeData:
        return CfeData(bills=dict(self._state.bills), paid=dict(self._state.paid))

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "bills": {f: b.to_dict() for f, b in self._state.bills.items()},
                "paid": self._state.paid,
                "uidvalidity": self._uidvalidity,
                "last_uid": self._last_uid,
                "gmail_ids": self._gmail_ids,
                "initial_import_done": self._initial_import_done,
            }
        )

    async def _async_fetch_imap(self) -> _Fetched:
        """New bill attachments (XML, PDF) over IMAP."""
        entry = self.config_entry
        try:
            result = await self.hass.async_add_executor_job(
                fetch_new_bills,
                entry.data[CONF_HOST],
                entry.data[CONF_PORT],
                entry.data[CONF_USERNAME],
                entry.data[CONF_PASSWORD],
                entry.data.get(CONF_FOLDER, DEFAULT_FOLDER),
                self._uidvalidity,
                self._last_uid,
            )
        except MailAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MailError as err:
            raise UpdateFailed(f"Mailbox unavailable: {err}") from err

        def commit() -> None:
            self._uidvalidity = result.uidvalidity
            self._last_uid = result.last_uid

        moved = (
            result.uidvalidity != self._uidvalidity
            or result.last_uid != self._last_uid
        )
        return _Fetched(list(result.attachments), moved, commit)

    async def _async_fetch_gmail(self) -> _Fetched:
        """New bill attachments over the Gmail API (each message fetched once)."""
        assert self._gmail is not None
        try:
            result = await self._gmail.fetch_new_bills(
                self._gmail_ids, xml_is_bill=_is_cfe_xml
            )
        except GmailAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except GmailError as err:
            raise UpdateFailed(f"Gmail unavailable: {err}") from err

        def commit() -> None:
            seen = set(self._gmail_ids)
            for message_id in result.processed_ids:
                if message_id not in seen:  # concurrent polls can return the same id
                    seen.add(message_id)
                    self._gmail_ids.append(message_id)

        return _Fetched(
            list(result.attachments), bool(result.processed_ids), commit, result.more
        )

    async def _async_update_data(self) -> CfeData:
        if self.auth_type == AUTH_GOOGLE:
            fetched = await self._async_fetch_gmail()
        else:
            fetched = await self._async_fetch_imap()

        today = dt_util.now().date()
        added: list[Bill] = []
        for uid, bills in (await self._async_parse(fetched.attachments)).items():
            for bill in bills:
                if not self._store_bill(uid, bill):
                    continue
                added.append(bill)
                # First import: anything already past its cut-off was paid (an
                # unpaid bill past cut-off on a new service means the power is cut).
                deadline = bill.cutoff_date or bill.due_date or bill.period_end
                if (
                    not self._initial_import_done
                    and deadline is not None
                    and deadline < today
                ):
                    self._state.paid.setdefault(bill.folio, True)

        changed = bool(added) or not self._initial_import_done or fetched.moved
        fetched.commit()
        # The first import (auto-paid) lasts until every waiting message was read.
        self._initial_import_done = self._initial_import_done or not fetched.more
        if fetched.more:
            self._async_schedule_more()
        if changed:
            await self._async_save()
        if added:
            _LOGGER.info("Stored %d new CFE bill(s)", len(added))
            self._async_import_statistics()
        return self._snapshot()

    async def _async_parse(
        self, attachments: list[tuple[Any, str, bytes]]
    ) -> dict[Any, list[Bill]]:
        """Bills for this RPU per message (message order kept).

        Within a message, an XML bill wins: the PDFs are read only when no
        XML attachment of the message is a CFE bill (for any service).
        """
        messages: dict[Any, list[tuple[str, bytes]]] = {}
        for uid, kind, payload in attachments:
            messages.setdefault(uid, []).append((kind, payload))
        found: dict[Any, list[Bill]] = {}
        for uid, parts in messages.items():
            bills = [
                b for k, p in parts if k == "xml" and (b := self._parse_xml(uid, p))
            ]
            if not bills:
                for kind, payload in parts:
                    if kind == "pdf" and (bill := await self._async_parse_pdf(uid, payload)):
                        bills.append(bill)
            found[uid] = [b for b in bills if self._for_this_service(uid, b)]
        return found

    def _parse_xml(self, uid: Any, payload: bytes) -> Bill | None:
        try:
            bill = parse_bill(payload)
        except BillParseError as err:
            _LOGGER.warning("Skipping malformed XML in message %s: %s", uid, err)
            return None
        if bill is None:
            _LOGGER.debug("Message %s: XML is not a CFE bill", uid)
        return bill

    def _for_this_service(self, uid: Any, bill: Bill) -> bool:
        if bill.rpu != self.rpu:
            _LOGGER.debug("Message %s: bill for another service, ignored", uid)
            return False
        return True

    async def _async_parse_pdf(self, uid: Any, payload: bytes) -> Bill | None:
        try:
            bill = await self.hass.async_add_executor_job(
                parse_pdf_bill, payload, self.rpu
            )
        except BillParseError as err:
            _LOGGER.warning("Skipping unreadable PDF in message %s: %s", uid, err)
            return None
        except Exception:  # noqa: BLE001 - one odd PDF must not stop the poll
            _LOGGER.warning("Skipping PDF in message %s: cannot decode", uid, exc_info=True)
            return None
        if bill is None:
            _LOGGER.debug("Message %s: PDF is not a CFE bill for this service", uid)
        return bill

    def _store_bill(self, uid: Any, bill: Bill) -> bool:
        """Store a new bill. True if stored (new, or an XML replacing a PDF)."""
        stored = self._state.bills.get(bill.folio)
        if stored is not None and not (stored.source == "pdf" and bill.source == "xml"):
            _LOGGER.debug("Message %s: folio %s already stored", uid, bill.folio)
            return False
        if stored is not None:
            _LOGGER.info("Folio %s: XML bill replaces the one read from the PDF", bill.folio)
        self._state.bills[bill.folio] = bill
        return True

    @callback
    def _async_schedule_more(self) -> None:
        """Poll again soon: Gmail has more new messages than one poll reads."""
        if self._unsub_more is not None:
            self._unsub_more()

        @callback
        def _poll(_now: datetime) -> None:
            self._unsub_more = None
            self.config_entry.async_create_background_task(
                self.hass, self.async_request_refresh(), f"{DOMAIN} more mail"
            )

        self._unsub_more = async_call_later(self.hass, MORE_DELAY, _poll)

    async def async_shutdown(self) -> None:
        """Cancel the pending follow-up poll."""
        if self._unsub_more is not None:
            self._unsub_more()
            self._unsub_more = None
        await super().async_shutdown()

    async def async_mark_paid(self, folio: str, paid: bool = True) -> None:
        """Set the local paid flag for a bill and persist it."""
        self._state.paid[folio] = paid
        await self._async_save()
        self.async_set_updated_data(self._snapshot())

    def _async_import_statistics(self) -> None:
        """Write one cumulative point per bill, at its period end.

        A zero point (sum unchanged, state 0) at the earliest bill's period
        start gives the first bill a baseline. Bills sharing a period end keep
        the latest issued one (as the sensors do). Cost is the period charge
        (CFDI Total), not the amount due, which can include one-off deposits.
        """
        tz = dt_util.get_default_time_zone()
        by_end: dict[date, Bill] = {}
        for bill in sorted(self._state.bills.values(), key=_sort_key):
            if bill.period_end is not None:
                by_end[bill.period_end] = bill  # later-issued wins
        bills = [by_end[end] for end in sorted(by_end)]
        energy: list[StatisticData] = []
        cost: list[StatisticData] = []
        energy_sum = 0.0
        cost_sum = 0.0
        first = bills[0] if bills else None
        if (
            first is not None
            and first.period_start is not None
            and first.period_start < first.period_end
        ):
            start = datetime.combine(first.period_start, time(0), tzinfo=tz)
            energy.append(StatisticData(start=start, state=0.0, sum=energy_sum))
            cost.append(StatisticData(start=start, state=0.0, sum=cost_sum))
        for bill in bills:
            assert bill.period_end is not None
            start = datetime.combine(bill.period_end, time(0), tzinfo=tz)
            period_cost = (
                bill.invoice_total if bill.invoice_total is not None else bill.total
            )
            energy_sum += bill.kwh or 0.0
            cost_sum += period_cost or 0.0
            energy.append(StatisticData(start=start, state=bill.kwh, sum=energy_sum))
            cost.append(StatisticData(start=start, state=period_cost, sum=cost_sum))
        if not energy:
            return
        title = f"CFE {self.rpu}"
        async_add_external_statistics(
            self.hass,
            StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"{title} energy",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:{self.rpu}_energy",
                unit_class=EnergyConverter.UNIT_CLASS,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            ),
            energy,
        )
        async_add_external_statistics(
            self.hass,
            StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"{title} cost",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:{self.rpu}_cost",
                unit_class=None,
                unit_of_measurement=CURRENCY,
            ),
            cost,
        )
