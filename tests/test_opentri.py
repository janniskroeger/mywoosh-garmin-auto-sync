import base64
import json
import logging
import os
import stat
import time
from datetime import date
from urllib.parse import parse_qs

import pytest
import requests
import responses

from mywhoosh_garmin_sync.opentri import (
    OpenTriAuthError,
    OpenTriClient,
    OpenTriExportError,
    OpenTriUnavailableError,
    PlannedBike,
)

API = "https://api.example.test"
KC = "https://auth.example.test"
REALM = "open-tri"
CLIENT_ID = "mywhoosh-garmin-sync"
DEVICE_URL = f"{KC}/realms/{REALM}/protocol/openid-connect/auth/device"
TOKEN_URL = f"{KC}/realms/{REALM}/protocol/openid-connect/token"
SUB = "athlete-1"
ZIP = b"PK\x03\x04rest-of-zip"
FETCH = (date(2026, 11, 7), date(2026, 11, 20))
PLAN_URL = f"{API}/athletes/{SUB}/training-plans/active"
FIT_URL = f"{API}/athletes/{SUB}/training-plans/p1/fit"
HINT = "mywhoosh-garmin-sync login-opentri"


def _b64url(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def jwt(exp: float | None = None, sub: str = SUB) -> str:
    exp = time.time() + 3600 if exp is None else exp
    return f"{_b64url({'alg': 'none'})}.{_b64url({'sub': sub, 'exp': int(exp)})}.sig"


def token_ok(refresh: str | None = "refresh-1", access: str | None = None) -> dict:
    body = {"access_token": access or jwt(), "expires_in": 3600}
    if refresh is not None:
        body["refresh_token"] = refresh
    return body


def make_client(tmp_path, sleeps=None, session=None) -> OpenTriClient:
    return OpenTriClient(
        api_url=API,
        keycloak_url=KC,
        realm=REALM,
        client_id=CLIENT_ID,
        data_dir=tmp_path,
        session=session,
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
    )


def write_session(tmp_path, refresh="refresh-1") -> None:
    (tmp_path / "opentri_session.json").write_text(json.dumps({"refresh_token": refresh}))


def form(call) -> dict:
    return {k: v[0] for k, v in parse_qs(call.request.body).items()}


@pytest.fixture
def rsps():
    with responses.RequestsMock() as mock:
        yield mock


@pytest.fixture
def client(tmp_path):
    write_session(tmp_path)
    return make_client(tmp_path)


def add_plan(rsps, status=200):
    if status == 200:
        rsps.get(PLAN_URL, json={"id": "p1"})
    else:
        rsps.get(PLAN_URL, status=status)


def add_fit(rsps, **kwargs):
    kwargs.setdefault("body", ZIP)
    kwargs.setdefault("content_type", "application/zip")
    rsps.get(FIT_URL, **kwargs)


def test_device_login_stores_refresh_token(tmp_path, rsps):
    rsps.post(
        DEVICE_URL,
        json={
            "device_code": "DEVCODE",
            "user_code": "ABCD-EFGH",
            "verification_uri": f"{KC}/device",
            "verification_uri_complete": f"{KC}/device?user_code=ABCD-EFGH",
            "expires_in": 600,
            "interval": 5,
        },
    )
    rsps.post(TOKEN_URL, status=400, json={"error": "authorization_pending"})
    rsps.post(TOKEN_URL, status=400, json={"error": "slow_down"})
    rsps.post(TOKEN_URL, json=token_ok("offline-refresh"))
    shown, sleeps = [], []
    make_client(tmp_path, sleeps).device_login(shown.append)

    assert form(rsps.calls[0]) == {"client_id": CLIENT_ID, "scope": "openid offline_access"}
    polls = [form(c) for c in rsps.calls[1:]]
    assert all(
        p == {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": "DEVCODE",
            "client_id": CLIENT_ID,
        }
        for p in polls
    )
    assert any(f"{KC}/device?user_code=ABCD-EFGH" in line for line in shown)
    assert sleeps == [5, 5, 10]
    path = tmp_path / "opentri_session.json"
    saved = json.loads(path.read_text())
    assert saved["refresh_token"] == "offline-refresh"
    assert "DEVCODE" not in path.read_text()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_device_login_without_complete_uri_shows_uri_and_code(tmp_path, rsps):
    rsps.post(
        DEVICE_URL,
        json={"device_code": "D", "user_code": "WXYZ", "verification_uri": f"{KC}/device", "expires_in": 600},
    )
    rsps.post(TOKEN_URL, json=token_ok())
    shown, sleeps = [], []
    make_client(tmp_path, sleeps).device_login(shown.append)
    text = "\n".join(shown)
    assert f"{KC}/device" in text and "WXYZ" in text
    assert sleeps == [5]


@pytest.mark.parametrize("error", ["expired_token", "access_denied", "invalid_client"])
def test_device_login_expired_or_denied_raises_auth_error(tmp_path, rsps, error):
    rsps.post(
        DEVICE_URL,
        json={"device_code": "DEVCODE", "user_code": "U", "verification_uri": f"{KC}/device", "expires_in": 600},
    )
    rsps.post(TOKEN_URL, status=400, json={"error": error})
    with pytest.raises(OpenTriAuthError) as excinfo:
        make_client(tmp_path).device_login(lambda s: None)
    assert error not in str(excinfo.value) and "DEVCODE" not in str(excinfo.value)
    assert not (tmp_path / "opentri_session.json").exists()


def test_device_login_gives_up_after_expires_in(tmp_path, rsps):
    rsps.post(
        DEVICE_URL,
        json={"device_code": "D", "user_code": "U", "verification_uri": f"{KC}/device", "expires_in": 12, "interval": 5},
    )
    rsps.post(TOKEN_URL, status=400, json={"error": "authorization_pending"})
    sleeps = []
    with pytest.raises(OpenTriAuthError):
        make_client(tmp_path, sleeps).device_login(lambda s: None)
    assert sum(sleeps) <= 12 + 5


def test_device_login_without_refresh_token_in_response_fails(tmp_path, rsps):
    rsps.post(
        DEVICE_URL,
        json={"device_code": "D", "user_code": "U", "verification_uri": f"{KC}/device", "expires_in": 600},
    )
    rsps.post(TOKEN_URL, json=token_ok(refresh=None))
    with pytest.raises(OpenTriUnavailableError):
        make_client(tmp_path).device_login(lambda s: None)


def test_fetch_uses_refresh_then_active_plan_then_export(client, rsps):
    rsps.post(TOKEN_URL, json=token_ok("refresh-1", access=jwt()))
    add_plan(rsps)
    add_fit(rsps)
    assert client.fetch_bundle(*FETCH) == ZIP

    assert form(rsps.calls[0]) == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-1",
        "client_id": CLIENT_ID,
    }
    plan_call, fit_call = rsps.calls[1], rsps.calls[2]
    assert plan_call.request.headers["Authorization"].startswith("Bearer ")
    assert fit_call.request.headers["Authorization"] == plan_call.request.headers["Authorization"]
    assert parse_qs(fit_call.request.url.split("?", 1)[1]) == {
        "from": ["2026-11-07"],
        "to": ["2026-11-20"],
        "schedule": ["true"],
    }


