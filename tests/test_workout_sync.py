import copy
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest

from mywhoosh_garmin_sync import workout_sync
from mywhoosh_garmin_sync.garmin import GarminAuthError, GarminUnavailableError
from mywhoosh_garmin_sync.garmin_workouts import GarminWorkoutError, ScheduledEntry
from mywhoosh_garmin_sync.opentri import OpenTriAuthError, OpenTriUnavailableError
from mywhoosh_garmin_sync.workout_json import marker
from mywhoosh_garmin_sync.workout_state import WorkoutStateStore
from mywhoosh_garmin_sync.workout_sync import MAX_WORKOUT_ATTEMPTS, sync_workouts
from workoutfactory import make_bundle, make_schedule_fit, make_workout_fit

TODAY = date(2026, 11, 9)
NOW = datetime(2026, 11, 9, 6, 0, tzinfo=UTC)
STEPS = [{"name": "Locker", "intensity": "active", "duration_type": "time", "duration_time": 600}]
OTHER_STEPS = [{"name": "Hart", "intensity": "active", "duration_type": "time", "duration_time": 900}]
WRITES = {"create", "update", "delete", "schedule", "reschedule", "unschedule"}


def day(offset: int) -> date:
    return TODAY + timedelta(days=offset)


@dataclass
class W:
    serial: int
    name: str
    day: date
    sport: str = "running"
    steps: list | None = None


def bundle(*items: W) -> bytes:
    files = {
        f"{index}-{w.name}.fit": make_workout_fit(w.serial, w.name, w.sport, w.steps or STEPS)
        for index, w in enumerate(items)
    }
    files["Schedules.fit"] = make_schedule_fit({w.serial: w.day for w in items})
    return make_bundle(files)


class FakeSource:
    def __init__(self, result):
        self.result = result
        self.calls: list[tuple[date, date]] = []

    def fetch_bundle(self, start, end):
        self.calls.append((start, end))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeGarmin:
    """Library and calendar in memory; deleting a workout also drops its calendar entries (probe)."""

    def __init__(self):
        self.library: dict[int, dict] = {}
        self.calendar: dict[int, tuple[int, date]] = {}
        self.calls: list[tuple] = []
        self.ghosts: set[int] = set()  # workouts whose delete is a 404 while the calendar entry stays
        self._failures: list[tuple[str, Exception, object]] = []
        self._next_workout = 1000
        self._next_schedule = 5000

    # test helpers
    def fail(self, op, exc, match=None):
        self._failures.append((op, exc, match))

    def clear_failures(self):
        self._failures.clear()

    def seed(self, workout_json, on_day):
        """Put a workout into library and calendar without recording a call."""
        workout_id = self._new_workout_id()
        self.library[workout_id] = copy.deepcopy(workout_json)
        schedule_id = self._new_schedule_id()
        self.calendar[schedule_id] = (workout_id, on_day)
        return workout_id, schedule_id

    def ops(self):
        return [call[0] for call in self.calls]

    def writes(self):
        return [call for call in self.calls if call[0] in WRITES]

    def entries_for(self, serial):
        wanted = marker(serial)
        return sorted(
            (on_day, workout_id)
            for workout_id, on_day in self.calendar.values()
            if self.library.get(workout_id, {}).get("description") == wanted
        )

    def workouts_for(self, serial):
        return [wid for wid, j in self.library.items() if j.get("description") == marker(serial)]

    def _check(self, op, *args):
        for failing_op, exc, match in self._failures:
            if failing_op == op and (match is None or match(*args)):
                raise exc

    def _new_workout_id(self):
        self._next_workout += 1
        return self._next_workout

    def _new_schedule_id(self):
        self._next_schedule += 1
        return self._next_schedule

    # WorkoutTarget
    def create(self, workout_json):
        self.calls.append(("create", workout_json["description"]))
        self._check("create", workout_json)
        workout_id = self._new_workout_id()
        self.library[workout_id] = copy.deepcopy(workout_json)
        return workout_id

    def update(self, workout_id, workout_json):
        self.calls.append(("update", workout_id))
        self._check("update", workout_id, workout_json)
        if workout_id not in self.library:
            raise GarminWorkoutError("HTTP 404")
        self.library[workout_id] = copy.deepcopy(workout_json)

    def delete(self, workout_id):
        self.calls.append(("delete", workout_id))
        self._check("delete", workout_id)
        if workout_id in self.ghosts:
            return False  # Garmin answers 404: the workout is gone, but its calendar entry lingers
        existed = workout_id in self.library
        self.library.pop(workout_id, None)
        for schedule_id in [s for s, (w, _) in self.calendar.items() if w == workout_id]:
            del self.calendar[schedule_id]
        return existed

    def schedule(self, workout_id, on_day):
        self.calls.append(("schedule", workout_id, on_day))
        self._check("schedule", workout_id, on_day)
        if workout_id not in self.library:
            raise GarminWorkoutError("HTTP 404")
        schedule_id = self._new_schedule_id()
        self.calendar[schedule_id] = (workout_id, on_day)
        return schedule_id

    def reschedule(self, schedule_id, on_day):
        self.calls.append(("reschedule", schedule_id, on_day))
        self._check("reschedule", schedule_id, on_day)
        # unprobed how Garmin answers an unknown id; model the worst case: silent success
        if schedule_id in self.calendar:
            self.calendar[schedule_id] = (self.calendar[schedule_id][0], on_day)

    def unschedule(self, schedule_id):
        self.calls.append(("unschedule", schedule_id))
        self._check("unschedule", schedule_id)
        return self.calendar.pop(schedule_id, None) is not None

    def scheduled(self, start, end):
        self.calls.append(("scheduled", start, end))
        self._check("scheduled", start, end)
        return [
            ScheduledEntry(
                schedule_id=schedule_id,
                workout_id=workout_id,
                date=on_day,
                name=self.library[workout_id].get("workoutName", ""),
                description=self.library[workout_id].get("description"),
            )
            for schedule_id, (workout_id, on_day) in sorted(self.calendar.items(), key=lambda i: (i[1][1], i[0]))
            if start <= on_day <= end
        ]

    def description(self, workout_id):
        self.calls.append(("description", workout_id))
        return self.library[workout_id].get("description")

    def send_to_devices(self):
        self.calls.append(("send_to_devices",))
        self._check("send_to_devices")


