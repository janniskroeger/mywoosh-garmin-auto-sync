import logging
from datetime import date
from types import SimpleNamespace

import pytest
import requests
from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from mywhoosh_garmin_sync.garmin import GarminAuthError, GarminUnavailableError, GarminUploader
from mywhoosh_garmin_sync.garmin_workouts import (
    GarminWorkoutError,
    GarminWorkouts,
    ScheduledEntry,
)


class FakeGarmin:
    """Stands in for garminconnect.Garmin; replays configured results or raises them."""

    instances: list["FakeGarmin"] = []
    login_error: Exception | None = None
    results: dict[str, object] = {}

    def __init__(self, email=None, password=None, **kwargs):
        self.calls: list[tuple] = []
        FakeGarmin.instances.append(self)

    def login(self, tokenstore=None):
        if FakeGarmin.login_error is not None:
            raise FakeGarmin.login_error
        return None, None

    def _answer(self, name, *args):
        self.calls.append((name, *args))
        result = FakeGarmin.results.get(name)
        if isinstance(result, Exception):
            raise result
        return result

    def upload_workout(self, workout):
        return self._answer("upload_workout", workout)

    def update_workout(self, workout_id, workout):
        return self._answer("update_workout", workout_id, workout)

    def delete_workout(self, workout_id):
        return self._answer("delete_workout", workout_id)

    def schedule_workout(self, workout_id, day):
        return self._answer("schedule_workout", workout_id, day)

    def reschedule_workout(self, schedule_id, day):
        return self._answer("reschedule_workout", schedule_id, day)

    def unschedule_workout(self, schedule_id):
        return self._answer("unschedule_workout", schedule_id)

    def get_scheduled_workouts_by_period(self, start, end):
        return self._answer("get_scheduled_workouts_by_period", start, end)

    def get_workout_by_id(self, workout_id):
        return self._answer("get_workout_by_id", workout_id)

    def send_scheduled_workouts_to_devices(self):
        return self._answer("send_scheduled_workouts_to_devices")


@pytest.fixture(autouse=True)
def reset_fake():
    FakeGarmin.instances = []
    FakeGarmin.login_error = None
    FakeGarmin.results = {}


def make_workouts(tmp_path):
    uploader = GarminUploader(
        "me@example.com",
        "pw-secret",
        tmp_path / "tokens",
        client_factory=FakeGarmin,
        sleep=lambda _seconds: None,
    )
    return GarminWorkouts(uploader)


def http_error(message: str, status: int) -> GarminConnectConnectionError:
    exc = GarminConnectConnectionError(message)
    exc.response = SimpleNamespace(status_code=status)
    return exc


def test_create_returns_workout_id(tmp_path):
    FakeGarmin.results["upload_workout"] = {"workoutId": 77, "workoutName": "x"}
    workouts = make_workouts(tmp_path)

    assert workouts.create({"workoutName": "x"}) == 77
    assert ("upload_workout", {"workoutName": "x"}) in FakeGarmin.instances[0].calls


def test_update_passes_id_and_json(tmp_path):
    workouts = make_workouts(tmp_path)

    workouts.update(77, {"workoutName": "y"})

    assert ("update_workout", 77, {"workoutName": "y"}) in FakeGarmin.instances[0].calls


def test_delete_calls_library(tmp_path):
    workouts = make_workouts(tmp_path)

    workouts.delete(77)

    assert ("delete_workout", 77) in FakeGarmin.instances[0].calls


def test_schedule_returns_schedule_id_and_uses_iso_date(tmp_path):
    FakeGarmin.results["schedule_workout"] = {"workoutScheduleId": 555}
    workouts = make_workouts(tmp_path)

    assert workouts.schedule(77, date(2026, 11, 9)) == 555
    assert ("schedule_workout", 77, "2026-11-09") in FakeGarmin.instances[0].calls


def test_reschedule_uses_iso_date(tmp_path):
    workouts = make_workouts(tmp_path)

    workouts.reschedule(555, date(2026, 11, 10))

    assert ("reschedule_workout", 555, "2026-11-10") in FakeGarmin.instances[0].calls


def test_unschedule_calls_library(tmp_path):
    workouts = make_workouts(tmp_path)

    workouts.unschedule(555)

    assert ("unschedule_workout", 555) in FakeGarmin.instances[0].calls


