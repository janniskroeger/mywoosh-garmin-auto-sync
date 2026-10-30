import base64
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests
import responses

from mywhoosh_garmin_sync.crypto import decrypt_payload
from mywhoosh_garmin_sync.mywhoosh import (
    MyWhooshActivity,
    MyWhooshAuthError,
    MyWhooshClient,
    MyWhooshConnectionError,
    MyWhooshDownloadError,
    MyWhooshUnavailableError,
)

KEY = "D9436E508087E863"
LOGIN_URL = "https://services.mywhoosh.com/http-service/api/login"
LIST_URL = "https://service14.mywhoosh.com/v2/rider/profile/activities"
DOWNLOAD_URL = "https://service14.mywhoosh.com/v2/rider/profile/download-activity-file"
S3_URL = "https://mywhooshprod.s3.eu-west-1.amazonaws.com/ride/x/y.fit?X-Amz-Signature=secret"
FILE_ID = "6ae4e5be7b1cb9f1280c676e"


def _b64url(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def jwt(exp: int) -> str:
    return f"{_b64url({'alg': 'HS256'})}.{_b64url({'exp': exp})}.sig"


def entry(**overrides) -> dict:
    base = {
        "id": "6ae4e5c7ce79013b34b71eb8",
        "title": "MyWhoosh - Locker 45 Min (Regeneration)",
        "sportType": "cycling",
        "startDatetime": "2026-10-30T18:00:09.000Z",
        "activityFileId": FILE_ID,
    }
    base.update(overrides)
    return base


def list_body(results: list[dict]) -> dict:
    return {
        "error": False,
        "code": 200,
        "message": "Rider Profile Activities",
        "data": {"results": results, "totalCount": len(results), "totalPages": 1, "currentPage": 1},
    }


def login_ok(token: str | None = None) -> dict:
    return {
        "Success": True,
        "AccessToken": token or jwt(int(time.time()) + 3600),
        "RefreshToken": "refresh",
        "WhooshId": "w1",
        "Message": "ok",
    }


def write_session(data_dir: Path, **fields) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "mywhoosh_session.json").write_text(json.dumps(fields))


def login_calls(rsps) -> list:
    return [c for c in rsps.calls if c.request.url == LOGIN_URL]


@pytest.fixture
def rsps():
    with responses.RequestsMock() as mock:
        yield mock


@pytest.fixture
def client(tmp_path, rsps):
    return MyWhooshClient("rider@example.com", "hunter2-secret", tmp_path, KEY)


def test_list_recent_logs_in_and_parses(client, rsps):
    token = jwt(int(time.time()) + 3600)
    rsps.add(responses.POST, LOGIN_URL, json=login_ok(token))
    rsps.add(
        responses.POST,
        LIST_URL,
        json=list_body([entry(), entry(activityFileId="second", title="Zweite Fahrt")]),
    )

    activities = client.list_recent()

    assert activities == [
        MyWhooshActivity(
            file_id=FILE_ID,
            title="MyWhoosh - Locker 45 Min (Regeneration)",
            sport_type="cycling",
            start=datetime(2026, 10, 30, 18, 0, 9, tzinfo=UTC),
        ),
        MyWhooshActivity(
            file_id="second",
            title="Zweite Fahrt",
            sport_type="cycling",
            start=datetime(2026, 10, 30, 18, 0, 9, tzinfo=UTC),
        ),
    ]
    list_request = rsps.calls[1].request
    assert list_request.headers["Authorization"] == f"Bearer {token}"
    assert list_request.headers["Origin"] == "https://event.mywhoosh.com"
    assert list_request.headers["Referer"] == "https://event.mywhoosh.com/"
    assert list_request.headers["Content-Type"] == "application/json"
    assert list_request.headers["Accept"] == "application/json"
    assert decrypt_payload(json.loads(list_request.body), KEY) == {
        "type": "",
        "page": 1,
        "sortDate": "DESC",
    }
    login_body = json.loads(rsps.calls[0].request.body)
    assert login_body["Platform"] == "Android"
    assert login_body["Action"] == 1001
    assert login_body["Username"] == "rider@example.com"
    assert login_body["Password"] == "hunter2-secret"
    assert login_body["DeviceId"]
    assert login_body["CorrelationId"]
    assert login_body["Authorization"] == ""


