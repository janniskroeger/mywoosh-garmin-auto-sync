from datetime import date
from pathlib import Path

import pytest

from mywhoosh_garmin_sync.mywhoosh_workout_json import (
    MAX_NAME_LENGTH,
    ZwoBlock,
    ZwoError,
    content_hash,
    marker,
    parse_zwo,
    planned_id_from_description,
    to_mywhoosh_json,
    workout_id_for,
    workout_name,
)
from zwofactory import make_zwo


def test_parse_steady_ramp_free_with_cadence():
    w = parse_zwo(make_zwo("Grundlage", [
        '<SteadyState Duration="600" Power="0.650" Cadence="90"/>',
        '<Ramp Duration="300" PowerLow="0.800" PowerHigh="0.500"/>',
        '<FreeRide Duration="120"/>']))
    assert w.name == "Grundlage"
    assert w.blocks == (
        ZwoBlock("steady", 600, 0.65, None, None, 90),
        ZwoBlock("ramp", 300, None, 0.8, 0.5, None),
        ZwoBlock("free", 120, None, None, None, None))


@pytest.mark.parametrize("blocks", [[], ['<IntervalsT Repeat="3" OnDuration="60" OffDuration="60"/>'],
                                    ['<SteadyState Power="0.6"/>']])
def test_parse_rejects_unknown_empty_or_incomplete(blocks):
    with pytest.raises(ZwoError):
        parse_zwo(make_zwo("X", blocks))


def test_parse_rejects_non_xml():
    with pytest.raises(ZwoError):
        parse_zwo(b"<html>proxy</html")


def test_parse_rejects_missing_name():
    data = b'<workout_file><workout><FreeRide Duration="60"/></workout></workout_file>'
    with pytest.raises(ZwoError):
        parse_zwo(data)


def test_workout_name_plain_and_brick():
    assert workout_name(date(2026, 11, 9), 3, None, "Grundlage") == "Mo 09.11. W3: Grundlage"
    assert workout_name(date(2026, 11, 15), 4, 1, "Rad") == "So 15.11. W4: 2. Rad"


def test_workout_name_cuts_training_name_only():
    name = workout_name(date(2026, 11, 9), 3, None, "Ü" * (MAX_NAME_LENGTH + 50))
    assert len(name) == MAX_NAME_LENGTH
    assert name.startswith("Mo 09.11. W3: ÜÜ")


def test_workout_name_cut_on_space_has_no_trailing_whitespace():
    prefix = "Mo 09.11. W3: "
    cut = MAX_NAME_LENGTH - len(prefix)
    training = "A" * (cut - 1) + " " + "B" * 20  # the cut point lands right after the space
    name = workout_name(date(2026, 11, 9), 3, None, training)
    assert name == prefix + "A" * (cut - 1)
    assert len(name) <= MAX_NAME_LENGTH


@pytest.mark.parametrize("block", [
    '<SteadyState Duration="600" Power="nan"/>',
    '<SteadyState Duration="600" Power="inf"/>',
    '<SteadyState Duration="600" Power="-inf"/>',
    '<Ramp Duration="300" PowerLow="nan" PowerHigh="0.8"/>',
    '<Ramp Duration="300" PowerLow="0.5" PowerHigh="inf"/>',
])
def test_parse_rejects_non_finite_power(block):
    with pytest.raises(ZwoError):
        parse_zwo(make_zwo("X", [block]))


@pytest.mark.parametrize("block", [
    '<SteadyState Duration="0" Power="0.6"/>',
    '<SteadyState Duration="-60" Power="0.6"/>',
    '<Ramp Duration="0" PowerLow="0.5" PowerHigh="0.8"/>',
    '<FreeRide Duration="0"/>',
])
def test_parse_rejects_non_positive_duration(block):
    with pytest.raises(ZwoError):
        parse_zwo(make_zwo("X", [block]))


def test_json_steps_and_totals():
    w = parse_zwo(make_zwo("G", ['<SteadyState Duration="600" Power="0.650" Cadence="90"/>',
                                 '<Ramp Duration="300" PowerLow="0.500" PowerHigh="0.800"/>']))
    j = to_mywhoosh_json(w, "Mo 09.11. W3: G", "abc-uuid")
    assert j["Name"] == "Mo 09.11. W3: G"
    assert j["Description"] == "open-tri:abc-uuid"
    assert j["Time"] == 900 and j["StepCount"] == 2
    s1, s2 = j["WorkoutStepsArray"]
    assert (s1["Id"], s1["Power"], s1["Time"], s1["Rpm"]) == (1, 0.65, 600, 90)
    assert (s2["Id"], s2["StartPower"], s2["EndPower"], s2["Power"]) == (2, 0.5, 0.8, 0)
    assert (s1["StepType"], s2["StepType"]) == ("E_Normal", "E_WarmUp")
    assert j["Id"] == workout_id_for("abc-uuid")


def test_json_down_ramp_and_free_ride():
    w = parse_zwo(make_zwo("G", ['<Ramp Duration="300" PowerLow="0.800" PowerHigh="0.500"/>',
                                 '<FreeRide Duration="120"/>']))
    s1, s2 = to_mywhoosh_json(w, "n", "p")["WorkoutStepsArray"]
    assert (s1["StepType"], s1["StartPower"], s1["EndPower"]) == ("E_CoolDown", 0.8, 0.5)
    assert (s2["StepType"], s2["Power"], s2["StartPower"], s2["EndPower"]) == ("E_FreeRide", 0, 0, 0)


def test_json_matches_editor_import_fields():
    # tests/fixtures/zwo/probe.zwo → same keys and fixed values as the editor body in the probe results
    path = Path(__file__).parent / "fixtures" / "zwo" / "probe.zwo"
    j = to_mywhoosh_json(parse_zwo(path.read_bytes()), "n", "p")
    assert (j["Mode"], j["Type"], j["CategoryId"], j["SubcategoryId"], j["WokoutAssociationId"]) == \
        ("E_Ride", "E_Normal", 200000000, 1, 0)
    assert j["IF"] == pytest.approx(0.6467, abs=1e-3)
    assert j["TSS"] == pytest.approx(11.85, abs=0.01)
    assert j["KJ"] == 0


def test_json_without_power_steps_has_zero_if_and_tss():
    j = to_mywhoosh_json(parse_zwo(make_zwo("G", ['<FreeRide Duration="120"/>'])), "n", "p")
    assert (j["IF"], j["TSS"], j["Time"]) == (0, 0, 120)


def test_workout_id_is_stable_and_in_range():
    assert workout_id_for("abc") == workout_id_for("abc") != workout_id_for("abd")
    assert 100_000_000 <= workout_id_for("abc") <= 999_999_999


def test_marker_roundtrip_and_foreign_descriptions():
    assert planned_id_from_description(marker("abc")) == "abc"
    for other in (None, "", "Locker!", "open-tri:", 5):
        assert planned_id_from_description(other) is None


def test_hash_ignores_description_but_not_name():
    w = parse_zwo(make_zwo("G", ['<SteadyState Duration="600" Power="0.650"/>']))
    a = to_mywhoosh_json(w, "Mo 09.11. W3: G", "a")
    assert content_hash(a) == content_hash(to_mywhoosh_json(w, "Mo 09.11. W3: G", "b"))
    assert content_hash(a) != content_hash(to_mywhoosh_json(w, "Di 10.11. W3: G", "a"))
