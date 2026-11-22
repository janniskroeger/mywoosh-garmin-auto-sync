import logging
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from mywhoosh_garmin_sync.mywhoosh import MyWhooshUnavailableError
from mywhoosh_garmin_sync.mywhoosh_workout_json import (
    content_hash,
    marker,
    parse_zwo,
    to_mywhoosh_json,
    workout_id_for,
    workout_name,
)
from mywhoosh_garmin_sync.mywhoosh_workout_state import MyWhooshWorkoutEntry, MyWhooshWorkoutStateStore
from mywhoosh_garmin_sync.mywhoosh_workout_sync import (
    CONFIRM_POLL_SECONDS,
    CONFIRM_TIMEOUT_SECONDS,
    MAX_WORKOUT_ATTEMPTS,
    SLOT_POLL_SECONDS,
    SLOT_SETTLE_SECONDS,
    sync_mywhoosh_workouts,
)
from mywhoosh_garmin_sync.mywhoosh_workouts import MyWhooshNoSlotsError, MyWhooshWorkoutError, RemoteWorkout
from mywhoosh_garmin_sync.opentri import OpenTriAuthError, OpenTriExportError, PlannedBike
from zwofactory import make_zwo

TODAY = date(2026, 11, 9)
NOW = datetime(2026, 11, 9, 6, 0, tzinfo=UTC)
DAYS = 14
SLOTS = 10
EASY = make_zwo("Grundlage", ['<SteadyState Duration="600" Power="0.6"/>'])
HARD = make_zwo("Grundlage", ['<SteadyState Duration="900" Power="0.9"/>'])


def day(offset: int) -> date:
    return TODAY + timedelta(days=offset)


def bike(pid: str, offset: int = 0, training: str = "t1", name: str = "Grundlage", brick: int | None = None,
         week: int = 3) -> PlannedBike:
    return PlannedBike(planned_id=pid, day=day(offset), brick_order=brick, training_id=training,
                       training_name=name, week=week)


def json_for(b: PlannedBike, zwo: bytes = EASY) -> dict:
    return to_mywhoosh_json(parse_zwo(zwo), workout_name(b.day, b.week, b.brick_order, b.training_name),
                            b.planned_id)


class FakeSource:
    def __init__(self, bikes=None, zwos=None, export_errors=(), error: Exception | None = None):
        self.bikes = bikes
        self.zwos = zwos if zwos is not None else {}
        self.export_errors = set(export_errors)
        self.error = error
        self.calls: list[tuple] = []

    def fetch_planned_bikes(self, start, end):
        self.calls.append(("bikes", start, end))
        if self.error is not None:
            raise self.error
        return None if self.bikes is None else list(self.bikes)

    def fetch_zwo(self, training_id):
        self.calls.append(("zwo", training_id))
        if training_id in self.export_errors:
            raise OpenTriExportError(f"Training {training_id} nicht exportierbar")
        return self.zwos.get(training_id, EASY)

    def zwo_calls(self):
        return [c for c in self.calls if c[0] == "zwo"]


