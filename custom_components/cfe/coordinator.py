"""Coordinator: polls the mailbox, keeps every bill in a local Store."""

from __future__ import annotations

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
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .const import (
    CONF_FOLDER,
    CONF_RPU,
    CONF_SCAN_INTERVAL,
    CURRENCY,
    DEFAULT_FOLDER,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    STORAGE_VERSION,
)
from .mail import MailAuthError, MailError, fetch_new_xml
from .parser import Bill, BillParseError, parse_bill

_LOGGER = logging.getLogger(__name__)

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


def _sort_key(bill: Bill) -> tuple[date, datetime]:
    return (
        bill.period_end or date.min,
        bill.issued.replace(tzinfo=None) if bill.issued else datetime.min,
    )


class CfeCoordinator(DataUpdateCoordinator[CfeData]):
    """Polls IMAP for new CFE bills; serves stored data when mail is down."""

    config_entry: CfeConfigEntry

    def __init__(self, hass: HomeAssistant, entry: CfeConfigEntry) -> None:
        """Set up the coordinator for one RPU."""
        hours = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_RPU]}",
            update_interval=timedelta(hours=hours),
        )
        self.rpu: str = entry.data[CONF_RPU]
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._uidvalidity: int | None = None
        self._last_uid = 0
        self._initial_import_done = False
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
                "initial_import_done": self._initial_import_done,
            }
        )

    async def _async_update_data(self) -> CfeData:
        entry = self.config_entry
        try:
            result = await self.hass.async_add_executor_job(
                fetch_new_xml,
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

        today = dt_util.now().date()
        added: list[Bill] = []
        for uid, payload in result.attachments:
            try:
                bill = parse_bill(payload)
            except BillParseError as err:
                _LOGGER.warning("Skipping malformed XML in message UID %s: %s", uid, err)
                continue
            if bill is None:
                _LOGGER.debug("UID %s: XML is not a CFE bill", uid)
                continue
            if bill.rpu != self.rpu:
                _LOGGER.debug("UID %s: bill for another service, ignored", uid)
                continue
            if bill.folio in self._state.bills:
                _LOGGER.debug("UID %s: folio %s already stored", uid, bill.folio)
                continue
            self._state.bills[bill.folio] = bill
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

        changed = (
            bool(added)
            or not self._initial_import_done
            or result.uidvalidity != self._uidvalidity
            or result.last_uid != self._last_uid
        )
        self._uidvalidity = result.uidvalidity
        self._last_uid = result.last_uid
        self._initial_import_done = True
        if changed:
            await self._async_save()
        if added:
            _LOGGER.info("Stored %d new CFE bill(s)", len(added))
            self._async_import_statistics()
        return self._snapshot()

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