def test_api_url_with_trailing_slash_is_normalized(tmp_path, rsps):
    write_session(tmp_path)
    c = OpenTriClient(
        api_url=API + "/", keycloak_url=KC + "/", realm=REALM, client_id=CLIENT_ID, data_dir=tmp_path
    )
    rsps.post(TOKEN_URL, json=token_ok())
    add_plan(rsps)
    add_fit(rsps)
    assert c.fetch_bundle(*FETCH) == ZIP


def test_rotated_refresh_token_is_saved(tmp_path, rsps):
    write_session(tmp_path, "old")
    first = make_client(tmp_path)
    rsps.post(TOKEN_URL, json=token_ok("rotated", access=jwt(exp=time.time() + 10)))  # too short to reuse
    add_plan(rsps)
    add_fit(rsps)
    first.fetch_bundle(*FETCH)
    assert json.loads((tmp_path / "opentri_session.json").read_text())["refresh_token"] == "rotated"

    rsps.post(TOKEN_URL, json=token_ok("rotated-2"))
    add_plan(rsps)
    add_fit(rsps)
    first.fetch_bundle(*FETCH)
    assert form(rsps.calls[-3])["refresh_token"] == "rotated"

    # A fresh client reads the persisted token as well.
    rsps.post(TOKEN_URL, json=token_ok("rotated-3"))
    add_plan(rsps)
    add_fit(rsps)
    make_client(tmp_path).fetch_bundle(*FETCH)
    assert form(rsps.calls[-3])["refresh_token"] == "rotated-2"


def test_refresh_response_without_new_refresh_token_keeps_old(client, tmp_path, rsps):
    rsps.post(TOKEN_URL, json=token_ok(refresh=None))
    add_plan(rsps)
    add_fit(rsps)
    client.fetch_bundle(*FETCH)
    assert json.loads((tmp_path / "opentri_session.json").read_text())["refresh_token"] == "refresh-1"