@pytest.fixture
def store(tmp_path):
    return WorkoutStateStore(tmp_path / "workouts.json")


@pytest.fixture
def garmin():
    return FakeGarmin()


def run(source_result, garmin, store, today=TODAY, days=14):
    source = source_result if isinstance(source_result, FakeSource) else FakeSource(source_result)
    return sync_workouts(source, garmin, store, today=today, days=days, pool_length_m=25.0, now=lambda: NOW)


def state(store_path):
    """A fresh store read from disk, to check what was persisted."""
    return WorkoutStateStore(store_path).all()


LAUF = W(42, "Lauf", day(1))
RAD = W(43, "Rad", day(2), sport="cycling")


def test_new_workouts_are_created_scheduled_and_sent(garmin, store, tmp_path):
    source = FakeSource(bundle(LAUF, RAD))
    summary = run(source, garmin, store)

    assert summary.created == 2 and summary.aborted is None
    assert source.calls == [(TODAY, day(13))]
    assert garmin.calls[0] == ("scheduled", TODAY, day(13))
    assert garmin.ops().count("create") == 2 and garmin.ops().count("schedule") == 2
    assert garmin.ops()[-1] == "send_to_devices" and garmin.ops().count("send_to_devices") == 1
    assert garmin.entries_for(42) == [(day(1), garmin.workouts_for(42)[0])]
    assert garmin.entries_for(43) == [(day(2), garmin.workouts_for(43)[0])]

    saved = state(tmp_path / "workouts.json")
    lauf = saved[42]
    assert (lauf.status, lauf.day, lauf.name, lauf.in_library, lauf.attempts) == ("active", day(1), "Lauf", True, 0)
    assert lauf.workout_id == garmin.workouts_for(42)[0]
    assert garmin.calendar[lauf.schedule_id] == (lauf.workout_id, day(1))
    assert lauf.content_hash and lauf.updated_at == NOW.isoformat()


def test_nothing_changed_makes_no_garmin_writes(garmin, store):
    run(bundle(LAUF, RAD), garmin, store)
    garmin.calls.clear()

    summary = run(bundle(LAUF, RAD), garmin, store)

    assert garmin.calls == [("scheduled", TODAY, day(13))]
    assert (summary.created, summary.updated, summary.moved, summary.removed) == (0, 0, 0, 0)


def test_content_change_updates_workout(garmin, store, tmp_path):
    run(bundle(LAUF), garmin, store)
    before = state(tmp_path / "workouts.json")[42]
    garmin.calls.clear()

    summary = run(bundle(W(42, "Lauf", day(1), steps=OTHER_STEPS)), garmin, store)

    assert summary.updated == 1
    assert garmin.writes() == [("update", before.workout_id)]
    assert garmin.ops()[-1] == "send_to_devices"
    after = state(tmp_path / "workouts.json")[42]
    assert after.content_hash != before.content_hash
    assert garmin.library[before.workout_id]["workoutSegments"][0]["workoutSteps"][0]["endConditionValue"] == 900


