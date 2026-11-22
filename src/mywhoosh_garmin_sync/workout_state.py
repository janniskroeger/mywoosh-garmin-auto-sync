"""Persistent record of which open-tri workouts are mapped to Garmin workouts and calendar entries."""

from dataclasses import asdict, dataclass, fields
from datetime import date
from pathlib import Path
from typing import Literal, get_args

from mywhoosh_garmin_sync.json_state import check_optional, check_type, iso_date, read_envelope, write_atomic
from mywhoosh_garmin_sync.state import StateCorruptError

WorkoutStatus = Literal["active", "past", "removed"]

_VERSION = 1


@dataclass(frozen=True)
class WorkoutEntry:
    serial: int
    name: str
    day: date
    status: WorkoutStatus
    workout_id: int | None
    schedule_id: int | None
    content_hash: str | None
    in_library: bool
    attempts: int
    last_error: str | None
    updated_at: str


class WorkoutStateStore:
    """Keeps one entry per FIT serial number in a JSON file, written on every change."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: dict[int, WorkoutEntry] | None = None

    def exists(self) -> bool:
        return self._path.exists()

    def load(self) -> None:
        """Read the state file if it exists; later calls do nothing."""
        self._loaded_entries()

    def all(self) -> dict[int, WorkoutEntry]:
        return dict(self._loaded_entries())

    def put(self, entry: WorkoutEntry) -> None:
        self.put_many([entry])

    def put_many(self, entries: list[WorkoutEntry]) -> None:
        """Replace entries with the same serial and keep the others, using one atomic write."""
        merged = {**self._loaded_entries(), **{entry.serial: entry for entry in entries}}
        self._write(merged)
        self._entries = merged

    def _loaded_entries(self) -> dict[int, WorkoutEntry]:
        if self._entries is None:
            self._entries = self._read()
        return self._entries

    def _read(self) -> dict[int, WorkoutEntry]:
        workouts = read_envelope(self._path, _VERSION, "workouts")
        if workouts is None:
            return {}
        try:
            entries = {}
            for key, item in workouts.items():
                serial = _serial_from_key(key)
                entries[serial] = _entry_from_dict(serial, item)
            return entries
        except ValueError as exc:
            raise StateCorruptError(f"Zustandsdatei {self._path} ist beschädigt") from exc

    def _write(self, entries: dict[int, WorkoutEntry]) -> None:
        write_atomic(
            self._path,
            {"version": _VERSION, "workouts": {str(serial): _to_dict(entry) for serial, entry in entries.items()}},
        )


def _to_dict(entry: WorkoutEntry) -> dict[str, object]:
    data = asdict(entry)
    del data["serial"]
    data["day"] = entry.day.isoformat()
    return data


def _serial_from_key(key: str) -> int:
    if not (key.isascii() and key.isdigit()):
        raise ValueError("Seriennummer ist keine Ziffernfolge")
    return int(key)


def _entry_from_dict(serial: int, item: object) -> WorkoutEntry:
    if not isinstance(item, dict):
        raise ValueError("Eintrag ist kein Objekt")
    expected = {f.name for f in fields(WorkoutEntry)} - {"serial"}
    if set(item) != expected:
        raise ValueError("Eintrag hat unerwartete oder fehlende Felder")
    day = iso_date(item["day"])
    if item["status"] not in get_args(WorkoutStatus):
        raise ValueError("Eintrag hat unbekannten Status")
    check_type(item["name"], str)
    check_type(item["updated_at"], str)
    check_optional(item["workout_id"], int)
    check_optional(item["schedule_id"], int)
    check_optional(item["content_hash"], str)
    check_optional(item["last_error"], str)
    check_type(item["in_library"], bool)
    check_type(item["attempts"], int)
    if item["attempts"] < 0:
        raise ValueError("attempts ist negativ")
    return WorkoutEntry(
        serial=serial,
        name=item["name"],
        day=day,
        status=item["status"],
        workout_id=item["workout_id"],
        schedule_id=item["schedule_id"],
        content_hash=item["content_hash"],
        in_library=item["in_library"],
        attempts=item["attempts"],
        last_error=item["last_error"],
        updated_at=item["updated_at"],
    )
