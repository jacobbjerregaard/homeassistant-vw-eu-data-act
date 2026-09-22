"""Check the integration's portal client and sensors against a real account.

Signs in, lists the account's vehicles, downloads the newest dataset of one
of them and shows what the integration would make of it. Everything it
fetches is written to ``datasets/`` (git-ignored), because a real dataset
holds the VIN and the account's user id.

The password is read from the terminal, or from ``EUDA_PASSWORD``; it is
never written anywhere.

Usage, from the repository root, with Home Assistant installed::

    python tools/live_check.py --brand volkswagen --email you@example.com
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import ssl
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import aiohttp
import certifi

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from custom_components.vwg_eu_data_act.api.brands import BRANDS  # noqa: E402
from custom_components.vwg_eu_data_act.api.client import (  # noqa: E402
    _LIST_PATH,
    EudaClient,
)
from custom_components.vwg_eu_data_act.api.dataset import (  # noqa: E402
    Dataset,
    DatasetFile,
)
from custom_components.vwg_eu_data_act.api.dictionary import (  # noqa: E402
    load_data_dictionary,
)
from custom_components.vwg_eu_data_act.api.exception import (  # noqa: E402
    EudaError,
    EudaNoDataError,
)
from custom_components.vwg_eu_data_act.const import INITIAL_DATASETS  # noqa: E402
from custom_components.vwg_eu_data_act.sensor import (  # noqa: E402
    _DICTIONARY_UNITS,
    _DISTANCE_UNITS,
    SENSORS,
    _find_field,
)

OUT = REPO / "datasets"


#: Pauses between attempts after a transient failure. The portal answers with
#: an occasional 5xx; the integration rides those out by keeping its state,
#: this script by trying again.
RETRY_DELAYS = (5, 15, 30)


async def _retry[T](
    say: Callable[[str], None], what: str, call: Callable[[], Awaitable[T]]
) -> T:
    """Run ``call``, retrying transient portal failures."""
    for delay in (*RETRY_DELAYS, None):
        try:
            return await call()
        except EudaError as err:
            if delay is None or not err.is_transient:
                raise
            say(f"  ({what}: {err}; retrying in {delay} s)")
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def _mask(vin: str) -> str:
    return f"{vin[:3]}…{vin[-4:]}"


async def run(brand: str, email: str, password: str, vin: str | None) -> int:
    """Run the check; return the process exit code."""
    OUT.mkdir(exist_ok=True)
    report: list[str] = []

    def say(line: str = "") -> None:
        print(line)
        report.append(line)

    # Verify against certifi's CA bundle, as Home Assistant does. The
    # python.org macOS installer ships Python without any CA certificates.
    context = ssl.create_default_context(cafile=certifi.where())
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(),
        connector=aiohttp.TCPConnector(ssl=context),
    ) as session:
        client = EudaClient(session, BRANDS[brand], email, password)

        say("== Sign-in")
        await _retry(say, "sign-in", client.login)
        say("ok")

        say("\n== Vehicles")
        vehicles = await _retry(say, "vehicle list", client.list_vehicles)
        for each, nickname in vehicles.items():
            say(f"{_mask(each)}  nickname={nickname!r}")
        if not vehicles:
            say("No vehicles on this account.")
            return 1
        vin = vin or next(iter(vehicles))
        if vin not in vehicles:
            say(f"{vin} is not on this account.")
            return 1

        say("\n== Data requests")
        metadata: dict[str, dict[str, object]] = {}
        for each in vehicles:
            try:
                metadata[each] = await _retry(
                    say,
                    "data request lookup",
                    partial(client.get_request_metadata, each),
                )
            except EudaNoDataError:
                say(f"{_mask(each)}: no data request")
                continue
            except EudaError as err:
                say(f"{_mask(each)}: lookup failed: {err}")
                continue
            say(f"{_mask(each)}:")
            for name, value in metadata[each].items():
                say(f"  {name} = {_redact(value, each)}")
        (OUT / "metadata.json").write_text(json.dumps(metadata, indent=1))
        identifier = str((metadata.get(vin) or {}).get("Identifier") or "")
        if not identifier:
            say(f"\nNo data request to check for {_mask(vin)}.")
            return 1

        say("\n== Delivery listing")
        try:
            raw_listing = await _retry(
                say,
                "delivery list",
                lambda: client._get_json(  # noqa: SLF001 - dev tool
                    "Delivery list",
                    _LIST_PATH.format(vin=vin, identifier=identifier),
                    headers={"type": "partial"},
                ),
            )
        except EudaError as err:
            if err.status != 404:
                raise
            raw_listing = []  # Nothing delivered yet.
        (OUT / "listing.json").write_text(json.dumps(raw_listing, indent=1))
        entries = raw_listing if isinstance(raw_listing, list) else []
        files = [DatasetFile.from_json(e) for e in entries if isinstance(e, dict)]
        say(f"{len(files)} files, {sum(f.has_content for f in files)} with content")
        for file in sorted(files, key=_created)[-6:]:
            say(f"  {file.created}  size={file.size}  {_shape(file.name, vin)}")
        content = sorted((f for f in files if f.has_content), key=_created)
        if not content:
            say("Nothing delivered yet.")
            return 1

        # Merge the recent datasets oldest first, as the integration does at
        # start-up, and keep each one as delivered.
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        say(f"\n== Merging the {min(len(content), INITIAL_DATASETS)} newest datasets")
        merged: Dataset | None = None
        for index, file in enumerate(content[-INITIAL_DATASETS:]):
            try:
                dataset = await _retry(
                    say,
                    "download",
                    partial(client.download_dataset, vin, identifier, file),
                )
            except EudaError as err:
                say(f"  {file.created}: failed: {err}")
                continue
            say(f"  {file.created}: {len(dataset.points)} data points")
            (OUT / f"dataset-{stamp}-{index}.json").write_text(
                json.dumps(_as_json(dataset), indent=1)
            )
            merged = dataset if merged is None else merged.merged_with(dataset)
        if merged is None:
            return 1
        dataset = merged
        say(f"{len(dataset.points)} data points, captured at {dataset.captured_at}")

        say("\n== Curated sensors")
        for description in SENSORS:
            field = _find_field(description, dataset)
            if field is None:
                say(f"  {description.key:26} (not created)")
                continue
            value = field.convert(dataset.value(field.name, field.key))
            unit = description.native_unit_of_measurement
            if field.unit_field:
                reported = dataset.value(field.unit_field)
                if isinstance(reported, str):
                    reported = reported.upper()
                unit = _DISTANCE_UNITS.get(reported, unit)  # type: ignore[arg-type]
            say(f"  {description.key:26} {value!r} {unit or ''}  <- {field.name}")

        say("\n== Data points against the dictionary")
        dictionary = load_data_dictionary()
        undocumented = []
        for point in sorted(dataset.points.values(), key=lambda p: p.name):
            entry = dictionary.get(point.key)
            if entry is None:
                undocumented.append(point)
                continue
            mapped = _DICTIONARY_UNITS.get(entry.unit or "")
            shown = mapped.convert(point.value) if mapped else point.value
            suffix = (
                f" {mapped.unit}" if mapped else (" s" if point.is_duration else "")
            )
            say(f"  {point.name:60} {shown!r}{suffix}  [{entry.type}]")
        say(f"\n{len(undocumented)} undocumented data points")
        for point in undocumented:
            say(f"  {point.key}  {point.name} = {point.raw!r}")

    (OUT / f"report-{stamp}.txt").write_text("\n".join(report) + "\n")
    print(f"\nSaved to {OUT}")
    return 0


def _redact(value: object, vin: str) -> str:
    """Show a metadata value with the VIN and long identifiers masked."""
    text = json.dumps(value, ensure_ascii=False).replace(vin, "<VIN>")
    if isinstance(value, str) and len(value) >= 24 and " " not in value:
        return f'"{value[:4]}…"'
    return text[:200]


def _as_json(dataset: Dataset) -> dict[str, object]:
    """Return a dataset in the portal's own document format."""
    return {
        "vin": dataset.vin,
        "Data": [
            {
                "key": p.key,
                "dataFieldName": p.name,
                "value": p.raw,
                "timestampUtc": p.timestamp.isoformat() if p.timestamp else None,
            }
            for p in dataset.points.values()
        ],
    }


def _created(file: DatasetFile) -> datetime:
    return file.created or datetime.min.replace(tzinfo=UTC)


def _shape(name: str, vin: str) -> str:
    """Show a file name's layout without the VIN."""
    return name.replace(vin, "<VIN>")


def main() -> int:
    """Parse arguments, ask for the password and run the check."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--brand", choices=sorted(BRANDS), default="volkswagen")
    parser.add_argument("--email", required=True)
    parser.add_argument("--vin", help="defaults to the account's first vehicle")
    args = parser.parse_args()

    password = os.environ.get("EUDA_PASSWORD") or getpass.getpass("Password: ")
    try:
        return asyncio.run(run(args.brand, args.email, password, args.vin))
    except EudaError as err:
        print(f"\nFailed: {type(err).__name__}: {err} (status {err.status})")
        return 1


if __name__ == "__main__":
    sys.exit(main())
