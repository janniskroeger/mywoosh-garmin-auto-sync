"""Settings read from environment variables."""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PAYLOAD_KEY = "D9436E508087E863"
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(Exception):
    """The environment is incomplete or contains an invalid value. Never contains secrets."""


@dataclass(frozen=True)
class OpenTriSettings:
    api_url: str
    keycloak_url: str
    realm: str
    client_id: str
    days: int
    pool_length_m: float


@dataclass(frozen=True)
class Settings:
    mywhoosh_email: str | None
    mywhoosh_password: str | None
    garmin_email: str | None
    garmin_password: str | None
    interval_minutes: int = 15
    payload_key: str = DEFAULT_PAYLOAD_KEY
    data_dir: Path = Path("/data")
    log_level: str = "INFO"
    opentri: OpenTriSettings | None = None
    mywhoosh_workouts: bool = False

    @classmethod
    def from_env(
        cls, env: Mapping[str, str], require_garmin: bool, require_mywhoosh: bool = True
    ) -> "Settings":
        required = ["MYWHOOSH_EMAIL", "MYWHOOSH_PASSWORD"] if require_mywhoosh else []
        if require_garmin:
            required += ["GARMIN_EMAIL", "GARMIN_PASSWORD"]
        missing = [name for name in required if not env.get(name)]
        if missing:
            raise ConfigError("Fehlende Umgebungsvariablen: " + ", ".join(missing))

        opentri = _parse_opentri(env)
        mywhoosh_workouts = _parse_mywhoosh_workouts(env.get("MYWHOOSH_WORKOUT_SYNC"))
        if mywhoosh_workouts and opentri is None:
            raise ConfigError(
                "MYWHOOSH_WORKOUT_SYNC=true braucht OPENTRI_API_URL und OPENTRI_KEYCLOAK_URL"
            )

        return cls(
            mywhoosh_email=env.get("MYWHOOSH_EMAIL") or None,
            mywhoosh_password=env.get("MYWHOOSH_PASSWORD") or None,
            garmin_email=env.get("GARMIN_EMAIL") or None,
            garmin_password=env.get("GARMIN_PASSWORD") or None,
            interval_minutes=_parse_interval(env.get("SYNC_INTERVAL_MINUTES")),
            payload_key=env.get("MYWHOOSH_PAYLOAD_KEY") or DEFAULT_PAYLOAD_KEY,
            data_dir=Path(env.get("DATA_DIR") or "/data"),
            log_level=_parse_log_level(env.get("LOG_LEVEL")),
            opentri=opentri,
            mywhoosh_workouts=mywhoosh_workouts,
        )


def _parse_opentri(env: Mapping[str, str]) -> OpenTriSettings | None:
    # Validate the numeric values even when open-tri is off, so a typo never hides.
    days = _parse_days(env.get("WORKOUT_SYNC_DAYS"))
    pool_length_m = _parse_pool_length(env.get("WORKOUT_POOL_LENGTH_M"))
    urls = ("OPENTRI_API_URL", "OPENTRI_KEYCLOAK_URL")
    missing = [name for name in urls if not env.get(name)]
    if len(missing) == len(urls):
        return None
    if missing:
        raise ConfigError("open-tri ist nur teilweise konfiguriert, es fehlt: " + ", ".join(missing))
    return OpenTriSettings(
        api_url=env["OPENTRI_API_URL"],
        keycloak_url=env["OPENTRI_KEYCLOAK_URL"],
        realm=env.get("OPENTRI_KEYCLOAK_REALM") or "open-tri",
        client_id=env.get("OPENTRI_CLIENT_ID") or "mywhoosh-garmin-sync",
        days=days,
        pool_length_m=pool_length_m,
    )


def _parse_mywhoosh_workouts(raw: str | None) -> bool:
    value = (raw or "").strip().lower()
    if value in ("", "false"):
        return False
    if value == "true":
        return True
    raise ConfigError("MYWHOOSH_WORKOUT_SYNC muss true oder false sein")


def _parse_days(raw: str | None) -> int:
    if not raw:
        return 14
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ConfigError("WORKOUT_SYNC_DAYS muss eine ganze Zahl ab 1 sein")
    return value


def _parse_pool_length(raw: str | None) -> float:
    if not raw:
        return 25.0
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if not math.isfinite(value) or value <= 0:
        raise ConfigError("WORKOUT_POOL_LENGTH_M muss eine Zahl größer 0 sein")
    return value


def _parse_interval(raw: str | None) -> int:
    if not raw:
        return 15
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ConfigError("SYNC_INTERVAL_MINUTES muss eine ganze Zahl ab 1 sein")
    return value


def _parse_log_level(raw: str | None) -> str:
    if not raw:
        return "INFO"
    level = raw.strip().upper()
    if level not in _LOG_LEVELS:
        raise ConfigError("LOG_LEVEL muss einer von " + ", ".join(_LOG_LEVELS) + " sein")
    return level