def test_moved_day_reschedules(garmin, store, tmp_path):
    run(bundle(LAUF), garmin, store)
    before = state(tmp_path / "workouts.json")[42]
    garmin.calls.clear()

    summary = run(bundle(W(42, "Lauf", day(4))), garmin, store)

    assert summary.moved == 1
    assert garmin.writes() == [("reschedule", before.schedule_id, day(4))]
    after = state(tmp_path / "workouts.json")[42]
    assert (after.day, after.schedule_id) == (day(4), before.schedule_id)
    assert garmin.entries_for(42) == [(day(4), before.workout_id)]


def test_removed_in_opentri_deletes_workout_and_its_calendar_entry(garmin, store, tmp_path):
    run(bundle(LAUF, RAD), garmin, store)
    lauf = state(tmp_path / "workouts.json")[42]
    garmin.calls.clear()

    summary = run(bundle(RAD), garmin, store)

    assert summary.removed == 1
    assert garmin.writes() == [("delete", lauf.workout_id)]
    assert garmin.entries_for(42) == [] and garmin.workouts_for(42) == []
    after = state(tmp_path / "workouts.json")[42]
    assert (after.status, after.workout_id, after.schedule_id, after.in_library) == ("removed", None, None, False)


def test_past_day_becomes_past_and_is_left_alone(garmin, store, tmp_path):
    run(bundle(W(42, "Lauf", TODAY)), garmin, store)
    before = state(tmp_path / "workouts.json")[42]
    garmin.calls.clear()

    summary = run(None, garmin, store, today=day(1))

    assert garmin.writes() == [] and "send_to_devices" not in garmin.ops()
    assert summary.removed == 0
    after = state(tmp_path / "workouts.json")[42]
    assert (after.status, after.workout_id, after.schedule_id, after.in_library) == (
        "past", before.workout_id, before.schedule_id, True
    )
    assert garmin.calendar[before.schedule_id] == (before.workout_id, TODAY)


@pytest.mark.parametrize("flag", [True, False])
def test_past_library_cleanup_follows_flag(monkeypatch, garmin, store, tmp_path, flag):
    monkeypatch.setattr(workout_sync, "DELETE_PAST_LIBRARY_WORKOUTS", flag)
    run(bundle(W(42, "Lauf", TODAY)), garmin, store)
    before = state(tmp_path / "workouts.json")[42]
    garmin.calls.clear()

    run(None, garmin, store, today=day(1))

    after = state(tmp_path / "workouts.json")[42]
    assert "unschedule" not in garmin.ops()
    assert after.status == "past" and after.schedule_id == before.schedule_id
    if flag:
        assert garmin.writes() == [("delete", before.workout_id)]
        assert after.in_library is False
    else:
        assert garmin.writes() == []
        assert after.in_library is True and before.workout_id in garmin.library


def test_default_keeps_past_library_workouts():
    assert workout_sync.DELETE_PAST_LIBRARY_WORKOUTS is False


def test_missed_and_moved_forward_moves_the_existing_workout(garmin, store, tmp_path):
    run(bundle(W(42, "Lauf", TODAY)), garmin, store)
    before = state(tmp_path / "workouts.json")[42]
    garmin.calls.clear()

    summary = run(bundle(W(42, "Lauf", day(2))), garmin, store, today=day(1))

    assert summary.moved == 1 and summary.created == 0
    assert garmin.writes() == [("unschedule", before.schedule_id), ("schedule", before.workout_id, day(2))]
    after = state(tmp_path / "workouts.json")[42]
    assert (after.status, after.day, after.workout_id) == ("active", day(2), before.workout_id)
    assert garmin.entries_for(42) == [(day(2), before.workout_id)]
    assert before.schedule_id not in garmin.calendar


def test_missed_and_moved_forward_recreates_when_not_in_library(monkeypatch, garmin, store, tmp_path):
    monkeypatch.setattr(workout_sync, "DELETE_PAST_LIBRARY_WORKOUTS", True)
    run(bundle(W(42, "Lauf", TODAY)), garmin, store)
    run(None, garmin, store, today=day(1))
    past = state(tmp_path / "workouts.json")[42]
    assert past.in_library is False
    garmin.calls.clear()

    summary = run(bundle(W(42, "Lauf", day(3))), garmin, store, today=day(2))

    assert summary.moved == 1
    assert garmin.ops()[:4] == ["scheduled", "unschedule", "create", "schedule"]
    assert ("unschedule", past.schedule_id) in garmin.calls
    after = state(tmp_path / "workouts.json")[42]
    assert (after.status, after.day, after.in_library) == ("active", day(3), True)
    assert garmin.entries_for(42) == [(day(3), after.workout_id)]
    assert garmin.workouts_for(42) == [after.workout_id]