def test_no_refresh_token_raises_auth_error_with_hint(tmp_path, rsps):
    with pytest.raises(OpenTriAuthError) as excinfo:
        make_client(tmp_path).fetch_bundle(*FETCH)
    assert HINT in str(excinfo.value)
    assert len(rsps.calls) == 0


def test_rejected_refresh_raises_auth_error_with_hint(client, rsps):
    rsps.post(TOKEN_URL, status=400, json={"error": "invalid_grant", "error_description": "Token is not active"})
    with pytest.raises(OpenTriAuthError) as excinfo:
        client.fetch_bundle(*FETCH)
    assert HINT in str(excinfo.value)
    assert "refresh-1" not in str(excinfo.value)


def test_refresh_server_error_raises_unavailable(client, rsps):
    rsps.post(TOKEN_URL, status=503)
    with pytest.raises(OpenTriUnavailableError):
        client.fetch_bundle(*FETCH)


def test_access_token_reused_until_expiry(client, rsps):
    rsps.post(TOKEN_URL, json=token_ok())
    for _ in range(2):
        add_plan(rsps)
        add_fit(rsps)
    client.fetch_bundle(*FETCH)
    client.fetch_bundle(*FETCH)
    assert [c.request.url for c in rsps.calls].count(TOKEN_URL) == 1


def test_expiring_access_token_is_refreshed(client, rsps):
    rsps.post(TOKEN_URL, json=token_ok(access=jwt(exp=time.time() + 30)))
    rsps.post(TOKEN_URL, json=token_ok(access=jwt()))
    for _ in range(2):
        add_plan(rsps)
        add_fit(rsps)
    client.fetch_bundle(*FETCH)
    client.fetch_bundle(*FETCH)
    assert [c.request.url for c in rsps.calls].count(TOKEN_URL) == 2


def test_api_401_refreshes_once(client, rsps):
    rsps.post(TOKEN_URL, json=token_ok(access=jwt()))
    rsps.get(PLAN_URL, status=401)
    rsps.post(TOKEN_URL, json=token_ok(access=jwt()))
    add_plan(rsps)
    add_fit(rsps)
    assert client.fetch_bundle(*FETCH) == ZIP
    assert [c.request.url for c in rsps.calls].count(TOKEN_URL) == 2


def test_api_401_twice_raises_unavailable(client, rsps):
    rsps.post(TOKEN_URL, json=token_ok())
    rsps.get(PLAN_URL, status=401)
    rsps.post(TOKEN_URL, json=token_ok())
    rsps.get(PLAN_URL, status=401)
    with pytest.raises(OpenTriUnavailableError):
        client.fetch_bundle(*FETCH)


def test_no_active_plan_returns_none(client, rsps):
    rsps.post(TOKEN_URL, json=token_ok())
    add_plan(rsps, status=204)
    assert client.fetch_bundle(*FETCH) is None


def test_plan_without_id_raises_unavailable(client, rsps):
    rsps.post(TOKEN_URL, json=token_ok())
    rsps.get(PLAN_URL, json={"name": "x"})
    with pytest.raises(OpenTriUnavailableError):
        client.fetch_bundle(*FETCH)


def test_server_error_raises_unavailable(client, rsps):
    rsps.post(TOKEN_URL, json=token_ok())
    add_plan(rsps)
    add_fit(rsps, status=500, body=b"boom")
    with pytest.raises(OpenTriUnavailableError):
        client.fetch_bundle(*FETCH)


@pytest.mark.parametrize("ctype,body", [("text/html", b"<html>"), ("application/json", b"{}")])
def test_non_zip_200_raises_unavailable(client, rsps, ctype, body):
    rsps.post(TOKEN_URL, json=token_ok())
    add_plan(rsps)
    add_fit(rsps, body=body, content_type=ctype)
    with pytest.raises(OpenTriUnavailableError):
        client.fetch_bundle(*FETCH)


def test_network_error_raises_unavailable(client, rsps):
    rsps.post(TOKEN_URL, body=requests.ConnectionError("https://x/?token=refresh-1"))
    with pytest.raises(OpenTriUnavailableError) as excinfo:
        client.fetch_bundle(*FETCH)
    assert "refresh-1" not in str(excinfo.value)


def test_tokens_never_logged(client, tmp_path, rsps, caplog):
    caplog.set_level(logging.DEBUG)
    access = jwt()
    rsps.post(TOKEN_URL, json=token_ok("secret-refresh", access=access))
    add_plan(rsps)
    add_fit(rsps)
    client.fetch_bundle(*FETCH)
    assert "refresh-1" not in caplog.text
    assert "secret-refresh" not in caplog.text
    assert access not in caplog.text
    assert access.split(".")[1] not in caplog.text


