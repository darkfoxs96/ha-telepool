from types import SimpleNamespace

from custom_components.telepool.diagnostics import async_get_config_entry_diagnostics


async def test_diagnostics_does_not_serialize_private_configuration():
    coordinator = SimpleNamespace(
        snapshot=SimpleNamespace(stale=True),
        availability=SimpleNamespace(status="paused", fresh=False),
        cloud_error="authentication",
        policy=SimpleNamespace(power="off"),
    )
    entry = SimpleNamespace(
        data={"credentials": "PRIVATE-CIPHERTEXT", "station": {"user_id": "PRIVATE-ACCOUNT"}},
        options={"climate_source": "climate.PRIVATE-SOURCE"},
        runtime_data=SimpleNamespace(
            coordinator=coordinator,
            station=SimpleNamespace(firmware="3.3.0", screen_type=7, id="PRIVATE-SERIAL", name="PRIVATE-POOL"),
        ),
    )
    result = await async_get_config_entry_diagnostics(None, entry)
    assert result["status"] == "paused"
    assert "PRIVATE" not in str(result)