def test_missed_and_moved_forward_schedules_anew_when_past_entry_is_gone(garmin, store, tmp_path):
    run(bundle(W(42, "Lauf", TODAY)), garmin, store)
    before = state(tmp_path / "workouts.json")[42]
    del garmin.calendar[before.schedule_id]  # removed in Garmin after its day
    garmin.calls.clear()

    run(bundle(W(42, "Lauf", day(2))), garmin, store, today=day(1))

    assert garmin.writes() == [("unschedule", before.schedule_id), ("schedule", before.workout_id, day(2))]
    after = state(tmp_path / "workouts.json")[42]
    assert (after.status, after.day, after.attempts) == ("active", day(2), 0)
    assert garmin.entries_for(42) == [(day(2), before.workout_id)]


def test_missed_and_moved_forward_with_changed_content_updates_and_moves(garmin, store, tmp_path):
    run(bundle(W(42, "Lauf", TODAY)), garmin, store)
    before = state(tmp_path / "workouts.json")[42]
    garmin.calls.clear()

    summary = run(bundle(W(42, "Lauf", day(2), steps=OTHER_STEPS)), garmin, store, today=day(1))

    assert (summary.updated, summary.moved, summary.created) == (1, 1, 0)
    assert garmin.writes() == [
        ("update", before.workout_id),
        ("unschedule", before.schedule_id),
        ("schedule", before.workout_id, day(2)),
    ]
    after = state(tmp_path / "workouts.json")[42]
    assert after.content_hash != before.content_hash and after.status == "active"


def test_failed_reschedule_then_day_rollover_does_not_leave_a_stale_entry(garmin, store, tmp_path):
    run(bundle(W(42, "Lauf", day(3))), garmin, store)
    garmin.fail("reschedule", GarminWorkoutError("HTTP 400"))
    run(bundle(W(42, "Lauf", TODAY)), garmin, store)
    assert state(tmp_path / "workouts.json")[42].attempts == 1
    assert garmin.entries_for(42)[0][0] == day(3)  # Garmin still holds the old day
    garmin.clear_failures()
    garmin.calls.clear()

    summary = run(None, garmin, store, today=day(1))  # gone from open-tri the next day

    assert summary.removed == 1
    assert garmin.entries_for(42) == [] and garmin.workouts_for(42) == []
    assert state(tmp_path / "workouts.json")[42].status == "removed"


def test_missed_entry_moved_forward_in_garmin_only_is_kept_as_history(garmin, store, tmp_path):
    run(bundle(W(42, "Lauf", TODAY)), garmin, store)
    before = state(tmp_path / "workouts.json")[42]
    garmin.calendar[before.schedule_id] = (before.workout_id, day(1))  # operator moves it in Garmin
    garmin.calls.clear()

    # open-tri still plans it yesterday, so the export starting today does not contain it
    summary = run(None, garmin, store, today=day(1))

    assert garmin.writes() == [] and summary.removed == 0
    assert garmin.calendar[before.schedule_id] == (before.workout_id, day(1))
    after = state(tmp_path / "workouts.json")[42]
    assert (after.status, after.workout_id, after.schedule_id) == ("past", before.workout_id, before.schedule_id)


def test_moved_into_past_is_treated_as_removed(garmin, store, tmp_path):
    run(bundle(W(42, "Lauf", day(3))), garmin, store)
    before = state(tmp_path / "workouts.json")[42]
    garmin.calls.clear()

    # moved to yesterday in open-tri: the export starts today, so the workout is gone from it
    summary = run(None, garmin, store, today=day(1))

    assert summary.removed == 1
    assert garmin.writes() == [("delete", before.workout_id)]
    assert "schedule" not in garmin.ops()
    assert state(tmp_path / "workouts.json")[42].status == "removed"


def test_calendar_moved_in_garmin_is_corrected(garmin, store, tmp_path):
    run(bundle(LAUF), garmin, store)
    entry = state(tmp_path / "workouts.json")[42]
    garmin.calendar[entry.schedule_id] = (entry.workout_id, day(5))
    garmin.calls.clear()

    summary = run(bundle(LAUF), garmin, store)

    assert summary.moved == 1
    assert garmin.writes() == [("reschedule", entry.schedule_id, day(1))]
    assert garmin.entries_for(42) == [(day(1), entry.workout_id)]


