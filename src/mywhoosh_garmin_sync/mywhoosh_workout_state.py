"""Persistent record of which planned open-tri sessions are mapped to MyWhoosh workouts."""

from dataclasses import asdict, dataclass, fields
from datetime import date
from pathlib import Path

from mywhoosh_garmin_sync.json_state import check_optional, check_type, iso_date, read_envelope, write_atomic
from mywhoosh_garmin_sync.state import StateCorruptError

_VERSION = 1


@dataclass(frozen=True)
class MyWhooshWorkoutEntry:
    planned_id: str
    name: str
    day: date
    workout_id: int | None  # None: not (yet) in MyWhoosh
    content_hash: str | None
    attempts: int
    last_error: str | None
    updated_at: str


class MyWhooshWorkoutStateStore:
    """Keeps one entry per planned-session id in a JSON file, written on every change."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: dict[str, MyWhooshWorkoutEntry] | None = None

    def exists(self) -> bool:
        return self._path.exists()

    def all(self) -> dict[str, MyWhooshWorkoutEntry]:
        return dict(self._loaded_entries())

    def put(self, entry: MyWhooshWorkoutEntry) -> None:
        merged = {**self._loaded_entries(), entry.planned_id: entry}
        self._write(merged)
        self._entries = merged

    def remove(self, planned_id: str) -> None:
        """Drop an entry; an unknown id is a no-op and does not touch the file."""
        current = self._loaded_entries()
        if planned_id not in current:
            return
        remaining = {key: value for key, value in current.items() if key != planned_id}
        self._write(remaining)
        self._entries = remaining

    def _loaded_entries(self) -> dict[str, MyWhooshWorkoutEntry]:
        if self._entries is None:
            self._entries = self._read()
        return self._entries

    def _read(self) -> dict[str, MyWhooshWorkoutEntry]:
        workouts = read_envelope(self._path, _VERSION, "workouts")
        if workouts is None:
            return {}
        try:
            return {key: _entry_from_dict(key, item) for key, item in workouts.items()}
        except ValueError as exc:
            raise StateCorruptError(f"Zustandsdatei {self._path} ist beschädigt") from exc

    def _write(self, entries: dict[str, MyWhooshWorkoutEntry]) -> None:
        write_atomic(
            self._path,
            {"version": _VERSION, "workouts": {key: _to_dict(entry) for key, entry in entries.items()}},
        )


def _to_dict(entry: MyWhooshWorkoutEntry) -> dict[str, object]:
    data = asdict(entry)
    del data["planned_id"]
    data["day"] = entry.day.isoformat()
    return data


def _entry_from_dict(planned_id: str, item: object) -> MyWhooshWorkoutEntry:
    if not isinstance(item, dict):
        raise ValueError("Eintrag ist kein Objekt")
    expected = {f.name for f in fields(MyWhooshWorkoutEntry)} - {"planned_id"}
    if set(item) != expected:
        raise ValueError("Eintrag hat unerwartete oder fehlende Felder")
    day = iso_date(item["day"])
    check_type(item["name"], str)
    check_type(item["updated_at"], str)
    check_optional(item["workout_id"], int)
    check_optional(item["content_hash"], str)
    check_optional(item["last_error"], str)
    check_type(item["attempts"], int)
    if item["attempts"] < 0:
        raise ValueError("attempts ist negativ")
    return MyWhooshWorkoutEntry(
        planned_id=planned_id,
        name=item["name"],
        day=day,
        workout_id=item["workout_id"],
        content_hash=item["content_hash"],
        attempts=item["attempts"],
        last_error=item["last_error"],
        updated_at=item["updated_at"],
    )
