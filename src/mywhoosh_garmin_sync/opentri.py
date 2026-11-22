"""Client for open-tri: Keycloak device login and the planned-workout FIT export."""

import base64
import binascii
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 30
_TOKEN_MARGIN_SECONDS = 60
_DEFAULT_POLL_INTERVAL = 5
_SLOW_DOWN_STEP = 5
_DEFAULT_DEVICE_LIFETIME = 600
_SESSION_FILE = "opentri_session.json"
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_ZIP_SIGNATURE = b"PK\x03\x04"
_LOGIN_HINT = (
    "Bitte einmalig 'mywhoosh-garmin-sync login-opentri' ausführen "
    "(Portainer-Konsole des Containers bzw. 'docker compose run --rm sync login-opentri')"
)


class OpenTriError(Exception):
    """Base class for all open-tri client errors."""


class OpenTriAuthError(OpenTriError):
    """No refresh token, or Keycloak rejected it / the device login failed."""


class OpenTriUnavailableError(OpenTriError):
    """Network failure, 5xx or unexpected response."""


class OpenTriExportError(OpenTriError):
    """open-tri refused to export a single training (HTTP 4xx), e.g. because the FTP is missing."""


@dataclass(frozen=True)
class PlannedBike:
    planned_id: str
    day: date
    brick_order: int | None
    training_id: str
    training_name: str
    week: int  # floorDiv((day - plan.startDate).days, 7) + 1