@pytest.mark.parametrize("moved_to", [day(-3), day(20)])
def test_calendar_moved_in_garmin_outside_the_window_is_moved_back(garmin, store, tmp_path, moved_to):
    run(bundle(LAUF), garmin, store)
    entry = state(tmp_path / "workouts.json")[42]
    garmin.calendar[entry.schedule_id] = (entry.workout_id, moved_to)
    garmin.calls.clear()

    summary = run(bundle(LAUF), garmin, store)

    assert summary.moved == 1
    assert garmin.writes() == [("unschedule", entry.schedule_id), ("schedule", entry.workout_id, day(1))]
    assert garmin.entries_for(42) == [(day(1), entry.workout_id)]
    assert entry.schedule_id not in garmin.calendar


def test_calendar_entry_deleted_in_garmin_is_rescheduled(garmin, store, tmp_path):
    run(bundle(LAUF), garmin, store)
    entry = state(tmp_path / "workouts.json")[42]
    del garmin.calendar[entry.schedule_id]
    garmin.calls.clear()

    summary = run(bundle(LAUF), garmin, store)

    assert summary.moved == 1
    assert garmin.writes() == [("unschedule", entry.schedule_id), ("schedule", entry.workout_id, day(1))]
    after = state(tmp_path / "workouts.json")[42]
    assert after.schedule_id != entry.schedule_id
    assert garmin.entries_for(42) == [(day(1), entry.workout_id)]


def test_workout_deleted_in_garmin_is_recreated(garmin, store, tmp_path):
    run(bundle(LAUF), garmin, store)
    entry = state(tmp_path / "workouts.json")[42]
    garmin.library.pop(entry.workout_id)
    garmin.calendar.pop(entry.schedule_id)
    garmin.calls.clear()

    run(bundle(LAUF), garmin, store)

    after = state(tmp_path / "workouts.json")[42]
    assert "create" in garmin.ops()
    assert garmin.workouts_for(42) == [after.workout_id]
    assert garmin.entries_for(42) == [(day(1), after.workout_id)]
    assert after.attempts == 0


def test_unmarked_garmin_workouts_are_never_touched(garmin, store):
    own_id, own_schedule = garmin.seed({"workoutName": "Eigenes", "description": "Mein Training"}, day(1))
    coach_id, coach_schedule = garmin.seed({"workoutName": "Coach", "description": None}, day(2))
    run(bundle(LAUF), garmin, store)
    run(None, garmin, store)

    assert garmin.library[own_id]["workoutName"] == "Eigenes" and garmin.library[coach_id]
    assert garmin.calendar[own_schedule] == (own_id, day(1))
    assert garmin.calendar[coach_schedule] == (coach_id, day(2))
    touched = {arg for call in garmin.writes() for arg in call[1:]}
    assert not touched & {own_id, own_schedule, coach_id, coach_schedule}
    assert "description" not in garmin.ops()


def test_missing_state_adopts_marked_workouts(garmin, tmp_path):
    first = WorkoutStateStore(tmp_path / "first.json")
    run(bundle(LAUF, RAD), garmin, first)
    known = first.all()
    garmin.calls.clear()

    fresh = WorkoutStateStore(tmp_path / "lost.json")
    summary = run(bundle(LAUF, W(43, "Rad", day(3), sport="cycling")), garmin, fresh)

    assert summary.adopted == 2 and summary.created == 0
    assert "create" not in garmin.ops() and "description" not in garmin.ops()
    # the content of an adopted workout is unknown, so it is written once
    assert ("update", known[42].workout_id) in garmin.calls
    assert ("reschedule", known[43].schedule_id, day(3)) in garmin.calls
    adopted = fresh.all()
    assert adopted[42].workout_id == known[42].workout_id and adopted[42].schedule_id == known[42].schedule_id
    assert adopted[42].content_hash == known[42].content_hash and adopted[43].day == day(3)
    assert len(garmin.library) == 2 and len(garmin.calendar) == 2

    garmin.calls.clear()
    run(bundle(LAUF, W(43, "Rad", day(3), sport="cycling")), garmin, fresh)
    assert garmin.writes() == []


def test_duplicate_marked_entries_keep_one(garmin, store, tmp_path):
    json = {"workoutName": "Lauf", "description": marker(42)}
    first_id, first_schedule = garmin.seed(json, day(1))
    second_id, _ = garmin.seed(json, day(1))

    run(bundle(LAUF), garmin, store)

    assert ("delete", second_id) in garmin.calls and "create" not in garmin.ops()
    assert garmin.entries_for(42) == [(day(1), first_id)]
    assert state(tmp_path / "workouts.json")[42].schedule_id == first_schedule


