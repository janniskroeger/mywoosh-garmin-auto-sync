"""Persistent record of which MyWhoosh rides have already been handled."""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, get_args

Status = Literal["baseline", "uploaded", "duplicate", "skipped", "failed", "pending"]
DONE_STATUSES = {"baseline", "uploaded", "duplicate", "skipped", "failed"}

_VERSION = 1


class StateCorruptError(Exception):
    """Raised when the state file exists but cannot be read as valid state."""


@dataclass
class Entry:
    status: Status
    title: str
    start: str
    updated_at: str
    attempts: int = 0
    garmin_activity_id: int | None = None
    last_error: str | None = None


class StateStore:
    """Keeps one entry per MyWhoosh file id in a JSON file, written on every change."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: dict[str, Entry] | None = None

    def exists(self) -> bool:
        return self._path.exists()

    def load(self) -> None:
        """Read the state file if it exists; later calls do nothing."""
        self._loaded_entries()

    def get(self, file_id: str) -> Entry | None:
        return self._loaded_entries().get(file_id)

    def is_done(self, file_id: str) -> bool:
        entry = self.get(file_id)
        return entry is not None and entry.status in DONE_STATUSES

    def put(self, file_id: str, entry: Entry) -> None:
        entries = {**self._loaded_entries(), file_id: entry}
        self._write(entries)
        self._entries = entries

    def put_many(self, entries: dict[str, Entry]) -> None:
        """Merge several entries with one atomic write; also creates the file for an empty dict."""
        merged = {**self._loaded_entries(), **entries}
        self._write(merged)
        self._entries = merged

    def _loaded_entries(self) -> dict[str, Entry]:
        if self._entries is None:
            self._entries = self._read()
        return self._entries

    def _read(self) -> dict[str, Entry]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text())
            if not isinstance(raw, dict) or raw.get("version") != _VERSION:
                raise ValueError("kein gültiges Zustandsformat")
            activities = raw.get("activities")
            if not isinstance(activities, dict):
                raise ValueError("Feld activities fehlt")
            return {file_id: _entry_from_dict(item) for file_id, item in activities.items()}
        except ValueError as exc:
            raise StateCorruptError(f"Zustandsdatei {self._path} ist beschädigt") from exc

    def _write(self, entries: dict[str, Entry]) -> None:
        """Write atomically: temp file in the same directory, then replace."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": _VERSION, "activities": {file_id: asdict(entry) for file_id, entry in entries.items()}}
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        tmp_path.write_text(json.dumps(data, indent=2))
        os.replace(tmp_path, self._path)


def _entry_from_dict(item: object) -> Entry:
    if not isinstance(item, dict):
        raise ValueError("Eintrag ist kein Objekt")
    try:
        entry = Entry(**item)
    except TypeError as exc:
        raise ValueError("Eintrag hat unerwartete Felder") from exc
    if entry.status not in get_args(Status):
        raise ValueError("Eintrag hat unbekannten Status")
    return entry
