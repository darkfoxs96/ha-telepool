"""Use the Home Assistant harness without real device or internet calls."""

import pytest


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    yield
