"""Builds a synthetic FIT activity that resembles a MyWhoosh export."""

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import FileType, Sport, SubSport

START_MS = 1793383209000
MYWHOOSH_MANUFACTURER = 331
MYWHOOSH_PRODUCT = 3570
MYWHOOSH_SERIAL = 3313379353


def make_mywhoosh_like_fit(n_records: int = 60) -> bytes:
    """Return a FIT activity with one record per second and no DeviceInfo message."""
    builder = FitFileBuilder(auto_define=True)

    file_id = FileIdMessage()
    file_id.type = FileType.ACTIVITY
    file_id.manufacturer = MYWHOOSH_MANUFACTURER
    file_id.product = MYWHOOSH_PRODUCT
    file_id.serial_number = MYWHOOSH_SERIAL
    file_id.time_created = START_MS
    builder.add(file_id)

    total_distance = 0.0
    for i in range(n_records):
        speed = 8.0 + (i % 5) * 0.1
        total_distance += speed
        record = RecordMessage()
        record.timestamp = START_MS + i * 1000
        record.heart_rate = 120 + i % 30
        record.cadence = 80 + i % 10
        record.power = 150 + i % 50
        record.speed = speed
        record.distance = total_distance
        record.position_lat = 47.0 + i * 1e-5
        record.position_long = 8.0 + i * 1e-5
        builder.add(record)

    elapsed_s = float(n_records)

    lap = LapMessage()
    lap.timestamp = START_MS + n_records * 1000
    lap.start_time = START_MS
    lap.total_elapsed_time = elapsed_s
    lap.total_distance = total_distance
    builder.add(lap)

    session = SessionMessage()
    session.timestamp = START_MS + n_records * 1000
    session.start_time = START_MS
    session.sport = Sport.CYCLING
    session.sub_sport = SubSport.VIRTUAL_ACTIVITY
    session.total_elapsed_time = elapsed_s
    session.total_distance = total_distance
    builder.add(session)

    activity = ActivityMessage()
    activity.timestamp = START_MS + n_records * 1000
    activity.total_timer_time = elapsed_s
    activity.num_sessions = 1
    builder.add(activity)

    return builder.build().to_bytes()