def test_device_id_is_persisted_and_reused(tmp_path, rsps):
    rsps.add(responses.POST, LOGIN_URL, json=login_ok(jwt(int(time.time()) - 10)))
    rsps.add(responses.POST, LIST_URL, json=list_body([entry()]))

    MyWhooshClient("rider@example.com", "pw", tmp_path, KEY).list_recent()
    # Second instance: token from the first login is already expired -> logs in again.
    MyWhooshClient("rider@example.com", "pw", tmp_path, KEY).list_recent()

    first, second = (json.loads(c.request.body)["DeviceId"] for c in login_calls(rsps))
    assert first == second
    stored = json.loads((tmp_path / "mywhoosh_session.json").read_text())
    assert stored["device_id"] == first


def test_session_file_is_private(client, rsps, tmp_path):
    rsps.add(responses.POST, LOGIN_URL, json=login_ok())
    rsps.add(responses.POST, LIST_URL, json=list_body([entry()]))

    client.list_recent()

    path = tmp_path / "mywhoosh_session.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.tmp"))


def test_cached_token_is_reused_without_login(tmp_path, rsps):
    token = jwt(int(time.time()) + 3600)
    write_session(tmp_path, device_id="dev-1", access_token=token)
    rsps.add(responses.POST, LIST_URL, json=list_body([entry()]))

    activities = MyWhooshClient("rider@example.com", "pw", tmp_path, KEY).list_recent()

    assert len(activities) == 1
    assert not login_calls(rsps)
    assert rsps.calls[0].request.headers["Authorization"] == f"Bearer {token}"


@pytest.mark.parametrize(
    "cached",
    [
        pytest.param(lambda: jwt(int(time.time()) - 10), id="expired"),
        pytest.param(lambda: jwt(int(time.time()) + 60), id="within-margin"),
        pytest.param(lambda: "garbage", id="garbage"),
        pytest.param(lambda: f"a.{_b64url({'sub': 'x'})}.c", id="no-exp"),
    ],
)
def test_expired_or_garbage_cached_token_triggers_login(tmp_path, rsps, cached):
    write_session(tmp_path, device_id="dev-1", access_token=cached())
    rsps.add(responses.POST, LOGIN_URL, json=login_ok())
    rsps.add(responses.POST, LIST_URL, json=list_body([entry()]))

    activities = MyWhooshClient("rider@example.com", "pw", tmp_path, KEY).list_recent()

    assert len(activities) == 1
    assert len(login_calls(rsps)) == 1
    assert json.loads(login_calls(rsps)[0].request.body)["DeviceId"] == "dev-1"


def test_corrupt_session_file_is_ignored(tmp_path, rsps):
    (tmp_path / "mywhoosh_session.json").write_text("{not json")
    rsps.add(responses.POST, LOGIN_URL, json=login_ok())
    rsps.add(responses.POST, LIST_URL, json=list_body([entry()]))

    assert len(MyWhooshClient("rider@example.com", "pw", tmp_path, KEY).list_recent()) == 1


def test_401_relogs_once_and_retries(client, rsps):
    rsps.add(responses.POST, LOGIN_URL, json=login_ok())
    rsps.add(responses.POST, LIST_URL, status=401)
    rsps.add(responses.POST, LOGIN_URL, json=login_ok())
    rsps.add(responses.POST, LIST_URL, json=list_body([entry()]))

    assert len(client.list_recent()) == 1
    assert len(login_calls(rsps)) == 2


def test_401_after_relogin_raises_unavailable(client, rsps):
    rsps.add(responses.POST, LOGIN_URL, json=login_ok())
    rsps.add(responses.POST, LIST_URL, status=401)
    rsps.add(responses.POST, LOGIN_URL, json=login_ok())
    rsps.add(responses.POST, LIST_URL, status=401)

    with pytest.raises(MyWhooshUnavailableError):
        client.list_recent()
    assert len(login_calls(rsps)) == 2


def test_bad_credentials_raise_auth_error(client, rsps):
    rsps.add(responses.POST, LOGIN_URL, json={"Success": False, "Message": "Invalid"})

    with pytest.raises(MyWhooshAuthError) as excinfo:
        client.list_recent()
    assert "hunter2-secret" not in str(excinfo.value)
    assert "Invalid" not in str(excinfo.value)
    assert str(excinfo.value).endswith("MYWHOOSH_EMAIL/MYWHOOSH_PASSWORD in .env prüfen")