# --- F1: the running client re-reads the session file written by another process -----------


def test_refresh_picks_up_token_written_after_construction(tmp_path, rsps):
    waiting = make_client(tmp_path)  # no session file yet, like the container before login
    write_session(tmp_path, "from-login-opentri")  # another process ran login-opentri
    rsps.post(TOKEN_URL, json=token_ok(refresh="rotated"))
    add_plan(rsps, status=204)

    assert waiting.fetch_bundle(*FETCH) is None

    assert form(rsps.calls[0])["refresh_token"] == "from-login-opentri"


def test_refresh_picks_up_newer_token_after_rejection(tmp_path, rsps):
    write_session(tmp_path, "old")
    holder = make_client(tmp_path)
    rsps.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})
    with pytest.raises(OpenTriAuthError):
        holder.fetch_bundle(*FETCH)

    write_session(tmp_path, "newer")  # login-opentri in another process
    rsps.post(TOKEN_URL, json=token_ok(refresh="rotated"))
    add_plan(rsps, status=204)

    assert holder.fetch_bundle(*FETCH) is None
    assert form(rsps.calls[1])["refresh_token"] == "newer"


# --- F2: only a JSON 404 means "empty plan" ---------------------------------------------------


def test_json_404_export_is_empty(client, rsps):
    rsps.post(TOKEN_URL, json=token_ok())
    add_plan(rsps)
    add_fit(rsps, status=404, body=b'{"message": "no workouts"}', content_type="application/json")
    assert client.fetch_bundle(*FETCH) is None


@pytest.mark.parametrize("ctype,body", [("text/html", b"<html>Not Found</html>"), ("application/json", b"")])
def test_non_json_404_export_raises_unavailable(client, rsps, ctype, body):
    rsps.post(TOKEN_URL, json=token_ok())
    add_plan(rsps)
    add_fit(rsps, status=404, body=body, content_type=ctype)
    with pytest.raises(OpenTriUnavailableError):
        client.fetch_bundle(*FETCH)


def test_empty_204_export_is_empty(client, rsps):
    rsps.post(TOKEN_URL, json=token_ok())
    add_plan(rsps)
    add_fit(rsps, status=204, body=b"")
    assert client.fetch_bundle(*FETCH) is None


# --- Planned bike sessions and .zwo ------------------------------------------------------------

ZWO = b'<?xml version="1.0"?><workout_file/>'


def item(pid, day, sport="BIKE", brick=None, tid="t1", name="Grundlage"):
    return {
        "id": pid,
        "date": day,
        "brickOrder": brick,
        "training": {"id": tid, "sport": sport, "name": name},
    }


def add_items(rsps, items, start="2026-10-26"):
    rsps.get(PLAN_URL, json={"id": "p1", "startDate": start, "plannedTrainings": items})


WINDOW = (date(2026, 11, 9), date(2026, 11, 22))


def test_fetch_planned_bikes_filters_sport_and_window(client, rsps):
    rsps.post(TOKEN_URL, json=token_ok())
    add_items(
        rsps,
        [
            item("a", "2026-11-09"),
            item("b", "2026-11-09", sport="RUN", tid="t2"),
            item("c", "2026-11-30", tid="t3"),
            item("d", "2026-11-08", tid="t4"),
        ],
    )
    assert client.fetch_planned_bikes(*WINDOW) == [PlannedBike("a", date(2026, 11, 9), None, "t1", "Grundlage", 3)]


def test_fetch_planned_bikes_window_is_inclusive(client, rsps):
    rsps.post(TOKEN_URL, json=token_ok())
    add_items(rsps, [item("a", "2026-11-09"), item("b", "2026-11-22", tid="t2")])
    assert [b.planned_id for b in client.fetch_planned_bikes(*WINDOW)] == ["a", "b"]


def test_fetch_planned_bikes_sorts_bricks(client, rsps):
    rsps.post(TOKEN_URL, json=token_ok())
    add_items(rsps, [item("a", "2026-11-10", brick=1), item("b", "2026-11-10", brick=None, tid="t2")])
    assert [b.planned_id for b in client.fetch_planned_bikes(*WINDOW)] == ["b", "a"]


def test_fetch_planned_bikes_no_active_plan_returns_none(client, rsps):
    rsps.post(TOKEN_URL, json=token_ok())
    add_plan(rsps, status=204)
    assert client.fetch_planned_bikes(*WINDOW) is None