class FakeLibrary:
    """Custom workouts in memory. upload is an upsert over Id, delete removes one entry (probe).

    Every applied upsert gives the entry a new ``updatedAt``. With ``credit_lag`` a deleted slot
    is hidden from the next ``credit_lag`` ``credits()`` calls (MyWhoosh frees it ~2 s later).
    """

    def __init__(self, visible_after_polls: int = 1, credit_lag: int = 0):
        self.entries: list[dict] = []  # {"Id", "Name", "Description", "json", "updatedAt"}
        self.calls: list[tuple] = []
        self.visible_after_polls = visible_after_polls
        self.credit_lag = credit_lag
        self._lagging: list[int] = []  # remaining credits() calls per freed slot
        self._tick = 0
        self.drop_writes: list[str] = []  # ops whose next call is accepted but never becomes visible
        self.failures: dict[str, list[Exception]] = {}
        self._pending: list[list] = []  # [remaining polls, apply]

    # helpers
    def _stamp(self):
        self._tick += 1
        return f"2026-11-09T06:00:{self._tick:02}.000Z"

    def seed(self, workout_json=None, *, workout_id=None, name="Fremd", description=None, stamped=True):
        updated_at = self._stamp() if stamped else None
        if workout_json is not None:
            self.entries.append({"Id": workout_json["Id"], "Name": workout_json["Name"],
                                 "Description": workout_json["Description"], "json": workout_json,
                                 "updatedAt": updated_at})
        else:
            self.entries.append({"Id": workout_id, "Name": name, "Description": description, "json": None,
                                 "updatedAt": updated_at})

    def fail(self, op, exc):
        self.failures.setdefault(op, []).append(exc)

    def writes(self):
        return [c for c in self.calls if c[0] in ("upload", "delete")]

    def ops(self):
        return [c[0] for c in self.calls]

    def ids(self):
        return sorted(e["Id"] for e in self.entries)

    def _write(self, op, apply):
        failures = self.failures.get(op)
        if failures:
            raise failures.pop(0)
        if op in self.drop_writes:
            self.drop_writes.remove(op)
            return
        self._pending.append([self.visible_after_polls, apply])

    # WorkoutLibrary
    def list(self):
        self.calls.append(("list",))
        for item in self._pending:
            item[0] -= 1
            if item[0] <= 0:
                item[1]()
        self._pending = [item for item in self._pending if item[0] > 0]
        return [RemoteWorkout(e["Id"], e["Name"], e["Description"], e["updatedAt"]) for e in self.entries]

    def credits(self):
        self.calls.append(("credits",))
        hidden = len(self._lagging)
        self._lagging = [left - 1 for left in self._lagging if left > 1]
        return max(SLOTS - len(self.entries) - hidden, 0)

    def upload(self, workout_json):
        self.calls.append(("upload", workout_json["Id"], workout_json["Name"]))

        def apply():
            for entry in self.entries:
                if entry["Id"] == workout_json["Id"]:
                    entry.update(Name=workout_json["Name"], Description=workout_json["Description"],
                                 json=workout_json, updatedAt=self._stamp())
                    return
            self.seed(workout_json)

        self._write("upload", apply)

    def delete(self, workout_id):
        self.calls.append(("delete", workout_id))

        def apply():
            for index, entry in enumerate(self.entries):
                if entry["Id"] == workout_id:
                    del self.entries[index]
                    if self.credit_lag:
                        self._lagging.append(self.credit_lag)
                    return

        self._write("delete", apply)


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def store(tmp_path):
    return MyWhooshWorkoutStateStore(tmp_path / "mywhoosh_workouts.json")


@pytest.fixture
def library():
    return FakeLibrary()


def run(source, library, store, clock=None, today=TODAY):
    clock = clock or Clock()
    return sync_mywhoosh_workouts(source, library, store, today, DAYS, lambda: NOW,
                                  sleep=clock.sleep, clock=clock.clock)


def seed_own(library, store, b, zwo=EASY, *, in_state=True):
    """An own workout in MyWhoosh (and in the state) as an earlier run would have left it."""
    workout_json = json_for(b, zwo)
    library.seed(workout_json)
    if in_state:
        store.put(MyWhooshWorkoutEntry(b.planned_id, workout_json["Name"], b.day, workout_json["Id"],
                                       content_hash(workout_json), 0, None, "t"))
    return workout_json


def fresh(library):
    library.calls.clear()


# --- create, change, delete --------------------------------------------------------------


def test_creates_new_sessions_with_dated_names_and_derived_ids(library, store):
    source = FakeSource([bike("p1", 0), bike("p2", 1, name="Sweet Spot", training="t2")])

    summary = run(source, library, store)

    uploads = [c for c in library.calls if c[0] == "upload"]
    assert uploads == [
        ("upload", workout_id_for("p1"), "Mo 09.11. W3: Grundlage"),
        ("upload", workout_id_for("p2"), "Di 10.11. W3: Sweet Spot"),
    ]
    assert summary.created == 2
    entries = store.all()
    assert entries["p1"].workout_id == workout_id_for("p1")
    assert entries["p2"].workout_id == workout_id_for("p2")
    assert entries["p1"].content_hash == content_hash(json_for(bike("p1", 0)))
    assert entries["p1"].updated_at == NOW.isoformat()
    assert sorted(e["Description"] for e in library.entries) == [marker("p1"), marker("p2")]


def test_second_run_without_change_writes_nothing(library, store):
    source = FakeSource([bike("p1", 0), bike("p2", 1)])
    run(source, library, store)
    fresh(library)

    summary = run(source, library, store)

    assert library.writes() == []
    assert (summary.created, summary.updated, summary.deleted) == (0, 0, 0)