def _jwt_claims(token: object) -> dict[str, Any]:
    """Read the payload of a JWT without verifying its signature; empty if unreadable."""
    if not isinstance(token, str):
        return {}
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    segment = parts[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (binascii.Error, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _json_object(response: requests.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


class OpenTriClient:
    def __init__(
        self,
        api_url: str,
        keycloak_url: str,
        realm: str,
        client_id: str,
        data_dir: Path,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        oidc = f"{keycloak_url.rstrip('/')}/realms/{quote(realm, safe='')}/protocol/openid-connect"
        self._device_url = f"{oidc}/auth/device"
        self._token_url = f"{oidc}/token"
        self._client_id = client_id
        self._session_path = data_dir / _SESSION_FILE
        self._http = session or requests.Session()
        self._sleep = sleep
        self._refresh_token = self._load_refresh_token()
        self._access_token: str | None = None

    # --- Device login ---------------------------------------------------------------------

    def device_login(self, show: Callable[[str], None]) -> None:
        """Run the OAuth device grant: show the link, poll until approved, store the refresh token."""
        response = self._post(
            self._device_url, {"client_id": self._client_id, "scope": "openid offline_access"}
        )
        body = _json_object(response)
        if not response.ok or body is None:
            raise OpenTriUnavailableError(f"Keycloak lehnte die Geräteanmeldung ab (HTTP {response.status_code})")
        device_code = body.get("device_code")
        if not isinstance(device_code, str) or not device_code:
            raise OpenTriUnavailableError("Keycloak lieferte keinen Geräte-Code")
        complete = body.get("verification_uri_complete")
        uri = body.get("verification_uri")
        if isinstance(complete, str) and complete:
            show(f"Zum Anmelden bei open-tri diesen Link im Browser öffnen: {complete}")
        elif isinstance(uri, str) and uri:
            show(f"Zum Anmelden bei open-tri {uri} öffnen und den Code {body.get('user_code')} eingeben")
        else:
            raise OpenTriUnavailableError("Keycloak lieferte keinen Anmelde-Link")
        lifetime = self._positive_number(body.get("expires_in"), _DEFAULT_DEVICE_LIFETIME)
        interval = self._positive_number(body.get("interval"), _DEFAULT_POLL_INTERVAL)

        waited = 0.0
        while waited < lifetime:
            self._sleep(interval)
            waited += interval
            response = self._post(
                self._token_url,
                {"grant_type": _DEVICE_GRANT, "device_code": device_code, "client_id": self._client_id},
            )
            if response.ok:
                self._accept_tokens(response, require_refresh=True)
                logger.info("Bei open-tri angemeldet")
                return
            if response.status_code >= 500:
                raise OpenTriUnavailableError(f"Keycloak antwortete mit HTTP {response.status_code}")
            error = (_json_object(response) or {}).get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += _SLOW_DOWN_STEP
                continue
            if error == "expired_token":
                raise OpenTriAuthError("Der Anmeldecode ist abgelaufen, bitte 'login-opentri' erneut starten")
            if error == "access_denied":
                raise OpenTriAuthError("Die Anmeldung bei open-tri wurde abgelehnt")
            raise OpenTriAuthError("Die Anmeldung bei open-tri ist fehlgeschlagen")
        raise OpenTriAuthError("Der Anmeldecode ist abgelaufen, bitte 'login-opentri' erneut starten")

    # --- Export ---------------------------------------------------------------------------

    def fetch_bundle(self, start: date, end: date) -> bytes | None:
        """Return the FIT export ZIP of the active plan, or None if there is no plan or no workouts."""
        sub = self._athlete_id()
        plan = self._active_plan()
        if plan is None:
            return None
        plan_id = plan.get("id")
        if not isinstance(plan_id, str) or not plan_id:
            raise OpenTriUnavailableError("open-tri lieferte keinen gültigen Trainingsplan")
        export = self._api_get(
            f"/athletes/{quote(sub, safe='')}/training-plans/{quote(plan_id, safe='')}/fit",
            params={"from": start.isoformat(), "to": end.isoformat(), "schedule": "true"},
        )
        # A 404 only means "no workouts" when open-tri itself answered; a proxy page must not look empty.
        if export.status_code == 204 or (export.status_code == 404 and _json_object(export) is not None):
            logger.info("Keine geplanten Workouts im Zeitraum %s bis %s", start, end)
            return None
        if not export.content.startswith(_ZIP_SIGNATURE):
            raise OpenTriUnavailableError("open-tri lieferte keine ZIP-Datei")
        return export.content

    def fetch_planned_bikes(self, start: date, end: date) -> list[PlannedBike] | None:
        """Return the planned bike sessions of the active plan within start..end (inclusive), or None without plan."""
        plan = self._active_plan()
        if plan is None:
            return None
        items = plan.get("plannedTrainings")
        if not isinstance(items, list):
            raise OpenTriUnavailableError("open-tri lieferte keine gültige Liste geplanter Einheiten")
        plan_start: date | None = None
        bikes: list[PlannedBike] = []
        for entry in items:
            training = entry.get("training") if isinstance(entry, dict) else None
            if not isinstance(training, dict) or training.get("sport") != "BIKE":
                continue
            planned_id = entry.get("id")
            training_id = training.get("id")
            name = training.get("name")
            day = self._iso_date(entry.get("date"))
            if (
                not isinstance(planned_id, str)
                or not planned_id
                or day is None
                or not isinstance(training_id, str)
                or not training_id
                or not isinstance(name, str)
            ):
                raise OpenTriUnavailableError("open-tri lieferte eine ungültige geplante Radeinheit")
            if not start <= day <= end:
                continue
            if plan_start is None:
                plan_start = self._iso_date(plan.get("startDate"))
                if plan_start is None:
                    raise OpenTriUnavailableError("open-tri lieferte kein gültiges Startdatum des Trainingsplans")
            brick = entry.get("brickOrder")
            if isinstance(brick, bool) or not isinstance(brick, int):
                brick = None
            bikes.append(
                PlannedBike(planned_id, day, brick, training_id, name, (day - plan_start).days // 7 + 1)
            )
        bikes.sort(key=lambda b: (b.day, -1 if b.brick_order is None else b.brick_order))
        return bikes

    def fetch_zwo(self, training_id: str) -> bytes:
        """Return the Zwift workout (.zwo) of one training."""
        sub = self._athlete_id()
        response = self._api_get(
            f"/athletes/{quote(sub, safe='')}/trainings/{quote(training_id, safe='')}/zwo",
            client_errors=OpenTriExportError,
            label=f"Training {training_id}",
        )
        if not response.content.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"):
            raise OpenTriUnavailableError(f"open-tri lieferte keine Zwift-Datei für Training {training_id}")
        return response.content

    def _athlete_id(self) -> str:
        return self._subject(self._access_token_for_use())

    def _active_plan(self) -> dict[str, Any] | None:
        """The active training plan as a JSON object, or None if there is none (HTTP 204)."""
        sub = self._athlete_id()
        response = self._api_get(f"/athletes/{quote(sub, safe='')}/training-plans/active")
        if response.status_code == 204:
            logger.info("Kein aktiver Trainingsplan bei open-tri")
            return None
        plan = _json_object(response)
        if plan is None:
            raise OpenTriUnavailableError("open-tri lieferte keinen gültigen Trainingsplan")
        return plan

    @staticmethod
    def _iso_date(value: object) -> date | None:
        if not isinstance(value, str):
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    def _api_get(
        self,
        path: str,
        params: dict[str, str] | None = None,
        client_errors: type[OpenTriError] | None = None,
        label: str = "open-tri",
    ) -> requests.Response:
        """GET an API path with the bearer token; refresh and retry once on HTTP 401.

        With client_errors, any other 4xx raises that class (message names label and status, never the token).
        """
        for attempt in (1, 2):
            token = self._access_token_for_use()
            response = self._request(
                "get",
                self._api_url + path,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code == 401 and attempt == 1:
                logger.info("open-tri hat das Token abgelehnt, erneuere es")
                self._access_token = None
                continue
            if client_errors is not None and 400 <= response.status_code < 500 and response.status_code != 401:
                raise client_errors(f"open-tri verweigert den Export ({label}): HTTP {response.status_code}")
            if response.status_code in (204, 404) or response.ok:
                return response
            raise OpenTriUnavailableError(f"open-tri antwortete mit HTTP {response.status_code}")
        raise OpenTriUnavailableError("open-tri lehnt das erneuerte Token ab")

    @staticmethod
    def _subject(token: str) -> str:
        sub = _jwt_claims(token).get("sub")
        if not isinstance(sub, str) or not sub:
            raise OpenTriUnavailableError("Access-Token enthält keine Athleten-ID")
        return sub

    # --- Tokens ---------------------------------------------------------------------------

    def _access_token_for_use(self) -> str:
        token = self._access_token
        exp = _jwt_claims(token).get("exp")
        if (
            token
            and isinstance(exp, int | float)
            and not isinstance(exp, bool)
            and exp - time.time() > _TOKEN_MARGIN_SECONDS
        ):
            return token
        return self._refresh()

    def _refresh(self) -> str:
        # login-opentri runs in another process and writes the file; pick up its token without a restart
        self._refresh_token = self._load_refresh_token() or self._refresh_token
        if not self._refresh_token:
            raise OpenTriAuthError(f"Kein open-tri-Login vorhanden. {_LOGIN_HINT}")
        response = self._post(
            self._token_url,
            {"grant_type": "refresh_token", "refresh_token": self._refresh_token, "client_id": self._client_id},
        )
        if response.status_code >= 500:
            raise OpenTriUnavailableError(f"Keycloak antwortete mit HTTP {response.status_code}")
        if not response.ok:
            self._access_token = None
            raise OpenTriAuthError(f"Keycloak hat den open-tri-Login abgelehnt oder er ist abgelaufen. {_LOGIN_HINT}")
        return self._accept_tokens(response, require_refresh=False)

    def _accept_tokens(self, response: requests.Response, require_refresh: bool) -> str:
        body = _json_object(response) or {}
        access = body.get("access_token")
        if not isinstance(access, str) or not access:
            raise OpenTriUnavailableError("Keycloak lieferte kein Access-Token")
        refresh = body.get("refresh_token")
        if isinstance(refresh, str) and refresh:
            self._refresh_token = refresh
            self._save_refresh_token()
        elif require_refresh:
            raise OpenTriUnavailableError("Keycloak lieferte kein Refresh-Token")
        self._access_token = access
        return access

    def _load_refresh_token(self) -> str | None:
        try:
            state = json.loads(self._session_path.read_text())
        except (OSError, ValueError):
            return None
        token = state.get("refresh_token") if isinstance(state, dict) else None
        return token if isinstance(token, str) and token else None

    def _save_refresh_token(self) -> None:
        """Write the session file atomically with owner-only permissions."""
        self._session_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._session_path.with_name(self._session_path.name + ".tmp")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump({"refresh_token": self._refresh_token}, handle)
        os.replace(tmp_path, self._session_path)

    # --- HTTP -----------------------------------------------------------------------------

    def _post(self, url: str, data: dict[str, str]) -> requests.Response:
        return self._request("post", url, data=data)

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        try:
            return self._http.request(method, url, timeout=_TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            # The exception text may contain URLs with parameters, so only its type is kept.
            raise OpenTriUnavailableError(f"open-tri nicht erreichbar ({type(exc).__name__})") from None

    @staticmethod
    def _positive_number(value: object, default: float) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            return default
        return value
