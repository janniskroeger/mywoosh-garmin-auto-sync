"""Converts an open-tri Zwift workout (.zwo) into the workout JSON MyWhoosh accepts.

The fixed field values come from the probe of the real coaching API
(docs/superpowers/specs/2026-11-08-mywhoosh-workout-probe-results.md, "Für den Builder").
"""

import hashlib
import json
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

MAX_NAME_LENGTH = 100
MARKER_PREFIX = "open-tri:"

_WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")

_WORKOUT_DEFAULTS: dict[str, Any] = {
    "Mode": "E_Ride",
    "ERGMode": "E_OFF",
    "IsRecovery": False,
    "IsIntervals": False,
    "FTPMode": "E_NoFTP",
    "IsTT": False,
    "IsTSS": False,
    "IsIF": False,
    "FTPMultiplier": 1,
    "StressPoint": 0,
    "CustomTagDescription": "2",
    "CategoryId": 200000000,
    "SubcategoryId": 1,
    "Type": "E_Normal",
    "DisplayType": "E_byWatts",
    "IsFavorite": False,
    "CompletedCount": 0,
    "WorkoutSteps": [],
    "AuthorName": "mywhoosh",
    "WokoutAssociationId": 0,  # misspelled like this in the original
    "KJ": 0,  # needs the profile FTP, which we do not know
}

_STEP_DEFAULTS: dict[str, Any] = {
    "IntervalId": 0,
    "WorkoutMessage": [],
    "Pace": 0,
    "IsManualGrade": False,
    "ManualGradeValue": 0,
    "ShowAveragePower": True,
    "FlatRoad": 0,
}


class ZwoError(ValueError):
    """The .zwo is not valid XML or uses something we cannot convert."""


@dataclass(frozen=True)
class ZwoBlock:
    kind: Literal["steady", "ramp", "free"]
    seconds: int
    power: float | None  # steady
    start_power: float | None  # ramp
    end_power: float | None  # ramp
    cadence: int | None


@dataclass(frozen=True)
class ZwoWorkout:
    name: str
    blocks: tuple[ZwoBlock, ...]


def _number(element: ET.Element, attribute: str, convert: Any) -> Any:
    raw = element.get(attribute)
    if raw is None:
        raise ZwoError(f"<{element.tag}> misses {attribute}")
    try:
        return convert(raw)
    except ValueError as error:
        raise ZwoError(f"<{element.tag}> has an invalid {attribute}: {raw!r}") from error


def _power(element: ET.Element, attribute: str) -> float:
    value = _number(element, attribute, float)
    if not math.isfinite(value):
        raise ZwoError(f"<{element.tag}> has a non-finite {attribute}: {element.get(attribute)!r}")
    return value


def _cadence(element: ET.Element) -> int | None:
    return _number(element, "Cadence", int) if element.get("Cadence") is not None else None


def _block(element: ET.Element) -> ZwoBlock:
    seconds = _number(element, "Duration", int)
    if seconds <= 0:
        raise ZwoError(f"<{element.tag}> has a non-positive Duration: {seconds}")
    if element.tag == "SteadyState":
        return ZwoBlock("steady", seconds, _power(element, "Power"), None, None, _cadence(element))
    if element.tag == "Ramp":
        return ZwoBlock("ramp", seconds, None, _power(element, "PowerLow"),
                        _power(element, "PowerHigh"), _cadence(element))
    if element.tag == "FreeRide":
        return ZwoBlock("free", seconds, None, None, None, None)
    raise ZwoError(f"unsupported block <{element.tag}>")


def parse_zwo(data: bytes) -> ZwoWorkout:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise ZwoError(f"not valid XML: {error}") from error
    name = root.findtext("name")
    if not name or not name.strip():
        raise ZwoError("the workout has no <name>")
    workout = root.find("workout")
    blocks = tuple(_block(element) for element in workout) if workout is not None else ()
    if not blocks:
        raise ZwoError("the workout has no blocks")
    return ZwoWorkout(name=name.strip(), blocks=blocks)


