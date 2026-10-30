"""Thin wrapper around the garminconnect library: login, FIT import and renaming."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import requests
from garminconnect import Garmin
from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectInvalidFileFormatError,
    GarminConnectTooManyRequestsError,
)

logger = logging.getLogger(__name__)

_POLL_ATTEMPTS = 6
_POLL_INTERVAL_SECONDS = 5
_START_TOLERANCE_SECONDS = 120
_TOKEN_DIR_MODE = 0o700
_SESSION_REJECTED_STATUSES = {401, 403}
_MFA_HINT = (
    "MFA nötig: einmalig 'docker compose run --rm sync login-garmin' ausführen, "
    "danach 'docker compose restart sync'"
)
_REJECTED_HINT = "Zugangsdaten in .env prüfen, dann 'docker compose up -d --force-recreate'"
# garminconnect reports transient profile/settings fetch problems as authentication errors.
_TRANSIENT_AUTH_PREFIXES = ("Failed to retrieve", "Invalid profile", "Invalid user settings")


class GarminAuthError(Exception):
    """Garmin rejected the credentials or needs MFA; logins stay paused."""


class GarminUnavailableError(Exception):
    """Network failure, rate limit or server error that is not tied to one ride."""


class GarminImportError(Exception):
    """Importing one specific ride into Garmin Connect failed."""


@dataclass(frozen=True)
class ImportOutcome:
    status: Literal["uploaded", "duplicate"]
    activity_id: int | None


class GarminUploader:
    """Logs in lazily with a persisted token store and imports FIT files."""

    def __init__(
        self,
        email: str,
        password: str,
        token_dir: Path,
        client_factory: Callable[..., Garmin] = Garmin,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._email = email
        self._password = password
        self._token_dir = token_dir
        self._client_factory = client_factory
        self._sleep = sleep
        self._client: Garmin | None = None
        self._auth_error: GarminAuthError | None = None
        self._fresh_login = False  # True until the first API call after a login got an answer

    def ensure_login(self) -> None:
        """Log in once; rejected credentials pause every further attempt until restart."""
        if self._client is not None:
            return
        if self._auth_error is not None:
            raise self._auth_error
        try:
            # login() clears the password on its instance, so every attempt needs a new one.
            self._client = _login(
                self._client_factory(self._email, self._password), self._token_dir
            )
            self._fresh_login = True
        except GarminAuthError as exc:
            self._auth_error = exc
            raise

    def import_fit(self, path: Path, start: datetime) -> ImportOutcome:
        """Import a FIT file; duplicates are reported as an outcome, not as an error."""
        client = self._logged_in_client()
        try:
            result = client.import_activity(str(path))
        except GarminConnectTooManyRequestsError as exc:
            raise GarminUnavailableError(f"Garmin Rate-Limit beim Import: {exc}") from exc
        except GarminConnectInvalidFileFormatError as exc:
            self._fresh_login = False
            raise GarminImportError(f"Ungültiges Dateiformat: {exc}") from exc
        except GarminConnectConnectionError as exc:
            if "duplicate" in str(exc).lower():
                self._fresh_login = False
                return ImportOutcome("duplicate", None)
            status = _http_status(exc)
            if status in _SESSION_REJECTED_STATUSES:
                return self._session_rejected(status, exc)
            if status is None or status == 429:
                raise GarminUnavailableError(f"Garmin nicht erreichbar beim Import: {exc}") from exc
            self._fresh_login = False
            raise GarminImportError(f"Garmin hat den Import abgelehnt: {exc}") from exc
        except requests.RequestException as exc:
            raise GarminUnavailableError(
                f"Garmin nicht erreichbar beim Import ({type(exc).__name__})"
            ) from exc
        except Exception as exc:
            raise GarminImportError(
                f"Unerwarteter Fehler beim Garmin-Import ({type(exc).__name__}): {exc}"
            ) from exc

        self._fresh_login = False
        try:
            outcome = _interpret_import_result(result)
        except GarminImportError:
            raise
        except Exception as exc:
            raise GarminImportError(
                f"Unerwartete Antwort von Garmin beim Import ({type(exc).__name__})"
            ) from exc
        if outcome.status == "uploaded" and outcome.activity_id is None:
            return ImportOutcome("uploaded", self._find_activity_id(client, start))
        return outcome

    def _session_rejected(self, status: int, exc: BaseException) -> ImportOutcome:
        fresh, self._fresh_login = self._fresh_login, False
        # The cached session is dead; the next ensure_login() starts a fresh one.
        self._client = None
        if fresh:
            # A session created just for this import was refused too: the ride is the problem.
            raise GarminImportError(f"Garmin hat den Import abgelehnt (HTTP {status}): {exc}") from exc
        raise GarminUnavailableError(
            f"Garmin-Sitzung abgelehnt (HTTP {status}), melde mich neu an: {exc}"
        ) from exc

    def set_name(self, activity_id: int, title: str) -> None:
        self._logged_in_client().set_activity_name(str(activity_id), title)

    def _logged_in_client(self) -> Garmin:
        self.ensure_login()
        assert self._client is not None
        return self._client

    def _find_activity_id(self, client: Garmin, start: datetime) -> int | None:
        """Garmin did not report an id; look the new activity up by its start time."""
        start_utc = start.astimezone(UTC)
        # Garmin may file a ride under a neighbouring day; the start-time match picks the right one.
        first = (start_utc - timedelta(days=1)).date().isoformat()
        last = (start_utc + timedelta(days=1)).date().isoformat()
        for _ in range(_POLL_ATTEMPTS):
            self._sleep(_POLL_INTERVAL_SECONDS)
            try:
                activities = client.get_activities_by_date(first, last)
            except Exception as exc:  # the ride is uploaded; a failing lookup must not undo that
                logger.warning(
                    "Aktivitätsliste nach dem Import nicht abrufbar, Aktivitäts-ID bleibt offen: %s",
                    exc,
                )
                return None
            match = _closest_activity(activities, start_utc)
            if match is not None:
                return match
        logger.warning("Aktivität nach dem Import nicht in der Garmin-Liste gefunden")
        return None


def interactive_login(
    email: str,
    password: str,
    token_dir: Path,
    prompt: Callable[[str], str] = input,
    client_factory: Callable[..., Garmin] = Garmin,
) -> None:
    """Log in once with credentials (asking for the MFA code) and persist the tokens."""
    client = client_factory(email, password, prompt_mfa=lambda: prompt("MFA-Code: "))
    _login(client, token_dir)


def _login(client: Garmin, token_dir: Path) -> Garmin:
    token_dir.mkdir(parents=True, exist_ok=True, mode=_TOKEN_DIR_MODE)
    token_dir.chmod(_TOKEN_DIR_MODE)
    try:
        client.login(tokenstore=str(token_dir))
    except GarminConnectAuthenticationError as exc:
        text = str(exc)
        if text.startswith(_TRANSIENT_AUTH_PREFIXES):
            raise GarminUnavailableError(f"Garmin-Anmeldung nicht möglich: {exc}") from exc
        if "mfa" in text.lower():
            raise GarminAuthError(f"Garmin verlangt MFA. {_MFA_HINT}") from exc
        raise GarminAuthError(f"Garmin lehnt die Anmeldung ab: {exc}. {_REJECTED_HINT}") from exc
    except Exception as exc:
        raise GarminUnavailableError(f"Garmin-Anmeldung nicht möglich: {exc}") from exc
    logger.info("Bei Garmin Connect angemeldet")
    return client


def _interpret_import_result(result: Any) -> ImportOutcome:
    """Turn the library's import response into an outcome (id None if Garmin reported none)."""
    detailed = (result.get("detailedImportResult") if result else None) or {}
    successes = detailed.get("successes") or []
    failures = detailed.get("failures") or []
    if not successes and failures:
        messages = _failure_messages(failures)
        if any("duplicate" in message.lower() for message in messages):
            return ImportOutcome("duplicate", None)
        raise GarminImportError(
            "Garmin hat den Import abgelehnt: " + ("; ".join(messages) or "unbekannter Fehler")
        )
    internal_id = successes[0].get("internalId") if successes else None
    return ImportOutcome("uploaded", internal_id)


def _http_status(exc: BaseException) -> int | None:
    """HTTP status of a failed request; the library wraps errors and may drop .response."""
    for candidate in (exc, exc.__cause__):
        status = getattr(getattr(candidate, "response", None), "status_code", None)
        if status is not None:
            return status
    return None


def _failure_messages(failures: list[dict[str, Any]]) -> list[str]:
    messages = []
    for failure in failures:
        for message in failure.get("messages") or []:
            content = message.get("content") if isinstance(message, dict) else message
            if isinstance(content, str) and content:
                messages.append(content)
    return messages


def _closest_activity(activities: list[dict[str, Any]], start: datetime) -> int | None:
    best: tuple[float, int] | None = None
    for activity in activities:
        try:
            activity_start = datetime.strptime(activity["startTimeGMT"], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=UTC
            )
            deviation = abs((activity_start - start).total_seconds())
            activity_id = int(activity["activityId"])
        except (KeyError, TypeError, ValueError):
            continue
        if deviation <= _START_TOLERANCE_SECONDS and (best is None or deviation < best[0]):
            best = (deviation, activity_id)
    return best[1] if best else None
