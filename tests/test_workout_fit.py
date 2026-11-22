import logging
import io
import zipfile
from datetime import date
from pathlib import Path

import pytest

from mywhoosh_garmin_sync.workout_fit import (
    FitRepeat,
    FitStep,
    WorkoutBundleError,
    parse_schedule,
    parse_workout,
    read_bundle,
    read_bundle_report,
)
from workoutfactory import make_bundle, make_schedule_fit, make_workout_fit

REAL = Path(__file__).parent / "fixtures" / "local" / "opentri_bundle.zip"
requires_real_export = pytest.mark.skipif(not REAL.exists(), reason="no local open-tri export")


def simple_steps() -> list[dict]:
    return [{"name": "Locker", "intensity": "active", "duration_type": "open"}]


def test_parses_steps_targets_and_offsets():
    raw = make_workout_fit(42, "W3: Grundlage", "running", [
        {"name": "Einlaufen", "intensity": "warmup", "duration_type": "time", "duration_time": 600,
         "target_type": "heart_rate", "target_low": 120, "target_high": 140},
        {"name": "Hart", "intensity": "active", "duration_type": "distance", "duration_distance": 1000,
         "target_type": "speed", "target_low": 3.5, "target_high": 3.7},
        {"name": "Locker", "intensity": "recovery", "duration_type": "open", "target_type": "open"},
        {"duration_type": "repeat_until_steps_cmplt", "duration_step": 1, "repeat_count": 3},
    ])
    w = parse_workout(raw)
    assert (w.serial, w.name, w.sport) == (42, "W3: Grundlage", "running")
    assert w.items[0] == FitStep("Einlaufen", None, "warmup", "time", 600, "heart_rate", 120, 140)
    rep = w.items[1]
    assert isinstance(rep, FitRepeat) and rep.iterations == 3 and len(rep.steps) == 2
    assert rep.steps[0].target == "speed" and rep.steps[0].target_low == pytest.approx(3.5)
    assert rep.steps[0].duration_value == 1000
    assert rep.steps[1].duration == "open" and rep.steps[1].duration_value is None
    assert rep.steps[1].target_low is None and rep.steps[1].intensity == "recovery"


def test_power_offset_removed():
    raw = make_workout_fit(1, "Rad", "cycling", [
        {"name": "Sweetspot", "intensity": "active", "duration_type": "time", "duration_time": 1200,
         "target_type": "power", "target_low": 180, "target_high": 200},
    ])
    step = parse_workout(raw).items[0]
    assert (step.target, step.target_low, step.target_high) == ("power", 180, 200)
    assert step.duration_value == 1200


def test_notes_are_kept():
    raw = make_workout_fit(1, "Rad", "cycling", [
        {"name": "Test", "notes": "Ø-Leistung notieren", "intensity": "active", "duration_type": "open"},
    ])
    assert parse_workout(raw).items[0].notes == "Ø-Leistung notieren"


def test_steps_after_repeat_stay_top_level():
    raw = make_workout_fit(1, "Schwimmen", "swimming", [
        {"name": "Einschwimmen", "intensity": "warmup", "duration_type": "distance", "duration_distance": 400},
        {"name": "Zug", "intensity": "active", "duration_type": "distance", "duration_distance": 50},
        {"name": "Pause", "intensity": "rest", "duration_type": "time", "duration_time": 20},
        {"duration_type": "repeat_until_steps_cmplt", "duration_step": 1, "repeat_count": 4},
        {"name": "Letzte", "intensity": "active", "duration_type": "distance", "duration_distance": 50},
    ])
    items = parse_workout(raw).items
    assert [type(i) for i in items] == [FitStep, FitRepeat, FitStep]
    assert items[1].iterations == 4 and [s.name for s in items[1].steps] == ["Zug", "Pause"]
    assert items[2].name == "Letzte"