def workout_name(day: date, week: int, brick_order: int | None, training_name: str) -> str:
    """'Mo 09.11. W3: Name' (bricks: 'So 15.11. W4: 2. Name'); only the training name is cut."""
    prefix = f"{_WEEKDAYS[day.weekday()]} {day:%d.%m.} W{week}: "
    if brick_order is not None:
        prefix += f"{brick_order + 1}. "
    return (prefix + training_name[: max(MAX_NAME_LENGTH - len(prefix), 0)].rstrip()).rstrip()


def workout_id_for(planned_id: str) -> int:
    digest = hashlib.sha256(planned_id.encode()).digest()
    return int.from_bytes(digest[:8], "big") % 900_000_000 + 100_000_000


def marker(planned_id: str) -> str:
    return f"{MARKER_PREFIX}{planned_id}"


def planned_id_from_description(description: object) -> str | None:
    """The open-tri planned id from a workout description, None when it is not ours."""
    if not isinstance(description, str) or not description.startswith(MARKER_PREFIX):
        return None
    return description[len(MARKER_PREFIX):] or None


def _step_json(block: ZwoBlock, step_id: int) -> dict[str, Any]:
    step: dict[str, Any] = {
        **_STEP_DEFAULTS,
        "Id": step_id,
        "Time": block.seconds,
        "Power": 0,
        "StartPower": 0,
        "EndPower": 0,
        "Rpm": 0,
    }
    if block.kind == "steady":
        step.update(StepType="E_Normal", Power=block.power, Rpm=block.cadence or 0)
    elif block.kind == "ramp":
        assert block.start_power is not None and block.end_power is not None
        up = block.end_power >= block.start_power
        step.update(StepType="E_WarmUp" if up else "E_CoolDown",
                    StartPower=block.start_power, EndPower=block.end_power, Rpm=block.cadence or 0)
    else:
        step["StepType"] = "E_FreeRide"
    return step


def _power_fourth(block: ZwoBlock) -> float | None:
    """Mean of power^4 over the block, None for free rides."""
    if block.kind == "steady" and block.power is not None:
        return block.power ** 4
    if block.kind == "ramp" and block.start_power is not None and block.end_power is not None:
        a, b = block.start_power, block.end_power
        if a == b:
            return a ** 4
        return (b ** 5 - a ** 5) / (5 * (b - a))
    return None


def _intensity(blocks: tuple[ZwoBlock, ...], total_seconds: int) -> tuple[float, float]:
    """(IF, TSS), unrounded; IF is the normalized-power style mean over all non-free blocks."""
    weighted = 0.0
    seconds = 0
    for block in blocks:
        mean = _power_fourth(block)
        if mean is not None:
            weighted += mean * block.seconds
            seconds += block.seconds
    if seconds == 0:
        return 0.0, 0.0
    intensity = (weighted / seconds) ** 0.25
    return intensity, total_seconds / 3600 * intensity ** 2 * 100


def to_mywhoosh_json(workout: ZwoWorkout, name: str, planned_id: str) -> dict[str, Any]:
    total = sum(block.seconds for block in workout.blocks)
    intensity, tss = _intensity(workout.blocks, total)
    return {
        **_WORKOUT_DEFAULTS,
        "Id": workout_id_for(planned_id),
        "Name": name,
        "Description": marker(planned_id),
        "Time": total,
        "StepCount": len(workout.blocks),
        "IF": round(intensity, 3),
        "TSS": round(tss, 2),
        "WorkoutStepsArray": [_step_json(block, number) for number, block in enumerate(workout.blocks, 1)],
    }


def content_hash(workout_json: dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON without the top-level Description (the marker) and Id (derived from the marker)."""
    content = {key: value for key, value in workout_json.items() if key not in ("Description", "Id")}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