def test_moved_session_is_updated_in_place(library, store):
    run(FakeSource([bike("p1", 0)]), library, store)
    fresh(library)

    summary = run(FakeSource([bike("p1", 2)]), library, store)

    assert library.writes() == [("upload", workout_id_for("p1"), "Mi 11.11. W3: Grundlage")]
    assert summary.updated == 1
    assert library.ids() == [workout_id_for("p1")]
    assert store.all()["p1"].day == day(2)


def test_changed_content_is_updated_in_place(library, store):
    run(FakeSource([bike("p1", 0)]), library, store)
    fresh(library)

    summary = run(FakeSource([bike("p1", 0)], zwos={"t1": HARD}), library, store)

    assert library.writes() == [("upload", workout_id_for("p1"), "Mo 09.11. W3: Grundlage")]
    assert summary.updated == 1
    assert library.entries[0]["json"]["Time"] == 900
    assert store.all()["p1"].content_hash == content_hash(json_for(bike("p1", 0), HARD))


def test_session_gone_from_opentri_is_deleted_and_forgotten(library, store):
    run(FakeSource([bike("p1", 0), bike("p2", 1)]), library, store)
    fresh(library)

    summary = run(FakeSource([bike("p1", 0)]), library, store)

    assert library.writes() == [("delete", workout_id_for("p2"))]
    assert summary.deleted == 1
    assert library.ids() == [workout_id_for("p1")]
    assert set(store.all()) == {"p1"}


def test_past_day_is_deleted(library, store):
    seed_own(library, store, bike("old", -1))

    summary = run(FakeSource([]), library, store)

    assert library.writes() == [("delete", workout_id_for("old"))]
    assert summary.deleted == 1
    assert library.entries == []
    assert store.all() == {}


def test_manually_deleted_workout_is_recreated(library, store):
    run(FakeSource([bike("p1", 0)]), library, store)
    library.entries.clear()
    fresh(library)

    summary = run(FakeSource([bike("p1", 0)]), library, store)

    assert library.writes() == [("upload", workout_id_for("p1"), "Mo 09.11. W3: Grundlage")]
    assert summary.created == 1
    assert store.all()["p1"].workout_id == workout_id_for("p1")


def test_lost_state_adopts_marked_workouts_without_duplicates(library, store, tmp_path):
    source = FakeSource([bike("p1", 0), bike("p2", 1)])
    run(source, library, store)
    fresh(library)
    lost = MyWhooshWorkoutStateStore(tmp_path / "lost.json")

    summary = run(source, library, lost)

    # adopted workouts are upserted exactly once with the same Id (no slot, no duplicate)
    assert summary.adopted == 2
    assert sorted(c for c in library.writes()) == sorted([
        ("upload", workout_id_for("p1"), "Mo 09.11. W3: Grundlage"),
        ("upload", workout_id_for("p2"), "Di 10.11. W3: Grundlage"),
    ])
    assert library.ids() == sorted([workout_id_for("p1"), workout_id_for("p2")])
    assert summary.created == 0
    assert {pid: e.workout_id for pid, e in lost.all().items()} == {
        "p1": workout_id_for("p1"), "p2": workout_id_for("p2"),
    }
    fresh(library)

    again = run(source, library, lost)

    assert library.writes() == []
    assert again.adopted == 0


def test_marked_workout_with_foreign_id_is_deleted(library, store):
    library.seed(workout_id=4242, name="Alt", description=marker("p1"))

    summary = run(FakeSource([bike("p1", 0)]), library, store)

    assert library.writes() == [("delete", 4242), ("upload", workout_id_for("p1"), "Mo 09.11. W3: Grundlage")]
    assert library.ids() == [workout_id_for("p1")]
    assert (summary.deleted, summary.created) == (1, 1)


def test_two_entries_same_id_delete_one_then_reupload(library, store):
    b = bike("p1", 0)
    seed_own(library, store, b)
    seed_own(library, store, b, in_state=False)

    summary = run(FakeSource([b]), library, store)

    assert library.writes() == [("delete", workout_id_for("p1")), ("upload", workout_id_for("p1"),
                                                                    "Mo 09.11. W3: Grundlage")]
    assert library.ids() == [workout_id_for("p1")]
    assert summary.deleted == 1 and summary.updated == 1
    fresh(library)
    run(FakeSource([b]), library, store)
    assert library.writes() == []


