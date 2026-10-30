import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fit_tool import FitFile
from fit_tool.profile.messages.file_id_message import FileIdMessage

from fitfactory import make_mywhoosh_like_fit
from mywhoosh_garmin_sync.garmin import (
    GarminAuthError,
    GarminImportError,
    GarminUnavailableError,
    ImportOutcome,
)
from mywhoosh_garmin_sync.mywhoosh import (
    MyWhooshActivity,
    MyWhooshAuthError,
    MyWhooshConnectionError,
    MyWhooshDownloadError,
    MyWhooshUnavailableError,
)
from mywhoosh_garmin_sync.state import Entry, StateStore
from mywhoosh_garmin_sync.sync import MAX_ATTEMPTS, dry_run, run_once

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
BASE_START = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)


def activity(file_id: str, hours: int = 0, sport: str = "cycling") -> MyWhooshActivity:
    return MyWhooshActivity(
        file_id=file_id,
        title=f"Ride {file_id}",
        sport_type=sport,
        start=BASE_START + timedelta(hours=hours),
    )


class FakeSource:
    def __init__(self, activities, fits=None, list_error=None):
        self.activities = activities
        self.fits = fits or {}
        self.list_error = list_error
        self.downloads: list[str] = []

    def list_recent(self):
        if self.list_error is not None:
            raise self.list_error
        return list(self.activities)

    def download_fit(self, file_id):
        self.downloads.append(file_id)
        result = self.fits.get(file_id, make_mywhoosh_like_fit())
        if isinstance(result, Exception):
            raise result
        return result


class FakeGarmin:
    def __init__(self, outcomes=None, login_error=None, rename_error=None):
        self.outcomes = outcomes if outcomes is not None else []
        self.login_error = login_error
        self.rename_error = rename_error
        self.login_calls = 0
        self.imports: list[tuple[Path, datetime, bytes]] = []
        self.renames: list[tuple[int, str]] = []
        self._next_id = 123

    def ensure_login(self):
        self.login_calls += 1
        if self.login_error is not None:
            raise self.login_error

    def import_fit(self, path, start):
        self.imports.append((path, start, path.read_bytes()))
        if self.outcomes:
            outcome = self.outcomes.pop(0)
        else:
            outcome = ImportOutcome("uploaded", self._next_id)
            self._next_id += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def set_name(self, activity_id, title):
        if self.rename_error is not None:
            raise self.rename_error
        self.renames.append((activity_id, title))


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "state.json")


@pytest.fixture
def work_dir(tmp_path):
    return tmp_path / "work"


def baseline_entry(a: MyWhooshActivity) -> Entry:
    return Entry("baseline", a.title, a.start.isoformat(), NOW.isoformat())


def seed_baseline(store: StateStore, *activities: MyWhooshActivity) -> None:
    for a in activities:
        store.put(a.file_id, baseline_entry(a))


def run(source, garmin, store, work_dir):
    return run_once(source, garmin, store, work_dir, now=lambda: NOW)


def product_of(fit_bytes: bytes) -> int:
    file_ids = [r.message for r in FitFile.from_bytes(fit_bytes).records if isinstance(r.message, FileIdMessage)]
    return file_ids[0].product


def test_first_run_creates_baseline_without_uploading(store, work_dir):
    a, b = activity("A"), activity("B", hours=1)
    source, garmin = FakeSource([b, a]), FakeGarmin()

    summary = run(source, garmin, store, work_dir)

    assert summary.baseline == 2
    assert summary.new == 0
    assert summary.aborted is None
    assert garmin.login_calls == 0
    assert source.downloads == []
    assert store.get("A").status == "baseline"
    assert store.get("B").status == "baseline"
    assert store.get("A").updated_at == NOW.isoformat()


def test_baseline_is_one_atomic_write(store, work_dir, monkeypatch):
    writes = []
    original_write = StateStore._write

    def counting_write(self, entries):
        writes.append(len(entries))
        original_write(self, entries)

    monkeypatch.setattr(StateStore, "_write", counting_write)

    summary = run(FakeSource([activity("B", hours=1), activity("A")]), FakeGarmin(), store, work_dir)

    assert summary.baseline == 2
    assert writes == [2]


def test_first_run_with_empty_list_persists_state_so_next_ride_is_uploaded(store, work_dir):
    garmin = FakeGarmin()

    first = run(FakeSource([]), garmin, store, work_dir)

    assert first.baseline == 0
    assert store.exists()

    second = run(FakeSource([activity("A")]), garmin, StateStore(store._path), work_dir)

    assert second.baseline == 0
    assert second.uploaded == 1
    assert StateStore(store._path).get("A").status == "uploaded"


