"""Converts a parsed open-tri workout into the JSON dict Garmin Connect accepts.

The result is a plain dict (no pydantic) shaped like the workouts Garmin accepted in the
probe (docs/superpowers/specs/2026-11-07-workout-probe-results.md). IDs and keys come from
``garminconnect.workout``.
"""

import hashlib
import json
from typing import Any

from garminconnect.workout import ConditionType, SportType, StepType, TargetType

from mywhoosh_garmin_sync.workout_fit import FitRepeat, FitStep, FitWorkout

MARKER_PREFIX = "open-tri:"

SPORT_TYPES = {
    "running": {"sportTypeId": SportType.RUNNING, "sportTypeKey": "running", "displayOrder": 1},
    "cycling": {"sportTypeId": SportType.CYCLING, "sportTypeKey": "cycling", "displayOrder": 2},
    "swimming": {"sportTypeId": SportType.SWIMMING, "sportTypeKey": "swimming", "displayOrder": 3},
}

# intensity -> (stepTypeId, stepTypeKey, displayOrder)
STEP_TYPES = {
    "warmup": (StepType.WARMUP, "warmup", 1),
    "cooldown": (StepType.COOLDOWN, "cooldown", 2),
    "active": (StepType.INTERVAL, "interval", 3),
    "recovery": (StepType.RECOVERY, "recovery", 4),
    "rest": (StepType.REST, "rest", 5),
}

LAP_BUTTON = {"conditionTypeId": ConditionType.LAP_BUTTON, "conditionTypeKey": "lap.button", "displayOrder": 1, "displayable": True}
TIME = {"conditionTypeId": ConditionType.TIME, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True}
DISTANCE = {"conditionTypeId": ConditionType.DISTANCE, "conditionTypeKey": "distance", "displayOrder": 3, "displayable": True}
ITERATIONS = {"conditionTypeId": ConditionType.ITERATIONS, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False}
REPEAT_STEP_TYPE = {"stepTypeId": StepType.REPEAT, "stepTypeKey": "repeat", "displayOrder": 6}

NO_TARGET = {"workoutTargetTypeId": TargetType.NO_TARGET, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
POOL_LENGTH_UNIT = {"unitId": 1, "unitKey": "meter", "factor": 100.0}


def marker(serial: int) -> str:
    return f"{MARKER_PREFIX}{serial}"


def serial_from_description(description: str | None) -> int | None:
    """The open-tri serial from a workout description, None when it is not ours."""
    if not description or not description.startswith(MARKER_PREFIX):
        return None
    digits = description[len(MARKER_PREFIX):]
    return int(digits) if digits.isdigit() else None


def _target(step: FitStep, sport: str) -> dict[str, Any]:
    """targetType plus the two target values (empty values for no target)."""
    if step.target == "open" or step.target_low is None or step.target_high is None:
        return {"targetType": dict(NO_TARGET)}
    if step.target == "heart_rate":
        type_id, key = TargetType.HEART_RATE_ZONE, "heart.rate.zone"
    elif step.target == "power":
        type_id, key = TargetType.POWER_ZONE, "power.zone"
    elif sport == "cycling":
        type_id, key = TargetType.SPEED_ZONE, "speed.zone"
    else:  # running and swimming show speed as pace; the slower pace is the lower m/s
        type_id, key = TargetType.PACE_ZONE, "pace.zone"
    low, high = sorted((step.target_low, step.target_high))
    return {
        "targetType": {"workoutTargetTypeId": type_id, "workoutTargetTypeKey": key, "displayOrder": 1},
        "targetValueOne": low,
        "targetValueTwo": high,
    }


def _description(step: FitStep) -> str | None:
    """Notes hold the full text open-tri cut the name from, so they win when they differ."""
    if step.notes and step.notes != step.name:
        return step.notes
    return step.name


def _step_json(step: FitStep, order: int, sport: str) -> dict[str, Any]:
    type_id, type_key, display_order = STEP_TYPES[step.intensity]
    result: dict[str, Any] = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": {"stepTypeId": type_id, "stepTypeKey": type_key, "displayOrder": display_order},
    }
    if step.duration == "time" and step.duration_value is not None:
        result["endCondition"] = dict(TIME)
        result["endConditionValue"] = step.duration_value
    elif step.duration == "distance" and step.duration_value is not None:
        result["endCondition"] = dict(DISTANCE)
        result["endConditionValue"] = step.duration_value
    else:
        result["endCondition"] = dict(LAP_BUTTON)
        result["endConditionValue"] = None
    result.update(_target(step, sport))
    description = _description(step)
    if description is not None:
        result["description"] = description
    return result


def _items_json(items: tuple[FitStep | FitRepeat, ...], start: int, sport: str) -> tuple[list[dict[str, Any]], int]:
    """Convert items with a global stepOrder; returns the steps and the next free order."""
    steps: list[dict[str, Any]] = []
    order = start
    for item in items:
        if isinstance(item, FitRepeat):
            group_order = order
            children, order = _items_json(item.steps, order + 1, sport)
            steps.append({
                "type": "RepeatGroupDTO",
                "stepOrder": group_order,
                "stepType": dict(REPEAT_STEP_TYPE),
                "numberOfIterations": item.iterations,
                "workoutSteps": children,
                "endCondition": dict(ITERATIONS),
                "endConditionValue": float(item.iterations),
                "smartRepeat": False,
            })
        else:
            steps.append(_step_json(item, order, sport))
            order += 1
    return steps, order


def _estimated_seconds(items: tuple[FitStep | FitRepeat, ...]) -> int:
    """Sum of time steps times iterations; distance and lap steps count 0."""
    total = 0.0
    for item in items:
        if isinstance(item, FitRepeat):
            total += item.iterations * _estimated_seconds(item.steps)
        elif item.duration == "time" and item.duration_value is not None:
            total += item.duration_value
    return round(total)


def to_garmin_json(workout: FitWorkout, pool_length_m: float) -> dict[str, Any]:
    """Garmin workout JSON without IDs; the description carries the open-tri marker."""
    sport_type = SPORT_TYPES[workout.sport]
    steps, _ = _items_json(workout.items, 1, workout.sport)
    result: dict[str, Any] = {
        "workoutName": workout.name,
        "description": marker(workout.serial),
        "sportType": dict(sport_type),
        "estimatedDurationInSecs": _estimated_seconds(workout.items),
        "workoutSegments": [{"segmentOrder": 1, "sportType": dict(sport_type), "workoutSteps": steps}],
    }
    if workout.sport == "swimming":
        result["poolLength"] = float(pool_length_m)
        result["poolLengthUnit"] = dict(POOL_LENGTH_UNIT)
    return result


def content_hash(workout_json: dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON without the top-level description (the marker)."""
    content = {key: value for key, value in workout_json.items() if key != "description"}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
