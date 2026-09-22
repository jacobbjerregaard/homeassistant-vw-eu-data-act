[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

[![CodeQL](https://github.com/jacobbjerregaard/homeassistant-vw-eu-data-act/actions/workflows/codeql.yml/badge.svg)](https://github.com/jacobbjerregaard/homeassistant-vw-eu-data-act/actions/workflows/codeql.yml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

# VW Group EU Data Act for Home Assistant

A Home Assistant custom integration that reads your vehicle's data from the
Volkswagen Group [EU Data Act portal](https://eu-data-act.drivesomethinggreater.com/).

Under the EU Data Act, owners of connected vehicles are entitled to the data
their vehicle generates, free of charge. The portal serves it for Volkswagen,
Audi, Škoda, SEAT and CUPRA vehicles. This integration downloads the datasets
the portal produces and turns them into sensors. It does not need a paid
connected-services subscription.

> **Status: early development.** The portal has no public API. This
> integration replays what the portal's web front end does, so a change on
> VW Group's side can break it without notice.

## Before you start: set up a continuous data request

The integration only downloads data the portal has already produced. It
cannot create the data request for you.

1. Sign in at <https://eu-data-act.drivesomethinggreater.com/> with your brand
   account (Volkswagen ID, myAudi, myŠkoda, SEAT or CUPRA ID).
2. Under **Data clusters**, link your vehicle if it is not already listed.
3. Choose **Get customised data** and set up a **continuous** data request
   with a **15-minute** frequency.
4. Wait for the first dataset to appear. That can take a few hours.

## Installation

### HACS

1. In HACS, choose **Integrations -> ⋮ -> Custom repositories**.
2. Add `https://github.com/jacobbjerregaard/homeassistant-vw-eu-data-act`
   with category **Integration**.
3. Install **VW Group EU Data Act**, then restart Home Assistant.
4. Go to **Settings -> Devices & services -> Add integration** and search for
   **VW Group EU Data Act**.

### Manual

Copy `custom_components/vwg_eu_data_act` into your Home Assistant
`config/custom_components` directory and restart.

## Configuration

Pick your brand and sign in with the same e-mail and password you use on the
portal. Then choose a vehicle. Each vehicle is a separate config entry; add the
integration again for a second vehicle.

## How it works

* The delivery listing is checked every 5 minutes. A new dataset is
  downloaded only when one has appeared, roughly every 15 minutes.
* A dataset is not a full snapshot: while the vehicle is parked and asleep the
  portal delivers reduced ones that leave most fields out. The vehicle's state
  is therefore every dataset so far, merged in order, so a sensor keeps its
  last known value until the vehicle reports a new one. At start-up the
  8 newest datasets (about two hours) are merged, so a restart while the
  vehicle is parked does not leave the sensors empty.
* The portal fails with a server error now and then. The last state is kept
  through up to three failures in a row before the sensors go unavailable. A
  dataset that cannot be downloaded is retried on the next polls, then
  skipped; the ones before and after it are still used.
* If the data request is deleted and set up again on the portal, it gets a
  new identifier. The integration follows it automatically.
* When the password stops working, Home Assistant asks you to sign in again.
  When the account instead needs something confirmed, such as new terms of
  use, the integration says so: sign in once at the portal in a browser.
* Error messages and logs never contain the VIN or the data request
  identifier, so they are safe to paste into an issue.

## Sensors

A sensor is created once the vehicle reports a value for it, so an electric
car gets no fuel level sensor and a petrol car no battery sensor. Field names
differ between MEB/SSP vehicles (the ID. family, Enyaq, Born, Q4) and older
platforms; each sensor reads whichever one the vehicle sends.

| Sensor | MEB/SSP field | Older platforms |
| --- | --- | --- |
| Battery level | `battery_state_report.soc` | `state_of_charge`, `hv_soc` |
| Target battery level | `settings.target_soc` | |
| Charging power | `battery_state_report.charge_power` | |
| Charging time remaining | `battery_state_report.remaining_charging_time_complete` | `remaining_charging_time` |
| Charging state | `charging_state_report.current_charge_state` | `charging_state` |
| Plug state | | `plug_state` |
| Odometer | `mileage.value` | `mileage` |
| Range | `estimatedcruisingrangeprimary.value` | `cruising_range_combined` |
| Fuel level | | `fuel_level_current_level` |
| Outside temperature | `outdoor_temperature` | `outside_temperature` |
| Last reported *(diagnostic)* | newest `car_captured_time` or `timestampUtc` | |
| Dataset delivered *(diagnostic)* | when the portal created the dataset | |

### Every other data point

Every data point in a dataset also gets a sensor of its own, **disabled by
default** and listed under the device's diagnostic entities. Enable the ones
you want. Each is named after the field the vehicle sends, and carries VW's
description of it, its data clusters and when the vehicle measured it as
attributes. Where VW's data dictionary states a unit that maps cleanly onto
Home Assistant (%, km, min, kW, kWh, °C, deci-Kelvin, bar, V, ...), the sensor
has that unit and records long-term statistics.

If your vehicle reports something that deserves a curated sensor, download
the diagnostics from the device page (the VIN, credentials and position are
redacted) and open an issue with it.

## The data dictionary

VW documents the 1,141 data points a continuous data request can deliver in
a PDF data dictionary, *List of Continuous Data*, available from the portal.
`custom_components/vwg_eu_data_act/data_dictionary.json` is generated from
it; the PDF itself is not part of this repository. To regenerate the JSON
from a newer edition:

```bash
pip install pdfplumber
python tools/parse_data_dictionary.py path/to/DataDictionary.pdf
```

## Development

The portal client under `custom_components/vwg_eu_data_act/api` does not
import Home Assistant, so it can be tested on its own:

```bash
pip install -r requirements_test.txt
pytest
```

The Home Assistant tests under `tests/integration` need the larger test
dependencies:

```bash
pip install -r requirements_test_ha.txt
pytest
```

The test datasets under `tests/fixtures` are synthetic. Do not commit real
datasets: they contain your VIN and account id.

## Credits

The portal's sign-in flow, endpoints and brand clients were worked out by the
community, in particular by [evcc](https://github.com/evcc-io/evcc) and
[hass-vw-eu-data-act](https://github.com/mikrohard/hass-vw-eu-data-act).

## Disclaimer

This project is not affiliated with or endorsed by Volkswagen AG, the
Volkswagen Group or any of its brands. Use it at your own risk.
