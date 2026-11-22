"""Shared helpers for the JSON state files: atomic writes, envelope reading and field validation."""

import json
import os
from datetime import date
from pathlib import Path

from mywhoosh_garmin_sync.state import StateCorruptError


def write_atomic(path: Path, payload: dict) -> None:
    """Write atomically: temp file in the same directory, then replace; no temp file is left on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        tmp_path.write_text(json.dumps(payload, indent=2))
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def read_envelope(path: Path, version: int, key: str) -> dict[str, object] | None:
    """Return the dict stored under `key`, or None when the file does not exist."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict) or raw.get("version") != version:
            raise ValueError("kein gültiges Zustandsformat")
        content = raw.get(key)
        if not isinstance(content, dict):
            raise ValueError(f"Feld {key} fehlt")
        return content
    except ValueError as exc:
        raise StateCorruptError(f"Zustandsdatei {path} ist beschädigt") from exc


def iso_date(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError("Datum ist kein Text")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Datum ist kein ISO-Datum") from exc
    if parsed.isoformat() != value:
        raise ValueError("Datum ist kein ISO-Datum")
    return parsed


def check_type(value: object, expected: type) -> None:
    # bool is a subclass of int, so it must be rejected explicitly for int fields.
    if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
        raise ValueError(f"Feld hat falschen Typ, erwartet {expected.__name__}")


def check_optional(value: object, expected: type) -> None:
    if value is not None:
        check_type(value, expected)
