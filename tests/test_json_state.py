import json

import pytest

from mywhoosh_garmin_sync import json_state
from mywhoosh_garmin_sync.json_state import read_envelope, write_atomic
from mywhoosh_garmin_sync.state import StateCorruptError


def test_write_atomic_creates_parent_and_leaves_no_tmp(tmp_path):
    path = tmp_path / "sub" / "s.json"
    write_atomic(path, {"version": 1, "workouts": {}})
    assert json.loads(path.read_text()) == {"version": 1, "workouts": {}}
    assert [p.name for p in path.parent.iterdir()] == ["s.json"]


def test_write_atomic_failure_removes_tmp_and_keeps_old_file(tmp_path, monkeypatch):
    path = tmp_path / "s.json"
    write_atomic(path, {"a": 1})

    def boom(*args, **kwargs):
        raise OSError("disk")

    monkeypatch.setattr(json_state.os, "replace", boom)
    with pytest.raises(OSError):
        write_atomic(path, {"a": 2})
    assert json.loads(path.read_text()) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["s.json"]


def test_read_envelope_missing_file_is_none(tmp_path):
    assert read_envelope(tmp_path / "none.json", 1, "workouts") is None


def test_read_envelope_returns_content(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"version": 1, "workouts": {"a": 1}}))
    assert read_envelope(path, 1, "workouts") == {"a": 1}


@pytest.mark.parametrize(
    "text", ['{"version": 2, "workouts": {}}', '{"version": 1}', '{"version": 1, "workouts": 3}', "[]", "{x"]
)
def test_read_envelope_invalid_raises_state_corrupt(tmp_path, text):
    path = tmp_path / "s.json"
    path.write_text(text)
    with pytest.raises(StateCorruptError, match=str(path)):
        read_envelope(path, 1, "workouts")
