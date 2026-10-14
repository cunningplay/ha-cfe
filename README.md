# CFE bills for Home Assistant

A custom integration that reads CFE (Comisión Federal de Electricidad, Mexico)
bills from email. CFE has no usable API, but every bill arrives by email with
its CFDI 4.0 XML. This integration reads the mailbox over IMAP, parses the XML
and turns the latest bill into sensors. It also writes each bill's kWh and cost
into long-term statistics.

- Only XML attachments whose issuer RFC is `CFE370814QI0` and whose RPU matches
  the configured service number are used. The sender does not matter, so bills
  forwarded by someone else work too.
- Each bill is stored locally (keyed by folio). If the mailbox is unreachable,
  the sensors keep showing the stored data.
- No CFE website login, no captcha, no PDF parsing, no extra Python packages.

## Install (HACS)

1. HACS → Integrations → ⋮ → Custom repositories → add this repository as an
   *Integration*.
2. Install **CFE**, then restart Home Assistant.
3. Settings → Devices & services → Add integration → **CFE**.

Manual install: copy `custom_components/cfe` into your `config/custom_components/`.

Requires Home Assistant 2026.10 or newer.

## Setup

### 1. Gmail app password

1. Turn on 2-Step Verification for the Google account that receives the bills.
2. Open <https://myaccount.google.com/apppasswords>, create an app password
   (name it e.g. "Home Assistant CFE") and copy the 16 characters.
3. IMAP is on by default for Gmail. If you turned it off, enable it in Gmail →
   Settings → Forwarding and POP/IMAP.

Other providers work too: use their IMAP host and port 993 (SSL).

### 2. Folder

`INBOX` by default. If a filter moves the bills into a label, enter the label
name (Gmail labels are IMAP folders, e.g. `CFE`). The folder is opened read-only;
messages are never moved, deleted or marked as read.

### 3. RPU

The 12-digit service number printed on the bill ("No. de servicio"/RPU).
Spaces are removed. One config entry (and one device) per RPU.

| Field | Default |
|---|---|
| IMAP server | `imap.gmail.com` |
| Port | `993` |
| Email address | the mailbox that receives the bills |
| App password | the app password from step 1 |
| Folder | `INBOX` |
| Service number (RPU) | 12 digits |

## Entities

One device, `CFE <RPU>`, with:

| Entity | Description |
|---|---|
| Amount due | Total to pay (MXN). Attributes: folio, payment reference, line items |
| Due date | Payment deadline (`FECLIMITE`) |
| Cut-off date | Date the service can be cut (`FECORTE`) |
| Period start / end | Billing period |
| Bill date | Date the invoice was issued |
| Energy | kWh for the period. Attributes: days, tiers, history |
| Daily energy | Average kWh per day (kWh/d) |
| Daily cost | Average cost per day (MXN) |
| Meter reading | Current meter reading (total increasing). Attributes: meter id, previous reading |
| Tariff | e.g. `1A`. Attributes: summer season months |
| Government subsidy | `AportacionGub` (MXN) |
| Real energy cost | Cost without subsidy (MXN) |
| Paid (binary sensor) | Local paid flag for the latest bill |
| Mark paid (button) | Marks the latest bill as paid |

The paid flag is local: CFE emails carry no payment information. On the very
first import, bills whose cut-off date (or, if missing, due date or period end)
has already passed are marked paid (on a new service an unpaid bill past its
cut-off would mean the power had been cut). Later bills are never auto-marked.

## Long-term statistics

Each bill adds one point at its period end to two external statistics:

- `cfe:<rpu>_energy` (kWh): the energy billed for the period (`CONSUMO_R`).
- `cfe:<rpu>_cost` (MXN): the period charge, i.e. the CFDI invoice `Total`
  (energy + IVA + DAP). It is not the amount due, which can include one-off
  items such as the deposit on a new service.

Both have a running sum, so they can be used in the Energy dashboard or a
statistics graph. A zero point at the first bill's period start gives the first
bill a baseline. If two bills share a period end, the latest issued one counts.

## Polling and refresh

The mailbox is checked every 6 hours (change it under the integration's
**Configure**). Only messages newer than the last seen UID are fetched
(the folder's UIDVALIDITY is tracked); on Gmail the search is narrowed to
messages with an `.xml` attachment. Call `cfe.refresh` to check now; it fails with an error if the mailbox cannot be read.

If the login is rejected, Home Assistant asks for a new app password (reauth).
Any other mail error is retried at the next poll while the stored data stays
visible.

## Privacy

- A Gmail app password grants full access to that mailbox. Use a dedicated
  mailbox for the bills (or forward them to one), not your personal account.
- Parsed bills, including the customer name and address from the XML, are kept in
  `.storage/cfe.<entry_id>` in the Home Assistant config folder. The file is
  deleted when the integration entry is removed.
- The password is never logged.

## Development

```sh
# Parser and IMAP client (no Home Assistant needed)
uvx --with pytest pytest -q tests

# Home Assistant integration tests (config flow, polling, Store, statistics)
uvx -p 3.14 --with pytest-homeassistant-custom-component==0.13.372 \
  pytest -q tests_ha -o asyncio_mode=auto
```

The parser (`custom_components/cfe/parser.py`) has no Home Assistant imports and
is tested against an anonymized bill in `tests/fixtures/bill_anon.xml`.
Real bills belong in `samples/`, which is git-ignored and must never be committed.

## License

MIT
