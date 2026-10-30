"""Settings read from environment variables."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PAYLOAD_KEY = "D9436E508087E863"
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(Exception):
    """The environment is incomplete or contains an invalid value. Never contains secrets."""


@dataclass(frozen=True)
class Settings:
    mywhoosh_email: str
    mywhoosh_password: str
    garmin_email: str | None
    garmin_password: str | None
    interval_minutes: int = 15
    payload_key: str = DEFAULT_PAYLOAD_KEY
    data_dir: Path = Path("/data")
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str], require_garmin: bool) -> "Settings":
        required = ["MYWHOOSH_EMAIL", "MYWHOOSH_PASSWORD"]
        if require_garmin:
            required += ["GARMIN_EMAIL", "GARMIN_PASSWORD"]
        missing = [name for name in required if not env.get(name)]
        if missing:
            raise ConfigError("Fehlende Umgebungsvariablen: " + ", ".join(missing))

        return cls(
            mywhoosh_email=env["MYWHOOSH_EMAIL"],
            mywhoosh_password=env["MYWHOOSH_PASSWORD"],
            garmin_email=env.get("GARMIN_EMAIL") or None,
            garmin_password=env.get("GARMIN_PASSWORD") or None,
            interval_minutes=_parse_interval(env.get("SYNC_INTERVAL_MINUTES")),
            payload_key=env.get("MYWHOOSH_PAYLOAD_KEY") or DEFAULT_PAYLOAD_KEY,
            data_dir=Path(env.get("DATA_DIR") or "/data"),
            log_level=_parse_log_level(env.get("LOG_LEVEL")),
        )


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