def test_duplicate_schedule_of_the_same_workout_is_unscheduled(garmin, store, tmp_path):
    run(bundle(LAUF), garmin, store)
    entry = state(tmp_path / "workouts.json")[42]
    copy_schedule = garmin.schedule(entry.workout_id, day(4))
    garmin.calls.clear()

    run(bundle(LAUF), garmin, store)

    assert garmin.writes() == [("unschedule", copy_schedule)]
    assert garmin.entries_for(42) == [(day(1), entry.workout_id)]


def test_marked_orphan_not_in_opentri(garmin, store):
    future_id, _ = garmin.seed({"workoutName": "Alt", "description": marker(77)}, day(2))
    past_id, past_schedule = garmin.seed({"workoutName": "Vorbei", "description": marker(78)}, day(-2))

    summary = run(bundle(LAUF), garmin, store)

    assert summary.removed == 1
    assert ("delete", future_id) in garmin.calls and future_id not in garmin.library
    assert garmin.calendar[past_schedule] == (past_id, day(-2)) and past_id in garmin.library


def test_marked_orphan_with_gone_workout_is_unscheduled_once(garmin, store):
    ghost_id, ghost_schedule = garmin.seed({"workoutName": "Weg", "description": marker(77)}, day(2))
    garmin.ghosts.add(ghost_id)

    summary = run(bundle(LAUF), garmin, store)

    assert summary.removed == 1
    assert ("unschedule", ghost_schedule) in garmin.calls
    assert ghost_schedule not in garmin.calendar

    garmin.calls.clear()
    summary = run(bundle(LAUF), garmin, store)

    assert summary.removed == 0
    assert garmin.writes() == [] and "send_to_devices" not in garmin.ops()


def test_two_workouts_same_day(garmin, store):
    koppel = [W(42, "W3: Rad", day(2), sport="cycling"), W(44, "W3: 2. Lauf", day(2))]
    summary = run(bundle(*koppel), garmin, store)

    assert summary.created == 2
    assert len(garmin.entries_for(42)) == 1 and len(garmin.entries_for(44)) == 1
    assert garmin.entries_for(42)[0][0] == garmin.entries_for(44)[0][0] == day(2)

    garmin.calls.clear()
    run(bundle(*koppel), garmin, store)
    assert garmin.writes() == []


def test_no_active_plan_removes_future_entries(garmin, store, tmp_path):
    run(bundle(LAUF, RAD), garmin, store)
    garmin.calls.clear()

    summary = run(None, garmin, store)

    assert summary.removed == 2 and summary.aborted is None
    assert garmin.library == {} and garmin.calendar == {}
    assert {e.status for e in state(tmp_path / "workouts.json").values()} == {"removed"}
    assert garmin.ops()[-1] == "send_to_devices"


def test_removed_serial_reappearing_is_created_anew(garmin, store, tmp_path):
    run(bundle(LAUF), garmin, store)
    run(None, garmin, store)
    garmin.calls.clear()

    summary = run(bundle(LAUF), garmin, store)

    assert summary.created == 1
    assert garmin.ops().count("create") == 1
    assert state(tmp_path / "workouts.json")[42].status == "active"


@pytest.mark.parametrize("result", [
    b"not a zip file",
    OpenTriUnavailableError("open-tri nicht erreichbar"),
])
def test_bundle_error_aborts_without_writes(garmin, store, tmp_path, caplog, result):
    run(bundle(LAUF), garmin, store)
    saved = (tmp_path / "workouts.json").read_text()
    garmin.calls.clear()

    with caplog.at_level(logging.ERROR):
        summary = run(result, garmin, store, today=day(1))

    assert summary.aborted
    assert garmin.calls == []
    assert (tmp_path / "workouts.json").read_text() == saved
    assert any(r.levelno == logging.ERROR and "abgebrochen" in r.getMessage() for r in caplog.records)


def test_bundle_without_readable_workouts_aborts(garmin, store):
    run(bundle(LAUF), garmin, store)
    garmin.calls.clear()

    broken = make_bundle({"a.fit": make_workout_fit(42, "Lauf", "running", STEPS)})  # no Schedules.fit
    summary = run(broken, garmin, store)

    assert summary.aborted and garmin.calls == []