def test_new_ride_is_patched_imported_and_renamed(store, work_dir):
    a, b = activity("A"), activity("B", hours=1)
    seed_baseline(store, a)
    source, garmin = FakeSource([b, a]), FakeGarmin()

    summary = run(source, garmin, store, work_dir)

    assert len(garmin.imports) == 1
    path, start, content = garmin.imports[0]
    assert start == b.start
    assert product_of(content) == 4440
    assert garmin.renames == [(123, b.title)]
    entry = store.get("B")
    assert entry.status == "uploaded"
    assert entry.garmin_activity_id == 123
    assert entry.title == b.title
    assert entry.start == b.start.isoformat()
    assert entry.updated_at == NOW.isoformat()
    assert summary.new == 1
    assert summary.uploaded == 1
    assert list(work_dir.iterdir()) == []


def test_processes_oldest_first(store, work_dir):
    seed_baseline(store, activity("A"))
    newer, older = activity("N", hours=5), activity("O", hours=2)
    garmin = FakeGarmin()

    run(FakeSource([newer, older, activity("A")]), garmin, store, work_dir)

    assert [start for _, start, _ in garmin.imports] == [older.start, newer.start]


def test_nothing_new_does_not_touch_garmin(store, work_dir):
    a, b = activity("A"), activity("B", hours=1)
    seed_baseline(store, a, b)
    garmin = FakeGarmin()
    source = FakeSource([b, a])

    summary = run(source, garmin, store, work_dir)

    assert garmin.login_calls == 0
    assert source.downloads == []
    assert summary.new == 0


def test_non_cycling_is_skipped(store, work_dir):
    seed_baseline(store, activity("A"))
    r = activity("R", hours=1, sport="running")
    source, garmin = FakeSource([r, activity("A")]), FakeGarmin()

    summary = run(source, garmin, store, work_dir)

    assert store.get("R").status == "skipped"
    assert source.downloads == []
    assert garmin.imports == []
    assert garmin.login_calls == 0
    assert summary.skipped == 1
    assert summary.new == 1


def test_duplicate_is_done(store, work_dir):
    seed_baseline(store, activity("A"))
    garmin = FakeGarmin(outcomes=[ImportOutcome("duplicate", None)])

    summary = run(FakeSource([activity("B", hours=1), activity("A")]), garmin, store, work_dir)

    assert store.get("B").status == "duplicate"
    assert garmin.renames == []
    assert summary.duplicates == 1
    assert store.is_done("B")


def test_rename_failure_keeps_uploaded(store, work_dir, caplog):
    seed_baseline(store, activity("A"))
    garmin = FakeGarmin(rename_error=RuntimeError("boom"))
    source = FakeSource([activity("B", hours=1), activity("A")])

    with caplog.at_level(logging.WARNING):
        summary = run(source, garmin, store, work_dir)

    assert store.get("B").status == "uploaded"
    assert store.get("B").garmin_activity_id == 123
    assert summary.uploaded == 1
    assert any(r.levelno == logging.WARNING for r in caplog.records)

    run(source, garmin, store, work_dir)
    assert len(garmin.imports) == 1


def test_upload_without_activity_id_skips_rename(store, work_dir, caplog):
    seed_baseline(store, activity("A"))
    garmin = FakeGarmin(outcomes=[ImportOutcome("uploaded", None)])

    with caplog.at_level(logging.WARNING):
        run(FakeSource([activity("B", hours=1), activity("A")]), garmin, store, work_dir)

    assert store.get("B").status == "uploaded"
    assert store.get("B").garmin_activity_id is None
    assert garmin.renames == []
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def make_failure(kind: str):
    """Return (source_fits, garmin_outcome) that make one ride fail in the given way."""
    if kind == "download":
        return {"B": MyWhooshDownloadError("Download kaputt")}, None
    if kind == "patch":
        return {"B": b"not fit"}, None
    return {}, GarminImportError("Import abgelehnt")


@pytest.mark.parametrize("kind", ["download", "patch", "import"])
def test_ride_errors_count_attempts_until_failed(store, work_dir, caplog, kind):
    a, b = activity("A"), activity("B", hours=1)
    seed_baseline(store, a)
    fits, error = make_failure(kind)
    source = FakeSource([b, a], fits=fits)

    def garmin_for_run():
        return FakeGarmin(outcomes=[error] if error else None)

    for attempt in range(1, MAX_ATTEMPTS):
        summary = run(source, garmin_for_run(), store, work_dir)
        entry = store.get("B")
        assert entry.status == "pending"
        assert entry.attempts == attempt
        assert entry.last_error
        assert summary.retry_later == 1
        assert summary.failed == 0

    with caplog.at_level(logging.ERROR):
        summary = run(source, garmin_for_run(), store, work_dir)
    entry = store.get("B")
    assert entry.status == "failed"
    assert entry.attempts == MAX_ATTEMPTS
    assert summary.failed == 1
    assert any(r.levelno == logging.ERROR for r in caplog.records)

    garmin = garmin_for_run()
    downloads_before = len(source.downloads)
    summary = run(source, garmin, store, work_dir)
    assert len(source.downloads) == downloads_before
    assert garmin.imports == []
    assert summary.new == 0
    assert list(work_dir.iterdir()) == []


