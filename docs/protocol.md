# Protocol reference

This is an implementation reference for the undocumented [TelePool](https://telepool.ru/) Q3 website protocol used by the Maestro Q3 Duet pH/Rx controller from [Laboratoriya Vostrotina](https://vostrotin.ru/), not a vendor API specification. The source of truth is [`client/`](../custom_components/telepool/client/); unsupported website changes fail closed rather than expanding the write surface.

The implemented discovery rules, telemetry fields, and control form are Q3-specific. Other controllers using TelePool may share this protocol, but their compatibility has not been tested; this reference does not establish support for other models.

## HTTPS and authentication

Requests use `https://telepool.ru` with TLS verification, a dedicated `aiohttp.ClientSession` and its own cookie jar. The caller owns the session. Passwords, cookies, CSRF tokens, account routing context and raw response bodies must not be logged or included in diagnostics.

Login reads `/Account/Login`, extracts `__RequestVerificationToken`, then submits `Mail`, `Password`, `RememberMe=false` and the token. Authenticated read POSTs also include the current verification token. A generation-checked authentication lock permits one session recovery and read retry when the session expires. Rejected credentials stop further automatic password submissions until access is reconfigured.

Redirects are bounded and checked before following them. Only the TelePool HTTPS origin and explicitly supported paths are accepted. Access-denied responses and CSRF/protocol failures do not trigger repeated login attempts. Native settings POSTs use a separate single-attempt path and are not replayed by authentication recovery.

## Implemented operations

| Method and path | Purpose and principal fields |
| --- | --- |
| `GET /Account/Login`, `POST /Account/Login` | Login form and cookie/verification-token exchange. |
| `GET /` | Own-station discovery from dashboard metadata. |
| `POST /Home/FaceData` | Read status using `devices[n][Id]`, `devices[n][Type]` and CSRF token. |
| `POST /Home/ChartQ3` | Read chart data using `num`, empty `datetime`, `span=30`, `compress=0` and CSRF token. |
| `GET /Control/Q3/Index/{station_id}` | Read the current Q3 settings form with validated account/pool routing context. |
| `POST` to that validated form action | Change only `SetPointTemp` or `MenuEnableFu`, preserving the fresh successful form controls. |

Telemetry POSTs are read operations. No generalized endpoint caller, arbitrary settings writer or local-station transport is exposed.

## Discovery and telemetry

Own Q3 stations are selected from dashboard `DevicesMy` entries with device type `9`, then associated with their pool and private account routing context. Shared/trusted devices are not guessed from unrelated metadata. `FaceData.q3.screenType` is a separate channel bit mask:

| Bit | Channel |
| --- | --- |
| 1 | FU / temperature |
| 2 | Rx / ORP |
| 4 | pH |
| 8 | Free chlorine |

The parser combines the requested station's status with its chart. It rejects mismatched station data. Numeric values must be finite and non-boolean; missing or invalid values remain unknown. Temperature sentinel `-127` is treated as a missing probe. Absent channel bits suppress the corresponding readings.

Chart fields include `calcPh`, `calcRx`, optional `calcFcl`, `tempFu`, channel setpoints, mode codes and RSSI. Status/envelope fields supply stale/error flags and quality. Mode codes and the error flag are exposed without inventing physical actuator feedback.

The selected sample is the latest dated chart point with at least one usable measurement for an enabled channel. An empty trailing minute bucket does not erase measurements or advance their measurement time.

| Timestamp | Meaning in this implementation |
| --- | --- |
| `data[].visit` | Selected chart point time; used for freshness. It is not guaranteed to identify a raw physical measurement. |
| Chart `last` | Separate server envelope timestamp; its precise server meaning is not assumed. |
| Local last-poll timestamp | Start of an actual HTTP read attempt; never substituted for either server timestamp. |

Explicit time-zone offsets are preserved. Offset-free timestamps currently use the explicit `Europe/Moscow` assumption; this is not a documented universal TelePool rule. Screen-HTML dates are not used for freshness.

## Native form writes

The client parses the exact `forModalForm`, validates its HTTPS action and station/account/pool binding, and requires an ordinary command context. Disabled controls are excluded; ordered duplicate `__Invariant` values and normal checkbox/hidden pairs are preserved. Unknown successful controls, ambiguous duplicates and unsupported sensitive controls cause rejection.

Only one requested field is changed. `btnId` is forced to `Empty`; dosing, calibration, reset, Wi-Fi, schedule and service commands are not submitted as operations. Other successful settings remain unchanged, including values the server may consider invalid.

`SetPointTemp` is editable only with a finite current value, valid numeric bounds and a positive step. The client additionally restricts native edits to the implemented **5–35 °C** envelope and requires alignment with the live form's step. This envelope is a client compatibility constraint, not a claim about every Q3 model. The integration's broader 38 °C pool cap cannot override a lower form/client limit.

`MenuEnableFu` is read as a boolean only for recognized values and is writable only when the corresponding form control is editable. It represents the raw FU menu-control setting. It does not provide electrical-power feedback or a verified pump-running signal; `modeFu` is a separate reported mode.

Writes are serialized. Each operation obtains a fresh form; an already-matching value requires no POST. An authentication-generation check and optional synchronous dispatch guard run under the authentication lock immediately before dispatch. A denied guard raises `CommandNotSent`, proving that no settings POST was sent.

A dispatched settings POST is attempted once, without automatic redirect following, login recovery or replay. A subsequent form GET must show the requested value before the client returns success. This confirms server-side configuration only.

## Rejections and unknown outcomes

`SettingsRejected` has bounded reason codes: `http_400`, `http_403`, `access_denied` and `validation_error`. Known ASP.NET field-validation markers can identify up to eight schema-approved field names. The integration's UI/log boundary filters those names again and exposes only reviewed static messages; field values, raw validation text, response bodies and exception context are excluded.

A valid temperature request can be rejected because another unchanged field in the complete form is invalid. The integration reports that condition without correcting unrelated settings or retrying the POST.

A timeout, unrecognized response, unclassified summary notice or failed/mismatching read-back produces `WriteOutcomeUnknown`. An HTTP 200 response alone is not proof of success. Unknown outcomes are retained for later read confirmation and are not automatically resubmitted.

See [Architecture](architecture.md#native-q3-controls) for the durable `queued` / `sending` / `unknown` / `blocked` lifecycle and backend isolation.
