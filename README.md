# CFE bills for Home Assistant

A custom integration that reads CFE (Comisión Federal de Electricidad, Mexico)
bills from email. CFE has no usable API, but every bill arrives by email with
its CFDI 4.0 XML, its PDF, or both. This integration reads the mailbox (Google
sign-in with the Gmail API, or any mail server over IMAP), parses the XML (or
the PDF when there is no XML) and turns the latest bill into sensors. It also
writes each bill's kWh and cost into long-term statistics.

- Only bills whose issuer is CFE (RFC `CFE370814QI0`) and whose RPU matches
  the configured service number are used. The sender does not matter, so bills
  forwarded by someone else work too.
- Each bill is stored locally (keyed by folio). If the mailbox is unreachable,
  the sensors keep showing the stored data.
- No CFE website login, no captcha. The PDF reader needs two pure-Python
  packages, `pypdf` and `fonttools` (Home Assistant installs them).

## XML and PDF bills

The CFDI XML is the preferred source: it carries every field. Paperless CFE
bills may arrive with only the PDF, so the integration also reads `.pdf`
attachments:

- **Both in one message**: the XML is used and the PDF is ignored (not even
  decoded).
- **PDF first, XML later** (another message, same folio): the XML bill replaces
  the PDF one. The paid flag is kept.
- **XML first, PDF later**: the PDF is ignored. A PDF never replaces an XML bill.
- A PDF that is not a CFE bill, or is for another service number, is ignored.
  An unreadable or encrypted PDF is skipped with a warning; the message still
  counts as read.

CFE's PDFs have a broken text layer (the embedded fonts' character maps do not
match the glyphs), so ordinary copy/paste gives gibberish. The integration
recognizes each glyph by its outline (a table of Arial outline hashes ships in
`glyphs.json`; no font data), rebuilds the character maps and reads the text.
No OCR, nothing is rendered.

A bill read from the PDF has the same amount, dates, period, kWh, tariff,
meter reading, tiers, line items, payment reference and folio as the XML. It
has no issue date (*Bill date* stays unknown), no consumption history, daily
cost, real energy cost or summer season. The *Amount due* sensor's `source`
attribute says where the bill came from (`xml` or `pdf`). *Government
subsidy* shows the "Apoyo Gubernamental" amount printed on the PDF, which can
differ from the XML's `AportacionGub`.

Tip: in Gmail, a filter on `from:cfe.mx has:attachment` can label the bills
(and, for IMAP, the label can be the folder to read).

## Install (HACS)

1. HACS → Integrations → ⋮ → Custom repositories → add this repository as an
   *Integration*.
2. Install **CFE**, then restart Home Assistant.
3. Settings → Devices & services → Add integration → **CFE**.

Manual install: copy `custom_components/cfe` into your `config/custom_components/`.

Requires Home Assistant 2026.10 or newer.

## Setup

Settings → Devices & services → Add integration → **CFE**, then choose how the
bills are read:

- **Google account (sign in)**: sign in with Google; no app password, no
  2-Step Verification needed. Needs a one-time OAuth client (below).
- **Mail server (IMAP)**: any mailbox, with a Gmail app password or your
  provider's IMAP login.

Either way you then enter the RPU (12-digit service number printed on the bill,
"No. de servicio"). Spaces are removed. One config entry (and one device) per
RPU. Both methods feed the same sensors, paid flag and statistics.

### Google account sign-in

Home Assistant signs in with your own Google OAuth client (application
credentials). This is a one-time setup in Google Cloud Console:

1. Open <https://console.cloud.google.com/> and create a project (or pick one).
2. Enable the **Gmail API** for that project:
   <https://console.cloud.google.com/apis/library/gmail.googleapis.com>.
   Without it every request fails.
3. Set up the **OAuth consent screen** (Google Auth Platform → Branding /
   Audience): user type *External*, your email as support and developer
   contact. Under *Audience*, **publish the app** (*In production*). Apps left
   in *Testing* get refresh tokens that expire after 7 days, so the integration
   would ask you to sign in again every week.
4. **Credentials → Create credentials → OAuth client ID → Web application.**
   Add the authorized redirect URI
   `https://my.home-assistant.io/redirect/oauth`. Copy the client ID and secret.
5. In Home Assistant, choose **Google account (sign in)**. The first time, it
   asks for the client ID and secret (Settings → Devices & services → ⋮ →
   Application credentials also works). Then sign in with the Google account
   that receives the bills and allow read-only Gmail access.
6. Google warns that the app is unverified ("Google hasn't verified this app").
   That is expected for a personal OAuth client: you are both the developer and
   the user. Select **Advanced → Go to … (unsafe)** to continue.
7. Enter the RPU.

The integration asks for exactly one scope,
`https://www.googleapis.com/auth/gmail.readonly`. It searches for messages
with an `.xml` or `.pdf` attachment
(`has:attachment (filename:xml OR filename:pdf)`, all folders except spam and
trash) and downloads only those attachments. Each message is read once
(its id is remembered). At most 50 new messages are read per poll; if more are
waiting (e.g. a mailbox with years of bills), the next poll runs a minute later
until all are read. The account's email address is stored with the entry and
shown in its title.

