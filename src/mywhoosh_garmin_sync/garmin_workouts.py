"""Garmin Connect workouts and calendar: thin wrapper with a uniform error mapping."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, TypeVar

import requests
from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from mywhoosh_garmin_sync.garmin import (
    _SESSION_REJECTED_STATUSES,
    GarminUnavailableError,
    GarminUploader,
    _http_status,
)

logger = logging.getLogger(__name__)

_NOT_FOUND = 404
_RATE_LIMITED = 429

T = TypeVar("T")


class GarminWorkoutError(Exception):
    """Garmin rejected a single workout or calendar operation."""


@dataclass(frozen=True)
class ScheduledEntry:
    schedule_id: int
    workout_id: int
    date: date
    name: str
    description: str | None


class GarminWorkouts:
    """Creates, schedules and removes workouts through a shared GarminUploader session."""

    def __init__(self, uploader: GarminUploader) -> None:
        self._uploader = uploader

    def create(self, workout_json: dict[str, Any]) -> int:
        return self._run(
            "Training anlegen",
            lambda client: int(client.upload_workout(workout_json)["workoutId"]),
        )

    def update(self, workout_id: int, workout_json: dict[str, Any]) -> None:
        self._run(
            "Training aktualisieren",
            lambda client: client.update_workout(workout_id, workout_json),
        )

    def delete(self, workout_id: int) -> bool:
        """Delete a workout; False when Garmin no longer knows it (404)."""
        return self._gone_is_fine("Training löschen", lambda client: client.delete_workout(workout_id))

    def schedule(self, workout_id: int, day: date) -> int:
        return self._run(
            "Training einplanen",
            lambda client: int(
                client.schedule_workout(workout_id, day.isoformat())["workoutScheduleId"]
            ),
        )

    def reschedule(self, schedule_id: int, day: date) -> None:
        self._run(
            "Training verschieben",
            lambda client: client.reschedule_workout(schedule_id, day.isoformat()),
        )

    def unschedule(self, schedule_id: int) -> bool:
        """Remove a calendar entry; False when Garmin no longer knows it (404)."""
        return self._gone_is_fine(
            "Training aus dem Kalender nehmen", lambda client: client.unschedule_workout(schedule_id)
        )

    def _gone_is_fine(self, action: str, operation: Callable[[Any], Any]) -> bool:
        def call(client: Any) -> bool:
            operation(client)
            return True

        return bool(self._run(action, call, tolerate_not_found=True))

    def scheduled(self, start: date, end: date) -> list[ScheduledEntry]:
        def fetch(client: Any) -> list[ScheduledEntry]:
            items = client.get_scheduled_workouts_by_period(start.isoformat(), end.isoformat())
            if items is None or items == {}:
                return []  # garminconnect turns an HTTP 204 into an empty object
            if not isinstance(items, list):
                raise GarminWorkoutError("Unerwartete Antwort von Garmin für den Kalender")
            entries = (_to_entry(item) for item in items)
            return [entry for entry in entries if entry is not None]

        return self._run("Kalender lesen", fetch)

    def description(self, workout_id: int) -> str | None:
        def fetch(client: Any) -> str | None:
            value = client.get_workout_by_id(workout_id).get("description")
            return value if isinstance(value, str) else None

        return self._run("Trainingsbeschreibung lesen", fetch)

    def send_to_devices(self) -> None:
        self._run(
            "Training an Geräte senden",
            lambda client: client.send_scheduled_workouts_to_devices(),
        )

    def _run(
        self,
        action: str,
        operation: Callable[[Any], T],
        tolerate_not_found: bool = False,
    ) -> T | None:
        # Login errors (GarminAuthError, GarminUnavailableError) pass through unchanged.
        client = self._uploader.client()
        try:
            result = operation(client)
        except GarminWorkoutError:
            self._uploader.mark_answered()
            raise
        except GarminConnectTooManyRequestsError as exc:
            raise GarminUnavailableError(f"Garmin Rate-Limit ({action}): {exc}") from exc
        except GarminConnectAuthenticationError as exc:
            return self._rejected(action, None, exc)
        except Exception as exc:
            status = _http_status(exc)
            if status is not None:
                self._uploader.mark_answered()
            if status == _NOT_FOUND and tolerate_not_found:
                return None
            if status in _SESSION_REJECTED_STATUSES:
                return self._rejected(action, status, exc)
            if status == _RATE_LIMITED or (
                status is None and _is_connection_problem(exc)
            ):
                raise GarminUnavailableError(f"Garmin nicht erreichbar ({action}): {exc}") from exc
            if status is not None:
                raise GarminWorkoutError(
                    f"Garmin hat die Aktion abgelehnt ({action}, HTTP {status}): {exc}"
                ) from exc
            raise GarminWorkoutError(
                f"Unerwarteter Fehler bei Garmin ({action}, {type(exc).__name__}): {exc}"
            ) from exc
        else:
            self._uploader.mark_answered()
            return result

    def _rejected(self, action: str, status: int | None, exc: BaseException) -> Any:
        self._uploader.drop_session()
        label = f"HTTP {status}" if status is not None else "Anmeldung abgelaufen"
        raise GarminUnavailableError(
            f"Garmin-Sitzung abgelehnt ({action}, {label}), melde mich neu an: {exc}"
        ) from exc


def _is_connection_problem(exc: Exception) -> bool:
    """Network-level failure without an HTTP answer (library-wrapped or raw requests)."""
    return isinstance(exc, requests.RequestException | GarminConnectConnectionError)


def _to_entry(item: Any) -> ScheduledEntry | None:
    try:
        workout = item["workout"]
        return ScheduledEntry(
            schedule_id=int(item["workoutScheduleId"]),
            workout_id=int(workout["workoutId"]),
            date=date.fromisoformat(item["calendarDate"]),
            name=workout.get("workoutName") or "",
            description=workout.get("description") or None,
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        logger.warning("Garmin-Kalendereintrag ohne vollständige Angaben übersprungen")
        return None