@pytest.mark.parametrize(
    "bad",
    [
        {"date": "2026-11-10", "training": {"id": "t", "sport": "BIKE", "name": "n"}},
        {"id": 5, "date": "2026-11-10", "training": {"id": "t", "sport": "BIKE", "name": "n"}},
        item("a", "10.11.2026"),
        item("a", None),
        item("a", "2026-11-10", tid=None),
        item("a", "2026-11-10", name=None),
    ],
)
def test_fetch_planned_bikes_malformed_bike_raises_unavailable(client, rsps, bad):
    rsps.post(TOKEN_URL, json=token_ok())
    add_items(rsps, [bad])
    with pytest.raises(OpenTriUnavailableError):
        client.fetch_planned_bikes(*WINDOW)


@pytest.mark.parametrize("start", [None, "nope"])
def test_fetch_planned_bikes_bad_start_date_raises_when_bike_in_window(client, rsps, start):
    rsps.post(TOKEN_URL, json=token_ok())
    rsps.get(PLAN_URL, json={"id": "p1", "startDate": start, "plannedTrainings": [item("a", "2026-11-10")]})
    with pytest.raises(OpenTriUnavailableError):
        client.fetch_planned_bikes(*WINDOW)


def test_fetch_planned_bikes_bad_start_date_ok_without_bikes_in_window(client, rsps):
    rsps.post(TOKEN_URL, json=token_ok())
    rsps.get(PLAN_URL, json={"id": "p1", "plannedTrainings": [item("a", "2026-11-10", sport="RUN")]})
    assert client.fetch_planned_bikes(*WINDOW) == []


def test_fetch_planned_bikes_ignores_malformed_non_bike(client, rsps):
    rsps.post(TOKEN_URL, json=token_ok())
    add_items(rsps, [{"id": 1, "date": "x", "training": {"sport": "RUN"}}])
    assert client.fetch_planned_bikes(*WINDOW) == []


def test_fetch_planned_bikes_plan_not_json_object_raises(client, rsps):
    rsps.post(TOKEN_URL, json=token_ok())
    rsps.get(PLAN_URL, json=["x"])
    with pytest.raises(OpenTriUnavailableError):
        client.fetch_planned_bikes(*WINDOW)


def zwo_url(tid="t1"):
    return f"{API}/athletes/{SUB}/trainings/{tid}/zwo"


def test_fetch_zwo_returns_bytes_and_uses_training_path(client, rsps):
    rsps.post(TOKEN_URL, json=token_ok())
    rsps.get(zwo_url("t 1"), body=ZWO, content_type="application/xml")
    assert client.fetch_zwo("t 1") == ZWO
    assert rsps.calls[1].request.url == f"{API}/athletes/{SUB}/trainings/t%201/zwo"


def test_fetch_zwo_accepts_bom_and_whitespace(client, rsps):
    rsps.post(TOKEN_URL, json=token_ok())
    body = b"\xef\xbb\xbf \n" + ZWO
    rsps.get(zwo_url(), body=body)
    assert client.fetch_zwo("t1") == body


def test_fetch_zwo_non_xml_body_raises_unavailable(client, rsps):
    rsps.post(TOKEN_URL, json=token_ok())
    rsps.get(zwo_url(), body=b"proxy says hi", content_type="text/plain")
    with pytest.raises(OpenTriUnavailableError):
        client.fetch_zwo("t1")


@pytest.mark.parametrize("status", [400, 403, 404, 422])
def test_fetch_zwo_4xx_raises_export_error(client, rsps, status):
    rsps.post(TOKEN_URL, json=token_ok())
    rsps.get(zwo_url(), status=status, body=b"no ftp")
    with pytest.raises(OpenTriExportError) as excinfo:
        client.fetch_zwo("t1")
    assert "t1" in str(excinfo.value) and str(status) in str(excinfo.value)
    assert not isinstance(excinfo.value, OpenTriUnavailableError)


@pytest.mark.parametrize("status", [500, 503])
def test_fetch_zwo_5xx_raises_unavailable(client, rsps, status):
    rsps.post(TOKEN_URL, json=token_ok())
    rsps.get(zwo_url(), status=status)
    with pytest.raises(OpenTriUnavailableError):
        client.fetch_zwo("t1")


def test_fetch_zwo_401_twice_still_unavailable(client, rsps):
    rsps.post(TOKEN_URL, json=token_ok())
    rsps.get(zwo_url(), status=401)
    rsps.post(TOKEN_URL, json=token_ok())
    rsps.get(zwo_url(), status=401)
    with pytest.raises(OpenTriUnavailableError):
        client.fetch_zwo("t1")