If Google rejects the token or the authorization later (access revoked, refresh
token expired, Gmail access removed), Home Assistant asks you to sign in with
Google again (reauthentication), with the same Google account. The stored bills
stay visible meanwhile.

If the log says **the Gmail API must be enabled in the Google Cloud project**,
enable it (step 2) for the project that owns the OAuth client; no new sign-in
is needed.

### Mail server (IMAP)

#### 1. Gmail app password

1. Turn on 2-Step Verification for the Google account that receives the bills.
2. Open <https://myaccount.google.com/apppasswords>, create an app password
   (name it e.g. "Home Assistant CFE") and copy the 16 characters.
3. IMAP is on by default for Gmail. If you turned it off, enable it in Gmail →
   Settings → Forwarding and POP/IMAP.

Other providers work too: use their IMAP host and port 993 (SSL).

#### 2. Folder

`INBOX` by default. If a filter moves the bills into a label, enter the label
name (Gmail labels are IMAP folders, e.g. `CFE`). The folder is opened read-only;
messages are never moved, deleted or marked as read.

#### 3. RPU

The 12-digit service number printed on the bill ("No. de servicio"/RPU).
Spaces are removed.

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
| Amount due | Total to pay (MXN). Attributes: folio, source (`xml`/`pdf`), payment reference, invoice total, line items |
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
  items such as the deposit on a new service. For a bill read from the PDF it
  is "Fac. del Periodo" + DSAP, the same amount.

Both have a running sum, so they can be used in the Energy dashboard or a
statistics graph. A zero point at the first bill's period start gives the first
bill a baseline. If two bills share a period end, the latest issued one counts.

## Polling and refresh

The mailbox is checked once a day (change it under the integration's
**Configure**). Call `cfe.refresh` to check now; it fails with an error if the
check failed before any message was read. If it fails part way, the messages
read so far are kept (a warning is logged) and the rest are read at the next
poll.

- IMAP: only messages newer than the last seen UID are fetched (the folder's
  UIDVALIDITY is tracked); on Gmail the search is narrowed to messages with an
  `.xml` or `.pdf` attachment. If the login is rejected, Home Assistant asks for a new
  app password (reauth).
- Google: the Gmail API lists messages with an `.xml` or `.pdf` attachment; messages
  already read are skipped, and a message Gmail can no longer serve (deleted,
  400/404) is logged and skipped. Home Assistant asks you to sign in again only
  when Google rejects the token (refresh refused, HTTP 401) or the
  authorization (HTTP 403 for missing permissions). Quota and rate-limit
  errors, server errors and a token server that is down are retried at the
  next poll. A disabled Gmail API is logged as an error ("the Gmail API must be
  enabled in the Google Cloud project") and also retried.

Any other mail error is retried at the next poll while the stored data stays
visible.

## Privacy

- Google sign-in uses the read-only Gmail scope (`gmail.readonly`): the
  integration can read mail but cannot send, change or delete it, and it never
  sees your Google password. Home Assistant stores the OAuth token in the
  config entry. Revoke access any time at
  <https://myaccount.google.com/permissions>.
- A Gmail app password grants full access to that mailbox. Use a dedicated
  mailbox for the bills (or forward them to one), not your personal account.
- Either way, messages are never modified, moved or deleted.
- Parsed bills (from the XML or the PDF) are kept in
  `.storage/cfe.<entry_id>` in the Home Assistant config folder. The file is
  deleted when the integration entry is removed.
- The password is never logged.

## Development

```sh
# Parsers (XML, PDF), IMAP and Gmail clients (no Home Assistant needed)
uvx --with pytest --with pypdf --with fonttools pytest -q tests

# Home Assistant integration tests (config flows incl. Google OAuth, polling, Store, statistics)
uvx -p 3.14 --with pytest-homeassistant-custom-component==0.13.372 \
  pytest -q tests_ha -o asyncio_mode=auto
```

The parsers (`custom_components/cfe/parser.py`, `pdfbill.py`) have no Home
Assistant imports. The XML parser is tested against an anonymized bill in
`tests/fixtures/bill_anon.xml`, the PDF text parser against an anonymized
decoded text of the same bill (`tests/fixtures/bill_pdf_text_anon.txt`), and
the PDF decoder against a synthetic PDF built at test time with a generated
font. Real bills belong in `samples/`, which is git-ignored and must never be
committed; when `samples/*.pdf` exist, an extra local test decodes them and
compares them with the XML of the same bill.

`custom_components/cfe/glyphs.json` (outline hash → character) is generated
from local Arial fonts (macOS `Arial`, `Arial Bold`, `Arial Black`,
`Arial Italic`) by `python tools/make_glyph_table.py` (needs fonttools). The
font files themselves are never committed.

## License

MIT
