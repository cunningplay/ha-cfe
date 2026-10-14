"""Home Assistant integration tests (config flow, polling, Store, statistics). Needs pytest-homeassistant-custom-component:
  pytest tests_ha -o asyncio_mode=auto   (README: Development)."""
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

pytest_plugins = ["pytest_homeassistant_custom_component"]

@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    yield