def test_unmarked_workouts_are_never_touched(library, store):
    for number in range(8):
        library.seed(workout_id=500 + number, name=f"Eigen {number}", description="mein Workout")
    source = FakeSource([bike("p1", 0), bike("p2", 1), bike("p3", 2)])

    summary = run(source, library, store)

    assert all(c[1] not in range(500, 508) for c in library.writes())
    assert [c[1] for c in library.writes()] == [workout_id_for("p1"), workout_id_for("p2")]
    assert summary.deferred == 1
    assert len([e for e in library.entries if e["Description"] == "mein Workout"]) == 8
    fresh(library)
    run(FakeSource([]), library, store)
    assert sorted(c[1] for c in library.writes()) == sorted([workout_id_for("p1"), workout_id_for("p2")])
    assert library.ids() == list(range(500, 508))


def test_delete_skipped_when_workout_id_shared_with_unmarked_entry(library, store, caplog):
    seed_own(library, store, bike("p1", 0))
    library.seed(workout_id=workout_id_for("p1"), name="Fremd", description=None)

    with caplog.at_level(logging.WARNING):
        run(FakeSource([]), library, store)

    assert library.writes() == []
    assert any(r.levelno == logging.WARNING and str(workout_id_for("p1")) in r.getMessage()
               for r in caplog.records)
    assert len(library.entries) == 2


def test_derived_id_taken_by_unmarked_workout_is_skipped(library, store, caplog):
    library.seed(workout_id=workout_id_for("p1"), name="Fremd", description="von Hand")

    with caplog.at_level(logging.WARNING):
        summary = run(FakeSource([bike("p1", 0)]), library, store)

    assert library.writes() == []
    assert summary.skipped == 1
    assert any(r.levelno == logging.WARNING and "fremd" in r.getMessage().lower() for r in caplog.records)
    assert library.entries[0]["Name"] == "Fremd"


# --- slots -----------------------------------------------------------------------------------


def test_slots_limit_creates_earliest_and_warns_once(library, store, caplog):
    bikes = [bike(f"p{n:02}", n) for n in range(12)]

    with caplog.at_level(logging.WARNING):
        summary = run(FakeSource(bikes), library, store)

    uploads = [c[1] for c in library.writes()]
    assert uploads == [workout_id_for(f"p{n:02}") for n in range(10)]
    assert summary.deferred == 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "2 Radeinheiten" in warnings[0].getMessage()
    assert day(10).isoformat() in warnings[0].getMessage()


def test_earlier_session_pushes_out_latest(library, store):
    bikes = [bike(f"p{n:02}", n) for n in range(1, 11)]
    run(FakeSource(bikes), library, store)
    assert len(library.entries) == 10
    fresh(library)

    summary = run(FakeSource([bike("new", 0), *bikes]), library, store)

    assert library.writes() == [("delete", workout_id_for("p10")),
                                ("upload", workout_id_for("new"), "Mo 09.11. W3: Grundlage")]
    assert (summary.deleted, summary.created, summary.deferred) == (1, 1, 1)
    assert "p10" not in store.all()


def test_update_does_not_need_a_free_slot(library, store):
    seed_own(library, store, bike("p1", 0))
    for number in range(9):
        library.seed(workout_id=500 + number, description=None)

    summary = run(FakeSource([bike("p1", 0)], zwos={"t1": HARD}), library, store)

    assert library.writes() == [("upload", workout_id_for("p1"), "Mo 09.11. W3: Grundlage")]
    assert summary.updated == 1


def test_no_slots_error_defers_new_without_attempts_but_still_updates(library, store):
    seed_own(library, store, bike("p2", 1))
    library.fail("upload", MyWhooshNoSlotsError("Not enough Slot Credits"))
    source = FakeSource([bike("p1", 0), bike("p2", 1), bike("p3", 2)], zwos={"t1": HARD})

    summary = run(source, library, store)

    assert [c[1] for c in library.writes()] == [workout_id_for("p1"), workout_id_for("p2")]
    assert summary.deferred == 2  # p1 rejected, p3 not tried
    assert summary.updated == 1
    assert summary.failed == 0
    assert "p1" not in store.all() or store.all()["p1"].attempts == 0


# --- confirmation -------------------------------------------------------------------------