@pytest.mark.parametrize(
    "login_response",
    [
        pytest.param({"status": 500}, id="http-500"),
        pytest.param({"json": {"Success": True}}, id="no-token"),
        pytest.param({"body": "<html>"}, id="not-json"),
        pytest.param({"body": requests.ConnectionError("down")}, id="connection-error"),
    ],
)
def test_login_failures_raise_unavailable(client, rsps, login_response):
    rsps.add(responses.POST, LOGIN_URL, **login_response)

    with pytest.raises(MyWhooshUnavailableError) as excinfo:
        client.list_recent()
    assert "hunter2-secret" not in str(excinfo.value)


@pytest.mark.parametrize(
    "list_response",
    [
        pytest.param({"status": 502}, id="http-502"),
        pytest.param({"body": requests.ConnectionError("down")}, id="connection-error"),
        pytest.param({"body": requests.Timeout("slow")}, id="timeout"),
        pytest.param({"json": {"data": {}}}, id="no-results"),
        pytest.param({"json": {"data": {"results": "x"}}}, id="results-not-list"),
        pytest.param({"body": "<html>"}, id="not-json"),
    ],
)
def test_list_failures_raise_unavailable(client, rsps, list_response):
    rsps.add(responses.POST, LOGIN_URL, json=login_ok())
    rsps.add(responses.POST, LIST_URL, **list_response)

    with pytest.raises(MyWhooshUnavailableError):
        client.list_recent()


