"""One reconciliation run: make the Garmin calendar match the open-tri plan for a date window.

open-tri is the only source of truth. The key of a training is the FIT serial number; the
state file maps it to the Garmin workout and calendar entry. Garmin is read once per run
(the calendar of the window); everything else is a write that only happens on a difference.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from mywhoosh_garmin_sync.garmin import GarminAuthError, GarminUnavailableError
from mywhoosh_garmin_sync.garmin_workouts import GarminWorkoutError, ScheduledEntry
from mywhoosh_garmin_sync.opentri import OpenTriError
from mywhoosh_garmin_sync.state import StateCorruptError
from mywhoosh_garmin_sync.workout_fit import WorkoutBundleError, read_bundle_report
from mywhoosh_garmin_sync.workout_json import content_hash, serial_from_description, to_garmin_json
from mywhoosh_garmin_sync.workout_state import WorkoutEntry, WorkoutStateStore, WorkoutStatus

logger = logging.getLogger(__name__)

MAX_WORKOUT_ATTEMPTS = 5

# Probe result (Task 1): deleting a workout also removes its calendar entry, so past
# workouts must stay in the library to keep the calendar history.
DELETE_PAST_LIBRARY_WORKOUTS = False

_GARMIN_ABORTING_ERRORS = (GarminAuthError, GarminUnavailableError)

_CREATED = "angelegt"
_UPDATED = "aktualisiert"
_MOVED = "verschoben"
_REMOVED = "entfernt"
_ADOPTED = "übernommen"


class WorkoutSource(Protocol):
    def fetch_bundle(self, start: date, end: date) -> bytes | None: ...


class WorkoutTarget(Protocol):
    def create(self, workout_json: dict[str, Any]) -> int: ...

    def update(self, workout_id: int, workout_json: dict[str, Any]) -> None: ...

    def delete(self, workout_id: int) -> bool: ...  # False: already gone (404)

    def schedule(self, workout_id: int, day: date) -> int: ...

    def reschedule(self, schedule_id: int, day: date) -> None: ...

    def unschedule(self, schedule_id: int) -> bool: ...  # False: already gone (404)

    def scheduled(self, start: date, end: date) -> list[ScheduledEntry]: ...

    def description(self, workout_id: int) -> str | None: ...

    def send_to_devices(self) -> None: ...


@dataclass
class WorkoutRunSummary:
    created: int = 0
    updated: int = 0
    moved: int = 0
    removed: int = 0
    adopted: int = 0
    skipped: int = 0
    failed: int = 0
    aborted: str | None = None


def sync_workouts(
    source: WorkoutSource,
    garmin: WorkoutTarget,
    store: WorkoutStateStore,
    today: date,
    days: int,
    pool_length_m: float,
    now: Callable[[], datetime],
) -> WorkoutRunSummary:
    """Reconcile the window today .. today + days - 1. Never raises for expected failures."""
    summary = WorkoutRunSummary()
    writer = _CountingTarget(garmin)
    try:
        _Run(source, writer, store, today, days, pool_length_m, now, summary).execute()
    except StateCorruptError as exc:
        _abort(summary, f"{exc}. Trainings-Abgleich abgebrochen, es wird nichts überschrieben.")
    except (OpenTriError, WorkoutBundleError, *_GARMIN_ABORTING_ERRORS) as exc:
        _abort(summary, f"{_error_text(exc)} Trainings-Abgleich abgebrochen.")
    except GarminWorkoutError as exc:
        # per-workout errors are handled inside the run; one escaping comes from the calendar read
        _abort(summary, f"{_error_text(exc)} Trainings-Abgleich abgebrochen.")
    if writer.wrote:
        # also after an abort, so what was already written reaches the watch
        _send_to_devices(garmin)
    logger.info("Trainings-Abgleich beendet: %s", _describe(summary))
    return summary


@dataclass(frozen=True)
class _Target:
    name: str
    day: date
    workout_json: dict[str, Any]
    content_hash: str


@dataclass
class _Draft:
    """Mutable working copy of one state entry; reflects what Garmin holds after each write."""

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

    @classmethod
    def of(cls, entry: WorkoutEntry) -> "_Draft":
        return cls(
            serial=entry.serial,
            name=entry.name,
            day=entry.day,
            status=entry.status,
            workout_id=entry.workout_id,
            schedule_id=entry.schedule_id,
            content_hash=entry.content_hash,
            in_library=entry.in_library,
            attempts=entry.attempts,
            last_error=entry.last_error,
        )

    def entry(self, updated_at: str) -> WorkoutEntry:
        return WorkoutEntry(
            serial=self.serial,
            name=self.name,
            day=self.day,
            status=self.status,
            workout_id=self.workout_id,
            schedule_id=self.schedule_id,
            content_hash=self.content_hash,
            in_library=self.in_library,
            attempts=self.attempts,
            last_error=self.last_error,
            updated_at=updated_at,
        )


class _CountingTarget:
    """Forwards to the Garmin target and remembers whether a write succeeded."""

    def __init__(self, target: WorkoutTarget) -> None:
        self._target = target
        self.wrote = False

    def scheduled(self, start: date, end: date) -> list[ScheduledEntry]:
        return self._target.scheduled(start, end)

    def create(self, workout_json: dict[str, Any]) -> int:
        workout_id = self._target.create(workout_json)
        self.wrote = True
        return workout_id

    def update(self, workout_id: int, workout_json: dict[str, Any]) -> None:
        self._target.update(workout_id, workout_json)
        self.wrote = True

    def delete(self, workout_id: int) -> bool:
        changed = self._target.delete(workout_id)
        self.wrote = self.wrote or changed
        return changed

    def schedule(self, workout_id: int, day: date) -> int:
        schedule_id = self._target.schedule(workout_id, day)
        self.wrote = True
        return schedule_id

    def reschedule(self, schedule_id: int, day: date) -> None:
        self._target.reschedule(schedule_id, day)
        self.wrote = True

    def unschedule(self, schedule_id: int) -> bool:
        changed = self._target.unschedule(schedule_id)
        self.wrote = self.wrote or changed
        return changed


class _Run:
    def __init__(
        self,
        source: WorkoutSource,
        garmin: _CountingTarget,
        store: WorkoutStateStore,
        today: date,
        days: int,
        pool_length_m: float,
        now: Callable[[], datetime],
        summary: WorkoutRunSummary,
    ) -> None:
        self._source = source
        self._garmin = garmin
        self._store = store
        self._today = today
        self._end = today + timedelta(days=days - 1)
        self._pool_length_m = pool_length_m
        self._now = now
        self._summary = summary
        self._targets: dict[int, _Target] = {}
        self._untouchable: set[int] = set()  # in open-tri, but not readable or convertible
        self._by_schedule: dict[int, ScheduledEntry] = {}
        self._marked: dict[int, list[ScheduledEntry]] = {}

    def execute(self) -> None:
        self._store.load()
        self._read_targets()  # any open-tri or bundle error aborts here, before Garmin is touched
        self._read_calendar()
        self._close_past_days()
        for serial in self._serials():
            self._reconcile(serial)

    # --- reading -----------------------------------------------------------------------

    def _read_targets(self) -> None:
        bundle = self._source.fetch_bundle(self._today, self._end)
        if bundle is None:
            logger.info("Kein aktiver Plan oder keine Trainings im Zeitraum %s bis %s", self._today, self._end)
            return
        report = read_bundle_report(bundle)
        self._untouchable |= report.unreadable
        for item in report.planned:
            workout = item.workout
            if workout.serial in self._targets or workout.serial in self._untouchable:
                logger.warning(
                    "%s: Seriennummer %s doppelt im Export, übersprungen",
                    _label(workout.name, item.date),
                    workout.serial,
                )
                self._summary.skipped += 1
                continue
            if not self._today <= item.date <= self._end:
                logger.debug("Training %s liegt außerhalb des Zeitraums, ignoriert", workout.serial)
                continue
            try:
                workout_json = to_garmin_json(workout, self._pool_length_m)
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning(
                    "%s: nicht umrechenbar, übersprungen: %s", _label(workout.name, item.date), _error_text(exc)
                )
                self._summary.skipped += 1
                self._untouchable.add(workout.serial)
                continue
            self._targets[workout.serial] = _Target(workout.name, item.date, workout_json, content_hash(workout_json))

    def _read_calendar(self) -> None:
        for entry in self._garmin.scheduled(self._today, self._end):
            self._by_schedule[entry.schedule_id] = entry
            serial = serial_from_description(entry.description)
            if serial is not None:
                self._marked.setdefault(serial, []).append(entry)

    def _serials(self) -> list[int]:
        entries = self._store.all()
        serials = list(self._targets)
        serials += sorted(s for s, e in entries.items() if e.status == "active" and s not in self._targets)
        # marked calendar entries the state does not own (any more) may be orphans
        serials += sorted(s for s in self._marked if s not in entries or entries[s].status == "removed")
        return [s for s in dict.fromkeys(serials) if s not in self._untouchable]

    # --- past days -----------------------------------------------------------------------

    def _close_past_days(self) -> None:
        """Entries whose day is over become history; their calendar entry is never removed here.

        Exception: after a failed write (attempts > 0) the state keeps the wanted day, not the day
        Garmin shows. Such an entry stays active while Garmin still holds it inside the window, so it
        is put right or removed. An entry the operator moved in Garmin closes as usual.
        """
        stamp = self._now().isoformat()
        closed = [
            replace(entry, status="past", updated_at=stamp)
            for entry in self._store.all().values()
            if entry.status == "active" and entry.day < self._today and not self._pending_in_window(entry)
        ]
        if closed:
            self._store.put_many(closed)
        if not DELETE_PAST_LIBRARY_WORKOUTS:
            return
        for entry in self._store.all().values():
            if (
                entry.status == "past"
                and entry.in_library
                and entry.workout_id is not None
                and entry.serial not in self._targets
                and entry.attempts < MAX_WORKOUT_ATTEMPTS
            ):
                self._apply(entry, _Draft.of(entry), self._delete_past_library_workout, None)

    def _pending_in_window(self, entry: WorkoutEntry) -> bool:
        return entry.attempts > 0 and entry.schedule_id in self._by_schedule

    def _delete_past_library_workout(self, draft: _Draft) -> list[str]:
        assert draft.workout_id is not None
        self._garmin.delete(draft.workout_id)
        draft.workout_id = None
        draft.in_library = False
        logger.info("%s: aus der Bibliothek gelöscht", _label(draft.name, draft.day))
        return []

    # --- reconciliation -------------------------------------------------------------------

    def _reconcile(self, serial: int) -> None:
        initial = self._store.all().get(serial)
        usable = initial if initial is not None and initial.status != "removed" else None
        found = self._marked.get(serial, [])
        target = self._targets.get(serial)
        if target is not None:
            self._reconcile_target(serial, target, initial, usable, found)
        elif usable is not None and usable.status == "active":
            if usable.attempts < MAX_WORKOUT_ATTEMPTS:
                self._apply(usable, _Draft.of(usable), lambda d: self._remove(d, found), None)
        elif usable is None and found:
            if initial is None or initial.attempts < MAX_WORKOUT_ATTEMPTS:
                attempts = initial.attempts if initial else 0
                last_error = initial.last_error if initial else None
                draft = _Draft(serial, found[0].name, found[0].date, "removed", None, None, None, False,
                               attempts, last_error)
                self._apply(initial, draft, lambda d: self._remove_orphans(d, found), None)

    def _reconcile_target(
        self,
        serial: int,
        target: _Target,
        initial: WorkoutEntry | None,
        usable: WorkoutEntry | None,
        found: list[ScheduledEntry],
    ) -> None:
        unchanged = usable is not None and (usable.content_hash, usable.day) == (target.content_hash, target.day)
        if usable is not None and unchanged and usable.attempts >= MAX_WORKOUT_ATTEMPTS:
            logger.debug("Training %s wird nach %s Fehlversuchen nicht mehr versucht", serial, usable.attempts)
            return

        kept = self._kept_entry(usable, found)
        extras = [entry for entry in found if entry is not kept]
        if usable is not None:
            draft = _Draft.of(usable)
            draft.attempts = usable.attempts if unchanged else 0
            # a failed earlier run stored the wanted content and day, not what Garmin holds
            retry = usable.attempts > 0
            labels: list[str] | None = None
        elif kept is not None:
            draft = _Draft(serial, kept.name, kept.date, "active", kept.workout_id, kept.schedule_id,
                           None, True, 0, None)
            retry = False
            labels = [_ADOPTED]
        else:
            draft = _Draft(serial, target.name, target.day, "active", None, None, None, False, 0, None)
            retry = False
            labels = [_CREATED]

        def work(d: _Draft) -> list[str]:
            self._remove_extras(d, extras)
            changes = self._bring_up_to_date(d, target, retry)
            d.name, d.day, d.status = target.name, target.day, "active"
            d.attempts, d.last_error = 0, None
            if labels is not None and (changes or labels == [_ADOPTED]):
                return labels
            return changes

        self._apply(initial, draft, work, target)

    def _kept_entry(self, usable: WorkoutEntry | None, found: list[ScheduledEntry]) -> ScheduledEntry | None:
        """The calendar entry that belongs to the state; without state the first marked one."""
        if usable is not None:
            return next((e for e in found if e.schedule_id == usable.schedule_id), None)
        return found[0] if found else None

    def _remove_extras(self, draft: _Draft, extras: list[ScheduledEntry]) -> None:
        """Further marked entries for the same serial are duplicates and must go."""
        deleted: set[int] = set()
        for extra in extras:
            if extra.workout_id == draft.workout_id:
                self._garmin.unschedule(extra.schedule_id)
            elif extra.workout_id not in deleted:
                self._garmin.delete(extra.workout_id)
                deleted.add(extra.workout_id)

    def _bring_up_to_date(self, draft: _Draft, target: _Target, retry: bool) -> list[str]:
        changes: list[str] = []
        if draft.workout_id is None or not draft.in_library:
            if draft.schedule_id is None:
                self._create(draft, target)
                self._schedule(draft, target, fresh=True)
                return [_CREATED]
            # the old calendar entry points at a workout that is no longer in the library
            self._garmin.unschedule(draft.schedule_id)
            draft.schedule_id = None
            if draft.content_hash != target.content_hash:
                changes.append(_UPDATED)
            self._create(draft, target)
            self._schedule(draft, target, fresh=True)
            changes.append(_MOVED)
            return changes
        if retry or draft.content_hash != target.content_hash:
            self._garmin.update(draft.workout_id, target.workout_json)
            draft.content_hash = target.content_hash
            changes.append(_UPDATED)
        if self._place(draft, target):
            changes.append(_MOVED)
        return changes

    def _place(self, draft: _Draft, target: _Target) -> bool:
        """Make sure the workout has exactly its calendar entry on the target day."""
        if draft.schedule_id is not None:
            actual = self._by_schedule.get(draft.schedule_id)
            if actual is not None:
                if actual.date == target.day:
                    return False
                self._garmin.reschedule(draft.schedule_id, target.day)
                draft.day = target.day
                return True
            # not in the window: moved outside it or deleted in Garmin. How reschedule answers an
            # unknown id is unprobed, so replace the entry: unschedule tolerates 404.
            self._garmin.unschedule(draft.schedule_id)
            draft.schedule_id = None
        self._schedule(draft, target, fresh=False)
        return True

    def _schedule(self, draft: _Draft, target: _Target, fresh: bool) -> None:
        assert draft.workout_id is not None
        try:
            draft.schedule_id = self._garmin.schedule(draft.workout_id, target.day)
        except GarminWorkoutError:
            if fresh:
                raise
            # Garmin no longer knows the workout: replace it instead of keeping a dead entry
            self._garmin.delete(draft.workout_id)
            draft.workout_id, draft.in_library = None, False
            self._create(draft, target)
            draft.schedule_id = self._garmin.schedule(draft.workout_id, target.day)
        draft.day = target.day

    def _create(self, draft: _Draft, target: _Target) -> None:
        draft.workout_id = self._garmin.create(target.workout_json)
        draft.in_library = True
        draft.content_hash = target.content_hash

    def _remove(self, draft: _Draft, found: list[ScheduledEntry]) -> list[str]:
        """Gone from open-tri: deleting the workout also removes its calendar entries."""
        deleted: set[int] = set()
        if draft.workout_id is not None and draft.in_library:
            self._garmin.delete(draft.workout_id)
            deleted.add(draft.workout_id)
            draft.workout_id, draft.in_library = None, False
        for extra in found:
            if extra.workout_id not in deleted:
                self._garmin.delete(extra.workout_id)
                deleted.add(extra.workout_id)
        draft.status, draft.schedule_id, draft.workout_id, draft.in_library = "removed", None, None, False
        draft.attempts, draft.last_error = 0, None
        return [_REMOVED] if deleted else []

    def _remove_orphans(self, draft: _Draft, found: list[ScheduledEntry]) -> list[str]:
        """Marked entries in the window that open-tri no longer plans and the state does not know."""
        changed = False
        # Unschedule first: the entry can outlive its workout (delete then answers 404).
        for schedule_id in dict.fromkeys(entry.schedule_id for entry in found):
            changed = self._garmin.unschedule(schedule_id) or changed
        for workout_id in dict.fromkeys(entry.workout_id for entry in found):
            changed = self._garmin.delete(workout_id) or changed
        draft.attempts, draft.last_error = 0, None
        return [_REMOVED] if changed else []

    # --- bookkeeping -----------------------------------------------------------------------

    def _apply(
        self,
        initial: WorkoutEntry | None,
        draft: _Draft,
        work: Callable[[_Draft], list[str]],
        target: _Target | None,
    ) -> None:
        """Run the writes for one serial and persist what Garmin holds afterwards, also on errors."""
        try:
            changes = work(draft)
        except GarminWorkoutError as exc:
            self._record_failure(draft, exc, target)
            self._save(initial, draft)
            return
        except BaseException:
            self._save(initial, draft)  # keep partial progress; aborting errors count no attempt
            raise
        self._save(initial, draft)
        if changes:
            logger.info("%s: %s", _label(draft.name, draft.day), ", ".join(changes))
            self._count(changes)

    def _record_failure(self, draft: _Draft, exc: GarminWorkoutError, target: _Target | None) -> None:
        if target is not None:
            # remember what was wanted, so the attempt limit holds until open-tri changes it
            draft.name, draft.day, draft.content_hash, draft.status = (
                target.name, target.day, target.content_hash, "active",
            )
        draft.attempts += 1
        draft.last_error = _error_text(exc)
        self._summary.failed += 1
        text = (
            f"{_label(draft.name, draft.day)}: Fehler (Versuch {draft.attempts}/{MAX_WORKOUT_ATTEMPTS}): "
            f"{draft.last_error}"
        )
        if draft.attempts >= MAX_WORKOUT_ATTEMPTS:
            logger.error("%s. Das Training wird nicht mehr versucht, bis es sich in open-tri ändert.", text)
        else:
            logger.error(text)

    def _save(self, initial: WorkoutEntry | None, draft: _Draft) -> None:
        if initial is not None and draft.entry(initial.updated_at) == initial:
            return
        if initial is None and draft.workout_id is None and draft.attempts == 0 and draft.status == "active":
            return  # nothing happened in Garmin and nothing to remember
        self._store.put(draft.entry(self._now().isoformat()))

    def _count(self, changes: list[str]) -> None:
        counters = {
            _CREATED: "created",
            _UPDATED: "updated",
            _MOVED: "moved",
            _REMOVED: "removed",
            _ADOPTED: "adopted",
        }
        for change in changes:
            name = counters[change]
            setattr(self._summary, name, getattr(self._summary, name) + 1)


def _send_to_devices(garmin: WorkoutTarget) -> None:
    try:
        garmin.send_to_devices()
    except (GarminWorkoutError, *_GARMIN_ABORTING_ERRORS) as exc:
        logger.warning("Trainings konnten nicht an die Geräte gesendet werden: %s", _error_text(exc))


def _label(name: str, day: date) -> str:
    return f"{name} ({day.isoformat()})"


def _error_text(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def _abort(summary: WorkoutRunSummary, message: str) -> None:
    summary.aborted = message
    logger.error(message)


def _describe(summary: WorkoutRunSummary) -> str:
    parts = [
        f"{summary.created} angelegt",
        f"{summary.updated} aktualisiert",
        f"{summary.moved} verschoben",
        f"{summary.removed} entfernt",
        f"{summary.adopted} übernommen",
        f"{summary.skipped} übersprungen",
        f"{summary.failed} fehlgeschlagen",
    ]
    if summary.aborted:
        parts.append("abgebrochen")
    return ", ".join(parts)