def test_each_write_is_confirmed_before_the_next(store):
    library = FakeLibrary(visible_after_polls=2)
    clock = Clock()

    run(FakeSource([bike("p1", 0), bike("p2", 1)]), library, store, clock)

    ops = library.ops()
    first, second = ops.index("upload"), len(ops) - 1 - ops[::-1].index("upload")
    assert ops[first + 1:second] == ["list", "list"]
    assert ops[second + 1:] == ["list", "list"]
    assert clock.sleeps == [CONFIRM_POLL_SECONDS, CONFIRM_POLL_SECONDS]


def test_dropped_delete_stops_writing_and_keeps_state(library, store, caplog):
    run(FakeSource([bike("p1", 0), bike("p2", 1)]), library, store)
    fresh(library)
    library.drop_writes = ["delete"]
    clock = Clock()

    with caplog.at_level(logging.WARNING):
        summary = run(FakeSource([]), library, store, clock)

    assert len(library.writes()) == 1 and library.writes()[0][0] == "delete"
    assert summary.unconfirmed is True
    assert summary.aborted is None
    assert summary.deleted == 0
    assert clock.now >= CONFIRM_TIMEOUT_SECONDS
    assert clock.now <= CONFIRM_TIMEOUT_SECONDS + CONFIRM_POLL_SECONDS
    entries = store.all()
    assert entries["p1"].workout_id == workout_id_for("p1")
    assert entries["p2"].workout_id == workout_id_for("p2")
    assert any(r.levelno == logging.WARNING and "bestätigt" in r.getMessage() for r in caplog.records)


def test_dropped_content_only_update_is_not_confirmed(library, store, caplog):
    # same Id and Name before and after: only a new updatedAt proves the upsert was applied
    b = bike("p1", 0)
    run(FakeSource([b]), library, store)
    fresh(library)
    library.drop_writes = ["upload"]
    clock = Clock()

    with caplog.at_level(logging.WARNING):
        summary = run(FakeSource([b], zwos={"t1": HARD}), library, store, clock)

    assert library.writes() == [("upload", workout_id_for("p1"), "Mo 09.11. W3: Grundlage")]
    assert summary.unconfirmed is True
    assert summary.updated == 0
    assert clock.now >= CONFIRM_TIMEOUT_SECONDS
    assert store.all()["p1"].content_hash == content_hash(json_for(b))
    assert any(r.levelno == logging.WARNING and "bestätigt" in r.getMessage() for r in caplog.records)
    fresh(library)

    again = run(FakeSource([b], zwos={"t1": HARD}), library, store)

    assert library.writes() == [("upload", workout_id_for("p1"), "Mo 09.11. W3: Grundlage")]
    assert again.updated == 1 and again.unconfirmed is False
    assert library.entries[0]["json"]["Time"] == 900
    assert store.all()["p1"].content_hash == content_hash(json_for(b, HARD))


def test_dropped_adoption_upsert_is_not_confirmed(library, store):
    b = bike("p1", 0)
    seed_own(library, store, b, in_state=False)
    library.drop_writes = ["upload"]

    summary = run(FakeSource([b]), library, store)

    assert summary.adopted == 1
    assert summary.unconfirmed is True and summary.updated == 0
    assert store.all()["p1"].content_hash is None  # still uploaded once in a later run


def test_update_without_updated_at_falls_back_to_id_and_name(library, store):
    b = bike("p1", 0)
    workout_json = json_for(b)
    library.seed(workout_json, stamped=False)
    store.put(MyWhooshWorkoutEntry("p1", workout_json["Name"], b.day, workout_json["Id"],
                                   content_hash(workout_json), 0, None, "t"))

    summary = run(FakeSource([b], zwos={"t1": HARD}), library, store)

    assert summary.updated == 1 and summary.unconfirmed is False
    assert store.all()["p1"].content_hash == content_hash(json_for(b, HARD))


def test_push_out_waits_for_the_freed_slot(store, caplog):
    library = FakeLibrary(credit_lag=2)
    bikes = [bike(f"p{n:02}", n) for n in range(1, 11)]
    run(FakeSource(bikes), library, store)
    fresh(library)
    clock = Clock()

    with caplog.at_level(logging.WARNING):
        summary = run(FakeSource([bike("new", 0), *bikes]), library, store, clock)

    assert library.writes() == [("delete", workout_id_for("p10")),
                                ("upload", workout_id_for("new"), "Mo 09.11. W3: Grundlage")]
    assert (summary.deleted, summary.created, summary.deferred) == (1, 1, 1)  # only p10 is deferred
    assert clock.sleeps == [SLOT_POLL_SECONDS, SLOT_POLL_SECONDS]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "1 Radeinheiten" in warnings[0] and day(10).isoformat() in warnings[0]
    assert store.all()["new"].workout_id == workout_id_for("new")