def test_scheduled_maps_entries(tmp_path):
    FakeGarmin.results["get_scheduled_workouts_by_period"] = [
        {
            "workoutScheduleId": 1,
            "calendarDate": "2026-11-09",
            "workout": {"workoutId": 10, "workoutName": "Lauf", "description": "Locker"},
        },
        {
            "workoutScheduleId": 2,
            "calendarDate": "2026-11-10",
            "workout": {"workoutId": 11, "workoutName": "Rad", "description": None},
        },
    ]
    workouts = make_workouts(tmp_path)

    entries = workouts.scheduled(date(2026, 11, 9), date(2026, 11, 15))

    assert entries == [
        ScheduledEntry(1, 10, date(2026, 11, 9), "Lauf", "Locker"),
        ScheduledEntry(2, 11, date(2026, 11, 10), "Rad", None),
    ]
    assert (
        "get_scheduled_workouts_by_period",
        "2026-11-09",
        "2026-11-15",
    ) in FakeGarmin.instances[0].calls


def test_scheduled_skips_incomplete_entries_with_warning(tmp_path, caplog):
    FakeGarmin.results["get_scheduled_workouts_by_period"] = [
        {"calendarDate": "2026-11-09", "workout": {"workoutId": 10, "workoutName": "a"}},
        {"workoutScheduleId": 2, "calendarDate": "2026-11-09", "workout": {"workoutName": "b"}},
        {"workoutScheduleId": 3, "workout": {"workoutId": 12, "workoutName": "c"}},
        {"workoutScheduleId": 4, "calendarDate": "kaputt", "workout": {"workoutId": 13}},
        {
            "workoutScheduleId": 5,
            "calendarDate": "2026-11-11",
            "workout": {"workoutId": 14, "workoutName": "ok"},
        },
    ]
    workouts = make_workouts(tmp_path)

    with caplog.at_level(logging.WARNING):
        entries = workouts.scheduled(date(2026, 11, 9), date(2026, 11, 15))

    assert entries == [ScheduledEntry(5, 14, date(2026, 11, 11), "ok", None)]
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 4


def test_scheduled_unexpected_shape_is_workout_error(tmp_path):
    FakeGarmin.results["get_scheduled_workouts_by_period"] = {"not": "a list"}
    workouts = make_workouts(tmp_path)

    with pytest.raises(GarminWorkoutError):
        workouts.scheduled(date(2026, 11, 9), date(2026, 11, 15))


def test_create_without_workout_id_is_workout_error(tmp_path):
    FakeGarmin.results["upload_workout"] = {"oops": 1}
    workouts = make_workouts(tmp_path)

    with pytest.raises(GarminWorkoutError):
        workouts.create({})


def test_schedule_without_schedule_id_is_workout_error(tmp_path):
    FakeGarmin.results["schedule_workout"] = None
    workouts = make_workouts(tmp_path)

    with pytest.raises(GarminWorkoutError):
        workouts.schedule(1, date(2026, 11, 9))


def test_description_reads_workout(tmp_path):
    FakeGarmin.results["get_workout_by_id"] = {"workoutId": 5, "description": "Text"}
    workouts = make_workouts(tmp_path)

    assert workouts.description(5) == "Text"
    assert ("get_workout_by_id", 5) in FakeGarmin.instances[0].calls


def test_description_missing_is_none(tmp_path):
    FakeGarmin.results["get_workout_by_id"] = {"workoutId": 5}
    workouts = make_workouts(tmp_path)

    assert workouts.description(5) is None


def test_send_to_devices_calls_library(tmp_path):
    workouts = make_workouts(tmp_path)

    workouts.send_to_devices()

    assert ("send_scheduled_workouts_to_devices",) in FakeGarmin.instances[0].calls


@pytest.mark.parametrize("via_cause", [False, True])
def test_delete_and_unschedule_tolerate_404(tmp_path, via_cause):
    def not_found():
        if via_cause:
            wrapper = GarminConnectConnectionError("wrapped")
            wrapper.__cause__ = http_error("gone", 404)
            return wrapper
        return http_error("gone", 404)

    FakeGarmin.results["delete_workout"] = not_found()
    FakeGarmin.results["unschedule_workout"] = not_found()
    workouts = make_workouts(tmp_path)

    workouts.delete(1)
    workouts.unschedule(2)


def test_404_is_an_error_for_other_operations(tmp_path):
    FakeGarmin.results["update_workout"] = http_error("gone", 404)
    workouts = make_workouts(tmp_path)

    with pytest.raises(GarminWorkoutError):
        workouts.update(1, {})


