# Testing and development

The development environment pins Home Assistant and its official pytest helpers in [`pyproject.toml`](../pyproject.toml) and `uv.lock`. It requires Python 3.14.2 or newer. The optional Dev Container installs the same dependency set.

## Automated checks

GitHub Actions runs the test suite, Ruff linting and formatting checks on pushes and pull requests. Separate **HACS** and **hassfest** workflows validate the integration against the catalog and Home Assistant requirements without skipping checks. These two workflows also run daily and can be started manually from the repository's Actions tab. They use only the workflow's read-only GitHub token; no TelePool credentials are needed.

From the repository root:

```sh
uv sync --frozen --group dev
uv run ruff check custom_components tests scripts
uv run ruff format --check custom_components tests scripts
uv run pytest
```

The suite uses mocked network responses, synthetic station data and temporary HA storage. It does not require account credentials, a live Q3, real relays or HomeKit pairing. Do not turn fixture tests into live-device tests.

Focused checks are useful while changing a particular boundary:

```sh
uv run pytest tests/test_client.py tests/test_parsers.py
uv run pytest tests/test_config_flow.py tests/test_credentials.py tests/test_storage.py
uv run pytest tests/test_setup.py tests/test_polling.py tests/test_coordinator_edges.py
uv run pytest tests/test_proxies.py tests/test_climate_pause.py tests/test_climate_cache.py
uv run pytest tests/test_native_controls.py tests/test_native_control_manager.py
uv run pytest tests/test_homekit_mdns.py
```

Coverage includes authentication recovery, redirect restrictions, malformed/empty telemetry, station identity, missing probes, credential/key persistence, options and reauthentication, setup/unload, clock-driven polling, cached power pauses, source renames, unit conversion and command limits.

Native-control tests must preserve the distinction between no POST, a confirmed rejection and an unknown outcome. They cover preflight guards, durable sending markers, restart without ambiguous replay, changed form limits, step alignment, safe rejection diagnostics and queue isolation between backends. Successful mocked writes do not establish physical pump or heater behavior.

When updating Home Assistant, review the `StrictStore` private storage hook and the climate native-temperature API. Verify published units, bounds and step through the real HA service path; direct property tests alone do not catch display-unit conversion errors.

## Optional local Docker scaffold

The repository includes an isolated HA configuration with software-only Demo entities. Start it with Docker Compose:

```sh
sh scripts/start-dev.sh
docker compose exec -T homeassistant python -m homeassistant --script check_config --config /config
```