def test_one_failing_ride_does_not_block_others(store, work_dir):
    seed_baseline(store, activity("A"))
    first, second = activity("B", hours=1), activity("C", hours=2)
    source = FakeSource(
        [second, first, activity("A")], fits={"B": MyWhooshDownloadError("kaputt")}
    )
    garmin = FakeGarmin()

    summary = run(source, garmin, store, work_dir)

    assert store.get("B").status == "pending"
    assert store.get("C").status == "uploaded"
    assert summary.retry_later == 1
    assert summary.uploaded == 1


@pytest.mark.parametrize("error", [GarminAuthError("abgelehnt"), GarminUnavailableError("offline")])
def test_garmin_login_failure_aborts_without_counting(store, work_dir, error):
    seed_baseline(store, activity("A"))
    garmin = FakeGarmin(login_error=error)

    summary = run(FakeSource([activity("B", hours=1), activity("A")]), garmin, store, work_dir)

    assert summary.aborted
    assert store.get("B") is None
    assert garmin.imports == []


@pytest.mark.parametrize("error", [GarminAuthError("abgelehnt"), GarminUnavailableError("offline")])
def test_garmin_import_failure_aborts_without_counting(store, work_dir, error):
    seed_baseline(store, activity("A"))
    garmin = FakeGarmin(outcomes=[error])
    source = FakeSource([activity("C", hours=2), activity("B", hours=1), activity("A")])

    summary = run(source, garmin, store, work_dir)

    assert summary.aborted
    assert store.get("B") is None
    assert store.get("C") is None
    assert len(garmin.imports) == 1
    assert list(work_dir.iterdir()) == []


@pytest.mark.parametrize(
    "error",
    [
        MyWhooshAuthError("login"),
        MyWhooshUnavailableError("offline"),
        MyWhooshConnectionError("Verbindung"),
    ],
)
def test_mywhoosh_download_abort_keeps_earlier_rides(store, work_dir, error):
    seed_baseline(store, activity("A"))
    source = FakeSource(
        [activity("C", hours=2), activity("B", hours=1), activity("A")], fits={"C": error}
    )

    summary = run(source, FakeGarmin(), store, work_dir)

    assert summary.aborted
    assert store.get("B").status == "uploaded"
    assert store.get("C") is None
    assert summary.uploaded == 1
    assert summary.retry_later == 0


@pytest.mark.parametrize("error", [MyWhooshUnavailableError("offline"), MyWhooshAuthError("login")])
def test_mywhoosh_list_failure_aborts(store, work_dir, error):
    seed_baseline(store, activity("A"))
    before = (store._path).read_text()
    garmin = FakeGarmin()

    summary = run(FakeSource([], list_error=error), garmin, store, work_dir)

    assert summary.aborted
    assert garmin.login_calls == 0
    assert store._path.read_text() == before


def test_mywhoosh_list_failure_on_first_run_creates_no_state(store, work_dir):
    summary = run(FakeSource([], list_error=MyWhooshUnavailableError("offline")), FakeGarmin(), store, work_dir)

    assert summary.aborted
    assert not store.exists()


def test_corrupt_state_aborts_without_new_baseline(store, work_dir):
    store._path.write_text("{kaputt")
    source, garmin = FakeSource([activity("A")]), FakeGarmin()

    summary = run(source, garmin, store, work_dir)

    assert summary.aborted
    assert store._path.read_text() == "{kaputt"
    assert garmin.imports == []
    assert garmin.login_calls == 0
    assert source.downloads == []


def test_work_dir_is_created(store, tmp_path):
    seed_baseline(store, activity("A"))
    work_dir = tmp_path / "deep" / "work"

    run(FakeSource([activity("B", hours=1), activity("A")]), FakeGarmin(), store, work_dir)

    assert work_dir.is_dir()


def test_dry_run_writes_original_and_patched(tmp_path):
    older, newest = activity("A"), activity("B", hours=3)
    fits = {"A": make_mywhoosh_like_fit(30), "B": make_mywhoosh_like_fit(60)}
    source = FakeSource([older, newest], fits=fits)
    out_dir = tmp_path / "dry-run"

    original, patched = dry_run(source, out_dir)

    assert original == out_dir / "B.original.fit"
    assert patched == out_dir / "B.edge1050.fit"
    assert original.read_bytes() == fits["B"]
    assert product_of(patched.read_bytes()) == 4440

    original, patched = dry_run(source, out_dir, file_id="A")
    assert original == out_dir / "A.original.fit"
    assert original.read_bytes() == fits["A"]
    assert patched.name == "A.edge1050.fit"

    with pytest.raises(ValueError):
        dry_run(source, out_dir, file_id="nope")
