"""Tests for the figures calculated from readings over time."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from euda_api.metrics import Observation, VehicleMetrics, summarize

START = datetime(2026, 1, 12, 7, 0, tzinfo=UTC)  # a Monday, in ISO week 3

#: What charge() puts in at 11 kW: two hours, and the ramp down to zero over
#: the last five minutes, which the readings cannot tell apart from a step.
KWH = 11 * 2 + 11 / 2 * 5 / 60
#: ... for 30 %.
CAPACITY = KWH / 30 * 100


def at(minutes: float) -> datetime:
    return START + timedelta(minutes=minutes)


def charge(tracker, begin, soc_from, soc_to, power=11.0, minutes=120, step=5):
    """Feed a charging session: steady power, then the settled charge."""
    tracker.add(Observation(at(begin), soc=soc_from))
    for minute in range(0, minutes + 1, step):
        tracker.add(Observation(at(begin + minute), charge_power=power))
    # Charging stopped, and a while later the settled state of charge.
    tracker.add(Observation(at(begin + minutes + 5), charge_power=0.0))
    tracker.add(Observation(at(begin + minutes + 20), soc=soc_to))
    # A session only ends once charging has not resumed for a while.
    tracker.add(Observation(at(begin + minutes + 60), temperature=5.0))


def drive(tracker, begin, soc_from, soc_to, km_from, km_to, temperature=5.0):
    tracker.add(Observation(at(begin), soc=soc_from, odometer=km_from))
    tracker.add(Observation(at(begin + 1), temperature=temperature))
    tracker.add(Observation(at(begin + 60), odometer=km_to))
    tracker.add(Observation(at(begin + 61), soc=soc_to))


def test_capacity_from_a_charging_session():
    tracker = VehicleMetrics()
    charge(tracker, 0, 50, 80)

    assert tracker.sessions == 1
    assert tracker.charged_kwh == pytest.approx(KWH)
    assert tracker.own_capacity == pytest.approx(CAPACITY, abs=0.05)
    session = tracker.last_session
    assert session["energy"] == pytest.approx(KWH, abs=0.005)
    assert session["average_power"] == pytest.approx(11.2, abs=0.05)
    assert (session["type"], session["soc_start"], session["soc_end"]) == (
        "AC",
        50,
        80,
    )


def test_consumption_from_driving():
    tracker = VehicleMetrics()
    charge(tracker, 0, 50, 80)
    # 100 km on 20 % of the capacity.
    drive(tracker, 300, 80, 60, 1000, 1100)

    summary = summarize(tracker)
    assert summary.consumption(summary.weeks["2026-W03"]) == pytest.approx(
        CAPACITY * 0.2, abs=0.05
    )
    assert summary.consumption(summary.months["2026-01"]) == pytest.approx(
        CAPACITY * 0.2, abs=0.05
    )
    assert summary.band_consumption("0_to_10") == pytest.approx(
        CAPACITY * 0.2, abs=0.05
    )
    assert summary.band_consumption("below_0") is None
    assert tracker.used_kwh == pytest.approx(CAPACITY * 0.2, abs=0.01)
    assert tracker.parked_kwh == 0


def test_energy_used_before_the_capacity_is_known_is_kept():
    tracker = VehicleMetrics()
    drive(tracker, 0, 80, 70, 1000, 1050)
    assert tracker.used_kwh == 0  # No capacity yet.
    charge(tracker, 200, 70, 100)
    # The 10 % used earlier is counted once the capacity is known.
    assert tracker.used_kwh == pytest.approx(CAPACITY * 0.1, abs=0.01)


def test_parked_drain_and_jitter():
    tracker = VehicleMetrics()
    charge(tracker, 0, 50, 80)
    tracker.add(Observation(at(400), odometer=2000, soc=80))
    # Jitter nets out; a real drop while parked does not.
    for minute, soc in ((500, 79), (510, 80), (520, 79), (900, 77)):
        tracker.add(Observation(at(minute), soc=soc))

    assert tracker.parked_kwh == pytest.approx(3 / 100 * CAPACITY, abs=0.01)
    assert summarize(tracker).weeks["2026-W03"]["parked"] == 3


def test_a_pause_does_not_split_the_session():
    tracker = VehicleMetrics()
    tracker.add(Observation(at(0), soc=40))
    for minute in (0, 5, 10):
        tracker.add(Observation(at(minute), charge_power=7.0))
    tracker.add(Observation(at(15), charge_power=0.0))  # Paused ...
    for minute in (25, 30, 35):
        tracker.add(Observation(at(minute), charge_power=7.0))  # ... and on.
    tracker.add(Observation(at(120), soc=50))

    assert tracker.sessions == 1


def test_power_blips_are_not_sessions():
    tracker = VehicleMetrics()
    tracker.add(Observation(at(0), charge_power=1.0))
    tracker.add(Observation(at(1), charge_power=0.0))
    tracker.add(Observation(at(120), soc=50))
    assert tracker.sessions == 0


def test_dc_session():
    tracker = VehicleMetrics()
    charge(tracker, 0, 10, 80, power=150.0, minutes=20)
    assert tracker.last_session["type"] == "DC"
    assert summarize(tracker).dc_share() == 100.0


def test_charge_type_reported_by_the_vehicle():
    tracker = VehicleMetrics()
    tracker.add(Observation(at(0), charge_type="DC"))
    charge(tracker, 1, 20, 40, power=20.0, minutes=30)
    assert tracker.last_session["type"] == "DC"


def test_unseen_charge_between_sparse_readings():
    tracker = VehicleMetrics()
    charge(tracker, 0, 50, 80)  # So the capacity is known.
    tracker.add(Observation(at(2000), soc=40))
    # 15-minute snapshots missed a whole charge: +30 %.
    tracker.add(Observation(at(3000), soc=70))

    assert tracker.sessions == 2
    assert tracker.charged_kwh == pytest.approx(KWH + 0.30 * CAPACITY, abs=0.05)


def test_charge_settling_after_a_session_is_not_regenerated_energy():
    tracker = VehicleMetrics()
    charge(tracker, 0, 50, 76)
    # The state of charge creeps up a little more after the session.
    tracker.add(Observation(at(170), soc=80))
    assert tracker.sessions == 1
    assert tracker.used_kwh == pytest.approx(0)


def test_too_little_distance_gives_no_consumption():
    tracker = VehicleMetrics()
    charge(tracker, 0, 50, 80)
    drive(tracker, 300, 80, 79, 1000, 1010)
    summary = summarize(tracker)
    assert summary.consumption(summary.weeks["2026-W03"]) is None


def test_weeks_and_months_follow_the_time_zone():
    tracker = VehicleMetrics(ZoneInfo("Pacific/Auckland"))
    tracker.add(Observation(datetime(2026, 1, 31, 12, tzinfo=UTC), temperature=20))
    # 1 February already in New Zealand.
    assert list(tracker.months) == ["2026-02"]


def test_readings_out_of_order_are_ignored():
    tracker = VehicleMetrics()
    tracker.add(Observation(at(10), odometer=1000))
    tracker.add(Observation(at(5), odometer=900))
    assert tracker.last_time == at(10)


def test_hours_are_recorded_for_statistics():
    tracker = VehicleMetrics(record_hours=True)
    charge(tracker, 0, 50, 80)
    hours = sorted(tracker.hours.items())
    # Starting from a zero hour before the first reading.
    assert hours[0] == (datetime(2026, 1, 12, 6, tzinfo=UTC), (0.0, 0.0, 0.0, 0))
    assert hours[1][0] == datetime(2026, 1, 12, 7, tzinfo=UTC)
    charged, _used, _parked, sessions = hours[-1][1]
    assert charged == pytest.approx(KWH)
    assert sessions == 1


def test_stored_and_restored():
    tracker = VehicleMetrics()
    charge(tracker, 0, 50, 80)
    drive(tracker, 300, 80, 60, 1000, 1100)
    tracker.add(Observation(at(400), charge_power=11.0))  # A session under way.

    restored = VehicleMetrics.from_dict(tracker.to_dict())
    assert restored.to_dict() == tracker.to_dict()

    # And it carries on where it left off.
    for minute in range(405, 525, 5):
        restored.add(Observation(at(minute), charge_power=11.0))
    restored.add(Observation(at(600), soc=90))
    assert restored.sessions == 2


def test_summary_combines_separate_periods():
    history, live = VehicleMetrics(), VehicleMetrics()
    charge(history, 0, 50, 80)
    drive(history, 300, 80, 60, 1000, 1100)
    live.add(Observation(at(10_000), odometer=5000, soc=60))
    live.add(Observation(at(10_060), odometer=5100, soc=40))

    summary = summarize(history, live)
    assert summary.capacity == pytest.approx(CAPACITY, abs=0.05)
    assert summary.weeks["2026-W03"]["km"] == 100
    assert summary.weeks["2026-W04"]["km"] == 100
    assert summary.sessions == 1
    table = summary.table(summary.weeks, 1)
    assert list(table) == ["2026-W04"]
    assert table["2026-W04"]["consumption"] == pytest.approx(CAPACITY * 0.2, abs=0.05)


def test_capacity_trend_needs_enough_sessions():
    tracker = VehicleMetrics()
    for day in range(3):
        charge(tracker, day * 1440, 50, 80)
    tracker.add(Observation(START + timedelta(days=40), soc=50))
    charge(tracker, 40 * 1440 + 10, 50, 80)
    # January has three estimates; February only one, so it is left out.
    assert summarize(tracker).capacity_by_month == {"2026-01": round(CAPACITY, 1)}