Open [localhost:8123](http://localhost:8123) and complete HA onboarding. The helper script copies [`dev/configuration.yaml`](../dev/configuration.yaml) and [`dev/telepool-test.yaml`](../dev/telepool-test.yaml) into ignored `dev/config/` only when the destination files do not exist. Existing local settings are not overwritten by subsequent starts. Keep credentials, HA storage, recordings and pairing state in that ignored directory rather than committing them.

The dashboard at `/telepool-test/pool` contains:

- `climate.telepool_demo_thermostat`: a Generic Thermostat controlling only the Demo heater.
- `switch.telepool_demo_power`: a software power-state signal.
- `switch.telepool_demo_heater`: the virtual heater state.
- `input_number.telepool_demo_temperature`: manually controlled water temperature.

The Demo dashboard itself needs no TelePool account. To test the integration without cloud credentials, use the automated fixtures. If an account-backed entry is used with the Demo sources, select `external` for both controls before testing their commands; native control modes address Q3 instead.

For an external shared-power pause test, select the Demo thermostat and power switch, enable `climate_power_linked`, and change the target through the **TelePool proxy**, not the Demo source. Confirm that OFF retains the last reading and latest pending target, and ON applies the target only after a new source report. The scaffold publishes a Demo thermostat report three seconds after simulated power restoration.

Demo Power does not make Generic Thermostat unavailable or simulate a real power cut. Water temperature changes only through the input slider; there is no thermal model. A thermostat mode change may switch the Demo heater, which remains an HA software entity. For explicit missing-source and grace-expiry cases, use the automated tests rather than interpreting this scaffold as hardware feedback.

## Optional macOS HomeKit discovery helper

The development Compose file maps HAP TCP port 21063 to loopback by default. For a deliberately LAN-accessible test bridge, set `TELEPOOL_HOMEKIT_BIND_IP` to the Mac's LAN IPv4 address before creating the container and configure a HomeKit Bridge in the ignored local HA configuration with matching `advertise_ip` and an explicit entity allowlist. Keep pairing information out of documentation and fixtures.

The bundled helper supports a bridge whose instance name starts with `TelePool Test` and whose port is 21063. Once that bridge is running inside the container:

```sh
uv run python scripts/mac-homekit-mdns.py --address "$TELEPOOL_HOMEKIT_BIND_IP" --interface en0
```

Use the actual LAN interface if it is not `en0`. The helper discovers the bridge's real public HAP identity/TXT inside Docker and publishes a native Bonjour proxy at the explicit Mac address. It does not implement a HomeKit server, read pairing secrets or issue device commands. Stop it with Ctrl-C to remove its announcement.

Docker Desktop multicast isolation may require this announcement path even when the TCP mapping works. The helper has a bounded retry budget; if it exits after container restarts, start it again once HA is ready. Restarting this helper does not require resetting HomeKit pairing.

For bridge discovery details, see the [Home Assistant HomeKit documentation](https://www.home-assistant.io/integrations/homekit/#docker-network-isolation). Availability in Bonjour and a reachable TCP port do not by themselves verify ecosystem pairing or physical device operation.

## Matter ecosystem checks

Aqara Home with an M200 controller has been checked through Home Assistant Matter Hub (HAMH) 2.0.59: pH/Rx temperature aliases appeared, and temperature and switch commands reached virtual HA sources through the TelePool proxies. This verifies the export path, not physical Q3 pump or heater operation.

For repeatable checks:

1. Select the Demo thermostat and Demo power switch in TelePool's external control modes. Verify both proxy entities' `source_entity_id` attributes before exporting writable entities.
2. Include only the two TelePool proxies and the pH/Rx export aliases in the Matter bridge. Check local IPv6 and mDNS on the actual LAN interface; IPv6 on a VPN interface alone is insufficient.
3. Pair the bridge with the controller, then change the target and toggle power from the receiving app. Confirm that the TelePool proxies and their Demo sources agree. Check that Rx is displayed as Rx÷10 and that pH retains its numeric value, subject to the receiving app's rounding.
4. Check linked ecosystems for additional accessories. One successful setup without duplicates does not establish an automatic re-export or deduplication contract.

Keep the existing bridge identity and pairing when adding entities. Controller names may be cached independently of HA names or the Q3 model name.

HAMH 2.0.59 substitutes a thermostat's target for its current temperature when the latter is missing. A native Q3 thermostat without a temperature probe therefore cannot verify accurate current-temperature export through this version. Use a source with a valid measurement for the interoperability test. Native Q3 also exposes a fixed heating mode; a receiving app offering an Off mode does not establish that Q3 supports it.

## Yandex Alice checks

Home with Alice has been checked through Yandex Smart Home 1.1.3 and Yaha Cloud: all four TelePool entities appeared, and thermostat target changes and switch control worked. The Rx alias is named **RX** for Alice; its Celsius value still needs to be multiplied by ten to obtain mV. See the [Alice setup instructions](../README.md#yandex-alice).

## Reporting a problem

Include the integration/HA versions, selected control backends, relevant status/freshness indicators, local last-poll time and sanitized integration diagnostics. Distinguish an unchanged entity state from an absent HTTP poll: HA activity/history records state changes, not every successful request.

For native rejection reports, include only the bounded reason code and reviewed invalid-field names shown by the integration. Do not attach credentials, cookies, CSRF tokens, raw cabinet HTML, HAR files or unredacted URLs. A server-form read-back is evidence of a stored setting, not evidence that a pump or heater physically changed state.