def test_malformed_entries_are_skipped(client, rsps, caplog):
    rsps.add(responses.POST, LOGIN_URL, json=login_ok())
    rsps.add(
        responses.POST,
        LIST_URL,
        json=list_body(
            [
                {k: v for k, v in entry().items() if k != "activityFileId"},
                entry(activityFileId="no-sport", sportType=None),
                entry(activityFileId="good"),
            ]
        ),
    )

    with caplog.at_level(logging.WARNING):
        activities = client.list_recent()

    assert [a.file_id for a in activities] == ["good"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "no-sport" in warnings[1].getMessage()


@pytest.mark.parametrize("title", [None, "", 42, "   "])
def test_missing_or_empty_title_gets_default(client, rsps, title):
    rsps.add(responses.POST, LOGIN_URL, json=login_ok())
    rsps.add(responses.POST, LIST_URL, json=list_body([entry(title=title)]))

    activities = client.list_recent()

    assert [a.title for a in activities] == ["MyWhoosh-Fahrt"]


def test_absent_title_key_gets_default(client, rsps):
    rsps.add(responses.POST, LOGIN_URL, json=login_ok())
    rsps.add(
        responses.POST,
        LIST_URL,
        json=list_body([{k: v for k, v in entry().items() if k != "title"}]),
    )

    assert [a.title for a in client.list_recent()] == ["MyWhoosh-Fahrt"]


def test_entry_with_bad_start_is_skipped(client, rsps, caplog):
    rsps.add(responses.POST, LOGIN_URL, json=login_ok())
    rsps.add(
        responses.POST,
        LIST_URL,
        json=list_body([entry(startDatetime="yesterday"), entry(startDatetime="2026-10-30T18:00:09")]),
    )

    with caplog.at_level(logging.WARNING):
        assert client.list_recent() == []

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_download_fit(client, rsps, caplog):
    rsps.add(responses.POST, LOGIN_URL, json=login_ok())
    rsps.add(responses.POST, DOWNLOAD_URL, json={"error": False, "data": S3_URL})
    rsps.add(responses.GET, S3_URL, body=b"FITBYTES")

    with caplog.at_level(logging.DEBUG):
        assert client.download_fit(FILE_ID) == b"FITBYTES"

    download_request = rsps.calls[1].request
    assert decrypt_payload(json.loads(download_request.body), KEY) == {"fileId": FILE_ID}
    assert "Authorization" not in rsps.calls[2].request.headers
    assert "secret" not in caplog.text


def test_download_relogs_once_on_401(client, rsps):
    rsps.add(responses.POST, LOGIN_URL, json=login_ok())
    rsps.add(responses.POST, DOWNLOAD_URL, status=401)
    rsps.add(responses.POST, LOGIN_URL, json=login_ok())
    rsps.add(responses.POST, DOWNLOAD_URL, json={"data": S3_URL})
    rsps.add(responses.GET, S3_URL, body=b"FITBYTES")

    assert client.download_fit(FILE_ID) == b"FITBYTES"
    assert len(login_calls(rsps)) == 2


@pytest.mark.parametrize(
    "api_response, s3_response",
    [
        pytest.param({"status": 400}, None, id="api-400"),
        pytest.param({"json": {"error": False}}, None, id="data-missing"),
        pytest.param({"json": {"data": 42}}, None, id="data-not-string"),
        pytest.param({"json": {"data": "ftp://x/y.fit"}}, None, id="data-not-http"),
        pytest.param({"json": {"data": S3_URL}}, {"status": 403}, id="s3-403"),
        pytest.param({"json": {"data": S3_URL}}, {"status": 500}, id="s3-500"),
    ],
)
def test_download_errors_are_download_errors(client, rsps, api_response, s3_response):
    rsps.add(responses.POST, LOGIN_URL, json=login_ok())
    rsps.add(responses.POST, DOWNLOAD_URL, **api_response)
    if s3_response is not None:
        rsps.add(responses.GET, S3_URL, **s3_response)

    with pytest.raises(MyWhooshDownloadError) as excinfo:
        client.download_fit(FILE_ID)
    assert "X-Amz" not in str(excinfo.value)
    assert "secret" not in str(excinfo.value)


def test_download_s3_connection_error_is_unavailable_without_url(client, rsps):
    rsps.add(responses.POST, LOGIN_URL, json=login_ok())
    rsps.add(responses.POST, DOWNLOAD_URL, json={"data": S3_URL})
    rsps.add(responses.GET, S3_URL, body=requests.ConnectionError(f"failed {S3_URL}"))

    with pytest.raises(MyWhooshConnectionError) as excinfo:
        client.download_fit(FILE_ID)
    assert isinstance(excinfo.value, MyWhooshUnavailableError)
    assert not isinstance(excinfo.value, MyWhooshDownloadError)
    assert "ConnectionError" in str(excinfo.value)
    assert "X-Amz" not in str(excinfo.value)
    assert "secret" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(requests.ConnectionError("down"), id="connection-error"),
        pytest.param(requests.Timeout("slow"), id="timeout"),
    ],
)
def test_download_api_transport_error_is_unavailable_not_download_error(client, rsps, body):
    rsps.add(responses.POST, LOGIN_URL, json=login_ok())
    rsps.add(responses.POST, DOWNLOAD_URL, body=body)

    with pytest.raises(MyWhooshConnectionError):
        client.download_fit(FILE_ID)


def test_download_failing_relogin_after_401_is_unavailable_not_download_error(tmp_path, rsps):
    # A failing re-login after HTTP 401 must abort the run, not count against the ride.
    write_session(tmp_path, device_id="d", access_token=jwt(int(time.time()) + 3600))
    rsps.add(responses.POST, DOWNLOAD_URL, status=401)
    rsps.add(responses.POST, LOGIN_URL, body=requests.ConnectionError("down"))

    with pytest.raises(MyWhooshConnectionError):
        MyWhooshClient("rider@example.com", "pw", tmp_path, KEY).download_fit(FILE_ID)


@pytest.mark.parametrize(
    "login_response",
    [
        pytest.param({"status": 500}, id="http-500"),
        pytest.param({"json": {"Success": True}}, id="no-token"),
        pytest.param({"body": "<html>"}, id="not-json"),
    ],
)
def test_download_failing_login_is_unavailable_not_download_error(client, rsps, login_response):
    rsps.add(responses.POST, LOGIN_URL, **login_response)

    with pytest.raises(MyWhooshUnavailableError) as excinfo:
        client.download_fit(FILE_ID)
    assert not isinstance(excinfo.value, MyWhooshDownloadError)


def test_download_auth_error_propagates(client, rsps):
    rsps.add(responses.POST, LOGIN_URL, json={"Success": False})

    with pytest.raises(MyWhooshAuthError):
        client.download_fit(FILE_ID)