def test_push_out_defers_when_the_slot_stays_taken(store):
    library = FakeLibrary(credit_lag=100)
    bikes = [bike(f"p{n:02}", n) for n in range(1, 11)]
    run(FakeSource(bikes), library, store)
    fresh(library)
    clock = Clock()

    summary = run(FakeSource([bike("new", 0), *bikes]), library, store, clock)

    assert library.writes() == [("delete", workout_id_for("p10"))]
    assert (summary.deleted, summary.created, summary.deferred) == (1, 0, 2)
    assert clock.now == SLOT_SETTLE_SECONDS
    assert clock.sleeps == [SLOT_POLL_SECONDS] * int(SLOT_SETTLE_SECONDS / SLOT_POLL_SECONDS)
    assert "new" not in store.all() or store.all()["new"].workout_id is None


def test_no_slot_wait_without_a_delete(library, store):
    clock = Clock()

    summary = run(FakeSource([bike(f"p{n:02}", n) for n in range(12)]), library, store, clock)

    assert summary.deferred == 2
    assert clock.sleeps == []


def test_rejected_delete_does_not_stop_uploads(library, store, caplog):
    seed_own(library, store, bike("old", 1))
    library.fail("delete", MyWhooshWorkoutError("MyWhoosh hat das Löschen abgelehnt: Workout is assigned"))

    with caplog.at_level(logging.ERROR):
        summary = run(FakeSource([bike("new", 0)]), library, store)

    assert library.writes() == [("delete", workout_id_for("old")),
                                ("upload", workout_id_for("new"), "Mo 09.11. W3: Grundlage")]
    assert summary.aborted is None
    assert (summary.failed, summary.created, summary.deleted) == (1, 1, 0)
    assert store.all()["old"].workout_id == workout_id_for("old")
    assert any(r.levelno == logging.ERROR and "Workout is assigned" in r.getMessage() for r in caplog.records)


# --- review focus -------------------------------------------------------------------------


def test_same_training_twice_fetches_zwo_once_and_creates_two(library, store):
    source = FakeSource([bike("p1", 0, training="t1"), bike("p2", 1, training="t1")])

    summary = run(source, library, store)

    assert source.zwo_calls() == [("zwo", "t1")]
    assert [c[1] for c in library.writes()] == [workout_id_for("p1"), workout_id_for("p2")]
    assert summary.created == 2


def test_run_after_midnight_deletes_yesterday_keeps_today(library, store):
    run(FakeSource([bike("y", -1), bike("t", 0)]), library, store, today=day(-1))
    fresh(library)

    summary = run(FakeSource([bike("t", 0)]), library, store)

    assert library.writes() == [("delete", workout_id_for("y"))]
    assert summary.deleted == 1
    assert set(store.all()) == {"t"}
    assert library.ids() == [workout_id_for("t")]


def test_marked_orphan_outside_wanted_is_deleted(library, store):
    library.seed(workout_id=workout_id_for("q"), name="Alt", description=marker("q"))

    summary = run(FakeSource([bike("p1", 0)]), library, store)

    assert library.writes()[0] == ("delete", workout_id_for("q"))
    assert summary.deleted == 1
    assert "q" not in store.all()
    assert library.ids() == [workout_id_for("p1")]


def test_unavailable_after_delete_keeps_state_consistent(library, store, caplog):
    seed_own(library, store, bike("old", 1))
    library.fail("upload", MyWhooshUnavailableError("MyWhoosh antwortete mit HTTP 503"))
    source = FakeSource([bike("new", 0)])

    with caplog.at_level(logging.ERROR):
        summary = run(source, library, store)

    assert summary.aborted is not None
    assert summary.deleted == 1
    assert store.all() == {}
    assert any(r.levelno == logging.ERROR and "abgebrochen" in r.getMessage() for r in caplog.records)
    fresh(library)

    second = run(source, library, store)

    assert library.writes() == [("upload", workout_id_for("new"), "Mo 09.11. W3: Grundlage")]
    assert second.aborted is None
    assert set(store.all()) == {"new"}


