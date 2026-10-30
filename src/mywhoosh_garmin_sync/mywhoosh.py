"""Client for MyWhoosh's undocumented app and rider-profile API."""

import base64
import binascii
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from mywhoosh_garmin_sync.crypto import encrypt_payload

logger = logging.getLogger(__name__)

LOGIN_URL = "https://services.mywhoosh.com/http-service/api/login"
LIST_URL = "https://service14.mywhoosh.com/v2/rider/profile/activities"
DOWNLOAD_URL = "https://service14.mywhoosh.com/v2/rider/profile/download-activity-file"

_API_TIMEOUT = 30
_DOWNLOAD_TIMEOUT = 60
_TOKEN_MARGIN_SECONDS = 300
_DEFAULT_TITLE = "MyWhoosh-Fahrt"
_SESSION_FILE = "mywhoosh_session.json"
_API_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://event.mywhoosh.com",
    "Referer": "https://event.mywhoosh.com/",
}


class MyWhooshError(Exception):
    """Base class for all MyWhoosh client errors."""


class MyWhooshAuthError(MyWhooshError):
    """MyWhoosh rejected the credentials."""


class MyWhooshUnavailableError(MyWhooshError):
    """Network failure, timeout, non-2xx response or unexpected response structure."""


class MyWhooshConnectionError(MyWhooshUnavailableError):
    """The transport to MyWhoosh failed (connection error, timeout)."""


class MyWhooshDownloadError(MyWhooshError):
    """Downloading the FIT file of one specific ride failed."""


@dataclass(frozen=True)
class MyWhooshActivity:
    file_id: str
    title: str
    sport_type: str
    start: datetime  # UTC-aware


