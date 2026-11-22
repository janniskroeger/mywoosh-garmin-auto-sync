from mywhoosh_garmin_sync.workout_fit import FitRepeat, FitStep, FitWorkout
from mywhoosh_garmin_sync.workout_json import content_hash, marker, serial_from_description, to_garmin_json


def step(name="Schritt", notes=None, intensity="active", duration="time", duration_value=60.0,
         target="open", target_low=None, target_high=None):
    return FitStep(name, notes, intensity, duration, duration_value, target, target_low, target_high)


def run_workout(serial=42):
    return FitWorkout(serial, "W3: Grundlage", "running", (
        step("Warm-up", intensity="warmup", duration_value=600, target="heart_rate", target_low=120, target_high=140),
        FitRepeat((step("Hart", duration_value=180), step("Locker", intensity="recovery", duration_value=60)), 3),
        step("Cool-down", intensity="cooldown", duration_value=300),
    ))


def steps_of(workout, pool=25):
    return to_garmin_json(workout, pool_length_m=pool)["workoutSegments"][0]["workoutSteps"]


def single(workout_step, sport="running"):
    return steps_of(FitWorkout(1, "T", sport, (workout_step,)))[0]


def test_running_workout_shape():
    j = to_garmin_json(run_workout(serial=42), pool_length_m=25)
    assert j["workoutName"] == "W3: Grundlage" and j["description"] == "open-tri:42"
    assert j["sportType"]["sportTypeId"] == 1
    steps = j["workoutSegments"][0]["workoutSteps"]
    assert steps[0]["stepType"]["stepTypeKey"] == "warmup"
    assert steps[0]["endCondition"]["conditionTypeKey"] == "time" and steps[0]["endConditionValue"] == 600
    assert steps[0]["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert (steps[0]["targetValueOne"], steps[0]["targetValueTwo"]) == (120, 140) and "zoneNumber" not in steps[0]
    assert steps[1]["type"] == "RepeatGroupDTO" and steps[1]["numberOfIterations"] == 3
    assert steps[1]["stepType"]["stepTypeId"] == 6
    assert steps[1]["endCondition"]["conditionTypeId"] == 7 and steps[1]["endConditionValue"] == 3
    assert steps[1]["smartRepeat"] is False
    assert "poolLength" not in j


def test_step_order_is_global_and_increasing():
    steps = steps_of(run_workout())
    repeat = steps[1]
    orders = [steps[0]["stepOrder"], repeat["stepOrder"],
              *(s["stepOrder"] for s in repeat["workoutSteps"]), steps[2]["stepOrder"]]
    assert orders == [1, 2, 3, 4, 5]


def test_intensity_mapping():
    items = tuple(step(intensity=i) for i in ("warmup", "active", "recovery", "cooldown", "rest"))
    steps = steps_of(FitWorkout(1, "T", "running", items))
    assert [s["stepType"]["stepTypeKey"] for s in steps] == ["warmup", "interval", "recovery", "cooldown", "rest"]
    assert [s["stepType"]["stepTypeId"] for s in steps] == [1, 3, 4, 2, 5]


def test_open_duration_is_lap_button():
    s = single(step(duration="open", duration_value=None))
    assert s["endCondition"]["conditionTypeKey"] == "lap.button" and s["endCondition"]["conditionTypeId"] == 1
    assert s["endConditionValue"] is None


def test_distance_in_meters():
    s = single(step(duration="distance", duration_value=400))
    assert s["endCondition"]["conditionTypeKey"] == "distance" and s["endConditionValue"] == 400


def test_power_target_watts():
    s = single(step(target="power", target_low=180, target_high=200), "cycling")
    assert s["targetType"]["workoutTargetTypeKey"] == "power.zone" and s["targetType"]["workoutTargetTypeId"] == 2
    assert (s["targetValueOne"], s["targetValueTwo"]) == (180, 200) and "zoneNumber" not in s


def test_run_speed_becomes_pace_target():
    s = single(step(target="speed", target_low=2.5, target_high=3.0))
    assert s["targetType"]["workoutTargetTypeKey"] == "pace.zone" and s["targetType"]["workoutTargetTypeId"] == 6
    assert (s["targetValueOne"], s["targetValueTwo"]) == (2.5, 3.0)


def test_swim_speed_becomes_pace_target():
    s = single(step(target="speed", target_low=0.85, target_high=0.883), "swimming")
    assert s["targetType"]["workoutTargetTypeKey"] == "pace.zone"


def test_bike_speed_becomes_speed_target():
    s = single(step(target="speed", target_low=7.0, target_high=8.0), "cycling")
    assert s["targetType"]["workoutTargetTypeKey"] == "speed.zone" and s["targetType"]["workoutTargetTypeId"] == 5


def test_open_target_is_no_target():
    s = single(step())
    assert s["targetType"]["workoutTargetTypeKey"] == "no.target" and s["targetType"]["workoutTargetTypeId"] == 1
    assert "targetValueOne" not in s and "targetValueTwo" not in s


def test_swim_pool_length():
    j = to_garmin_json(FitWorkout(1, "CSS", "swimming", (step(),)), pool_length_m=25)
    assert j["sportType"]["sportTypeId"] == 4 and j["poolLength"] == 25
    assert j["poolLengthUnit"] == {"unitId": 1, "unitKey": "meter", "factor": 100.0}
    assert "poolLength" not in j["workoutSegments"][0]


def test_step_description_is_name_or_notes():
    assert single(step(name="Locker", notes=None))["description"] == "Locker"
    assert single(step(name="Locker", notes="Locker laufen, Puls unten halten"))["description"] == "Locker laufen, Puls unten halten"
    assert single(step(name="Locker", notes="Locker"))["description"] == "Locker"
    assert single(step(name=None, notes="Nur Notiz"))["description"] == "Nur Notiz"
    assert "description" not in single(step(name=None, notes=None))


def test_estimated_duration_counts_time_steps_times_iterations():
    items = (step(duration_value=600), FitRepeat((step(duration_value=60), step(duration="distance", duration_value=400),
                                                  step(duration="open", duration_value=None)), 3))
    j = to_garmin_json(FitWorkout(1, "T", "running", items), 25)
    assert j["estimatedDurationInSecs"] == 600 + 3 * 60


def test_hash_ignores_marker():
    a = to_garmin_json(run_workout(serial=42), 25)
    b = dict(a, description="open-tri:99")
    assert content_hash(a) == content_hash(b)


def test_hash_ignores_key_order():
    a = to_garmin_json(run_workout(), 25)
    b = dict(reversed(list(a.items())))
    assert content_hash(a) == content_hash(b)


def test_hash_changes_with_content():
    a = to_garmin_json(run_workout(), 25)
    b = to_garmin_json(FitWorkout(42, "W3: Grundlage", "running", (step(duration_value=601),)), 25)
    assert content_hash(a) != content_hash(b)
    assert content_hash(a) != content_hash(dict(a, workoutName="Anders"))


def test_serial_from_description():
    assert serial_from_description("open-tri:42") == 42
    assert serial_from_description("Mein Training") is None and serial_from_description(None) is None
    assert serial_from_description("open-tri:abc") is None and serial_from_description("open-tri:") is None
    assert marker(42) == "open-tri:42"