@pytest.mark.parametrize("status", [400, 404, 409, 500])
def test_http_error_is_workout_error_with_garmin_message(tmp_path, status):
    FakeGarmin.results["upload_workout"] = http_error("Step invalid", status)
    workouts = make_workouts(tmp_path)

    with pytest.raises(GarminWorkoutError, match="Step invalid"):
        workouts.create({})


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("via_cause", [False, True])
def test_rejected_session_drops_session_and_is_unavailable(tmp_path, status, via_cause):
    if via_cause:
        error = GarminConnectConnectionError("wrapped")
        error.__cause__ = http_error("API Error", status)
    else:
        error = http_error("API Error", status)
    workouts = make_workouts(tmp_path)
    workouts.send_to_devices()
    FakeGarmin.results["upload_workout"] = error

    with pytest.raises(GarminUnavailableError):
        workouts.create({})

    FakeGarmin.results["upload_workout"] = {"workoutId": 9}
    assert workouts.create({}) == 9
    assert len(FakeGarmin.instances) == 2


@pytest.mark.parametrize(
    "error",
    [
        http_error("API Error 429", 429),
        GarminConnectConnectionError("connection reset"),
        GarminConnectTooManyRequestsError("429"),
        requests.ConnectionError("down"),
        requests.Timeout("slow"),
    ],
)
def test_rate_limit_and_network_are_unavailable(tmp_path, error):
    FakeGarmin.results["upload_workout"] = error
    workouts = make_workouts(tmp_path)

    with pytest.raises(GarminUnavailableError):
        workouts.create({})
    # The session is kept: the next call reuses the same client.
    FakeGarmin.results["upload_workout"] = {"workoutId": 1}
    workouts.create({})
    assert len(FakeGarmin.instances) == 1


def test_library_authentication_error_drops_session(tmp_path):
    FakeGarmin.results["upload_workout"] = GarminConnectAuthenticationError("expired")
    workouts = make_workouts(tmp_path)

    with pytest.raises(GarminUnavailableError):
        workouts.create({})

    FakeGarmin.results["upload_workout"] = {"workoutId": 1}
    workouts.create({})
    assert len(FakeGarmin.instances) == 2


def test_auth_error_propagates_unchanged(tmp_path):
    FakeGarmin.login_error = GarminConnectAuthenticationError("Invalid credentials")
    workouts = make_workouts(tmp_path)

    with pytest.raises(GarminAuthError):
        workouts.create({})
    with pytest.raises(GarminAuthError):
        workouts.scheduled(date(2026, 11, 9), date(2026, 11, 10))


def test_login_unavailable_propagates_unchanged(tmp_path):
    FakeGarmin.login_error = GarminConnectConnectionError("connection reset")
    workouts = make_workouts(tmp_path)

    with pytest.raises(GarminUnavailableError, match="Anmeldung"):
        workouts.create({})


@pytest.mark.parametrize("error", [KeyError("x"), TypeError("y"), ValueError("no json")])
def test_unexpected_parsing_exception_is_workout_error(tmp_path, error):
    FakeGarmin.results["upload_workout"] = error
    workouts = make_workouts(tmp_path)

    with pytest.raises(GarminWorkoutError):
        workouts.create({})


@pytest.mark.parametrize("empty", [{}, None])
def test_scheduled_empty_answer_is_empty_calendar(tmp_path, empty):
    # garminconnect turns an HTTP 204 into an object whose json() is {}
    FakeGarmin.results["get_scheduled_workouts_by_period"] = empty
    workouts = make_workouts(tmp_path)

    assert workouts.scheduled(date(2026, 11, 9), date(2026, 11, 22)) == []


def test_http_answer_marks_the_session_as_answered(tmp_path):
    workouts = make_workouts(tmp_path)
    FakeGarmin.results["upload_workout"] = {"workoutId": 1}
    workouts.create({"workoutName": "x"})
    assert workouts._uploader._fresh_login is False


def test_http_error_answer_marks_the_session_as_answered(tmp_path):
    FakeGarmin.results["update_workout"] = http_error("bad", 400)
    workouts = make_workouts(tmp_path)

    with pytest.raises(GarminWorkoutError):
        workouts.update(1, {})

    assert workouts._uploader._fresh_login is False


def test_tolerated_404_marks_the_session_as_answered(tmp_path):
    FakeGarmin.results["delete_workout"] = http_error("gone", 404)
    workouts = make_workouts(tmp_path)

    workouts.delete(1)

    assert workouts._uploader._fresh_login is False
