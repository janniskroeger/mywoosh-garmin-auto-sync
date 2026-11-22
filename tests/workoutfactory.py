"""Builds synthetic open-tri workout exports that decode like the real files.

open-tri writes the FIT SDK main fields with raw values and no resolved subfields
(duration in ms or cm, speed in mm/s, heart rate + 100, power + 1000). This factory
takes friendly values (seconds, meters, m/s, bpm, W) and writes the raw form.
"""

import io
import zipfile
from datetime import date, datetime

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.schedule_message import ScheduleMessage
from fit_tool.profile.messages.workout_message import WorkoutMessage
from fit_tool.profile.messages.workout_step_message import WorkoutStepMessage

CREATED_MS = 1794067121000
FIT_LOCAL_EPOCH = datetime(1989, 12, 31)

SPORTS = {"running": 1, "cycling": 2, "swimming": 5, "training": 10}
INTENSITIES = {"active": 0, "rest": 1, "warmup": 2, "cooldown": 3, "recovery": 4}
DURATIONS = {"time": 0, "distance": 1, "open": 5, "repeat_until_steps_cmplt": 6}
TARGETS = {"speed": 0, "heart_rate": 1, "open": 2, "power": 4}


def _set_raw(message, name: str, value) -> None:
    message.get_field_by_name(name).set_value(0, value)


def _step_message(index: int, step: dict) -> WorkoutStepMessage:
    """Translate a friendly step dict into a WorkoutStepMessage with raw fields.

    Keys: name, notes, intensity, duration_type, duration_time (s), duration_distance (m),
    duration_step (repeat start index), repeat_count, target_type, target_low, target_high
    (m/s, bpm or W).
    """
    msg = WorkoutStepMessage()
    _set_raw(msg, "message_index", index)
    if step.get("name") is not None:
        _set_raw(msg, "wkt_step_name", step["name"])
    if step.get("notes") is not None:
        _set_raw(msg, "notes", step["notes"])
    if "intensity" in step:
        _set_raw(msg, "intensity", INTENSITIES[step["intensity"]])
    duration_type = step["duration_type"]
    _set_raw(msg, "duration_type", DURATIONS[duration_type])
    if duration_type == "time":
        _set_raw(msg, "duration_value", round(step["duration_time"] * 1000))
    elif duration_type == "distance":
        _set_raw(msg, "duration_value", round(step["duration_distance"] * 100))
    elif duration_type == "repeat_until_steps_cmplt":
        _set_raw(msg, "duration_value", step["duration_step"])
        _set_raw(msg, "target_value", step["repeat_count"])
    target_type = step.get("target_type", "open")
    _set_raw(msg, "target_type", TARGETS[target_type])
    if target_type != "open":
        scale, offset = {"speed": (1000, 0), "heart_rate": (1, 100), "power": (1, 1000)}[target_type]
        _set_raw(msg, "target_value", 0)
        _set_raw(msg, "custom_target_value_low", round(step["target_low"] * scale) + offset)
        _set_raw(msg, "custom_target_value_high", round(step["target_high"] * scale) + offset)
    return msg


def make_workout_fit(serial: int, name: str, sport: str, steps: list[dict]) -> bytes:
    builder = FitFileBuilder(auto_define=True)

    file_id = FileIdMessage()
    _set_raw(file_id, "type", 5)
    _set_raw(file_id, "manufacturer", 255)
    _set_raw(file_id, "product", 0)
    _set_raw(file_id, "serial_number", serial)
    _set_raw(file_id, "time_created", CREATED_MS)
    builder.add(file_id)

    workout = WorkoutMessage()
    _set_raw(workout, "sport", SPORTS[sport])
    _set_raw(workout, "num_valid_steps", len(steps))
    _set_raw(workout, "wkt_name", name)
    builder.add(workout)

    for index, step in enumerate(steps):
        builder.add(_step_message(index, step))
    return builder.build().to_bytes()


def make_schedule_fit(entries: dict[int, date]) -> bytes:
    builder = FitFileBuilder(auto_define=True)

    file_id = FileIdMessage()
    _set_raw(file_id, "type", 7)
    _set_raw(file_id, "manufacturer", 255)
    _set_raw(file_id, "product", 0)
    _set_raw(file_id, "serial_number", 2314175784)
    _set_raw(file_id, "time_created", CREATED_MS)
    builder.add(file_id)

    for serial, day in entries.items():
        entry = ScheduleMessage()
        _set_raw(entry, "manufacturer", 255)
        _set_raw(entry, "product", 0)
        _set_raw(entry, "serial_number", serial)
        _set_raw(entry, "time_created", CREATED_MS)
        _set_raw(entry, "completed", 0)
        _set_raw(entry, "type", 0)
        _set_raw(entry, "scheduled_time", int((datetime(day.year, day.month, day.day) - FIT_LOCAL_EPOCH).total_seconds()))
        builder.add(entry)
    return builder.build().to_bytes()


def make_bundle(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()
