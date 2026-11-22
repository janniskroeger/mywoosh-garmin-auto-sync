import json
from datetime import date
from pathlib import Path

import pytest

from mywhoosh_garmin_sync.mywhoosh_workout_state import MyWhooshWorkoutEntry, MyWhooshWorkoutStateStore
from mywhoosh_garmin_sync.state import StateCorruptError


def _entry(planned_id: str = "p1", **overrides) -> MyWhooshWorkoutEntry:
    fields = dict(
        planned_id=planned_id,
        name="Sweet Spot",
        day=date(2026, 11, 9),
        workout_id=42,
        content_hash="abc",
        attempts=0,
        last_error=None,
        updated_at="2026-11-08T10:00:00+00:00",
    )
    fields.update(overrides)
    return MyWhooshWorkoutEntry(**fields)


def _write(path: Path, entry_overrides: dict | None = None, **top) -> None:
    item = {
        "name": "x", "day": "2026-11-09", "workout_id": None, "content_hash": None,
        "attempts": 0, "last_error": None, "updated_at": "t",
    }
    item.update(entry_overrides or {})
    data = {"version": 1, "workouts": {"p1": item}}
    data.update(top)
    path.write_text(json.dumps(data))


def test_roundtrip_put_all_remove(tmp_path):
    path = tmp_path / "sub" / "mw.json"
    store = MyWhooshWorkoutStateStore(path)
    a, b = _entry("a"), _entry("b", workout_id=None, content_hash=None, last_error="boom", attempts=2)
    store.put(a)
    store.put(b)
    assert MyWhooshWorkoutStateStore(path).all() == {"a": a, "b": b}
    store.remove("a")
    assert MyWhooshWorkoutStateStore(path).all() == {"b": b}
    store.remove("unknown")
    assert store.all() == {"b": b}


def test_missing_file_is_empty_and_not_exists(tmp_path):
    store = MyWhooshWorkoutStateStore(tmp_path / "none.json")
    assert not store.exists()
    assert store.all() == {}
    assert not store.exists()


def test_corrupt_json_raises_state_corrupt(tmp_path):
    path = tmp_path / "mw.json"
    path.write_text("{nope")
    with pytest.raises(StateCorruptError):
        MyWhooshWorkoutStateStore(path).all()


@pytest.mark.parametrize(
    "overrides",
    [{"workout_id": "7"}, {"workout_id": True}, {"attempts": True}, {"attempts": "1"}, {"attempts": -1},
     {"day": "09.11.2026"}, {"name": 5}, {"content_hash": 1}],
)
def test_wrong_field_type_raises_state_corrupt(tmp_path, overrides):
    path = tmp_path / "mw.json"
    _write(path, overrides)
    with pytest.raises(StateCorruptError):
        MyWhooshWorkoutStateStore(path).all()


def test_wrong_version_raises_state_corrupt(tmp_path):
    path = tmp_path / "mw.json"
    _write(path, version=2)
    with pytest.raises(StateCorruptError):
        MyWhooshWorkoutStateStore(path).all()


def test_write_is_atomic_no_tmp_left(tmp_path):
    path = tmp_path / "mw.json"
    store = MyWhooshWorkoutStateStore(path)
    store.put(_entry())
    store.remove("p1")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["mw.json"]
    assert json.loads(path.read_text()) == {"version": 1, "workouts": {}}


def _raw_item(**overrides) -> dict:
    item = {
        "name": "x", "day": "2026-11-09", "workout_id": None, "content_hash": None,
        "attempts": 0, "last_error": None, "updated_at": "t",
    }
    item.update(overrides)
    return item


def _missing_key() -> dict:
    item = _raw_item()
    del item["attempts"]
    return item


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1, "workouts": {"p1": _missing_key()}},
        {"version": 1, "workouts": {"p1": _raw_item(extra=1)}},
        {"version": 1, "workouts": {"p1": "nope"}},
        {"version": 1},
        {"version": 1, "workouts": []},
        [1, 2],
    ],
)
def test_invalid_structure_raises_state_corrupt(tmp_path, payload):
    path = tmp_path / "mw.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(StateCorruptError):
        MyWhooshWorkoutStateStore(path).all()


def test_remove_unknown_does_not_write(tmp_path):
    path = tmp_path / "mw.json"
    store = MyWhooshWorkoutStateStore(path)
    store.remove("unknown")
    assert not path.exists()
    store.put(_entry())
    before = (path.read_text(), path.stat().st_mtime_ns)
    store.remove("unknown")
    assert (path.read_text(), path.stat().st_mtime_ns) == before


def test_put_replaces_existing_id(tmp_path):
    path = tmp_path / "mw.json"
    store = MyWhooshWorkoutStateStore(path)
    store.put(_entry("a", attempts=0))
    newer = _entry("a", attempts=3, last_error="x")
    store.put(newer)
    assert MyWhooshWorkoutStateStore(path).all() == {"a": newer}


def test_failed_write_keeps_cache_and_leaves_no_tmp(tmp_path, monkeypatch):
    path = tmp_path / "mw.json"
    store = MyWhooshWorkoutStateStore(path)
    a = _entry("a")
    store.put(a)

    def boom(*args, **kwargs):
        raise OSError("disk")

    monkeypatch.setattr("mywhoosh_garmin_sync.json_state.os.replace", boom)
    with pytest.raises(OSError):
        store.put(_entry("b"))
    with pytest.raises(OSError):
        store.remove("a")
    assert store.all() == {"a": a}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["mw.json"]
