"""Reads planned workouts from an open-tri FIT export bundle (ZIP).

open-tri writes the FIT SDK main fields with raw values and no resolved subfields, so
this module reads the raw field values and converts them itself (ms -> s, cm -> m,
mm/s -> m/s, heart rate - 100, power - 1000).
"""

import io
import logging
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from fit_tool.fit_file import FitFile
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.schedule_message import ScheduleMessage
from fit_tool.profile.messages.workout_message import WorkoutMessage
from fit_tool.profile.messages.workout_step_message import WorkoutStepMessage

logger = logging.getLogger(__name__)

Intensity = Literal["warmup", "active", "recovery", "cooldown", "rest"]
Duration = Literal["time", "distance", "open"]
Target = Literal["open", "heart_rate", "power", "speed"]
Sport = Literal["swimming", "cycling", "running"]

SCHEDULES_NAME = "schedules.fit"
FIT_INVALID = 4294967295
FIT_LOCAL_EPOCH = datetime(1989, 12, 31)

SPORTS: dict[int, Sport] = {1: "running", 2: "cycling", 5: "swimming"}
INTENSITIES: dict[int, Intensity] = {0: "active", 1: "rest", 2: "warmup", 3: "cooldown", 4: "recovery"}
DURATION_TIME, DURATION_DISTANCE, DURATION_OPEN, DURATION_REPEAT = 0, 1, 5, 6
TARGET_SPEED, TARGET_HEART_RATE, TARGET_OPEN, TARGET_POWER = 0, 1, 2, 4
HEART_RATE_OFFSET = 100
POWER_OFFSET = 1000


@dataclass(frozen=True)
class FitStep:
    name: str | None
    notes: str | None
    intensity: Intensity
    duration: Duration
    duration_value: float | None  # seconds or meters
    target: Target
    target_low: float | None  # bpm, W or m/s, without FIT offset
    target_high: float | None


@dataclass(frozen=True)
class FitRepeat:
    steps: tuple[FitStep, ...]
    iterations: int


@dataclass(frozen=True)
class FitWorkout:
    serial: int
    name: str
    sport: Sport
    items: tuple[FitStep | FitRepeat, ...]


@dataclass(frozen=True)
class PlannedWorkout:
    workout: FitWorkout
    date: date


class WorkoutBundleError(ValueError):
    """The bundle is not a readable ZIP or has no readable Schedules.fit."""


def _raw(message, name: str):
    """Raw main-field value, or None when the field is missing or unset."""
    field = message.get_field_by_name(name)
    if field is None or not field.is_valid():
        return None
    value = field.get_value()
    return None if value == FIT_INVALID else value


def _messages(fit_bytes: bytes, cls) -> list:
    return [r.message for r in FitFile.from_bytes(fit_bytes).records if isinstance(r.message, cls)]


def _parse_step(msg: WorkoutStepMessage) -> FitStep:
    intensity = INTENSITIES.get(_raw(msg, "intensity"))
    if intensity is None:
        raise ValueError(f"Unbekannte Intensität {_raw(msg, 'intensity')}")

    duration_type = _raw(msg, "duration_type")
    duration_value = _raw(msg, "duration_value")
    if duration_type == DURATION_TIME and duration_value is not None:
        duration, value = "time", duration_value / 1000
    elif duration_type == DURATION_DISTANCE and duration_value is not None:
        duration, value = "distance", duration_value / 100
    elif duration_type == DURATION_OPEN:
        duration, value = "open", None
    else:
        raise ValueError(f"Nicht unterstützte Schrittdauer {duration_type}")

    target_type = _raw(msg, "target_type")
    low = _raw(msg, "custom_target_value_low")
    high = _raw(msg, "custom_target_value_high")
    if target_type in (TARGET_OPEN, None) or low is None or high is None:
        target, low, high = "open", None, None
    elif target_type == TARGET_SPEED:
        target, low, high = "speed", low / 1000, high / 1000
    elif target_type == TARGET_HEART_RATE:
        target, low, high = "heart_rate", _remove_offset(low, HEART_RATE_OFFSET), _remove_offset(high, HEART_RATE_OFFSET)
    elif target_type == TARGET_POWER:
        target, low, high = "power", _remove_offset(low, POWER_OFFSET), _remove_offset(high, POWER_OFFSET)
    else:
        raise ValueError(f"Nicht unterstütztes Ziel {target_type}")

    return FitStep(_raw(msg, "wkt_step_name"), _raw(msg, "notes"), intensity, duration, value, target, low, high)


def _remove_offset(value: int, offset: int) -> int:
    if value < offset:
        raise ValueError("Zielwert ohne FIT-Offset (Zone oder Prozent) wird nicht unterstützt")
    return value - offset


