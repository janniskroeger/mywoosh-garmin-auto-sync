import base64
import json
import logging
import time
from pathlib import Path

import pytest
import requests
import responses

from mywhoosh_garmin_sync.mywhoosh import (
    MyWhooshAuthError,
    MyWhooshClient,
    MyWhooshConnectionError,
    MyWhooshUnavailableError,
)
from mywhoosh_garmin_sync.mywhoosh_workouts import (
    COACHING_URL,
    AppCoachingAuth,
    MyWhooshNoSlotsError,
    MyWhooshWorkoutError,
    MyWhooshWorkouts,
    RemoteWorkout,
)

LIST_URL = f"{COACHING_URL}/workout-builder/my-workouts"
CREDITS_URL = f"{COACHING_URL}/client/my-credits"
UPLOAD_URL = f"{COACHING_URL}/client/custom-workout-upload"
KEY = "D9436E508087E863"
TOKEN = "tok-secret-123"


def _b64url(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def jwt(**claims) -> str:
    return f"{_b64url({'alg': 'HS256'})}.{_b64url(claims)}.sig"


class FakeAuth:
    def __init__(self):
        self.invalidated = 0
        self.tokens = [TOKEN]

    def token(self) -> str:
        return self.tokens[min(self.invalidated, len(self.tokens) - 1)]

    def user_id(self) -> str:
        return "user-42"

    def invalidate(self) -> None:
        self.invalidated += 1


@pytest.fixture
def rsps():
    with responses.RequestsMock() as mock:
        yield mock


@pytest.fixture
def auth():
    return FakeAuth()


@pytest.fixture
def api(auth, rsps):
    return MyWhooshWorkouts(auth)


def test_list_parses_workouts_and_skips_malformed_entries(api, rsps, caplog):
    rsps.add(
        responses.GET,
        LIST_URL,
        json={
            "status": True,
            "data": [
                {"_id": "a", "WorkoutId": 1, "Name": "Eins", "Description": "d"},
                {"_id": "bad", "Name": "ohne Id"},
                {"_id": "boolid", "WorkoutId": True, "Name": "bool"},
                {"_id": "strid", "WorkoutId": "5", "Name": "str"},
                {"WorkoutId": 2, "Description": 7},
            ],
        },
    )
    with caplog.at_level(logging.WARNING):
        result = api.list()
    assert result == [RemoteWorkout(1, "Eins", "d"), RemoteWorkout(2, "", None)]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 3
    assert "bad" in warnings[0].getMessage()
    headers = rsps.calls[0].request.headers
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Origin"] == "https://workout.mywhoosh.com"
    assert headers["Referer"] == "https://workout.mywhoosh.com/"


def test_list_parses_updated_at(api, rsps):
    rsps.add(
        responses.GET,
        LIST_URL,
        json={
            "status": True,
            "data": [
                {"_id": "a", "WorkoutId": 1, "Name": "Eins", "Description": "d",
                 "createdAt": "2026-11-08T10:00:00.000Z", "updatedAt": "2026-11-08T10:05:00.000Z"},
                {"_id": "b", "WorkoutId": 2, "Name": "Zwei", "updatedAt": 17},
                {"_id": "c", "WorkoutId": 3, "Name": "Drei"},
            ],
        },
    )
    result = api.list()
    assert [w.updated_at for w in result] == ["2026-11-08T10:05:00.000Z", None, None]
    assert result[0] == RemoteWorkout(1, "Eins", "d", "2026-11-08T10:05:00.000Z")


@pytest.mark.parametrize("body", [{"status": True}, {"status": True, "data": {}}, [1], {"status": False, "data": []}])
def test_list_unexpected_structure_raises_unavailable(api, rsps, body):
    rsps.add(responses.GET, LIST_URL, json=body)
    with pytest.raises(MyWhooshUnavailableError):
        api.list()


def test_list_5xx_raises_unavailable(api, rsps):
    rsps.add(responses.GET, LIST_URL, status=502)
    with pytest.raises(MyWhooshUnavailableError):
        api.list()


def test_transport_error_raises_connection_error(api, rsps):
    rsps.add(responses.GET, LIST_URL, body=requests.ConnectionError("boom"))
    with pytest.raises(MyWhooshConnectionError):
        api.list()


def test_credits_reads_current_credits(api, rsps):
    rsps.add(responses.GET, CREDITS_URL, json={"status": True, "data": {"CurrentCredits": 10}})
    assert api.credits() == 10


@pytest.mark.parametrize(
    "data", [{}, {"CurrentCredits": "10"}, {"CurrentCredits": -1}, {"CurrentCredits": True}, {"CurrentCredits": 1.5}]
)
def test_credits_invalid_raises_unavailable(api, rsps, data):
    rsps.add(responses.GET, CREDITS_URL, json={"status": True, "data": data})
    with pytest.raises(MyWhooshUnavailableError):
        api.credits()


def test_credits_status_false_raises_unavailable(api, rsps):
    rsps.add(responses.GET, CREDITS_URL, json={"status": False, "message": "x", "data": {"CurrentCredits": 3}})
    with pytest.raises(MyWhooshUnavailableError):
        api.credits()


def test_upload_posts_user_id_and_workout(api, rsps):
    rsps.add(responses.POST, UPLOAD_URL, status=201, json={"status": True, "message": "Custom workout is being uploaded."})
    api.upload({"Id": 5, "Name": "W"})
    request = rsps.calls[0].request
    assert json.loads(request.body) == {"UserId": "user-42", "SportsModeType": 0, "WorkoutsData": [{"Id": 5, "Name": "W"}]}
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Content-Type"] == "application/json"


def test_upload_no_slots_raises_no_slots_error(api, rsps):
    rsps.add(responses.POST, UPLOAD_URL, status=400, json={"status": False, "message": "Not enough Slot Credits"})
    with pytest.raises(MyWhooshNoSlotsError):
        api.upload({"Id": 5})


def test_upload_credit_message_case_insensitive(api, rsps):
    rsps.add(responses.POST, UPLOAD_URL, status=403, json={"message": "no CREDIT left"})
    with pytest.raises(MyWhooshNoSlotsError):
        api.upload({"Id": 5})


def test_upload_other_4xx_raises_workout_error_with_message(api, rsps):
    rsps.add(responses.POST, UPLOAD_URL, status=422, json={"status": False, "message": "Invalid workout steps"})
    with pytest.raises(MyWhooshWorkoutError, match="Invalid workout steps") as info:
        api.upload({"Id": 5})
    assert not isinstance(info.value, MyWhooshNoSlotsError)


def test_upload_status_false_raises_workout_error(api, rsps):
    rsps.add(responses.POST, UPLOAD_URL, status=200, json={"status": False, "message": "Name taken"})
    with pytest.raises(MyWhooshWorkoutError, match="Name taken"):
        api.upload({"Id": 5})


def test_upload_5xx_raises_unavailable(api, rsps):
    rsps.add(responses.POST, UPLOAD_URL, status=500)
    with pytest.raises(MyWhooshUnavailableError) as info:
        api.upload({"Id": 5})
    assert not isinstance(info.value, MyWhooshWorkoutError)


def test_delete_uses_zero_prefix_path(api, rsps):
    rsps.add(
        responses.DELETE,
        f"{UPLOAD_URL}/0/1794152572",
        json={"status": True, "message": "Custom workout is being deleted."},
    )
    api.delete(1794152572)


def test_delete_404_is_success(api, rsps):
    rsps.add(responses.DELETE, f"{UPLOAD_URL}/0/7", status=404)
    api.delete(7)


def test_delete_status_false_raises_workout_error(api, rsps):
    rsps.add(responses.DELETE, f"{UPLOAD_URL}/0/7", json={"status": False, "message": "nope"})
    with pytest.raises(MyWhooshWorkoutError, match="nope"):
        api.delete(7)


@pytest.mark.parametrize("status", [400, 403, 409, 422])
def test_delete_other_4xx_raises_workout_error_with_message(api, rsps, status):
    rsps.add(responses.DELETE, f"{UPLOAD_URL}/0/7", status=status,
             json={"status": False, "message": "Workout is assigned"})
    with pytest.raises(MyWhooshWorkoutError, match="Workout is assigned"):
        api.delete(7)


def test_delete_4xx_without_message_names_the_status(api, rsps):
    rsps.add(responses.DELETE, f"{UPLOAD_URL}/0/7", status=400, body="nope")
    with pytest.raises(MyWhooshWorkoutError, match="HTTP 400"):
        api.delete(7)


def test_delete_5xx_raises_unavailable(api, rsps):
    rsps.add(responses.DELETE, f"{UPLOAD_URL}/0/7", status=503)
    with pytest.raises(MyWhooshUnavailableError):
        api.delete(7)


def test_401_invalidates_and_retries_once_then_auth_error(auth, api, rsps):
    auth.tokens = ["old", "new"]
    rsps.add(responses.GET, CREDITS_URL, status=401)
    rsps.add(responses.GET, CREDITS_URL, status=401)
    with pytest.raises(MyWhooshAuthError):
        api.credits()
    assert auth.invalidated == 1
    assert [c.request.headers["Authorization"] for c in rsps.calls] == ["Bearer old", "Bearer new"]


@pytest.mark.parametrize("call", ["list", "credits", "upload", "delete"])
def test_401_retry_applies_to_all_calls_and_succeeds(auth, api, rsps, call):
    auth.tokens = ["old", "new"]
    method, url, ok = {
        "list": (responses.GET, LIST_URL, {"status": True, "data": []}),
        "credits": (responses.GET, CREDITS_URL, {"status": True, "data": {"CurrentCredits": 1}}),
        "upload": (responses.POST, UPLOAD_URL, {"status": True}),
        "delete": (responses.DELETE, f"{UPLOAD_URL}/0/3", {"status": True}),
    }[call]
    rsps.add(method, url, status=401)
    rsps.add(method, url, json=ok)
    args = {"list": (), "credits": (), "upload": ({"Id": 1},), "delete": (3,)}[call]
    getattr(api, call)(*args)
    assert auth.invalidated == 1
    assert len(rsps.calls) == 2


def test_token_never_logged(api, rsps, caplog):
    rsps.add(responses.GET, CREDITS_URL, status=401)
    rsps.add(responses.GET, CREDITS_URL, json={"status": True, "data": {"CurrentCredits": 2}})
    with caplog.at_level(logging.DEBUG):
        api.credits()
    assert TOKEN not in caplog.text


def _client(tmp_path: Path, token: str) -> MyWhooshClient:
    (tmp_path / "mywhoosh_session.json").write_text(json.dumps({"access_token": token}))
    return MyWhooshClient("rider@example.com", "pw", tmp_path, KEY)


def test_app_auth_uses_client_token_and_user_id_claim(tmp_path, rsps):
    token = jwt(exp=int(time.time()) + 3600, userId="u-123")
    client = _client(tmp_path, token)
    auth = AppCoachingAuth(client)
    assert auth.token() == token
    assert auth.user_id() == "u-123"
    auth.invalidate()
    assert json.loads((tmp_path / "mywhoosh_session.json").read_text())["access_token"] is None


@pytest.mark.parametrize("claims", [{}, {"userId": ""}, {"userId": 5}])
def test_app_auth_missing_user_id_claim_raises_unavailable(tmp_path, rsps, claims):
    client = _client(tmp_path, jwt(exp=int(time.time()) + 3600, **claims))
    with pytest.raises(MyWhooshUnavailableError):
        AppCoachingAuth(client).user_id()
