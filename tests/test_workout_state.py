import json
from datetime import date

import pytest

from mywhoosh_garmin_sync.state import StateCorruptError
from mywhoosh_garmin_sync.workout_state import WorkoutEntry, WorkoutStateStore


def _entry(serial=1, **overrides):
    values = dict(
        serial=serial,
        name="Intervalle",
        day=date(2026, 11, 8),
        status="active",
        workout_id=None,
        schedule_id=None,
        content_hash=None,
        in_library=False,
        attempts=0,
        last_error=None,
        updated_at="2026-11-07T10:00:00+00:00",
    )
    values.update(overrides)
    return WorkoutEntry(**values)


def test_roundtrip_all_fields(tmp_path):
    p = tmp_path / "workouts.json"
    full = _entry(
        serial=7,
        status="past",
        workout_id=123,
        schedule_id=456,
        content_hash="abc",
        in_library=True,
        attempts=3,
        last_error="boom",
    )
    WorkoutStateStore(p).put(full)

    again = WorkoutStateStore(p)
    again.load()
    assert again.all() == {7: full}
    assert again.all()[7].day == date(2026, 11, 8)


def test_put_many_empty_creates_file(tmp_path):
    p = tmp_path / "workouts.json"
    store = WorkoutStateStore(p)
    assert not store.exists()
    store.put_many([])
    assert store.exists()
    assert json.loads(p.read_text()) == {"version": 1, "workouts": {}}
    assert WorkoutStateStore(p).all() == {}


def test_put_replaces_same_serial_and_keeps_others(tmp_path):
    p = tmp_path / "workouts.json"
    store = WorkoutStateStore(p)
    store.put_many([_entry(1), _entry(2)])
    store.put(_entry(1, status="removed", attempts=2))

    again = WorkoutStateStore(p)
    assert again.all()[1].status == "removed" and again.all()[1].attempts == 2
    assert again.all()[2] == _entry(2)


def test_put_many_replaces_and_merges(tmp_path):
    p = tmp_path / "workouts.json"
    store = WorkoutStateStore(p)
    store.put_many([_entry(1), _entry(2)])
    store.put_many([_entry(2, status="past"), _entry(3)])

    result = WorkoutStateStore(p).all()
    assert sorted(result) == [1, 2, 3]
    assert result[1] == _entry(1)
    assert result[2].status == "past"


def test_all_returns_copy(tmp_path):
    store = WorkoutStateStore(tmp_path / "workouts.json")
    store.put(_entry(1))
    snapshot = store.all()
    snapshot.clear()
    snapshot[99] = _entry(99)
    assert list(store.all()) == [1]


def test_atomic_write_no_tmp_left(tmp_path):
    WorkoutStateStore(tmp_path / "workouts.json").put(_entry(1))
    assert [f.name for f in tmp_path.iterdir()] == ["workouts.json"]


@pytest.mark.parametrize(
    "content",
    [
        "",
        "{x",
        "[]",
        '{"version": 1}',
        '{"version": 1, "workouts": {"1": {"status": "nope"}}}',
        '{"version": 1, "workouts": {"x": {}}}',
        '{"version": 2, "workouts": {}}',
    ],
)
def test_corrupt_raises(tmp_path, content):
    p = tmp_path / "workouts.json"
    p.write_text(content)
    store = WorkoutStateStore(p)
    assert store.exists()
    with pytest.raises(StateCorruptError, match=str(p)):
        store.load()
    with pytest.raises(StateCorruptError):
        store.all()


def _valid_item(**overrides):
    item = {
        "name": "Intervalle",
        "day": "2026-11-08",
        "status": "active",
        "workout_id": None,
        "schedule_id": None,
        "content_hash": None,
        "in_library": False,
        "attempts": 0,
        "last_error": None,
        "updated_at": "u",
    }
    item.update(overrides)
    return item


@pytest.mark.parametrize(
    "override",
    [
        {"name": 5},
        {"day": "08.11.2026"},
        {"day": 20261108},
        {"status": "bogus"},
        {"workout_id": "12"},
        {"workout_id": True},
        {"schedule_id": 1.5},
        {"content_hash": 3},
        {"in_library": 1},
        {"attempts": -1},
        {"attempts": False},
        {"last_error": 4},
        {"updated_at": None},
        {"unexpected": 1},
    ],
)
def test_invalid_field_types_raise(tmp_path, override):
    p = tmp_path / "workouts.json"
    p.write_text(json.dumps({"version": 1, "workouts": {"1": _valid_item(**override)}}))
    with pytest.raises(StateCorruptError):
        WorkoutStateStore(p).all()


@pytest.mark.parametrize("key", ["-1", "+1", " 1", "1.0", ""])
def test_non_digit_serial_key_raises(tmp_path, key):
    p = tmp_path / "workouts.json"
    p.write_text(json.dumps({"version": 1, "workouts": {key: _valid_item()}}))
    with pytest.raises(StateCorruptError):
        WorkoutStateStore(p).all()


def test_put_on_corrupt_does_not_overwrite(tmp_path):
    p = tmp_path / "workouts.json"
    p.write_text("{x")
    store = WorkoutStateStore(p)
    with pytest.raises(StateCorruptError):
        store.put(_entry(1))
    assert p.read_text() == "{x"
    assert [f.name for f in tmp_path.iterdir()] == ["workouts.json"]


def test_put_many_on_corrupt_does_not_overwrite(tmp_path):
    p = tmp_path / "workouts.json"
    p.write_text("{x")
    with pytest.raises(StateCorruptError):
        WorkoutStateStore(p).put_many([_entry(1)])
    assert p.read_text() == "{x"


def test_missing_file_means_empty_state(tmp_path):
    store = WorkoutStateStore(tmp_path / "workouts.json")
    assert not store.exists()
    store.load()
    assert store.all() == {}