def _build_items(step_messages: list[WorkoutStepMessage]) -> tuple[FitStep | FitRepeat, ...]:
    items: list[tuple[int, FitStep | FitRepeat]] = []
    for position, msg in enumerate(step_messages):
        if _raw(msg, "duration_type") != DURATION_REPEAT:
            items.append((position, _parse_step(msg)))
            continue
        start, iterations = _raw(msg, "duration_value"), _raw(msg, "target_value")
        if start is None or not iterations or iterations < 1:
            raise ValueError("Ungültiger Wiederholungsschritt")
        grouped = [(p, i) for p, i in items if p >= start]
        if not grouped or any(isinstance(i, FitRepeat) for _, i in grouped):
            raise ValueError("Wiederholung ohne Schritte oder verschachtelt")
        items = items[: len(items) - len(grouped)]
        items.append((grouped[0][0], FitRepeat(tuple(i for _, i in grouped), iterations)))
    return tuple(item for _, item in items)


def parse_workout(fit_bytes: bytes) -> FitWorkout | None:
    """Parse one workout FIT file; None for an unsupported sport. Raises on a broken file."""
    file_ids = _messages(fit_bytes, FileIdMessage)
    workouts = _messages(fit_bytes, WorkoutMessage)
    if not file_ids or not workouts:
        raise ValueError("Keine Workout-FIT-Datei")
    serial = _raw(file_ids[0], "serial_number")
    name = _raw(workouts[0], "wkt_name") or ""
    sport = SPORTS.get(_raw(workouts[0], "sport"))
    if sport is None:
        logger.warning("Workout '%s' übersprungen: Sportart %s wird nicht unterstützt", name, _raw(workouts[0], "sport"))
        return None
    if serial is None:
        raise ValueError("Workout ohne Seriennummer")
    items = _build_items(_messages(fit_bytes, WorkoutStepMessage))
    return FitWorkout(serial, name, sport, items)


def parse_schedule(fit_bytes: bytes) -> dict[int, date]:
    """Map workout serial number to its scheduled day (FIT local time read as UTC date)."""
    schedule = {}
    for msg in _messages(fit_bytes, ScheduleMessage):
        serial, scheduled = _raw(msg, "serial_number"), _raw(msg, "scheduled_time")
        if serial is not None and scheduled is not None:
            schedule[serial] = (FIT_LOCAL_EPOCH + timedelta(seconds=scheduled)).date()
    return schedule


@dataclass(frozen=True)
class BundleReport:
    planned: list[PlannedWorkout]
    unreadable: set[int]  # serials open-tri plans whose workout could not be delivered


def _file_serial(fit_bytes: bytes) -> int | None:
    """The file_id serial of a workout file, None when even that cannot be read."""
    try:
        file_ids = _messages(fit_bytes, FileIdMessage)
        return _raw(file_ids[0], "serial_number") if file_ids else None
    except Exception:
        return None


def read_bundle_report(zip_bytes: bytes) -> BundleReport:
    """Pair every workout with its date from Schedules.fit and name the serials that could not be read.

    Unsupported sports are left out on purpose and are not reported as unreadable.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise WorkoutBundleError("Export ist kein gültiges ZIP") from e
    with zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".fit")]
        schedule_names = [n for n in names if n.rsplit("/", 1)[-1].lower() == SCHEDULES_NAME]
        if not schedule_names:
            raise WorkoutBundleError("Export enthält keine Schedules.fit")
        try:
            schedule = parse_schedule(zf.read(schedule_names[0]))
        except Exception as e:
            raise WorkoutBundleError("Schedules.fit im Export ist nicht lesbar") from e

        planned: list[PlannedWorkout] = []
        unreadable: set[int] = set()
        accounted: set[int] = set()  # delivered or intentionally left out
        for name in names:
            if name in schedule_names:
                continue
            try:
                fit_bytes = zf.read(name)
            except Exception:  # broken member (CRC, zlib): its serial stays in the scheduled-but-unaccounted set
                logger.warning("Workout-Datei %s im Export beschädigt, übersprungen", name, exc_info=True)
                continue
            serial = _file_serial(fit_bytes)
            try:
                workout = parse_workout(fit_bytes)
            except Exception:
                logger.warning("Workout-Datei %s nicht lesbar, übersprungen", name, exc_info=True)
                if serial is not None:
                    unreadable.add(serial)
                continue
            if workout is None:
                if serial is not None:
                    accounted.add(serial)
                continue
            day = schedule.get(workout.serial)
            if day is None:
                logger.warning("Workout '%s' hat keinen Termin in Schedules.fit, übersprungen", workout.name)
                unreadable.add(workout.serial)
                continue
            planned.append(PlannedWorkout(workout, day))
            accounted.add(workout.serial)
        # a scheduled serial without a usable file (e.g. a file too broken to name its serial)
        unreadable |= set(schedule) - accounted
        unreadable -= {p.workout.serial for p in planned}
    return BundleReport(planned, unreadable)


def read_bundle(zip_bytes: bytes) -> list[PlannedWorkout]:
    """Pair every workout in the export with its date from Schedules.fit; skip what cannot be paired."""
    return read_bundle_report(zip_bytes).planned
