"""Rewrite the recording device of a MyWhoosh FIT file to a Garmin Edge 1050."""

from fit_tool.definition_message import DefinitionMessage
from fit_tool.fit_file import FitFile
from fit_tool.profile.messages.device_info_message import DeviceInfoMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.record import Record

GARMIN_MANUFACTURER = 1
EDGE_1050_PRODUCT = 4440
DEFAULT_SOFTWARE_VERSION = 29.19

_FIT_SIGNATURE = b".FIT"
_SIGNATURE_OFFSET = 8
_MAX_LOCAL_IDS = 16


class FitPatchError(ValueError):
    """Raised when a FIT file cannot be parsed or patched."""


def patch_device(fit_bytes: bytes, software_version: float = DEFAULT_SOFTWARE_VERSION) -> bytes:
    """Return the FIT file with its creator device set to a Garmin Edge 1050."""
    if fit_bytes[_SIGNATURE_OFFSET : _SIGNATURE_OFFSET + 4] != _FIT_SIGNATURE:
        raise FitPatchError("Not a FIT file: missing .FIT header signature")
    try:
        return _patch(fit_bytes, software_version)
    except FitPatchError:
        raise
    except Exception as exc:
        raise FitPatchError(f"Could not patch FIT file: {exc}") from exc


def _patch(fit_bytes: bytes, software_version: float) -> bytes:
    fit = FitFile.from_bytes(fit_bytes)
    records = fit.records

    file_id_index = next((i for i, r in enumerate(records) if isinstance(r.message, FileIdMessage)), None)
    if file_id_index is None:
        raise FitPatchError("FIT file has no file_id message")
    file_id = records[file_id_index].message
    file_id.manufacturer = GARMIN_MANUFACTURER
    file_id.product = EDGE_1050_PRODUCT

    existing = next(
        (
            r.message
            for r in records
            if isinstance(r.message, DeviceInfoMessage) and r.message.device_index == 0
        ),
        None,
    )
    if existing is not None:
        _set_device(existing, file_id.serial_number, software_version)
    else:
        info = DeviceInfoMessage(local_id=_free_local_id(records))
        info.timestamp = _start_time(records, file_id)
        info.device_index = 0
        _set_device(info, file_id.serial_number, software_version)
        records.insert(file_id_index + 1, Record.from_message(DefinitionMessage.from_data_message(info)))
        records.insert(file_id_index + 2, Record.from_message(info))

    fit.mark_dirty()
    return fit.to_bytes()


def _set_device(info: DeviceInfoMessage, serial_number: int | None, software_version: float) -> None:
    info.manufacturer = GARMIN_MANUFACTURER
    info.product = EDGE_1050_PRODUCT
    info.serial_number = serial_number
    info.software_version = software_version


def _start_time(records: list[Record], file_id: FileIdMessage) -> int | None:
    for record in records:
        if isinstance(record.message, SessionMessage) and record.message.start_time is not None:
            return record.message.start_time
    return file_id.time_created


def _free_local_id(records: list[Record]) -> int:
    used = {r.message.local_id for r in records}
    for local_id in range(_MAX_LOCAL_IDS):
        if local_id not in used:
            return local_id
    raise FitPatchError("FIT file uses all local message ids")