def test_unsupported_sport_returns_none_with_warning(caplog):
    raw = make_workout_fit(1, "Krafttraining", "training", simple_steps())
    with caplog.at_level(logging.WARNING):
        assert parse_workout(raw) is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_nested_repeat_is_rejected():
    raw = make_workout_fit(1, "Nested", "running", [
        {"name": "A", "intensity": "active", "duration_type": "open"},
        {"name": "B", "intensity": "active", "duration_type": "open"},
        {"duration_type": "repeat_until_steps_cmplt", "duration_step": 1, "repeat_count": 2},
        {"duration_type": "repeat_until_steps_cmplt", "duration_step": 0, "repeat_count": 2},
    ])
    with pytest.raises(ValueError):
        parse_workout(raw)


def test_schedule_local_time_to_date():
    raw = make_schedule_fit({42: date(2026, 11, 10), 43: date(2026, 11, 11)})
    assert parse_schedule(raw) == {42: date(2026, 11, 10), 43: date(2026, 11, 11)}


def two_workout_bundle() -> bytes:
    return make_bundle({
        "2026-11-10-Lauf.fit": make_workout_fit(42, "Lauf", "running", simple_steps()),
        "2026-11-11-Rad.fit": make_workout_fit(43, "Rad", "cycling", simple_steps()),
        "Schedules.fit": make_schedule_fit({42: date(2026, 11, 10), 43: date(2026, 11, 11)}),
    })


def test_read_bundle_pairs_workouts_with_dates():
    planned = read_bundle(two_workout_bundle())
    assert [(p.workout.serial, p.date) for p in planned] == [(42, date(2026, 11, 10)), (43, date(2026, 11, 11))]


def test_workout_without_schedule_entry_is_skipped(caplog):
    bundle = make_bundle({
        "a.fit": make_workout_fit(42, "Lauf", "running", simple_steps()),
        "b.fit": make_workout_fit(43, "Rad", "cycling", simple_steps()),
        "Schedules.fit": make_schedule_fit({42: date(2026, 11, 10)}),
    })
    with caplog.at_level(logging.WARNING):
        planned = read_bundle(bundle)
    assert [p.workout.serial for p in planned] == [42]
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_bundle_without_schedules_file_raises():
    bundle = make_bundle({"a.fit": make_workout_fit(42, "Lauf", "running", simple_steps())})
    with pytest.raises(WorkoutBundleError):
        read_bundle(bundle)


def test_bundle_with_unreadable_schedules_file_raises():
    bundle = make_bundle({
        "a.fit": make_workout_fit(42, "Lauf", "running", simple_steps()),
        "Schedules.fit": b"garbage, not a fit file",
    })
    with pytest.raises(WorkoutBundleError):
        read_bundle(bundle)


NESTED_STEPS = [
    {"name": "A", "intensity": "active", "duration_type": "open"},
    {"name": "B", "intensity": "active", "duration_type": "open"},
    {"duration_type": "repeat_until_steps_cmplt", "duration_step": 1, "repeat_count": 2},
    {"duration_type": "repeat_until_steps_cmplt", "duration_step": 0, "repeat_count": 2},
]


def test_report_names_unparseable_workout_by_its_serial(caplog):
    bundle = make_bundle({
        "a.fit": make_workout_fit(42, "Lauf", "running", NESTED_STEPS),
        "b.fit": make_workout_fit(43, "Rad", "cycling", simple_steps()),
        "Schedules.fit": make_schedule_fit({42: date(2026, 11, 10), 43: date(2026, 11, 11)}),
    })
    with caplog.at_level(logging.WARNING):
        report = read_bundle_report(bundle)
    assert [p.workout.serial for p in report.planned] == [43]
    assert report.unreadable == {42}
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_report_names_garbage_file_through_schedules():
    bundle = make_bundle({
        "a.fit": b"garbage, not a fit file",
        "b.fit": make_workout_fit(43, "Rad", "cycling", simple_steps()),
        "Schedules.fit": make_schedule_fit({42: date(2026, 11, 10), 43: date(2026, 11, 11)}),
    })
    report = read_bundle_report(bundle)
    assert [p.workout.serial for p in report.planned] == [43]
    assert report.unreadable == {42}


def test_report_names_workout_without_schedule_entry():
    bundle = make_bundle({
        "a.fit": make_workout_fit(42, "Lauf", "running", simple_steps()),
        "b.fit": make_workout_fit(43, "Rad", "cycling", simple_steps()),
        "Schedules.fit": make_schedule_fit({42: date(2026, 11, 10)}),
    })
    report = read_bundle_report(bundle)
    assert [p.workout.serial for p in report.planned] == [42]
    assert report.unreadable == {43}