def test_bundle_with_only_unsupported_sports_is_an_empty_plan(garmin, store):
    run(bundle(LAUF), garmin, store)

    summary = run(bundle(W(50, "Kraft", day(1), sport="training")), garmin, store)

    assert summary.aborted is None and summary.removed == 1


NESTED_STEPS = [
    {"name": "A", "intensity": "active", "duration_type": "open"},
    {"name": "B", "intensity": "active", "duration_type": "open"},
    {"duration_type": "repeat_until_steps_cmplt", "duration_step": 1, "repeat_count": 2},
    {"duration_type": "repeat_until_steps_cmplt", "duration_step": 0, "repeat_count": 2},
]


def test_unreadable_workout_file_leaves_its_garmin_workout_alone(garmin, store, tmp_path):
    run(bundle(LAUF, RAD), garmin, store)
    saved = state(tmp_path / "workouts.json")
    garmin.calls.clear()

    unreadable = bundle(W(42, "Lauf", day(1), steps=NESTED_STEPS), W(43, "Rad", day(4), sport="cycling"))
    summary = run(unreadable, garmin, store)

    assert summary.aborted is None and summary.removed == 0 and summary.moved == 1
    assert garmin.writes() == [("reschedule", saved[43].schedule_id, day(4))]
    assert state(tmp_path / "workouts.json")[42] == saved[42]
    assert garmin.entries_for(42) == [(day(1), saved[42].workout_id)]


def test_workout_without_schedule_entry_leaves_its_garmin_workout_alone(garmin, store, tmp_path):
    run(bundle(LAUF, RAD), garmin, store)
    saved = state(tmp_path / "workouts.json")
    garmin.calls.clear()

    unscheduled = make_bundle({
        "a.fit": make_workout_fit(42, "Lauf", "running", OTHER_STEPS),
        "b.fit": make_workout_fit(43, "Rad", "cycling", STEPS),
        "Schedules.fit": make_schedule_fit({43: day(4)}),
    })
    summary = run(unscheduled, garmin, store)

    assert summary.aborted is None and summary.removed == 0 and summary.updated == 0
    assert garmin.writes() == [("reschedule", saved[43].schedule_id, day(4))]
    assert state(tmp_path / "workouts.json")[42] == saved[42]


def test_calendar_read_error_aborts_without_counting(garmin, store, tmp_path, caplog):
    run(bundle(LAUF), garmin, store)
    saved = (tmp_path / "workouts.json").read_text()
    garmin.fail("scheduled", GarminWorkoutError("Unerwartete Antwort von Garmin für den Kalender"))
    garmin.calls.clear()

    with caplog.at_level(logging.ERROR):
        summary = run(bundle(LAUF, RAD), garmin, store)

    assert summary.aborted and summary.failed == 0
    assert garmin.writes() == [] and "send_to_devices" not in garmin.ops()
    assert (tmp_path / "workouts.json").read_text() == saved
    assert any(r.levelno == logging.ERROR and "abgebrochen" in r.getMessage() for r in caplog.records)


def test_opentri_auth_error_aborts(garmin, store):
    summary = run(OpenTriAuthError("Bitte login-opentri ausführen"), garmin, store)

    assert summary.aborted and "login-opentri" in summary.aborted
    assert garmin.calls == []


@pytest.mark.parametrize("error", [GarminUnavailableError("weg"), GarminAuthError("abgelehnt")])
def test_garmin_unavailable_aborts_without_counting(garmin, store, tmp_path, error):
    garmin.fail("create", error, match=lambda j: j["description"] == marker(43))

    summary = run(bundle(LAUF, RAD), garmin, store)

    assert summary.aborted and summary.failed == 0
    assert garmin.ops().count("send_to_devices") == 1  # serial 42 was already written
    saved = state(tmp_path / "workouts.json")
    assert saved[42].status == "active" and saved[42].schedule_id is not None
    assert 43 not in saved


def test_garmin_abort_keeps_partial_progress_of_a_workout(garmin, store, tmp_path):
    garmin.fail("schedule", GarminUnavailableError("weg"))

    run(bundle(LAUF), garmin, store)

    partial = state(tmp_path / "workouts.json")[42]
    assert partial.workout_id is not None and partial.schedule_id is None and partial.attempts == 0
    garmin.clear_failures()
    garmin.calls.clear()

    run(bundle(LAUF), garmin, store)

    assert "create" not in garmin.ops()
    assert garmin.entries_for(42) == [(day(1), partial.workout_id)]


