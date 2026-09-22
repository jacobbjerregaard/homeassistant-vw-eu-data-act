"""Fixtures for the Home Assistant integration tests.

These run only when Home Assistant and pytest-homeassistant-custom-component
are installed. The portal client is replaced by an in-memory fake, so the
config flow, coordinator and sensor platform run for real against canned
datasets.
"""

from contextlib import ExitStack
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vwg_eu_data_act.api.dataset import Dataset, DatasetFile
from custom_components.vwg_eu_data_act.api.exception import EudaNoDataError
from custom_components.vwg_eu_data_act.const import (
    CONF_BRAND,
    CONF_IDENTIFIER,
    CONF_NICKNAME,
    CONF_VIN,
    DOMAIN,
)
from tests.conftest import load_fixture

VIN = "WVWZZZE1ZTEST0001"
IDENTIFIER = "req-1"

ENTRY_DATA = {
    CONF_BRAND: "volkswagen",
    CONF_EMAIL: "owner@example.com",
    CONF_PASSWORD: "secret",
    CONF_VIN: VIN,
    CONF_NICKNAME: "ID.4",
    CONF_IDENTIFIER: IDENTIFIER,
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading the custom integration in every test."""
    yield


def delivery(name: str, minute: int) -> DatasetFile:
    """A delivery listing entry created at 10:<minute> UTC."""
    return DatasetFile(name, datetime(2026, 9, 20, 10, minute, tzinfo=UTC), 100)


class FakeClient:
    """Stands in for :class:`EudaClient`; tests adjust its attributes."""

    def __init__(self) -> None:
        self.vehicles: dict[str, str | None] = {VIN: "ID.4"}
        self.identifier = IDENTIFIER
        self.files = [delivery("a.zip", 0)]
        self.datasets = {"a.zip": Dataset.from_json(load_fixture("meb_dataset.json"))}
        self.login_error: Exception | None = None
        self.list_error: Exception | None = None
        self.identifier_error: Exception | None = None
        #: Errors to raise when downloading a file, by file name.
        self.download_errors: dict[str, Exception] = {}
        self.downloads: list[str] = []
        self.credentials: list[tuple] = []

    def __call__(self, session, brand, email, password):
        """Act as the class, so patching the constructor hands out this fake."""
        self.credentials.append((brand.key, email, password))
        return self

    async def login(self):
        if self.login_error:
            raise self.login_error

    async def list_vehicles(self):
        await self.login()
        return self.vehicles

    async def get_request_identifier(self, vin):
        if self.identifier_error:
            raise self.identifier_error
        if self.identifier is None:
            raise EudaNoDataError("no request")
        return self.identifier

    async def list_datasets(self, vin, identifier):
        if self.login_error:
            raise self.login_error
        if self.list_error:
            raise self.list_error
        return self.files

    async def download_dataset(self, vin, identifier, file):
        self.downloads.append(file.name)
        if error := self.download_errors.get(file.name):
            raise error
        return self.datasets[file.name]


@pytest.fixture
def client():
    """Patch client construction everywhere the integration does it."""
    fake = FakeClient()
    targets = (
        "custom_components.vwg_eu_data_act.EudaClient",
        "custom_components.vwg_eu_data_act.config_flow.EudaClient",
    )
    with ExitStack() as stack:
        for target in targets:
            stack.enter_context(patch(target, new=fake))
        yield fake


@pytest.fixture
def config_entry(hass):
    """A configured vehicle, not yet set up."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="ID.4", unique_id=VIN, data=dict(ENTRY_DATA)
    )
    entry.add_to_hass(hass)
    return entry
