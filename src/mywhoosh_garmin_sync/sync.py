"""One sync run: list new MyWhoosh rides, patch them and import them into Garmin Connect."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from mywhoosh_garmin_sync.fit_patch import FitPatchError, patch_device
from mywhoosh_garmin_sync.garmin import (
    GarminAuthError,
    GarminImportError,
    GarminUnavailableError,
    ImportOutcome,
)
from mywhoosh_garmin_sync.mywhoosh import (
    MyWhooshActivity,
    MyWhooshAuthError,
    MyWhooshDownloadError,
    MyWhooshUnavailableError,
)
from mywhoosh_garmin_sync.state import Entry, StateCorruptError, StateStore

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5

_CYCLING = "cycling"
_RIDE_ERRORS = (MyWhooshDownloadError, FitPatchError, GarminImportError)
_RUN_ABORTING_ERRORS = (
    MyWhooshAuthError,
    MyWhooshUnavailableError,
    GarminAuthError,
    GarminUnavailableError,
)


class MyWhooshSource(Protocol):
    def list_recent(self) -> list[MyWhooshActivity]: ...

    def download_fit(self, file_id: str) -> bytes: ...


class GarminSink(Protocol):
    def ensure_login(self) -> None: ...

    def import_fit(self, path: Path, start: datetime) -> ImportOutcome: ...

    def set_name(self, activity_id: int, title: str) -> None: ...


@dataclass
class RunSummary:
    new: int = 0
    uploaded: int = 0
    duplicates: int = 0
    skipped: int = 0
    failed: int = 0
    retry_later: int = 0
    baseline: int = 0
    aborted: str | None = None


def run_once(
    source: MyWhooshSource,
    garmin: GarminSink,
    store: StateStore,
    work_dir: Path,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RunSummary:
    """Process all rides that are not done yet, oldest first. Never raises for expected failures."""
    summary = RunSummary()
    try:
        _run(source, garmin, store, work_dir, now, summary)
    except StateCorruptError as exc:
        _abort(summary, f"{exc}. Lauf abgebrochen, es wird nichts überschrieben.")
    except _RUN_ABORTING_ERRORS as exc:
        _abort(summary, f"{_error_text(exc)} Lauf abgebrochen.")
    logger.info("Lauf beendet: %s", _describe(summary))
    return summary


def dry_run(
    source: MyWhooshSource, out_dir: Path, file_id: str | None = None
) -> tuple[Path, Path]:
    """Download one ride and write the original and the rewritten FIT file to out_dir."""
    activities = source.list_recent()
    if file_id is None:
        if not activities:
            raise ValueError("Keine Fahrten in der MyWhoosh-Liste gefunden")
        chosen = max(activities, key=lambda a: a.start)
    else:
        chosen = next((a for a in activities if a.file_id == file_id), None)
        if chosen is None:
            raise ValueError(f"Fahrt {file_id} steht nicht in der MyWhoosh-Liste")

    original_bytes = source.download_fit(chosen.file_id)
    patched_bytes = patch_device(original_bytes)

    out_dir.mkdir(parents=True, exist_ok=True)
    original = out_dir / f"{chosen.file_id}.original.fit"
    patched = out_dir / f"{chosen.file_id}.edge1050.fit"
    original.write_bytes(original_bytes)
    patched.write_bytes(patched_bytes)
    return original, patched


def _run(
    source: MyWhooshSource,
    garmin: GarminSink,
    store: StateStore,
    work_dir: Path,
    now: Callable[[], datetime],
    summary: RunSummary,
) -> None:
    # exists() is true for a corrupt file too; load() then raises instead of re-baselining.
    first_run = not store.exists()
    if not first_run:
        store.load()

    activities = source.list_recent()

    if first_run:
        store.put_many({a.file_id: _entry("baseline", a, now()) for a in activities})
        summary.baseline = len(activities)
        return

    pending = sorted(
        (a for a in activities if not store.is_done(a.file_id)), key=lambda a: a.start
    )
    summary.new = len(pending)
    if not pending:
        return

    work_dir.mkdir(parents=True, exist_ok=True)
    logged_in = False
    for activity in pending:
        if activity.sport_type != _CYCLING:
            store.put(activity.file_id, _entry("skipped", activity, now()))
            summary.skipped += 1
            logger.info("%s: übersprungen (Sportart %s)", _label(activity), activity.sport_type)
            continue
        if not logged_in:
            garmin.ensure_login()
            logged_in = True
        _process_ride(source, garmin, store, work_dir, now, summary, activity)


def _process_ride(
    source: MyWhooshSource,
    garmin: GarminSink,
    store: StateStore,
    work_dir: Path,
    now: Callable[[], datetime],
    summary: RunSummary,
    activity: MyWhooshActivity,
) -> None:
    fit_path = work_dir / f"{activity.file_id}.fit"
    try:
        fit_path.write_bytes(patch_device(source.download_fit(activity.file_id)))
        outcome = garmin.import_fit(fit_path, activity.start)
    except _RUN_ABORTING_ERRORS:
        raise
    except _RIDE_ERRORS as exc:
        _record_failure(store, summary, activity, exc, now())
        return
    finally:
        fit_path.unlink(missing_ok=True)

    if outcome.status == "duplicate":
        store.put(activity.file_id, _entry("duplicate", activity, now()))
        summary.duplicates += 1
        logger.info("%s: Duplikat", _label(activity))
        return

    store.put(
        activity.file_id,
        _entry("uploaded", activity, now(), garmin_activity_id=outcome.activity_id),
    )
    summary.uploaded += 1
    logger.info("%s: hochgeladen", _label(activity))
    _rename(garmin, activity, outcome.activity_id)


def _rename(garmin: GarminSink, activity: MyWhooshActivity, activity_id: int | None) -> None:
    if activity_id is None:
        logger.warning(
            "%s: Garmin-Aktivität nicht gefunden, Titel wird nicht gesetzt", _label(activity)
        )
        return
    try:
        garmin.set_name(activity_id, activity.title)
    except Exception as exc:  # the uploader does not wrap library errors; the ride stays uploaded
        logger.warning(
            "%s: Titel konnte nicht gesetzt werden: %s", _label(activity), _error_text(exc)
        )


def _record_failure(
    store: StateStore,
    summary: RunSummary,
    activity: MyWhooshActivity,
    exc: Exception,
    timestamp: datetime,
) -> None:
    previous = store.get(activity.file_id)
    attempts = (previous.attempts if previous else 0) + 1
    message = _error_text(exc)
    final = attempts >= MAX_ATTEMPTS
    store.put(
        activity.file_id,
        _entry(
            "failed" if final else "pending",
            activity,
            timestamp,
            attempts=attempts,
            last_error=message,
        ),
    )
    text = f"{_label(activity)}: Fehler (Versuch {attempts}/{MAX_ATTEMPTS}): {message}"
    if final:
        summary.failed += 1
        logger.error("%s. Die Fahrt wird nicht mehr versucht.", text)
    else:
        summary.retry_later += 1
        logger.warning(text)


def _entry(
    status: str,
    activity: MyWhooshActivity,
    timestamp: datetime,
    *,
    attempts: int = 0,
    garmin_activity_id: int | None = None,
    last_error: str | None = None,
) -> Entry:
    return Entry(
        status=status,  # type: ignore[arg-type]
        title=activity.title,
        start=activity.start.isoformat(),
        updated_at=timestamp.isoformat(),
        attempts=attempts,
        garmin_activity_id=garmin_activity_id,
        last_error=last_error,
    )


def _label(activity: MyWhooshActivity) -> str:
    local_start = activity.start.astimezone().strftime("%Y-%m-%d %H:%M")
    return f"{activity.title} ({local_start})"


def _error_text(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def _abort(summary: RunSummary, message: str) -> None:
    summary.aborted = message
    logger.error(message)


def _describe(summary: RunSummary) -> str:
    parts = [
        f"{summary.new} neu",
        f"{summary.uploaded} hochgeladen",
        f"{summary.duplicates} Duplikate",
        f"{summary.skipped} übersprungen",
        f"{summary.retry_later} später erneut",
        f"{summary.failed} fehlgeschlagen",
    ]
    if summary.baseline:
        parts.append(f"{summary.baseline} als Ausgangsstand erfasst")
    if summary.aborted:
        parts.append("abgebrochen")
    return ", ".join(parts)