def test_report_does_not_count_unsupported_sport_as_unreadable():
    bundle = make_bundle({
        "a.fit": make_workout_fit(50, "Kraft", "training", simple_steps()),
        "b.fit": make_workout_fit(43, "Rad", "cycling", simple_steps()),
        "Schedules.fit": make_schedule_fit({50: date(2026, 11, 10), 43: date(2026, 11, 11)}),
    })
    report = read_bundle_report(bundle)
    assert [p.workout.serial for p in report.planned] == [43]
    assert report.unreadable == set()


def test_broken_single_workout_is_skipped(caplog):
    bundle = make_bundle({
        "a.fit": b"garbage, not a fit file",
        "b.fit": make_workout_fit(43, "Rad", "cycling", simple_steps()),
        "Schedules.fit": make_schedule_fit({42: date(2026, 11, 10), 43: date(2026, 11, 11)}),
    })
    with caplog.at_level(logging.WARNING):
        planned = read_bundle(bundle)
    assert [p.workout.serial for p in planned] == [43]
    assert any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.parametrize("raw", [b"", b"<html>nope</html>"])
def test_not_a_zip_raises(raw):
    with pytest.raises(WorkoutBundleError):
        read_bundle(raw)


def real_planned():
    return {p.workout.serial: p for p in read_bundle(REAL.read_bytes())}


@requires_real_export
def test_real_export_parses():
    planned = read_bundle(REAL.read_bytes())
    assert planned and all(p.workout.items for p in planned)
    assert {2736911368, 647072668, 3305653501} <= {p.workout.serial for p in planned}


@requires_real_export
def test_real_css_test_workout():
    css = real_planned()[2736911368]
    assert css.date == date(2026, 11, 11)
    assert (css.workout.name, css.workout.sport) == ("W3: CSS-Test – 400 m + 200 m", "swimming")
    first, repeat, *rest = css.workout.items
    assert (first.intensity, first.duration, first.duration_value) == ("warmup", "distance", 400)
    assert isinstance(repeat, FitRepeat) and repeat.iterations == 4 and len(repeat.steps) == 2
    assert (repeat.steps[0].intensity, repeat.steps[0].duration, repeat.steps[0].duration_value) == ("active", "distance", 50)
    assert (repeat.steps[1].intensity, repeat.steps[1].duration, repeat.steps[1].duration_value) == ("rest", "time", 20)
    test1 = next(s for s in rest if s.duration_value == 400 and s.intensity == "active")
    assert test1.target == "speed"
    assert test1.target_low == pytest.approx(0.850) and test1.target_high == pytest.approx(0.883)


@requires_real_export
def test_real_schedule_dates():
    with zipfile.ZipFile(REAL) as zf:
        schedule = parse_schedule(zf.read("Schedules.fit"))
    assert schedule[672651861] == date(2026, 11, 7)
    assert schedule[647072668] == schedule[3305653501] == date(2026, 11, 15)
    planned = real_planned()
    assert planned[647072668].date == planned[3305653501].date == date(2026, 11, 15)


def _corrupt_member(bundle: bytes, member: str) -> bytes:
    """Flip one stored byte of a member so that reading it fails the CRC check."""
    with zipfile.ZipFile(io.BytesIO(bundle)) as zf:
        info = zf.getinfo(member)
        content = zf.read(member)
    start = bundle.index(content, info.header_offset)
    broken = bytearray(bundle)
    broken[start + len(content) // 2] ^= 0xFF
    return bytes(broken)


def test_report_counts_a_broken_workout_member_as_unreadable():
    bundle = _corrupt_member(two_workout_bundle(), "2026-11-11-Rad.fit")

    report = read_bundle_report(bundle)

    assert [p.workout.serial for p in report.planned] == [42]
    assert report.unreadable == {43}


def test_broken_schedules_member_raises_bundle_error():
    bundle = _corrupt_member(two_workout_bundle(), "Schedules.fit")
    with pytest.raises(WorkoutBundleError):
        read_bundle_report(bundle)
