<p align="center"><img src="https://raw.githubusercontent.com/cunningplay/ha-cfe/main/custom_components/cfe/brand/icon.png" alt="CFE" width="120"></p>

# Recibos CFE para Home Assistant

**Español** · [English](#cfe-bills-for-home-assistant)

Una integración personalizada que lee los recibos de la CFE (Comisión Federal
de Electricidad) desde tu correo. La CFE no tiene una API que se pueda usar,
pero cada recibo llega por correo con su XML CFDI 4.0, su PDF o ambos. Esta
integración lee el buzón (iniciando sesión con Google mediante la API de Gmail,
o con cualquier servidor de correo por IMAP), interpreta el XML (o el PDF
cuando no hay XML) y convierte el recibo más reciente en sensores. Además
guarda los kWh y el costo de cada recibo en las estadísticas a largo plazo.

- Solo se usan los recibos cuyo emisor es la CFE (RFC `CFE370814QI0`) y cuyo
  RPU coincide con el número de servicio configurado. El remitente no importa,
  así que también funcionan los recibos que alguien más te reenvía.
- Cada recibo se guarda localmente (por folio). Si no se puede acceder al
  buzón, los sensores siguen mostrando los datos guardados.
- No hace falta entrar al sitio de la CFE ni resolver captchas. El lector de
  PDF necesita dos paquetes de Python puro, `pypdf` y `fonttools` (Home
  Assistant los instala).

## Recibos en XML y en PDF

El XML del CFDI es la fuente preferida: trae todos los datos. Los recibos
digitales de la CFE pueden llegar solo con el PDF, así que la integración
también lee los adjuntos `.pdf`:

- **Ambos en el mismo mensaje**: se usa el XML y se ignora el PDF (con el
  inicio de sesión de Google ni siquiera se descarga).
- **Primero el PDF, después el XML** (en otro mensaje, mismo folio): el recibo
  del XML reemplaza al del PDF. Se conserva la marca de pagado.
- **Primero el XML, después el PDF**: el PDF se ignora. Un PDF nunca reemplaza
  a un recibo XML.
- Un PDF que no es un recibo de la CFE, o que es de otro número de servicio, se
  ignora. Un PDF ilegible o cifrado se omite con una advertencia; el mensaje
  cuenta como leído de todos modos.

Los PDF de la CFE tienen la capa de texto dañada (los mapas de caracteres de
las fuentes incrustadas no corresponden a los glifos), por eso al copiar y
pegar sale texto sin sentido. La integración reconoce cada glifo por su
contorno (incluye una tabla de hashes de contornos de Arial en `glyphs.json`,
sin datos de la fuente), reconstruye los mapas de caracteres y lee el texto.
No usa OCR ni dibuja nada. Si un glifo no se reconoce, el dato al que
pertenece se queda vacío en lugar de quedar mal. Un PDF solo se toma como
recibo si menciona a la CFE, tiene al menos dos de las etiquetas *PERIODO
FACTURADO*, *TOTAL A PAGAR* y *LÍMITE DE PAGO* (para que un comprobante de pago
no cuente como recibo), sus cargos suman el total y su lectura del medidor
coincide con los kWh; si no, se omite con una advertencia.

Un recibo leído del PDF tiene el mismo importe, fechas, periodo, kWh, tarifa,
lectura del medidor, conceptos, referencia de pago y folio que el XML. Los
escalones de consumo son distintos: el PDF imprime kWh enteros por escalón y
el subtotal de cada uno (el `subtotal` en el atributo de escalones del sensor
*Consumo*), mientras que el XML trae kWh con decimales y sin subtotal. No
tiene fecha de emisión (*Fecha del recibo* queda como desconocida), ni
historial de consumo, costo diario, costo real de la energía ni temporada de
verano. El atributo `source` del sensor *Total a pagar* indica de dónde salió
el recibo (`xml` o `pdf`).

*Aportación gubernamental* queda vacía en un recibo leído del PDF: el
"Apoyo Gubernamental" que viene impreso en el PDF es una cifra distinta a la
`AportacionGub` del XML (75.29 contra 265.29 en el mismo recibo). El importe
impreso queda en el atributo `apoyo_impreso` de *Total a pagar*.

Consejo: en Gmail, un filtro con `from:cfe.mx has:attachment` puede ponerles
una etiqueta a los recibos (y, con IMAP, esa etiqueta puede ser la carpeta que
se lee).

## Instalación (HACS)

1. HACS → Integraciones → ⋮ → Repositorios personalizados (*Custom
   repositories*) → agrega este repositorio como *Integración*
   (*Integration*).
2. Instala **CFE** y luego reinicia Home Assistant.
3. Ajustes → Dispositivos y servicios → Añadir integración (*Settings →
   Devices & services → Add integration*) → **CFE**.

Instalación manual: copia `custom_components/cfe` en tu carpeta
`config/custom_components/`.

Requiere Home Assistant 2026.10 o más reciente.

## Configuración

Ajustes → Dispositivos y servicios → Añadir integración → **CFE**, y luego
elige cómo se van a leer los recibos:

- **Cuenta de Google (iniciar sesión)**: inicias sesión con Google; no
  necesitas contraseña de aplicación ni verificación en 2 pasos. Requiere
  crear una sola vez un cliente OAuth (ver abajo).
- **Servidor de correo (IMAP)**: cualquier buzón, con una contraseña de
  aplicación de Gmail o el usuario y contraseña IMAP de tu proveedor.

En los dos casos después escribes el RPU (el número de servicio de 12 dígitos
impreso en el recibo, "No. de servicio"). Los espacios se quitan. Se crea una
entrada de configuración (y un dispositivo) por cada RPU. Los dos métodos
alimentan los mismos sensores, la misma marca de pagado y las mismas
estadísticas.

### Inicio de sesión con la cuenta de Google

Home Assistant inicia sesión con tu propio cliente OAuth de Google
(credenciales de aplicación). Esto se configura una sola vez en Google Cloud
Console:

1. Abre <https://console.cloud.google.com/> y crea un proyecto (o elige uno).
2. Habilita la **API de Gmail** (*Gmail API*) en ese proyecto:
   <https://console.cloud.google.com/apis/library/gmail.googleapis.com>.
   Sin ella todas las solicitudes fallan.
3. Configura la **pantalla de consentimiento de OAuth** (*OAuth consent
   screen*; en Google Auth Platform → Desarrollo de la marca / Público,
   *Branding / Audience*): tipo de usuario *Externo* (*External*), tu correo
   como correo de asistencia y como contacto del desarrollador. En *Público*
   (*Audience*), **publica la app** (*En producción*, *In production*). Las
   apps que se quedan en *Prueba* (*Testing*) reciben tokens de actualización
   que vencen a los 7 días, y la integración te pediría volver a iniciar
   sesión cada semana.
4. **Credenciales → Crear credenciales → ID de cliente de OAuth → Aplicación
   web** (*Credentials → Create credentials → OAuth client ID → Web
   application*). Agrega el URI de redireccionamiento autorizado
   `https://my.home-assistant.io/redirect/oauth`. Copia el ID de cliente y el
   secreto.
5. En Home Assistant, elige **Cuenta de Google (iniciar sesión)**. La primera
   vez te pide el ID de cliente y el secreto (también puedes ir a Ajustes →
   Dispositivos y servicios → ⋮ → Credenciales de la aplicación, *Application
   credentials*). Luego inicia sesión con la cuenta de Google que recibe los
   recibos y permite el acceso de solo lectura a Gmail.
6. Google avisa que la app no está verificada ("Google no verificó esta app",
   *Google hasn't verified this app*). Es normal con un cliente OAuth
   personal: tú eres el desarrollador y también el usuario. Elige
   **Configuración avanzada → Ir a … (no seguro)** (*Advanced → Go to …
   (unsafe)*) para continuar.
7. Escribe el RPU.

La integración pide exactamente un permiso (*scope*),
`https://www.googleapis.com/auth/gmail.readonly`. Busca mensajes con un
adjunto `.xml` o `.pdf`
(`has:attachment (filename:xml OR filename:pdf)`, en todas las carpetas menos
spam y papelera) y descarga solo esos adjuntos. Cada mensaje se lee una sola
vez (se recuerda su id). En cada consulta se leen como máximo 50 mensajes
nuevos; si hay más esperando (por ejemplo, un buzón con años de recibos), la
siguiente consulta se hace un minuto después, hasta leerlos todos. La
dirección de correo de la cuenta se guarda con la entrada y aparece en su
título.

Si más adelante Google rechaza el token o la autorización (se revocó el
acceso, venció el token de actualización, se quitó el acceso a Gmail), Home
Assistant te pide volver a iniciar sesión con Google (reautenticación), con la
misma cuenta de Google. Mientras tanto, los recibos guardados siguen
visibles.

Si el registro dice **the Gmail API must be enabled in the Google Cloud
project** (hay que habilitar la API de Gmail en el proyecto de Google Cloud),
habilítala (paso 2) en el proyecto al que pertenece el cliente OAuth; no hace
falta volver a iniciar sesión.

### Servidor de correo (IMAP)

#### 1. Contraseña de aplicación de Gmail

1. Activa la verificación en 2 pasos en la cuenta de Google que recibe los
   recibos.
2. Abre <https://myaccount.google.com/apppasswords>, crea una contraseña de
   aplicación (ponle un nombre, por ejemplo "Home Assistant CFE") y copia los
   16 caracteres.
3. En Gmail, IMAP está activado de forma predeterminada. Si lo desactivaste,
   actívalo en Gmail → Configuración → Reenvío y correo POP/IMAP (*Settings →
   Forwarding and POP/IMAP*).

También funcionan otros proveedores: usa su servidor IMAP y el puerto 993
(SSL).

#### 2. Carpeta

`INBOX` de forma predeterminada. Si un filtro mueve los recibos a una
etiqueta, escribe el nombre de la etiqueta (en IMAP las etiquetas de Gmail son
carpetas, por ejemplo `CFE`). La carpeta se abre en modo de solo lectura; los
mensajes nunca se mueven, se borran ni se marcan como leídos.

#### 3. RPU

El número de servicio de 12 dígitos impreso en el recibo ("No. de
servicio"/RPU). Los espacios se quitan.

| Campo | Valor predeterminado |
|---|---|
| Servidor IMAP | `imap.gmail.com` |
| Puerto | `993` |
| Correo electrónico | el buzón que recibe los recibos |
| Contraseña de aplicación | la contraseña de aplicación del paso 1 |
| Carpeta | `INBOX` |
| Número de servicio (RPU) | 12 dígitos |

## Entidades

Un dispositivo, `CFE <RPU>`, con:

| Entidad | Descripción |
|---|---|
| Total a pagar | Total a pagar (MXN). Atributos: folio, origen (`source`: `xml`/`pdf`), apoyo impreso (`apoyo_impreso`, recibos en PDF), referencia de pago, total de la factura, conceptos |
| Fecha límite de pago | Fecha límite para pagar (`FECLIMITE`) |
| Fecha de corte | Fecha a partir de la cual pueden cortar el servicio (`FECORTE`) |
| Inicio / Fin del periodo | Periodo de facturación |
| Fecha del recibo | Fecha en que se emitió la factura |
| Consumo | kWh del periodo. Atributos: días, escalones, historial |
| Consumo diario | Promedio de kWh por día (kWh/d) |
| Costo diario | Costo promedio por día (MXN) |
| Lectura del medidor | Lectura actual del medidor (siempre creciente). Atributos: número de medidor, lectura anterior |
| Tarifa | Por ejemplo `1A`. Atributos: meses de la temporada de verano |
| Aportación gubernamental | `AportacionGub` (MXN; vacía en un recibo leído del PDF) |
| Costo real de la energía | Costo sin subsidio (MXN) |
| Pagado (sensor binario) | Marca local de pagado del recibo más reciente |
| Marcar como pagado (botón) | Marca el recibo más reciente como pagado |

Los nombres de las entidades aparecen en el idioma de Home Assistant; esta
tabla usa los nombres en español.

La marca de pagado es local: los correos de la CFE no traen información de
pago. En la primera importación, los recibos cuya fecha de corte (o, si no
la tienen, la fecha límite de pago o el fin del periodo) ya pasó se marcan
como pagados (en un servicio nuevo, un recibo sin pagar después de su fecha de
corte significaría que ya te habrían cortado la luz). Los recibos posteriores
nunca se marcan solos.

## Estadísticas a largo plazo

Cada recibo agrega un punto, en la fecha de fin de su periodo, a dos
estadísticas externas:

- `cfe:<rpu>_energy` (kWh): la energía facturada en el periodo (`CONSUMO_R`).
- `cfe:<rpu>_cost` (MXN): el cargo del periodo, es decir, el `Total` de la
  factura CFDI (energía + IVA + DAP). No es el total a pagar, que puede
  incluir cargos únicos como el depósito de un servicio nuevo. En un recibo
  leído del PDF es "Fac. del Periodo" + DSAP, que da el mismo importe.

Las dos llevan una suma acumulada, así que se pueden usar en el panel de
Energía o en una gráfica de estadísticas. Un punto en cero al inicio del
periodo del primer recibo le da una base a ese primer recibo. Si dos recibos
tienen la misma fecha de fin de periodo, cuenta el emitido más recientemente.

## Consulta y actualización

El buzón se revisa una vez al día (puedes cambiarlo en **Configurar** de la
integración). Llama a `cfe.refresh` para revisarlo en ese momento; falla con
un error si la revisión falló antes de leer algún mensaje. Si falla a la
mitad, los mensajes leídos hasta ese momento se conservan (se registra una
advertencia) y el resto se lee en la siguiente consulta.

- IMAP: solo se descargan los mensajes más nuevos que el último UID visto (se
  lleva el control del UIDVALIDITY de la carpeta); en Gmail la búsqueda se
  limita a mensajes con un adjunto `.xml` o `.pdf`. Si el servidor rechaza el
  inicio de sesión, Home Assistant te pide una nueva contraseña de aplicación
  (reautenticación).
- Google: la API de Gmail lista los mensajes con un adjunto `.xml` o `.pdf`;
  los mensajes ya leídos se saltan, y un mensaje que Gmail ya no puede
  entregar (borrado, 400/404) se registra y se salta. Home Assistant te pide
  volver a iniciar sesión solo cuando Google rechaza el token (se niega la
  actualización, HTTP 401) o la autorización (HTTP 403 por falta de
  permisos). Los errores de cuota y de límite de solicitudes, los errores del
  servidor y un servidor de tokens caído se reintentan en la siguiente
  consulta. Si la API de Gmail está deshabilitada, se registra como error
  ("the Gmail API must be enabled in the Google Cloud project") y también se
  reintenta.

Cualquier otro error de correo se reintenta en la siguiente consulta, y
mientras tanto los datos guardados siguen visibles.

## Privacidad

- El inicio de sesión con Google usa el permiso de Gmail de solo lectura
  (`gmail.readonly`): la integración puede leer el correo, pero no puede
  enviarlo, cambiarlo ni borrarlo, y nunca ve tu contraseña de Google. Home
  Assistant guarda el token OAuth en la entrada de configuración. Puedes
  revocar el acceso cuando quieras en
  <https://myaccount.google.com/permissions>.
- Una contraseña de aplicación de Gmail da acceso completo a ese buzón. Usa un
  buzón dedicado a los recibos (o reenvíalos a uno), no tu cuenta personal.
- Con cualquiera de los dos métodos, los mensajes nunca se modifican, se
  mueven ni se borran.
- Los recibos interpretados (del XML o del PDF) se guardan en
  `.storage/cfe.<entry_id>` dentro de la carpeta de configuración de Home
  Assistant. El archivo se borra cuando eliminas la entrada de la
  integración.
- La contraseña nunca se escribe en el registro.

## Desarrollo

Las instrucciones para desarrolladores están en inglés: consulta la sección
[Development](#development).

## Licencia

MIT

---

# CFE bills for Home Assistant

[Español](#recibos-cfe-para-home-assistant) · **English**

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

- **Both in one message**: the XML is used and the PDF is ignored (with Google
  sign-in it is not even downloaded).
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
No OCR, nothing is rendered. A glyph that is not recognized leaves the field
it belongs to empty rather than wrong. A PDF is only taken as a bill if it
names CFE, has at least two of the labels *PERIODO FACTURADO*, *TOTAL A
PAGAR* and *LÍMITE DE PAGO* (so a payment receipt is not a bill), its charges
add up to the total and its meter reading matches the kWh; otherwise it is
skipped with a warning.

A bill read from the PDF has the same amount, dates, period, kWh, tariff,
meter reading, line items, payment reference and folio as the XML. Its tiers
differ: the PDF prints whole kWh per tier plus each tier's subtotal (the
`subtotal` in the *Energy* tiers attribute), while the XML has fractional kWh
and no subtotal. It has no issue date (*Bill date* stays unknown), no
consumption history, daily cost, real energy cost or summer season. The
*Amount due* sensor's `source` attribute says where the bill came from (`xml`
or `pdf`).

*Government subsidy* stays empty for a PDF bill: the "Apoyo Gubernamental"
amount printed on the PDF is a different figure from the XML's
`AportacionGub` (75.29 vs 265.29 on the same bill). The printed amount is the
*Amount due* attribute `apoyo_impreso`.

Tip: in Gmail, a filter on `from:cfe.mx has:attachment` can label the bills
(and, for IMAP, the label can be the folder to read).

## Install (HACS)

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=cunningplay&repository=ha-cfe&category=integration)

1. In HACS, search for **CFE** and download it, then restart Home Assistant.
2. Settings → Devices & services → Add integration → **CFE**.

Older HACS versions that don't list it yet: HACS → ⋮ → Custom repositories → add
`https://github.com/cunningplay/ha-cfe` as an *Integration*, then follow the steps above.

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
| Amount due | Total to pay (MXN). Attributes: folio, source (`xml`/`pdf`), printed subsidy (`apoyo_impreso`, PDF bills), payment reference, invoice total, line items |
| Due date | Payment deadline (`FECLIMITE`) |
| Cut-off date | Date the service can be cut (`FECORTE`) |
| Period start / end | Billing period |
| Bill date | Date the invoice was issued |
| Energy | kWh for the period. Attributes: days, tiers, history |
| Daily energy | Average kWh per day (kWh/d) |
| Daily cost | Average cost per day (MXN) |
| Meter reading | Current meter reading (total increasing). Attributes: meter id, previous reading |
| Tariff | e.g. `1A`. Attributes: summer season months |
| Government subsidy | `AportacionGub` (MXN; empty for a bill read from the PDF) |
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
uvx --with pytest --with pypdf==6.20.1 --with fonttools==4.66.1 pytest -q tests

# Home Assistant integration tests (config flows incl. Google OAuth, polling, Store, statistics)
uvx -p 3.14 --with pytest-homeassistant-custom-component==0.13.372 \
  --with pypdf==6.20.1 --with fonttools==4.66.1 pytest -q tests_ha -o asyncio_mode=auto
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
`Arial Italic`) by `python tools/make_glyph_table.py` (needs fonttools).
Provenance: it holds only one-way md5 hashes of glyph outlines (operator
names and integer coordinates, components decomposed; see
`pdfbill.glyph_hash`) and the characters they stand for. No outlines,
metrics or other font data are in it, and the font files themselves are
never committed. Changing `glyph_hash` means regenerating the table (a test
pins the hash of a synthetic glyph).

## License

MIT
