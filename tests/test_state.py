import json

import pytest

from mywhoosh_garmin_sync.state import Entry, StateCorruptError, StateStore

CORRUPT_CONTENTS = [
    "",
    "{not json",
    "[]",
    '{"version": 1}',
    '{"version": 1, "activities": {"a": {"status": "bogus", "title": "T", "start": "s", "updated_at": "u"}}}',
]


def test_missing_file_means_no_state(tmp_path):
    store = StateStore(tmp_path / "state.json")
    assert not store.exists() and store.get("x") is None and not store.is_done("x")


def test_put_persists_across_instances(tmp_path):
    p = tmp_path / "state.json"
    StateStore(p).put("a", Entry(status="uploaded", title="T", start="2026-10-30T18:00:09+00:00", updated_at="2026-10-30T19:00:00+00:00", garmin_activity_id=42))
    again = StateStore(p)
    assert again.exists() and again.get("a").garmin_activity_id == 42 and again.is_done("a")


def test_pending_is_not_done(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.put("a", Entry(status="pending", title="T", start="s", updated_at="u", attempts=2, last_error="boom"))
    assert not store.is_done("a")


def test_file_format(tmp_path):
    p = tmp_path / "state.json"
    StateStore(p).put("a", Entry(status="skipped", title="T", start="s", updated_at="u"))
    data = json.loads(p.read_text())
    assert data["version"] == 1 and data["activities"]["a"]["status"] == "skipped"


def test_atomic_write_leaves_no_temp_files(tmp_path):
    StateStore(tmp_path / "state.json").put("a", Entry(status="skipped", title="T", start="s", updated_at="u"))
    assert [f.name for f in tmp_path.iterdir()] == ["state.json"]


@pytest.mark.parametrize("content", CORRUPT_CONTENTS)
def test_corrupt_file_raises(tmp_path, content):  # Review Focus 3
    p = tmp_path / "state.json"; p.write_text(content)
    with pytest.raises(StateCorruptError):
        StateStore(p).get("a")


@pytest.mark.parametrize("content", CORRUPT_CONTENTS)
def test_load_raises_for_corrupt_file(tmp_path, content):
    p = tmp_path / "state.json"; p.write_text(content)
    store = StateStore(p)
    assert store.exists()
    with pytest.raises(StateCorruptError, match=str(p)):
        store.load()
    with pytest.raises(StateCorruptError):
        store.load()


def test_load_missing_file_does_nothing(tmp_path):
    p = tmp_path / "state.json"
    store = StateStore(p)
    store.load()
    assert not store.exists() and store.get("x") is None


def test_load_is_idempotent(tmp_path):
    p = tmp_path / "state.json"
    StateStore(p).put("a", Entry(status="uploaded", title="T", start="s", updated_at="u", garmin_activity_id=7))
    store = StateStore(p)
    store.load()
    p.write_text("{not json")
    store.load()
    assert store.get("a").garmin_activity_id == 7


def test_put_on_corrupt_file_raises_and_keeps_file(tmp_path):
    p = tmp_path / "state.json"; p.write_text("{not json")
    store = StateStore(p)
    with pytest.raises(StateCorruptError):
        store.put("a", Entry(status="skipped", title="T", start="s", updated_at="u"))
    assert p.read_text() == "{not json"
    assert [f.name for f in tmp_path.iterdir()] == ["state.json"]


def _entry(status="baseline"):
    return Entry(status=status, title="T", start="s", updated_at="u")


def test_put_many_with_empty_dict_creates_file(tmp_path):
    p = tmp_path / "state.json"
    store = StateStore(p)
    assert not store.exists()
    store.put_many({})
    assert store.exists()
    assert json.loads(p.read_text()) == {"version": 1, "activities": {}}
    assert StateStore(p).get("a") is None


def test_put_many_merges_into_existing_entries_in_one_write(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    store = StateStore(p)
    store.put("old", _entry("uploaded"))
    writes = []
    original_write = StateStore._write
    monkeypatch.setattr(StateStore, "_write", lambda self, e: (writes.append(len(e)), original_write(self, e)))

    store.put_many({"a": _entry(), "b": _entry()})

    assert writes == [3]
    again = StateStore(p)
    assert [again.get(k).status for k in ("old", "a", "b")] == ["uploaded", "baseline", "baseline"]
    assert [f.name for f in tmp_path.iterdir()] == ["state.json"]


def test_put_many_on_corrupt_file_raises_and_keeps_file(tmp_path):
    p = tmp_path / "state.json"; p.write_text("{not json")
    with pytest.raises(StateCorruptError):
        StateStore(p).put_many({"a": _entry()})
    assert p.read_text() == "{not json"