def _token_expiry(token: object) -> float | None:
    """Read the ``exp`` claim of a JWT without verifying its signature."""
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    segment = parts[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (binascii.Error, ValueError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    if isinstance(exp, bool) or not isinstance(exp, int | float):
        return None
    return float(exp)


def _parse_activity(raw: object) -> MyWhooshActivity:
    """Build an activity from one list entry; raise ValueError if it is unusable."""
    if not isinstance(raw, dict):
        raise ValueError("Eintrag ist kein Objekt")
    values = {key: raw.get(key) for key in ("activityFileId", "sportType", "startDatetime")}
    for key, value in values.items():
        if not isinstance(value, str) or not value:
            raise ValueError(f"Feld {key} fehlt oder ist kein Text")
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        title = _DEFAULT_TITLE
    start = datetime.fromisoformat(values["startDatetime"])
    if start.tzinfo is None:
        raise ValueError("startDatetime ohne Zeitzone")
    return MyWhooshActivity(
        file_id=values["activityFileId"],
        title=title,
        sport_type=values["sportType"],
        start=start.astimezone(UTC),
    )


class MyWhooshClient:
    def __init__(
        self,
        email: str,
        password: str,
        data_dir: Path,
        payload_key: str,
        session: requests.Session | None = None,
    ) -> None:
        self._email = email
        self._password = password
        self._session_path = data_dir / _SESSION_FILE
        self._payload_key = payload_key
        self._http = session or requests.Session()
        self._state = self._load_state()

    def list_recent(self) -> list[MyWhooshActivity]:
        """Return page 1 of the rider's activities, newest first, as delivered."""
        body = self._post_encrypted(LIST_URL, {"type": "", "page": 1, "sortDate": "DESC"})
        data = body.get("data")
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise MyWhooshUnavailableError("Aktivitätenliste von MyWhoosh hat eine unerwartete Struktur")
        activities = []
        for index, raw in enumerate(results):
            try:
                activities.append(_parse_activity(raw))
            except ValueError as exc:
                file_id = raw.get("activityFileId") if isinstance(raw, dict) else None
                suffix = f" (activityFileId {file_id})" if isinstance(file_id, str) and file_id else ""
                logger.warning("MyWhoosh-Eintrag %d%s übersprungen: %s", index, suffix, exc)
        return activities

    def download_fit(self, file_id: str) -> bytes:
        """Download the FIT file of one activity."""
        # Only an error response of the download endpoint itself counts against the ride;
        # transport and login problems propagate and abort the run.
        try:
            body = self._post_encrypted(DOWNLOAD_URL, {"fileId": file_id}, response_error=MyWhooshDownloadError)
        except MyWhooshDownloadError as exc:
            raise MyWhooshDownloadError(f"Download-Link für Fahrt {file_id} nicht erhältlich: {exc}") from exc
        url = body.get("data")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise MyWhooshDownloadError(f"MyWhoosh lieferte keinen Download-Link für Fahrt {file_id}")
        logger.info("Lade FIT-Datei von %s", url.split("?")[0])
        try:
            response = self._http.get(url, timeout=_DOWNLOAD_TIMEOUT)
        except requests.RequestException as exc:
            # The exception text may contain the presigned URL, so only its type is kept.
            raise MyWhooshConnectionError(
                f"Download der Fahrt {file_id} fehlgeschlagen ({type(exc).__name__})"
            ) from None
        if not response.ok:
            raise MyWhooshDownloadError(
                f"Download der Fahrt {file_id} fehlgeschlagen (HTTP {response.status_code})"
            )
        return response.content

    # --- API access -------------------------------------------------------------------

    def _post_encrypted(
        self,
        url: str,
        payload: dict[str, Any],
        response_error: type[MyWhooshError] = MyWhooshUnavailableError,
    ) -> dict[str, Any]:
        """POST an encrypted payload with the access token; re-login once on HTTP 401.

        ``response_error`` is raised for a bad response of this endpoint (HTTP status, body).
        """
        for attempt in (1, 2):
            token = self._access_token()
            response = self._send(
                url,
                data=json.dumps(encrypt_payload(payload, self._payload_key)),
                headers={**_API_HEADERS, "Authorization": f"Bearer {token}"},
            )
            if response.status_code == 401 and attempt == 1:
                logger.info("MyWhoosh hat das Token abgelehnt, melde mich neu an")
                self._store_token(None)
                continue
            return self._json_body(response, response_error)
        raise AssertionError("unreachable")

    def _send(self, url: str, **kwargs: Any) -> requests.Response:
        try:
            return self._http.post(url, timeout=_API_TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            raise MyWhooshConnectionError(
                f"MyWhoosh nicht erreichbar ({type(exc).__name__})"
            ) from exc

    @staticmethod
    def _json_body(
        response: requests.Response, error: type[MyWhooshError] = MyWhooshUnavailableError
    ) -> dict[str, Any]:
        if not response.ok:
            raise error(f"MyWhoosh antwortete mit HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise error("MyWhoosh lieferte kein gültiges JSON") from exc
        if not isinstance(body, dict):
            raise error("MyWhoosh-Antwort hat eine unerwartete Struktur")
        return body

    # --- Login and token cache ----------------------------------------------------------

    def _access_token(self) -> str:
        token = self._state.get("access_token")
        expiry = _token_expiry(token)
        if expiry is not None and expiry - time.time() > _TOKEN_MARGIN_SECONDS:
            return token
        return self._login()

    def _login(self) -> str:
        device_id = self._state.get("device_id")
        if not isinstance(device_id, str) or not device_id:
            device_id = str(uuid.uuid4())
            self._state["device_id"] = device_id
            self._save_state()
        response = self._send(
            LOGIN_URL,
            json={
                "Username": self._email,
                "Password": self._password,
                "Platform": "Android",
                "Action": 1001,
                "CorrelationId": str(uuid.uuid4()),
                "DeviceId": device_id,
                "Authorization": "",
            },
        )
        body = self._json_body(response)
        if body.get("Success") is not True:
            raise MyWhooshAuthError(
                "MyWhoosh hat die Zugangsdaten abgelehnt, MYWHOOSH_EMAIL/MYWHOOSH_PASSWORD in .env prüfen"
            )
        token = body.get("AccessToken")
        if not isinstance(token, str) or not token:
            raise MyWhooshUnavailableError("MyWhoosh-Login lieferte kein Access-Token")
        self._store_token(token)
        logger.info("Bei MyWhoosh angemeldet")
        return token

    def _load_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self._session_path.read_text())
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def _store_token(self, token: str | None) -> None:
        self._state["access_token"] = token
        self._save_state()

    def _save_state(self) -> None:
        """Write the session file atomically with owner-only permissions."""
        self._session_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._session_path.with_name(self._session_path.name + ".tmp")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(self._state, handle)
        os.replace(tmp_path, self._session_path)
