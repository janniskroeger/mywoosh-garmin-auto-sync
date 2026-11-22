import os
from pathlib import Path

import pytest
from fit_tool import FitFile
from fit_tool.profile.messages.device_info_message import DeviceInfoMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage

from fitfactory import make_mywhoosh_like_fit
from mywhoosh_garmin_sync.fit_patch import FitPatchError, patch_device


def data_messages(raw, skip=("DefinitionMessage", "DeviceInfoMessage")):
    out = []
    for rec in FitFile.from_bytes(raw).records:
        m = rec.message
        if type(m).__name__ in skip:
            continue
        fields = tuple(sorted((f.name, str(f.get_values())) for f in m.fields if f.is_valid()))
        dev = (
            tuple(sorted((d.name, str(d.get_value())) for d in (m.developer_fields or []) if d.is_valid()))
            if hasattr(m, "developer_fields")
            else ()
        )
        out.append((type(m).__name__, fields, dev))
    return out


def messages_of(raw, cls):
    return [r.message for r in FitFile.from_bytes(raw).records if isinstance(r.message, cls)]


def test_sets_garmin_edge_1050_in_file_id():
    fid = messages_of(patch_device(make_mywhoosh_like_fit()), FileIdMessage)[0]
    assert (fid.manufacturer, fid.product, fid.serial_number) == (1, 4440, 3313379353)


def test_adds_exactly_one_creator_device_info_after_file_id():
    raw = patch_device(make_mywhoosh_like_fit())
    infos = messages_of(raw, DeviceInfoMessage)
    assert len(infos) == 1
    d = infos[0]
    assert (d.device_index, d.manufacturer, d.product, d.serial_number) == (0, 1, 4440, 3313379353)
    assert d.timestamp == 1793383209000
    names = [
        type(r.message).__name__
        for r in FitFile.from_bytes(raw).records
        if type(r.message).__name__ != "DefinitionMessage"
    ]
    assert names[:2] == ["FileIdMessage", "DeviceInfoMessage"]


def test_all_other_messages_unchanged():
    orig = make_mywhoosh_like_fit()
    before, after = data_messages(orig), data_messages(patch_device(orig))
    assert len(before) == len(after)
    diffs = [b[0] for b, a in zip(before, after) if b != a]
    assert diffs == ["FileIdMessage"]


def test_idempotent():
    once = patch_device(make_mywhoosh_like_fit())
    twice = patch_device(once)
    assert len(messages_of(twice, DeviceInfoMessage)) == 1
    assert data_messages(once, skip=("DefinitionMessage",)) == data_messages(twice, skip=("DefinitionMessage",))


def test_no_new_validation_errors():
    orig = make_mywhoosh_like_fit()
    patched_errors = len(FitFile.from_bytes(patch_device(orig)).validate().errors)
    assert patched_errors <= len(FitFile.from_bytes(orig).validate().errors)


@pytest.mark.parametrize("raw", [b"", b"not a fit file at all", b"\x0e\x10" + b"\x00" * 30])
def test_rejects_non_fit(raw):
    with pytest.raises(FitPatchError):
        patch_device(raw)


REAL = Path(os.environ.get("MYWHOOSH_SAMPLE_FIT", "tests/fixtures/local/mywhoosh_sample.fit"))


@pytest.mark.skipif(not REAL.exists(), reason="no local MyWhoosh sample file")
def test_real_file_only_file_id_changes_and_dev_fields_kept():
    orig = REAL.read_bytes()
    before, after = data_messages(orig), data_messages(patch_device(orig))
    assert [b[0] for b, a in zip(before, after) if b != a] == ["FileIdMessage"]
    assert any(m[2] for m in after)  # MyWhoosh developer fields of the session are still present
