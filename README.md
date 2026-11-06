# TelePool for Home Assistant — Laboratoriya Vostrotina

**English** | [Русский](README.ru.md)

Community integration for [TelePool](https://telepool.ru/), the cloud monitoring and control service used by pool controllers from [Laboratoriya Vostrotina (Лаборатория Востротина)](https://vostrotin.ru/). The main tested controller is [Maestro Q3 Duet pH/Rx (Маэстро Q3 Дуэт pH/Rx)](https://vostrotin.ru/catalog/stancii-dozirovaniia-dlia-basseinov-telepool/726-telepool-phrx-c-derzateliami-elektrodov-12).

View pool readings, adjust the Q3 temperature target, and access its FU menu control. You can also use a separate thermostat or station power relay already connected to Home Assistant.

Other controllers using TelePool may share a compatible protocol, but have not been tested because the equipment was not available. This release implements Q3 station discovery, data parsing, and controls.

Requires [Home Assistant **2026.11.0 or newer**](https://www.home-assistant.io/installation/), a TelePool account with a Q3 station, and internet access to **telepool.ru**. This integration uses TelePool's cloud website; a local connection to the station is not required. The website interface is undocumented and may change.

## Install

### Manual installation

Use this method with the project folder or an archive downloaded from the [project repository](https://github.com/darkfoxs96/ha-telepool).

1. Find your [Home Assistant configuration directory](https://www.home-assistant.io/docs/configuration/#to-find-the-configuration-directory), which contains `configuration.yaml`. On Home Assistant OS this is `/config`; with Container, use the host directory mounted at `/config`.
2. Create a `custom_components` directory there if needed.
3. Copy this project's entire `custom_components/telepool` folder into it. The resulting path must be `<config>/custom_components/telepool/manifest.json`, alongside the integration's other files.
4. Fully restart Home Assistant.

No TelePool YAML configuration or manual installation of Python dependencies is needed.

### Marketplace

Use Home Assistant's built-in Marketplace:

1. Open **Settings → Marketplace** and connect GitHub if prompted, using the on-screen sign-in instructions.
2. Choose **Add from link** and paste `https://github.com/darkfoxs96/ha-telepool`.
3. Add the repository and install TelePool; choose **Integration** if asked for its type.
4. Restart Home Assistant, then connect your account below.

Adding the repository by link does not require inclusion in the default catalog. See the [Marketplace custom repository instructions](https://www.home-assistant.io/integrations/marketplace/#custom-repositories).

## Connect your station

1. Open **Settings → Devices & services → Add integration** and search for **TelePool**.
2. Enter your **TelePool** username and password.
3. Select your Q3 from the stations found in that account.
4. Open the station's device page to view its readings and controls, or add them to a dashboard.

Your TelePool username and password are encrypted before being saved in Home Assistant. They remain stored in encrypted form across restarts, so you do not need to enter them again each time Home Assistant starts. The integration uses the saved credentials to sign in automatically. See [Updates and backups](#updates-and-backups) for encryption key storage and migration.

Add a separate TelePool integration entry for each station. No Home Assistant cloud subscription is required. If your TelePool credentials stop working, Home Assistant will ask you to authenticate again.

New entries use **Q3 thermostat** and **Q3 FU menu control** by default. Selecting a mode exposes the control; it does not send a command by itself. You can disable either control and keep reading the station.

## Readings and controls

The integration provides pH, Rx/ORP in mV, Q3 water temperature when available, read-only chemical setpoints, controller mode codes, Wi-Fi information, freshness diagnostics, and a general station error flag. Mode codes describe Q3's operating modes; connection status describes telemetry availability. The general error flag does not identify the cause of a fault.

**Q3 thermostat** changes the cloud temperature target. The current form supports **5–35 °C in 0.5 °C steps**. The integration also applies an overall **38 °C** target ceiling, while preserving a lower limit from Q3 or an external thermostat; it does not extend Q3's 35 °C range. Without Q3's temperature probe, current water temperature stays unknown, but an editable target can still be controlled. The native thermostat's Heat mode describes its temperature-setting function; there is no separate native HVAC off command or inferred heating activity.

**Q3 FU menu control** changes TelePool's `MenuEnableFu` setting for the filtration unit (FU). The switch displays this setting, and changes are reflected in TelePool's interface. Q3 remains powered and continues reporting telemetry. Use an external power relay to disconnect the station's electrical supply.

**External controls** show an existing Home Assistant thermostat or relay and forward your commands to its standard `climate` or `switch` entity. Add that device through its own integration first. These controls work independently of the TelePool cloud. Source state changes do not send commands back to the source.

Temperature and power modes can be chosen independently. To return from external devices to native controls, select **Q3 thermostat** and **Q3 FU menu control** in TelePool's options. The external thermostat's temperature appears in **Water**; the separate **Q3 water temperature** sensor still belongs to Q3's own probe.

Chemical settings, dosing, calibration, schedules, firmware updates, restart, backwash, and other service commands are not exposed. TelePool saves a full settings form even for a temperature change: invalid existing fields elsewhere in that form can block the request. The integration reports rejected field names and does not automatically correct unrelated settings or retry a rejected write. Review those fields in TelePool before trying a new command.

## Options

Open **Settings → Devices & services → TelePool → Configure** for the station.

| Field | Default / allowed values | Purpose |
| --- | --- | --- |
| **Polling interval (seconds)** | **300**; 60–3600 | Automatic cloud read interval. A shorter interval creates more requests. Confirmed external power OFF with cached readings pauses polling. |
| **Data freshness limit (seconds)** | **900**; 120–7200 | Maximum age of a fresh chart point. Cloud errors and server stale flags also affect availability. This setting does not change the polling interval. |
| **Startup grace period (seconds)** | **180**; 0–900 | Time to wait for fresh data after the external relay turns on. Last readings may remain visible during this period. It also applies to an external thermostat when shared power is enabled. |
| **Pool temperature control** | **Q3 thermostat**; Disabled / External pool thermostat | Choose Q3's own target, a separate thermostat, or no temperature control. |
| **External pool thermostat** | Empty | Required only with External pool thermostat mode. Select the existing thermostat to display and control through TelePool. Ignored in other modes. |
| **FU menu / power control** | **Q3 FU menu control**; Disabled / External station power relay | Choose the Q3 menu setting, a relay that actually cuts power to Q3, or no switch control. These modes have different physical meanings. |
| **External station power relay** | Empty | Required only with External station power relay mode. Select the existing relay that powers Q3. Its OFF state enables the expected-power pause described below. Ignored in other modes. |
| **Create temperature aliases for pH/Rx export** | **Off** | Add read-only pH and Rx÷10 temperature representations for Apple Home or other bridges. The original sensors keep their proper types and units. |
| **External relay also powers the thermostat** | **Off** | Use only with both external modes selected, when the same relay cuts power to Q3 and the external thermostat. Enables retained thermostat readings and a queued target during that power cut. |

Leaving an external device field empty never switches to Q3 automatically. External mode requires a source; disabled/native modes discard the corresponding saved external selection. Thermostat and switch modes can be chosen independently. TelePool's own entities cannot be sources, and changing a mode or source does not transfer pending commands to another device.

## Pauses, freshness, and queued targets

**Station connection** describes data freshness and expected power:

| Status | Meaning |
| --- | --- |
| **Connected** | Recent data was received without a stale indication. |
| **Paused** | The selected external power relay is OFF. Last known readings remain available with their original times. |
| **Starting** | External power has returned; the integration is waiting for fresh readings within the startup grace period. |
| **No connection** | Data is missing, stale, or could not be read after any startup grace period. Main readings become unavailable. |

No cached value means no invented reading. Switching the native FU setting OFF does not count as cutting Q3's power and does not pause cloud polling. If a mechanical power switch is not connected to Home Assistant, the integration cannot distinguish an expected power cut from a connection failure.

With a **native Q3 thermostat and an external Q3 power relay**, a new target can be saved during confirmed relay OFF if valid control data was previously cached. Only the latest target is retained, including across HA restarts. After power returns, delivery waits for fresh Q3 data and a newly read control form.

For an **external thermostat**, this behavior requires **External relay also powers the thermostat** and both external sources. Its last temperature/target remain available during OFF; delivery waits for a new report from that thermostat after ON. Leave the option off if the thermostat has independent power. Native Q3 temperature control does not need this option.

Only temperature targets are queued; FU on/off and thermostat mode commands are not. An ordinary outage with power on or unknown does not enable offline queuing. A Q3 command whose result is unknown is not automatically repeated: check the reported value before making another explicit request.

### Polling and timestamps

The default **300-second** interval means roughly one automatic cycle every 5 minutes. Startup, manual refreshes, and control actions can cause additional reads. HA **Activity** records state changes, not every request, so unchanged pH/Rx values need not produce a new entry at each poll.

- **Last TelePool poll**: local start time of the latest actual polling attempt, including failed attempts.
- **Cloud timestamp**: TelePool's own timestamp, whose exact meaning is unverified; it is not the local polling time.
- **Measurement time**: the selected reading's time, used to assess freshness.

During confirmed external power OFF with cached readings, polling is skipped and **Last TelePool poll** stays unchanged.

Main readings keep normal HA history without generating long-term averages from held values. Optional diagnostic sensors named **pH — statistics**, **Rx / ORP — statistics**, and **Q3 water temperature — statistics** are disabled by default. Enable them for fresh-only statistics; they leave gaps during pauses. They use the same Q3 measurements and are not additional physical sensors or reagent-level sensors.

## Apple Home

Use Home Assistant's [HomeKit Bridge](https://www.home-assistant.io/integrations/homekit/) to publish the selected TelePool controls and readings to Apple's Home app.

1. Enable **Create temperature aliases for pH/Rx export** if you want chemical readings in Apple Home.
2. Add **HomeKit Bridge** under **Settings → Devices & services**, or configure an existing bridge. Include the TelePool thermostat/switch you use, **pH export alias**, **Rx export alias**, and optionally **Q3 water temperature**. With external devices, include the TelePool controls rather than also exporting their source devices, to avoid duplicates.
3. Keep the iPhone and Home Assistant bridge on the same home network. Open HA's **HomeKit Pairing** notification, then choose **+ → Add Accessory** in Apple Home and scan the QR code. Container installations may need additional network configuration; see the [HomeKit networking guidance](https://www.home-assistant.io/integrations/homekit/#docker-network-isolation).
4. Create a **Pool** room in Apple Home and assign each accessory to it. Suggested names are **Water**, **Pool power** or **Q3 FU menu**, **pH**, and **Rx ÷10**, matching the controls you selected.

These remain separate accessories grouped by room; assigning the room does not create a single pool accessory. Exporting a control does not change its TelePool mode or provide additional hardware capabilities.

Apple Home may keep an accessory's original name after pairing. If it still shows an older name, open the accessory's settings in Apple Home and rename it to **Water**. Room assignment and accessory names are separate settings.

### Reading pH and Rx aliases

Apple Home displays the aliases as temperature sensors, including degree units. They are read-only representations, not water temperatures. With a **Celsius** display:

| Original reading | Exported number before display rounding | Interpretation |
| --- | --- | --- |
| pH `7.23` | `7.23 °C` | Read directly as pH; no multiplication. |
| Rx `655 mV` | `65.5 °C` | Multiply the displayed number by **10** to obtain Rx in mV. |

Apple may round differently between screens: pH `7.23` might appear as `7.2`. Multiplication cannot recover rounded-away digits; use the original HA sensors for precise readings. If Apple displays Fahrenheit, convert back to Celsius before interpreting either alias, or use Celsius for these displays.

### Excluding pool values from home summaries

The chemical aliases look like thermometers to Apple Home, and pool water also differs from indoor air temperature. To keep these values out of the home's climate summary, repeat the following for **each pH alias, Rx alias, pool thermostat, and separately exported water temperature sensor**:

1. Open the accessory's settings in Apple Home.
2. Open **Status** or **Status and Notifications**, depending on the accessory and OS version.
3. Turn off **Include in Home Summaries**.

This does not hide, delete, or unpair the accessories. They remain accessible in the Pool room. Room assignment and participation in summaries are separate settings. See [Apple's accessory status instructions](https://support.apple.com/en-au/105042).

## Aqara Home

Install the separately maintained community project [Home Assistant Matter Hub (HAMH)](https://riddix.github.io/home-assistant-matter-hub/getting-started/installation). Home Assistant's built-in [Matter integration is a controller, not an exporter](https://www.home-assistant.io/integrations/matter/#using-a-matter-bridge), so installing TelePool alone does not publish its entities to Aqara.

Use an **Aqara hub with Matter Controller functionality that supports third-party Matter bridges** and the exported device types. The controller role imports devices into Aqara Home; the bridge role exports Aqara devices to other platforms. See [Aqara's Matter capabilities](https://www.aqara.com/us/explore/everything-matter/).

1. Enable **Create temperature aliases for pH/Rx export**, then create a HAMH bridge including the TelePool thermostat, switch, **pH export alias**, and **Rx export alias**. You can start with just pH to check connectivity, then add the other three.
2. Keep the phone, compatible Aqara hub, and HAMH host on the same home LAN. Allow local IPv6 and mDNS; an IPv6 internet connection is unnecessary. See [Aqara's connection checks](https://store-support.aqara.com/blogs/guide/troubleshooting-matter-devices-with-aqara).
3. In Aqara Home, choose **Add Matter Device**, scan the **HAMH bridge's Matter QR code**, and select your compatible Aqara controller if asked.

The setup was tested with an **Aqara Hub M200**: **pH and Rx readings**, **thermostat target changes**, and **switch control** were verified.

Aqara may initially name accessories **TelePool Q3** from their model. You can rename the devices in Aqara Home. The aliases remain temperature sensors: with Celsius display, read pH directly and multiply Rx by **10**. See [alias units and rounding](#reading-ph-and-rx-aliases).

With no Q3 probe, **HAMH 2.0.59 may display the target as current temperature**; use an external thermostat with a valid measurement if needed. See [Matter limitations](docs/testing.md#matter-ecosystem-checks).

The tested setup created no automatic duplicate entries in Apple Home or Alice. Other configurations may behave differently. Keep one export path per platform, and check the resulting devices before adding another path.

### Pool dashboard

Assign the accessories to a **Pool** room, then create a separate **Pool** dashboard and add their cards. The room groups devices; the dashboard lets you arrange their readings and controls together.

**Edit each card's title and icon separately from the device name.** In the tested Aqara app, new cards used template titles such as **Thermometer**, **Thermostat**, or **Socket/Switch**, even after the devices had been renamed.

| Card | Suggested title | Icon idea |
| --- | --- | --- |
| pH alias thermometer | **pH** | A distinct icon for the pH reading |
| Rx alias thermometer | **Rx ÷10** | A different icon for the Rx reading |
| Pool thermostat | **Water** | Water drop |
| External power switch | **Pool power** | Pump or equipment |

For native Q3 FU control, use **Q3 FU menu** for the last card. Place pH and Rx together, with Water and the switch beside them. Optionally add the hub or Matter bridge card to see that hub or bridge's online status.

Icons are decorative; the pH/Rx cards still show degree units and use the conversion described above. Changing a card's title or icon does not change the device type or reading.

## Yandex Alice

Use the community integration [Yandex Smart Home](https://github.com/dext0r/yandex_smart_home) to export selected TelePool entities to **Home with Alice**. Its cloud connection uses **Yaha Cloud**, a separate community service.

1. Follow the project's [installation guide](https://docs.yaha-cloud.ru/v1.1.x/install/component/): install **Yandex Smart Home**, restart HA, add the integration, and choose **Cloud** (**«Облачное»**).
2. Enable TelePool's temperature aliases and select only its **thermostat**, **switch**, **pH export alias**, and **Rx export alias** for export. Display of all four entities, thermostat target changes, and switch control have been verified in Home with Alice. See [entity selection](https://docs.yaha-cloud.ru/v1.1.x/config/filter/).
3. In HA, open **Yandex Smart Home → Configure → Linking credentials** (**«Реквизиты для привязки»**) and obtain the one-time linking code. On the phone, open **Home with Alice → + → Smart home device → Yaha Cloud**, link the account, and enter that code. See the project's [connection guide](https://docs.yaha-cloud.ru/v1.1.x/install/integration/).
4. Assign the devices to a **Pool** room and use names such as **Water**, **Pool power**, **pH**, and **RX**. For native FU control, use **FU menu** instead of Pool power.

If the one-time code expires, reopen **Linking credentials** to obtain a new one. Keep the existing integration entry.

For compatible device names in Alice, use **Latin or Cyrillic letters and spaces**, without digits or symbols such as `÷`. Name the Rx alias **RX**. To set a name only for Yandex export, use [`entity_config.<entity_id>.name`](https://docs.yaha-cloud.ru/v1.1.x/config/entity/#имя-и-комната); this leaves HA, Apple Home, and Aqara names unchanged. For an already added Rx card shown as **Climate sensor**, open its settings in Home with Alice and rename it to **RX**. There is no need to delete the device or relink Yaha Cloud. See [Yandex's renaming instructions](https://alice.yandex.ru/support/ru/smart-home/third-party/change-name).

The aliases remain read-only temperature displays with degree units: in Celsius, read pH directly and multiply the **RX reading by 10** to obtain Rx in mV. Renaming does not change the reading or conversion. An advanced alternative to Yaha Cloud is the project's [direct connection](https://docs.yaha-cloud.ru/v1.1.x/advanced/direct-connection/yandex/).

## Troubleshooting

| Problem | What to check |
| --- | --- |
| TelePool is missing from Add integration | Check `custom_components/telepool/manifest.json`, the HA version, and a full restart after installation. Then refresh the browser. |
| Sign-in fails or no station is found | Check sign-in at telepool.ru and access to Q3 with the same account. Complete reauthentication if HA requests it. |
| Readings are unavailable or stop updating | Check Q3 power, its TelePool connection, the selected external relay, and measurement time. A successful cloud request may return old data. |
| Q3 water temperature is unavailable | Check Q3's own probe. An external thermostat does not replace this separate sensor. |
| A command is rejected or its result is unknown | Check the temperature range/step, rejected fields in TelePool, and the reported target before sending another command. |
| Apple Home cannot find the bridge | Follow HomeKit Bridge's networking guidance, particularly for container installations. |

Diagnostics can be downloaded from the TelePool device page. They exclude credentials, cookies, account identifiers, and raw website responses.

## Updates and backups

For manual updates, replace `custom_components/telepool` with the new version; for Marketplace installations, use Marketplace. Fully restart Home Assistant after updating Python code, then refresh the browser. Reloading only the integration does not reload its Python modules. Existing station entries, credentials, and options are retained.

The username and password are encrypted before storage. The separate key is in `.storage/telepool.key`; session cookies stay in memory. To move an existing setup, restore a protected backup of the **whole HA configuration**, including both encrypted entries and their matching key. Losing the key makes those stored credentials unreadable. The key is stored on the same server, so this does not protect against access to the entire configuration; keep backups private.

Technical documentation: [architecture and limitations](docs/architecture.md), [protocol reference](docs/protocol.md), [testing](docs/testing.md).

Integration code is licensed under [MIT](LICENSE). Vendor logos and trademarks remain the property of their respective owners; see [artwork information](docs/assets/README.md).