def test_single_workout_error_counts_attempts_until_failed(garmin, store, tmp_path, caplog):
    garmin.fail("create", GarminWorkoutError("HTTP 400: ungültig"), match=lambda j: j["description"] == marker(42))

    for attempt in range(1, MAX_WORKOUT_ATTEMPTS + 1):
        with caplog.at_level(logging.ERROR):
            summary = run(bundle(LAUF, RAD), garmin, store)
        entry = state(tmp_path / "workouts.json")[42]
        assert (entry.attempts, summary.failed, summary.aborted) == (attempt, 1, None)
        assert entry.last_error and "ungültig" in entry.last_error
        assert entry.workout_id is None and entry.status == "active"
    assert garmin.workouts_for(43) and len(garmin.workouts_for(43)) == 1
    assert any("ungültig" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)

    garmin.calls.clear()
    summary = run(bundle(LAUF, RAD), garmin, store)
    assert garmin.writes() == [] and summary.failed == 0

    garmin.clear_failures()
    run(bundle(W(42, "Lauf", day(1), steps=OTHER_STEPS), RAD), garmin, store)
    entry = state(tmp_path / "workouts.json")[42]
    assert entry.attempts == 0 and entry.last_error is None and entry.workout_id in garmin.library


def test_failed_update_is_retried_next_run(garmin, store, tmp_path):
    run(bundle(LAUF), garmin, store)
    garmin.fail("update", GarminWorkoutError("HTTP 400"))
    changed = bundle(W(42, "Lauf", day(1), steps=OTHER_STEPS))

    run(changed, garmin, store)
    assert state(tmp_path / "workouts.json")[42].attempts == 1

    garmin.clear_failures()
    garmin.calls.clear()
    summary = run(changed, garmin, store)

    entry = state(tmp_path / "workouts.json")[42]
    assert ("update", entry.workout_id) in garmin.calls and summary.updated == 1
    assert entry.attempts == 0
    assert garmin.library[entry.workout_id]["workoutSegments"][0]["workoutSteps"][0]["endConditionValue"] == 900


def test_corrupt_state_aborts(garmin, tmp_path):
    path = tmp_path / "workouts.json"
    path.write_text("{kaputt")

    summary = run(bundle(LAUF), garmin, WorkoutStateStore(path))

    assert summary.aborted and garmin.calls == []
    assert path.read_text() == "{kaputt"


def test_bundle_with_unsupported_sport_creates_only_the_supported_ones(garmin, store):
    summary = run(bundle(LAUF, W(50, "Kraft", day(1), sport="training")), garmin, store)

    assert summary.created == 1 and summary.skipped == 0
    assert garmin.ops().count("create") == 1 and garmin.workouts_for(42)


def test_duplicate_serial_in_bundle_is_skipped(garmin, store, caplog):
    with caplog.at_level(logging.WARNING):
        summary = run(bundle(LAUF, W(42, "Lauf Kopie", day(1))), garmin, store)

    assert summary.skipped == 1 and summary.created == 1
    assert len(garmin.workouts_for(42)) == 1
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_conversion_error_skips_workout_and_keeps_it_in_garmin(monkeypatch, garmin, store, tmp_path, caplog):
    run(bundle(LAUF, RAD), garmin, store)
    original = workout_sync.to_garmin_json

    def broken(workout, pool_length_m):
        if workout.serial == 42:
            raise KeyError("unbekannter Schritt")
        return original(workout, pool_length_m)

    monkeypatch.setattr(workout_sync, "to_garmin_json", broken)
    garmin.calls.clear()
    with caplog.at_level(logging.WARNING):
        summary = run(bundle(LAUF, RAD), garmin, store)

    assert summary.skipped == 1 and summary.removed == 0
    assert garmin.writes() == []
    assert state(tmp_path / "workouts.json")[42].status == "active"
    assert any("Lauf" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


def test_send_to_devices_failure_is_only_a_warning(garmin, store, caplog):
    garmin.fail("send_to_devices", GarminWorkoutError("HTTP 500"))

    with caplog.at_level(logging.WARNING):
        summary = run(bundle(LAUF), garmin, store)

    assert summary.aborted is None and summary.created == 1
    assert any(r.levelno == logging.WARNING and "Geräte" in r.getMessage() for r in caplog.records)


def test_logs_one_german_line_per_training_and_a_summary(garmin, store, caplog):
    with caplog.at_level(logging.INFO):
        run(bundle(LAUF, RAD), garmin, store)

    messages = [r.getMessage() for r in caplog.records]
    assert f"Lauf ({day(1).isoformat()}): angelegt" in messages
    assert f"Rad ({day(2).isoformat()}): angelegt" in messages
    assert any(m.startswith("Trainings-Abgleich beendet: 2 angelegt") for m in messages)
