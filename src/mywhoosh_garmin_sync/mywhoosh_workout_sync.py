"""One reconciliation run: make the MyWhoosh custom workouts match the planned bike sessions.

open-tri is the only source of truth. The key of a session is its planned id; its MyWhoosh
WorkoutId is derived from it and every own workout carries the marker ``open-tri:<id>`` as its
Description. Workouts without the marker are never touched. MyWhoosh applies writes
asynchronously and drops a write sent too early (probe), so every write is confirmed in the
listing before the next one; the state only records confirmed writes.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from mywhoosh_garmin_sync.mywhoosh import MyWhooshError
from mywhoosh_garmin_sync.mywhoosh_workout_json import (
    ZwoError,
    content_hash,
    parse_zwo,
    planned_id_from_description,
    to_mywhoosh_json,
    workout_id_for,
    workout_name,
)
from mywhoosh_garmin_sync.mywhoosh_workout_state import MyWhooshWorkoutEntry, MyWhooshWorkoutStateStore
from mywhoosh_garmin_sync.mywhoosh_workouts import MyWhooshNoSlotsError, MyWhooshWorkoutError, RemoteWorkout
from mywhoosh_garmin_sync.opentri import OpenTriError, OpenTriExportError, PlannedBike
from mywhoosh_garmin_sync.state import StateCorruptError

logger = logging.getLogger(__name__)

MAX_WORKOUT_ATTEMPTS = 5
CONFIRM_TIMEOUT_SECONDS = 30
CONFIRM_POLL_SECONDS = 3
SLOT_SETTLE_SECONDS = 5  # a deleted slot comes back ~2 s after the workout left the listing (probe)
SLOT_POLL_SECONDS = 1


class BikeSource(Protocol):
    def fetch_planned_bikes(self, start: date, end: date) -> list[PlannedBike] | None: ...

    def fetch_zwo(self, training_id: str) -> bytes: ...


class WorkoutLibrary(Protocol):
    def list(self) -> list[RemoteWorkout]: ...

    def credits(self) -> int: ...

    def upload(self, workout_json: dict[str, Any]) -> None: ...

    def delete(self, workout_id: int) -> None: ...


@dataclass
class MyWhooshWorkoutRunSummary:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    adopted: int = 0
    skipped: int = 0
    deferred: int = 0  # outside the wanted set for lack of slots
    failed: int = 0
    unconfirmed: bool = False  # a write was not visible within CONFIRM_TIMEOUT_SECONDS; writing stopped
    aborted: str | None = None


def sync_mywhoosh_workouts(
    source: BikeSource,
    library: WorkoutLibrary,
    store: MyWhooshWorkoutStateStore,
    today: date,
    days: int,
    now: Callable[[], datetime],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> MyWhooshWorkoutRunSummary:
    """Reconcile the window today .. today + days - 1. Never raises for expected failures."""
    summary = MyWhooshWorkoutRunSummary()
    try:
        _Run(source, library, store, today, days, now, sleep, clock, summary).execute()
    except StateCorruptError as exc:
        _abort(summary, f"{exc}. MyWhoosh-Abgleich abgebrochen, es wird nichts überschrieben.")
    except (OpenTriError, MyWhooshError) as exc:
        # per-workout errors are handled inside the run; what escapes is auth or availability
        _abort(summary, f"{_error_text(exc)} MyWhoosh-Abgleich abgebrochen.")
    logger.info("MyWhoosh-Abgleich beendet: %s", _describe(summary))
    return summary


class _Unconfirmed(Exception):
    """A write did not become visible in time; no further writes in this run."""


@dataclass(frozen=True)
class _Desired:
    bike: PlannedBike
    name: str
    workout_json: dict[str, Any]
    content_hash: str
    workout_id: int


class _Run:
    def __init__(
        self,
        source: BikeSource,
        library: WorkoutLibrary,
        store: MyWhooshWorkoutStateStore,
        today: date,
        days: int,
        now: Callable[[], datetime],
        sleep: Callable[[float], None],
        clock: Callable[[], float],
        summary: MyWhooshWorkoutRunSummary,
    ) -> None:
        self._source = source
        self._library = library
        self._store = store
        self._today = today
        self._end = today + timedelta(days=days - 1)
        self._now = now
        self._sleep = sleep
        self._clock = clock
        self._summary = summary
        self._bikes: list[PlannedBike] = []
        self._desired: dict[str, _Desired] = {}
        self._unrenderable: set[str] = set()
        self._remote: list[RemoteWorkout] = []
        self._deletions: list[int] = []
        self._wanted: list[str] = []
        self._deferred_days: list[date] = []

    def execute(self) -> None:
        self._store.all()  # a corrupt state file aborts here, before anything is read or written
        self._read_bikes()
        self._remote = self._library.list()
        self._reconcile_state()
        self._choose_wanted()
        try:
            self._delete_unwanted()
            self._upload_wanted()
        except _Unconfirmed:
            self._summary.unconfirmed = True
        self._forget_finished()
        self._warn_deferred()

    # --- step 1-2: open-tri -------------------------------------------------------------------

    def _read_bikes(self) -> None:
        bikes = self._source.fetch_planned_bikes(self._today, self._end)
        if bikes is None:
            logger.info("Kein aktiver Plan, MyWhoosh soll keine Workouts des Syncs halten")
            bikes = []
        zwos: dict[str, bytes | OpenTriExportError] = {}
        for bike in bikes:
            if any(known.planned_id == bike.planned_id for known in self._bikes):
                logger.warning("Geplante Einheit %s doppelt in open-tri, übersprungen", bike.planned_id)
                continue
            self._bikes.append(bike)
            name = workout_name(bike.day, bike.week, bike.brick_order, bike.training_name)
            try:
                if bike.training_id not in zwos:
                    try:
                        zwos[bike.training_id] = self._source.fetch_zwo(bike.training_id)
                    except OpenTriExportError as exc:
                        zwos[bike.training_id] = exc
                zwo = zwos[bike.training_id]
                if isinstance(zwo, OpenTriExportError):
                    raise zwo
                workout_json = to_mywhoosh_json(parse_zwo(zwo), name, bike.planned_id)
            except (OpenTriExportError, ZwoError) as exc:
                logger.warning("%s: nicht umrechenbar, übersprungen: %s", _label(name, bike.day), _error_text(exc))
                self._summary.skipped += 1
                self._unrenderable.add(bike.planned_id)
                continue
            self._desired[bike.planned_id] = _Desired(
                bike, name, workout_json, content_hash(workout_json), workout_id_for(bike.planned_id)
            )

    # --- step 4: state against the listing --------------------------------------------------------

    def _reconcile_state(self) -> None:
        stamp = self._now().isoformat()
        for planned_id, entry in self._store.all().items():
            if entry.workout_id is not None and not self._ours_at(entry.workout_id, planned_id):
                logger.info("%s: in MyWhoosh nicht mehr vorhanden", _label(entry.name, entry.day))
                self._store.put(replace(entry, workout_id=None, content_hash=None, updated_at=stamp))

        for bike in self._bikes:
            workout_id = workout_id_for(bike.planned_id)
            if not self._ours_at(workout_id, bike.planned_id):
                continue
            entry = self._store.all().get(bike.planned_id)
            if entry is None:
                name = next(w.name for w in self._remote if w.workout_id == workout_id)
                # content_hash None: the listing has no steps to compare, so it is uploaded once
                self._store.put(MyWhooshWorkoutEntry(bike.planned_id, name, bike.day, workout_id, None, 0, None,
                                                     stamp))
                self._summary.adopted += 1
                logger.info("%s: übernommen", _label(name, bike.day))
            elif entry.workout_id is None:
                self._store.put(replace(entry, workout_id=workout_id, content_hash=None, updated_at=stamp))

        for workout_id, group in self._by_id().items():
            marked = [w for w in group if planned_id_from_description(w.description) is not None]
            for workout in marked:
                planned_id = planned_id_from_description(workout.description)
                assert planned_id is not None
                if workout.workout_id != workout_id_for(planned_id):
                    self._add_deletion(workout_id)
            if len(group) > 1 and len(marked) == len(group):
                self._add_deletion(workout_id)  # one delete per run removes one entry (probe)
                for entry in self._store.all().values():
                    if entry.workout_id == workout_id and entry.content_hash is not None:
                        self._store.put(replace(entry, content_hash=None, updated_at=stamp))

    def _add_deletion(self, workout_id: int) -> None:
        if workout_id not in self._deletions:
            self._deletions.append(workout_id)

    # --- step 5: wanted set -----------------------------------------------------------------------

    def _choose_wanted(self) -> None:
        own = [w for w in self._remote if planned_id_from_description(w.description) is not None]
        available = self._library.credits() + len(own)
        entries = self._store.all()
        # an unrenderable session keeps its old workout; without one it occupies nothing
        kept = {
            b.planned_id for b in self._bikes
            if b.planned_id in self._unrenderable
            and (entry := entries.get(b.planned_id)) is not None and entry.workout_id is not None
            and b.day >= self._today
        }
        capacity = available - len(kept)
        chosen = set(kept)
        for bike in self._bikes:
            desired = self._desired.get(bike.planned_id)
            if desired is None:
                continue
            entry = entries.get(bike.planned_id)
            if entry is not None and entry.workout_id is None and self._exhausted(entry, desired):
                continue  # gives up after MAX attempts; must not hold a slot it will never use
            if capacity > 0:
                chosen.add(bike.planned_id)
                capacity -= 1
            else:
                self._defer(bike)
        self._wanted = [b.planned_id for b in self._bikes if b.planned_id in chosen]

    def _defer(self, bike: PlannedBike) -> None:
        self._summary.deferred += 1
        self._deferred_days.append(bike.day)

    # --- step 6: deletes ---------------------------------------------------------------------------

    def _delete_unwanted(self) -> None:
        wanted = set(self._wanted)
        candidates = list(self._deletions)
        for workout in self._remote:
            planned_id = planned_id_from_description(workout.description)
            if planned_id is not None and planned_id not in wanted and workout.workout_id not in candidates:
                candidates.append(workout.workout_id)
        for workout_id in candidates:
            self._delete(workout_id, wanted)

    def _delete(self, workout_id: int, wanted: set[str]) -> None:
        group = self._by_id().get(workout_id, [])
        if not group:
            return
        if any(planned_id_from_description(w.description) is None for w in group):
            logger.warning(
                "Workout-ID %s ist auch an ein fremdes Workout vergeben, es wird nicht gelöscht", workout_id
            )
            return
        before = len(group)
        try:
            self._library.delete(workout_id)
        except MyWhooshWorkoutError as exc:
            logger.error("%s: Löschen abgelehnt: %s", group[0].name, _error_text(exc))
            self._summary.failed += 1
            return
        if not self._confirm(lambda remote: _count(remote, workout_id) < before):
            logger.warning(
                "Löschung von %s ist nach %s Sekunden in MyWhoosh nicht bestätigt. "
                "In diesem Lauf wird nicht weiter geschrieben.",
                group[0].name,
                CONFIRM_TIMEOUT_SECONDS,
            )
            raise _Unconfirmed
        remaining = _count(self._remote, workout_id)
        self._summary.deleted += 1
        logger.info("%s: gelöscht", group[0].name)
        if remaining:
            return  # a duplicate is left; the state entry still points at it
        stamp = self._now().isoformat()
        for planned_id, entry in self._store.all().items():
            if entry.workout_id != workout_id:
                continue
            if planned_id in wanted:
                self._store.put(replace(entry, workout_id=None, content_hash=None, updated_at=stamp))
            else:
                self._store.remove(planned_id)

    # --- step 7: uploads -----------------------------------------------------------------------------

    def _upload_wanted(self) -> None:
        free: int | None = None
        no_slots = False
        for index, planned_id in enumerate(self._wanted):
            desired = self._desired.get(planned_id)
            if desired is None:
                continue  # unrenderable, its old workout stays
            entry = self._store.all().get(planned_id)
            is_new = entry is None or entry.workout_id is None
            unchanged = entry is not None and entry.content_hash == desired.content_hash
            if entry is not None and not is_new and unchanged and entry.attempts == 0:
                continue
            if entry is not None and self._exhausted(entry, desired):
                logger.debug("Einheit %s wird nach %s Fehlversuchen nicht mehr versucht", planned_id, entry.attempts)
                continue
            if self._foreign_at(desired):
                logger.warning(
                    "%s: Workout-ID %s gehört einem fremden Workout, übersprungen",
                    _label(desired.name, desired.bike.day),
                    desired.workout_id,
                )
                self._summary.skipped += 1
                continue
            if is_new:
                if not no_slots:
                    if free is None:
                        free = self._read_credits(self._new_uploads(self._wanted[index:]))
                    no_slots = free <= 0
                if no_slots:
                    self._defer(desired.bike)
                    continue
            before = list(self._remote)
            try:
                self._library.upload(desired.workout_json)
            except MyWhooshNoSlotsError as exc:
                if not is_new:
                    self._record_failure(entry, desired, exc)
                    continue
                no_slots = True
                self._defer(desired.bike)
                continue
            except MyWhooshWorkoutError as exc:
                self._record_failure(entry, desired, exc)
                continue
            self._confirm_upload(desired, before)
            if is_new:
                assert free is not None
                free -= 1
                self._summary.created += 1
            else:
                self._summary.updated += 1
            self._store.put(MyWhooshWorkoutEntry(
                planned_id, desired.name, desired.bike.day, desired.workout_id, desired.content_hash, 0, None,
                self._now().isoformat(),
            ))
            logger.info("%s: %s", _label(desired.name, desired.bike.day), "angelegt" if is_new else "aktualisiert")

    def _foreign_at(self, desired: _Desired) -> bool:
        return any(planned_id_from_description(w.description) != desired.bike.planned_id
                   for w in self._by_id().get(desired.workout_id, []))

    def _new_uploads(self, planned_ids: list[str]) -> int:
        """How many of these wanted sessions still need a new workout (and thus a slot)."""
        count = 0
        for planned_id in planned_ids:
            desired = self._desired.get(planned_id)
            if desired is None:
                continue
            entry = self._store.all().get(planned_id)
            if entry is not None and entry.workout_id is not None:
                continue
            if entry is not None and self._exhausted(entry, desired):
                continue
            if self._foreign_at(desired):
                continue
            count += 1
        return count

    def _read_credits(self, needed: int) -> int:
        """Free slots; after a confirmed delete, wait up to SLOT_SETTLE_SECONDS for freed slots."""
        free = self._library.credits()
        if self._summary.deleted == 0:
            return free
        start = self._clock()
        while free < needed and self._clock() - start < SLOT_SETTLE_SECONDS:
            self._sleep(SLOT_POLL_SECONDS)
            free = self._library.credits()
        return free

    def _confirm_upload(self, desired: _Desired, before: list[RemoteWorkout]) -> None:
        previous = [w.updated_at for w in before if w.workout_id == desired.workout_id]
        # a new workout (no old entry to mistake for it) or no updatedAt to compare: Id and Name suffice
        need_new_stamp = bool(previous) and all(stamp is not None for stamp in previous)

        def visible(remote: list[RemoteWorkout]) -> bool:
            return any(
                w.workout_id == desired.workout_id and w.name == desired.name
                and (not need_new_stamp or w.updated_at not in previous)
                for w in remote
            )

        if not self._confirm(visible):
            logger.warning(
                "%s: Upload ist nach %s Sekunden in MyWhoosh nicht bestätigt. "
                "In diesem Lauf wird nicht weiter geschrieben.",
                _label(desired.name, desired.bike.day),
                CONFIRM_TIMEOUT_SECONDS,
            )
            raise _Unconfirmed

    def _exhausted(self, entry: MyWhooshWorkoutEntry, desired: _Desired) -> bool:
        # a failed attempt stores the wanted hash, so a change in open-tri allows new attempts
        return entry.attempts >= MAX_WORKOUT_ATTEMPTS and entry.content_hash == desired.content_hash

    def _record_failure(self, entry: MyWhooshWorkoutEntry | None, desired: _Desired, exc: Exception) -> None:
        unchanged = entry is not None and entry.content_hash == desired.content_hash
        attempts = (entry.attempts if entry is not None and unchanged else 0) + 1
        error = _error_text(exc)
        self._store.put(MyWhooshWorkoutEntry(
            desired.bike.planned_id, desired.name, desired.bike.day,
            entry.workout_id if entry is not None else None,
            desired.content_hash, attempts, error, self._now().isoformat(),
        ))
        self._summary.failed += 1
        text = f"{_label(desired.name, desired.bike.day)}: Fehler (Versuch {attempts}/{MAX_WORKOUT_ATTEMPTS}): {error}"
        if attempts >= MAX_WORKOUT_ATTEMPTS:
            logger.error("%s. Die Einheit wird nicht mehr versucht, bis sie sich in open-tri ändert.", text)
        else:
            logger.error(text)

    # --- step 8: confirmation --------------------------------------------------------------------------

    def _confirm(self, condition: Callable[[list[RemoteWorkout]], bool]) -> bool:
        start = self._clock()
        while True:
            remote = self._library.list()
            if condition(remote):
                self._remote = remote
                return True
            if self._clock() - start >= CONFIRM_TIMEOUT_SECONDS:
                return False
            self._sleep(CONFIRM_POLL_SECONDS)

    # --- step 9: cleanup ----------------------------------------------------------------------------------

    def _forget_finished(self) -> None:
        planned = {b.planned_id for b in self._bikes}
        for planned_id, entry in self._store.all().items():
            if entry.workout_id is None and (entry.day < self._today or planned_id not in planned):
                self._store.remove(planned_id)

    def _warn_deferred(self) -> None:
        if not self._deferred_days:
            return
        logger.warning(
            "%s Radeinheiten passen nicht in die freien MyWhoosh-Slots, die erste am %s",
            len(self._deferred_days),
            min(self._deferred_days).isoformat(),
        )

    # --- helpers ----------------------------------------------------------------------------------------------

    def _by_id(self) -> dict[int, list[RemoteWorkout]]:
        by_id: dict[int, list[RemoteWorkout]] = {}
        for workout in self._remote:
            by_id.setdefault(workout.workout_id, []).append(workout)
        return by_id

    def _ours_at(self, workout_id: int, planned_id: str) -> bool:
        return any(
            w.workout_id == workout_id and planned_id_from_description(w.description) == planned_id
            for w in self._remote
        )


def _count(remote: list[RemoteWorkout], workout_id: int) -> int:
    return sum(1 for w in remote if w.workout_id == workout_id)


def _label(name: str, day: date) -> str:
    return f"{name} ({day.isoformat()})"


def _error_text(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def _abort(summary: MyWhooshWorkoutRunSummary, message: str) -> None:
    summary.aborted = message
    logger.error(message)


def _describe(summary: MyWhooshWorkoutRunSummary) -> str:
    parts = [
        f"{summary.created} angelegt",
        f"{summary.updated} aktualisiert",
        f"{summary.deleted} gelöscht",
        f"{summary.adopted} übernommen",
        f"{summary.skipped} übersprungen",
        f"{summary.deferred} zurückgestellt",
        f"{summary.failed} fehlgeschlagen",
    ]
    if summary.unconfirmed:
        parts.append("unbestätigt")
    if summary.aborted:
        parts.append("abgebrochen")
    return ", ".join(parts)
