"""Coordinator that keeps one vehicle's state up to date."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api.client import EudaClient
from .api.dataset import Dataset, DatasetFile
from .api.dictionary import DataDictionary
from .api.exception import (
    EudaActionRequiredError,
    EudaAuthError,
    EudaError,
    EudaNoDataError,
)
from .const import (
    CONF_IDENTIFIER,
    CONF_VIN,
    DOMAIN,
    INITIAL_DATASETS,
    MAX_FILE_ATTEMPTS,
    MAX_TRANSIENT_FAILURES,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

_EPOCH = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class VehicleData:
    """The vehicle's state: every dataset so far, merged in order."""

    #: The newest delivery file merged into ``dataset``.
    file: DatasetFile
    dataset: Dataset


#: Config entry whose ``runtime_data`` is the vehicle's coordinator.
type EudaConfigEntry = ConfigEntry[EudaCoordinator]


def _created(file: DatasetFile) -> datetime:
    return file.created or _EPOCH


class EudaCoordinator(DataUpdateCoordinator[VehicleData]):
    """Poll the delivery listing and merge each new dataset once."""

    config_entry: EudaConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: EudaConfigEntry,
        client: EudaClient,
        dictionary: DataDictionary,
    ) -> None:
        """Initialise the coordinator for the entry's vehicle."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN} {config_entry.title}",
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self.dictionary = dictionary
        self.vin: str = config_entry.data[CONF_VIN]
        self._transient_failures = 0
        #: Failed download attempts per delivery file name.
        self._file_failures: dict[str, int] = {}

    @property
    def identifier(self) -> str:
        """The identifier of the vehicle's continuous data request."""
        return self.config_entry.data[CONF_IDENTIFIER]

    async def _async_update_data(self) -> VehicleData:
        try:
            data = await self._async_fetch()
        except EudaAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except EudaActionRequiredError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="action_required"
            ) from err
        except EudaNoDataError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="no_data"
            ) from err
        except EudaError as err:
            self._transient_failures += 1
            if (
                err.is_transient
                and self.data is not None
                and self._transient_failures < MAX_TRANSIENT_FAILURES
            ):
                _LOGGER.debug("Keeping the previous state after: %s", err)
                return self.data
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err

        self._transient_failures = 0
        return data

    async def _async_fetch(self) -> VehicleData:
        files = await self._async_list_content()
        # A data request that was deleted and set up again on the portal gets
        # a new identifier, and the old one stops listing anything.
        if not files and await self._async_refresh_identifier():
            files = await self._async_list_content()

        if not files:
            if self.data is not None:
                return self.data
            raise EudaNoDataError("The portal has not delivered any data yet")

        # Forget failures of files that have rotated out of the listing.
        listed = {file.name for file in files}
        self._file_failures = {
            name: count for name, count in self._file_failures.items() if name in listed
        }

        pending = self._pending(files)
        if not pending:
            if self.data is not None:
                return self.data
            raise EudaNoDataError("None of the delivered datasets could be read")

        newest = self.data.file if self.data is not None else None
        dataset = self.data.dataset if self.data is not None else None
        error: EudaError | None = None
        for file in pending:
            try:
                received = await self.client.download_dataset(
                    self.vin, self.identifier, file
                )
            except (EudaAuthError, EudaActionRequiredError):
                raise
            except EudaError as err:
                error = err
                self._record_failure(file, err)
                continue
            dataset = received if dataset is None else dataset.merged_with(received)
            newest = file

        if dataset is None or newest is None:
            # Nothing could be read and there is no earlier state to show.
            assert error is not None
            raise error
        # Files that failed are retried on the next poll, up to a limit; the
        # state merged so far stays current meanwhile.
        return VehicleData(file=newest, dataset=dataset)

    async def _async_list_content(self) -> list[DatasetFile]:
        """Return the delivery files that hold data, oldest first."""
        files = await self.client.list_datasets(self.vin, self.identifier)
        return sorted((file for file in files if file.has_content), key=_created)

    def _pending(self, files: list[DatasetFile]) -> list[DatasetFile]:
        """Return the files to download and merge, oldest first."""
        if self.data is None:
            candidates = files[-INITIAL_DATASETS:]
        else:
            merged = _created(self.data.file)
            candidates = [file for file in files if _created(file) > merged]
        return [
            file
            for file in candidates
            if self._file_failures.get(file.name, 0) < MAX_FILE_ATTEMPTS
        ]

    def _record_failure(self, file: DatasetFile, err: EudaError) -> None:
        attempts = self._file_failures.get(file.name, 0) + 1
        self._file_failures[file.name] = attempts
        if attempts >= MAX_FILE_ATTEMPTS:
            _LOGGER.warning(
                "Skipping the dataset delivered at %s after %d failed attempts: %s",
                file.created,
                attempts,
                err,
            )
        else:
            _LOGGER.debug(
                "Could not read the dataset delivered at %s: %s", file.created, err
            )

    async def _async_refresh_identifier(self) -> bool:
        """Look the data request up again; return whether it changed."""
        identifier = await self.client.get_request_identifier(self.vin)
        if identifier == self.identifier:
            return False
        _LOGGER.info("The data request was replaced on the portal; following it")
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_IDENTIFIER: identifier},
        )
        return True
