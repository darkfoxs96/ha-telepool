# Architecture

This community integration connects Home Assistant to [TelePool](https://telepool.ru/), the cloud service used by pool controllers from [Laboratoriya Vostrotina](https://vostrotin.ru/). The tested controller is Maestro Q3 Duet pH/Rx. Discovery, telemetry parsing, and native form handling currently implement the Q3 protocol. Other TelePool controllers may share compatible data, but have not been tested and are not assumed to support these paths or controls.

Each config entry represents one station. The integration reads telemetry and supports two narrowly scoped native settings; optional external controls delegate to entities already present in Home Assistant.

For endpoint and form details, see [Protocol reference](protocol.md). For development and reproducible checks, see [Testing and development](testing.md).

## Components

| Component | Responsibility |
| --- | --- |
| `client/` | Independent asynchronous HTTPS client, authentication, station discovery, telemetry parsing and validated Q3 form submissions. |
| `config_flow.py` | Account setup, station selection, reauthentication and control-source options. |
| `coordinator.py`, `availability.py` | Polling, telemetry cache, measurement freshness and external power-state policy. |
| `sensor.py`, `binary_sensor.py` | Readings, timestamps, connection status and freshness/error indicators. |
| `climate.py`, `switch.py`, `proxy.py` | External HA control proxies and platform routing. |
| `native_climate.py`, `native_switch.py`, `native_control.py` | Native Q3 entities and durable temperature intentions. |
| `credential_store.py`, `storage.py`, `climate_cache.py` | Encrypted credentials, acknowledged private storage writes and source-bound external climate cache. |

Each entry has a dedicated HTTP session and cookie jar. An initial cloud failure does not prevent independent external proxies from being registered. The client uses the TelePool cloud; no local-station API or LAN discovery is implemented.

## Selecting a control backend

`thermostat_mode` and `power_mode` independently select `disabled`, `native_q3` or `external`. The native switch is a Q3 menu setting; the external switch represents an existing power relay. These meanings must not be conflated.

New entries default to native Q3 controls. Selecting that mode does not create a command. Startup can resume an eligible, previously saved temperature intention under the power/freshness rules below. Existing explicit options are preserved. For legacy entries without a mode, an existing external source selects `external`; otherwise the control remains disabled.

External sources are tracked by entity-registry identity where available, so renaming an entity does not change its destination. TelePool entities cannot be selected as their own sources. Inactive source options are removed when options are saved. The logical station device and public climate/switch unique IDs remain stable across backend changes; ecosystem-specific pairing behavior remains outside this identity guarantee.

## Telemetry and availability

Freshness uses the selected chart point's `visit`, its age, server stale flags and the outcome of the cloud read. The chart envelope's `last` and the local polling time are separate values. The connection-status sensor describes data availability, not pump or heater activity.

| Condition | Display readings | Freshness |
| --- | --- | --- |
| Confirmed external power OFF with cached values | Retained; `paused` | False |
| External power ON during startup grace | Retained if cached; `starting` | False until a newer valid point arrives |
| Valid, sufficiently recent cloud point | Available; `working` | True |
| Startup grace expires without a new point | Unavailable; `no_connection` | False |
| No confirmed power pause and data is stale, missing or the cloud read fails | Unavailable | False |

A confirmed OFF requires a valid external source state; unknown, unavailable, restored or disabled states do not establish a power pause. With a telemetry cache and confirmed OFF, normal cloud polling is skipped. A native `MenuEnableFu=false` does not represent an electrical power cut and does not activate this policy.

The coordinator retains original sample times across restarts. Thirty-second availability notifications do not reset the configured polling interval. The last-poll timestamp records a real local read attempt, including a failed attempt; a skipped OFF poll does not advance it.

Display sensors can retain values during a confirmed pause and have no statistics state class. Optional measurement sensors report only fresh data and are disabled by default. Optional pH/Rx export aliases deliberately use temperature metadata for compatibility with other ecosystems; Rx is scaled by 0.1. They are read-only representations, not water temperatures.

## External controls

External climate and switch entities mirror their selected HA sources and forward explicit commands using HA services. They remain independent of TelePool cloud availability. There is no second thermostat regulator, fallback source selection or integration-owned schedule.

Temperature commands are limited to the lower of the source's maximum and `max_target_temperature`, converted to the appropriate units. The option defaults to 35 °C and accepts 5–40 °C in 0.5 °C steps. The source minimum and supported scalar/range features are respected. An incompatible range makes the proxy unavailable. The limit applies only to the TelePool proxy; direct commands to the source are outside it. Measured temperatures and already reported targets are not clamped or changed automatically.

`climate_power_linked` is an explicit option for an external thermostat and external relay that share a power supply. During confirmed OFF, a validated snapshot retains temperatures, mode and capabilities. Only the latest temperature target can be stored durably; mode and on/off actions are not queued. Startup grace can retain the display after ON, but new commands are rejected until the source reconnects.

Recovery requires a valid source report after power restoration, including an unchanged-value `state_reported` event. The pending target retains its original unit and source identity and is revalidated against the current configured and source limits. A target above a newly lowered limit remains blocked rather than being sent or silently reduced. After successful HA service completion it is cleared; failures retain it for a later source event without a tight retry loop. Command serialization prevents an older recovery flush from overtaking a newer user target. A crash can cause an idempotent external setpoint to be delivered again; this is not an exactly-once guarantee.

## Native Q3 controls

The native thermostat exposes `SetPointTemp` and reads current temperature only from Q3 telemetry. A missing probe leaves current temperature unknown; a known target can still be displayed. Its advertised maximum is the configured `max_target_temperature`. A valid request within that limit is submitted even when it exceeds the preflight form's advertised maximum; the old form does not establish the server's present acceptance range. The observed form advertises 5–35 °C in 0.5 °C steps, without a model-specific hardcoded temperature envelope. See [Protocol reference](protocol.md#native-form-writes).

Only a confirmed temperature range rejection carrying valid numeric minimum, maximum and step permits one replacement command. Its target is the highest step-aligned value within both the returned range and the current configured cap. The validated rejection's range and grid override stale HTML metadata, while a fresh form still validates routing and preserves unrelated fields. The reported range and confirmed replacement are shown to the user. The displayed target changes only after server read-back confirms it. Authentication failures, transport failures, ambiguous outcomes, generic rejections, other invalid fields and malformed range data do not authorize a replacement. A failed replacement is not retried. Its durable `sending` marker is saved before any further await, so a crash cannot restore the correction as an ordinary queued command eligible for another replacement.

The single `HEAT` HVAC mode represents the temperature-target function. No heating action, HVAC enable/disable command or physical heater state is inferred.

The native switch exposes the editable `MenuEnableFu` flag as **Q3 FU menu control**. It is not a station power switch or pump-feedback sensor. The flag alone does not establish physical pump/heater operation, a forced start outside the schedule, or a protective interlock. It is available only with fresh Q3 data and an editable control form; on/off requests are never queued.

A native temperature intention is addressed to the configured Q3, not an external HA thermostat. It can be queued only during confirmed external power OFF with valid cached controls. After ON, automatic delivery requires a newer fresh Q3 point and a new control-form read. Unknown power or an ordinary cloud outage does not authorize offline queueing. A pending target above a newly lowered user limit remains blocked until corrected; the preflight form's advertised maximum alone does not block a valid user request.

Before native dispatch, a durable `sending` marker is written. The client then rechecks dispatch conditions after form preflight and immediately before POST. A proven unsent request is distinguished from an unknown outcome. Ambiguous POSTs and restored `sending` markers are not automatically replayed; later reads can confirm the target, otherwise another explicit request is needed. Native-mode transitions rotate the cache generation so old pending intentions cannot reappear after switching backends.

## Storage and boundaries

Credentials are encrypted with a separate local Fernet key. Restoring access requires the matching key and config entry; a missing key is not silently replaced. A backup containing both can decrypt the credentials. Entity names, ordinary HA history and telemetry are not encrypted by this mechanism.

`StrictStore` propagates storage failures that HA's standard Store can suppress. A pending intention is not acknowledged as durable when its write fails. The adapter uses a private HA storage hook, so it must be reviewed when updating Home Assistant.

The integration does not provide dosing, chemical-threshold editing, calibration, schedules, Wi-Fi settings, firmware updates or service commands. Native writes preserve unrelated fields in the current website form. Unrelated invalid fields can therefore cause server rejection; they are reported safely and are never repaired automatically. Server read-back confirms the website setting, not physical actuator behavior.
