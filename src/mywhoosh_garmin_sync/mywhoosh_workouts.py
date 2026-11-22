"""Client for MyWhoosh's coaching API: list, upload and delete custom workouts.

Writes are not confirmed here (the API answers before the change is visible); the
reconciler reads the list afterwards.
"""

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from mywhoosh_garmin_sync.mywhoosh import (
    MyWhooshAuthError,
    MyWhooshClient,
    MyWhooshConnectionError,
    MyWhooshError,
    MyWhooshUnavailableError,
)

logger = logging.getLogger(__name__)

COACHING_URL = "https://coaching.mywhoosh.com/api/v2"

_TIMEOUT = 30
_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://workout.mywhoosh.com",
    "Referer": "https://workout.mywhoosh.com/",
}


@dataclass(frozen=True)
class RemoteWorkout:
    workout_id: int
    name: str
    description: str | None
    updated_at: str | None = None  # changes on every applied upsert (probe)


class MyWhooshWorkoutError(MyWhooshError):
    """MyWhoosh rejected this one workout."""


class MyWhooshNoSlotsError(MyWhooshWorkoutError):
    """No free workout slot or credit."""


class CoachingAuth(Protocol):
    def token(self) -> str: ...

    def user_id(self) -> str: ...

    def invalidate(self) -> None: ...


class AppCoachingAuth:
    """Coaching auth backed by the app token of a ``MyWhooshClient``."""

    def __init__(self, client: MyWhooshClient) -> None:
        self._client = client

    def token(self) -> str:
        return self._client.coaching_token()

    def user_id(self) -> str:
        return self._client.coaching_user_id()

    def invalidate(self) -> None:
        self._client.invalidate_token()


def _message(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    message = body.get("message") if isinstance(body, dict) else None
    if isinstance(message, str) and message:
        return message
    return f"HTTP {response.status_code}"


class MyWhooshWorkouts:
    def __init__(self, auth: CoachingAuth, session: requests.Session | None = None) -> None:
        self._auth = auth
        self._http = session or requests.Session()

    def list(self) -> list[RemoteWorkout]:
        body = self._json(self._request("GET", "/workout-builder/my-workouts"), MyWhooshUnavailableError)
        data = body.get("data")
        if not isinstance(data, list):
            raise MyWhooshUnavailableError("Workout-Liste von MyWhoosh hat eine unerwartete Struktur")
        workouts = []
        for raw in data:
            entry = raw if isinstance(raw, dict) else {}
            workout_id = entry.get("WorkoutId")
            if isinstance(workout_id, bool) or not isinstance(workout_id, int):
                _id = entry.get("_id")
                suffix = f" (_id {_id})" if _id else ""
                logger.warning("MyWhoosh-Workout ohne gültige WorkoutId übersprungen%s", suffix)
                continue
            name = entry.get("Name")
            description = entry.get("Description")
            updated_at = entry.get("updatedAt")
            workouts.append(
                RemoteWorkout(
                    workout_id=workout_id,
                    name=name if isinstance(name, str) else "",
                    description=description if isinstance(description, str) else None,
                    updated_at=updated_at if isinstance(updated_at, str) else None,
                )
            )
        return workouts

    def credits(self) -> int:
        body = self._json(self._request("GET", "/client/my-credits"), MyWhooshUnavailableError)
        data = body.get("data")
        value = data.get("CurrentCredits") if isinstance(data, dict) else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise MyWhooshUnavailableError("MyWhoosh lieferte keine gültigen CurrentCredits")
        return value

    def upload(self, workout_json: dict[str, Any]) -> None:
        """Create or overwrite (by ``Id``) a workout. The response carries no id."""
        response = self._request(
            "POST",
            "/client/custom-workout-upload",
            json={"UserId": self._auth.user_id(), "SportsModeType": 0, "WorkoutsData": [workout_json]},
        )
        if 400 <= response.status_code < 500:
            message = _message(response)
            lowered = message.lower()
            error = MyWhooshNoSlotsError if "credit" in lowered or "slot" in lowered else MyWhooshWorkoutError
            raise error(f"MyWhoosh hat das Workout abgelehnt: {message}")
        self._json(response, MyWhooshWorkoutError)

    def delete(self, workout_id: int) -> None:
        response = self._request("DELETE", f"/client/custom-workout-upload/0/{workout_id}")
        if response.status_code == 404:
            return  # already gone
        if 400 <= response.status_code < 500:
            # a synchronous rejection changed nothing; only this workout is affected
            raise MyWhooshWorkoutError(f"MyWhoosh hat das Löschen abgelehnt: {_message(response)}")
        self._json(response, MyWhooshWorkoutError)

    # --- HTTP ---------------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """Send with the bearer token; on HTTP 401 invalidate and retry once."""
        for attempt in (1, 2):
            try:
                response = self._http.request(
                    method,
                    COACHING_URL + path,
                    headers={**_HEADERS, "Authorization": f"Bearer {self._auth.token()}"},
                    timeout=_TIMEOUT,
                    **kwargs,
                )
            except requests.RequestException as exc:
                raise MyWhooshConnectionError(f"MyWhoosh nicht erreichbar ({type(exc).__name__})") from exc
            if response.status_code != 401:
                return response
            if attempt == 1:
                logger.info("MyWhoosh hat das Token abgelehnt, hole ein neues")
                self._auth.invalidate()
        raise MyWhooshAuthError("MyWhoosh lehnt das Token der Coaching-API ab")

    @staticmethod
    def _json(response: requests.Response, status_false_error: type[MyWhooshError]) -> dict[str, Any]:
        """Parse a 2xx body; ``"status": false`` raises ``status_false_error``."""
        if not response.ok:
            raise MyWhooshUnavailableError(f"MyWhoosh antwortete mit HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise MyWhooshUnavailableError("MyWhoosh lieferte kein gültiges JSON") from exc
        if not isinstance(body, dict):
            raise MyWhooshUnavailableError("MyWhoosh-Antwort hat eine unerwartete Struktur")
        if body.get("status") is False:
            raise status_false_error(f"MyWhoosh meldet einen Fehler: {_message(response)}")
        return body