# --- errors --------------------------------------------------------------------------------


def test_unrenderable_session_is_skipped_and_keeps_existing_workout(library, store, caplog):
    seed_own(library, store, bike("p1", 0, training="t1"))
    source = FakeSource([bike("p1", 0, training="t1"), bike("p2", 1, training="t2")], export_errors={"t1", "t2"})

    with caplog.at_level(logging.WARNING):
        summary = run(source, library, store)

    assert library.writes() == []
    assert summary.skipped == 2
    assert store.all()["p1"].workout_id == workout_id_for("p1")
    assert library.ids() == [workout_id_for("p1")]
    assert sum(r.levelno == logging.WARNING for r in caplog.records) == 2


def test_unrenderable_zwo_counts_as_skipped(library, store):
    source = FakeSource([bike("p1", 0)], zwos={"t1": b"<kaputt"})

    summary = run(source, library, store)

    assert library.writes() == []
    assert summary.skipped == 1


def test_rejected_workout_counts_attempts_and_stops_after_max(library, store, caplog):
    source = FakeSource([bike("p1", 0)])
    for _ in range(MAX_WORKOUT_ATTEMPTS + 2):
        library.fail("upload", MyWhooshWorkoutError("MyWhoosh hat das Workout abgelehnt: kaputt"))

    with caplog.at_level(logging.ERROR):
        for _ in range(MAX_WORKOUT_ATTEMPTS + 1):
            summary = run(source, library, store)

    assert len(library.writes()) == MAX_WORKOUT_ATTEMPTS
    assert summary.failed == 0
    entry = store.all()["p1"]
    assert entry.attempts == MAX_WORKOUT_ATTEMPTS
    assert "kaputt" in entry.last_error
    assert entry.workout_id is None
    assert any(r.levelno == logging.ERROR and "kaputt" in r.getMessage() for r in caplog.records)
    # a change in open-tri allows new attempts
    library.failures.clear()
    fresh(library)
    run(FakeSource([bike("p1", 0)], zwos={"t1": HARD}), library, store)
    assert len(library.writes()) == 1
    assert store.all()["p1"].attempts == 0


def test_no_active_plan_deletes_own_future_workouts(library, store):
    run(FakeSource([bike("p1", 0), bike("p2", 3)]), library, store)
    library.seed(workout_id=777, description=None)
    fresh(library)

    summary = run(FakeSource(None), library, store)

    assert sorted(library.writes()) == sorted([("delete", workout_id_for("p1")), ("delete", workout_id_for("p2"))])
    assert summary.deleted == 2
    assert library.ids() == [777]
    assert store.all() == {}


def test_corrupt_state_aborts_without_writes(library, store, tmp_path, caplog):
    (tmp_path / "mywhoosh_workouts.json").write_text("{kaputt")
    source = FakeSource([bike("p1", 0)])

    with caplog.at_level(logging.ERROR):
        summary = run(source, library, store)

    assert summary.aborted is not None
    assert library.calls == []
    assert source.calls == []
    assert (tmp_path / "mywhoosh_workouts.json").read_text() == "{kaputt"
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_opentri_auth_error_aborts_without_writes(library, store, caplog):
    seed_own(library, store, bike("p1", 0))
    before = store.all()

    with caplog.at_level(logging.ERROR):
        summary = run(FakeSource(error=OpenTriAuthError("Token abgelehnt")), library, store)

    assert summary.aborted is not None
    assert library.calls == []
    assert store.all() == before
    assert any(r.levelno == logging.ERROR and "Token abgelehnt" in r.getMessage() for r in caplog.records)


def test_summary_is_logged(library, store, caplog):
    with caplog.at_level(logging.INFO):
        run(FakeSource([bike("p1", 0)]), library, store)

    assert any("MyWhoosh-Abgleich beendet: 1 angelegt" in r.getMessage() for r in caplog.records)


def test_window_is_today_to_today_plus_days_minus_one(library, store):
    source = FakeSource([])

    run(source, library, store)

    assert source.calls[0] == ("bikes", TODAY, TODAY + timedelta(days=DAYS - 1))


def test_brick_name(library, store):
    run(FakeSource([replace(bike("p1", 6), brick_order=1)]), library, store)

    assert library.writes()[0][2] == "So 15.11. W3: 2. Grundlage"
