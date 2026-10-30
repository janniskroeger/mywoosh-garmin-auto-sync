from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import requests
from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from mywhoosh_garmin_sync.garmin import (
    GarminAuthError,
    GarminImportError,
    GarminUnavailableError,
    GarminUploader,
    ImportOutcome,
    interactive_login,
)

START = datetime(2026, 10, 30, 18, 0, 9, tzinfo=UTC)


class FakeGarmin:
    """Stands in for garminconnect.Garmin; records calls and replays configured results."""

    instances: list["FakeGarmin"] = []
    login_error: Exception | None = None
    import_result: object = {
        "detailedImportResult": {"successes": [{"internalId": 123}], "failures": []}
    }
    activity_lists: list[list[dict]] = []
    mfa_to_call: bool = False

    def __init__(self, email=None, password=None, prompt_mfa=None, **kwargs):
        self.email = email
        self.password = password
        self.prompt_mfa = prompt_mfa
        self.kwargs = kwargs
        self.calls: list[tuple] = []
        self.mfa_code = None
        FakeGarmin.instances.append(self)

    def login(self, tokenstore=None):
        self.calls.append(("login", tokenstore))
        if self.prompt_mfa is not None and FakeGarmin.mfa_to_call:
            self.mfa_code = self.prompt_mfa()
        if FakeGarmin.login_error is not None:
            raise FakeGarmin.login_error
        return None, None

    def import_activity(self, path):
        self.calls.append(("import_activity", path))
        if isinstance(FakeGarmin.import_result, Exception):
            raise FakeGarmin.import_result
        return FakeGarmin.import_result

    def get_activities_by_date(self, startdate, enddate):
        self.calls.append(("get_activities_by_date", startdate, enddate))
        lists = FakeGarmin.activity_lists
        index = sum(1 for c in self.calls if c[0] == "get_activities_by_date") - 1
        return lists[min(index, len(lists) - 1)] if lists else []

    def set_activity_name(self, activity_id, title):
        self.calls.append(("set_activity_name", activity_id, title))


@pytest.fixture(autouse=True)
def reset_fake():
    FakeGarmin.instances = []
    FakeGarmin.login_error = None
    FakeGarmin.import_result = {
        "detailedImportResult": {"successes": [{"internalId": 123}], "failures": []}
    }
    FakeGarmin.activity_lists = []
    FakeGarmin.mfa_to_call = False


class Sleeper:
    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_uploader(tmp_path, sleeper=None):
    return GarminUploader(
        "me@example.com",
        "pw-secret",
        tmp_path / "tokens",
        client_factory=FakeGarmin,
        sleep=sleeper or Sleeper(),
    )


def http_error(message: str, status: int) -> GarminConnectConnectionError:
    exc = GarminConnectConnectionError(message)
    exc.response = SimpleNamespace(status_code=status)
    return exc


def test_ensure_login_uses_tokenstore_and_logs_in_once(tmp_path):
    uploader = make_uploader(tmp_path)

    uploader.ensure_login()
    uploader.ensure_login()

    assert len(FakeGarmin.instances) == 1
    client = FakeGarmin.instances[0]
    assert (client.email, client.password) == ("me@example.com", "pw-secret")
    assert client.calls == [("login", str(tmp_path / "tokens"))]
    assert (tmp_path / "tokens").is_dir()
    assert (tmp_path / "tokens").stat().st_mode & 0o777 == 0o700


def test_bad_credentials_pause_further_logins(tmp_path):
    FakeGarmin.login_error = GarminConnectAuthenticationError("Invalid credentials")
    uploader = make_uploader(tmp_path)

    with pytest.raises(GarminAuthError):
        uploader.ensure_login()
    with pytest.raises(GarminAuthError):
        uploader.ensure_login()

    assert len(FakeGarmin.instances) == 1


def test_auth_error_text_does_not_leak_password(tmp_path):
    FakeGarmin.login_error = GarminConnectAuthenticationError("Invalid credentials")
    uploader = make_uploader(tmp_path)

    with pytest.raises(GarminAuthError) as excinfo:
        uploader.ensure_login()

    assert "pw-secret" not in str(excinfo.value)
    assert "Invalid credentials" in str(excinfo.value)
    assert ".env prüfen" in str(excinfo.value)
    assert "docker compose up -d --force-recreate" in str(excinfo.value)


def test_mfa_required_raises_auth_error_with_hint(tmp_path):
    FakeGarmin.login_error = GarminConnectAuthenticationError(
        "MFA Required but no prompt_mfa mechanism supplied"
    )
    uploader = make_uploader(tmp_path)

    with pytest.raises(GarminAuthError, match="login-garmin") as excinfo:
        uploader.ensure_login()
    with pytest.raises(GarminAuthError, match="login-garmin"):
        uploader.ensure_login()

    assert "docker compose restart sync" in str(excinfo.value)
    assert len(FakeGarmin.instances) == 1


@pytest.mark.parametrize(
    "message",
    [
        "Failed to retrieve social profile",
        "Failed to retrieve user settings",
        "Invalid profile data found",
        "Invalid user settings found",
    ],
)
def test_transient_profile_failures_are_unavailable_and_do_not_pause(tmp_path, message):
    FakeGarmin.login_error = GarminConnectAuthenticationError(message)
    uploader = make_uploader(tmp_path)

    with pytest.raises(GarminUnavailableError):
        uploader.ensure_login()

    FakeGarmin.login_error = None
    uploader.ensure_login()

    assert len(FakeGarmin.instances) == 2


@pytest.mark.parametrize(
    "error",
    [
        GarminConnectTooManyRequestsError("429"),
        GarminConnectConnectionError("connection reset"),
    ],
)
def test_rate_limit_and_connection_errors_are_unavailable_and_not_paused(tmp_path, error):
    FakeGarmin.login_error = error
    uploader = make_uploader(tmp_path)

    with pytest.raises(GarminUnavailableError):
        uploader.ensure_login()

    FakeGarmin.login_error = None
    uploader.ensure_login()

    assert len(FakeGarmin.instances) == 2
    assert FakeGarmin.instances[0] is not FakeGarmin.instances[1]


def test_import_success_with_internal_id(tmp_path):
    uploader = make_uploader(tmp_path)
    fit = tmp_path / "ride.fit"

    outcome = uploader.import_fit(fit, START)

    assert outcome == ImportOutcome("uploaded", 123)
    assert ("import_activity", str(fit)) in FakeGarmin.instances[0].calls


def test_import_logs_in_lazily(tmp_path):
    uploader = make_uploader(tmp_path)

    uploader.import_fit(tmp_path / "ride.fit", START)

    assert [c[0] for c in FakeGarmin.instances[0].calls] == ["login", "import_activity"]


def test_import_duplicate_409(tmp_path):
    FakeGarmin.import_result = GarminConnectConnectionError(
        "Activity already exists (duplicate): x.fit"
    )
    uploader = make_uploader(tmp_path)

    outcome = uploader.import_fit(tmp_path / "x.fit", START)

    assert outcome == ImportOutcome("duplicate", None)


def test_import_duplicate_in_failures(tmp_path):
    FakeGarmin.import_result = {
        "detailedImportResult": {
            "successes": [],
            "failures": [{"messages": [{"code": 202, "content": "Duplicate Activity."}]}],
        }
    }
    uploader = make_uploader(tmp_path)

    outcome = uploader.import_fit(tmp_path / "x.fit", START)

    assert outcome == ImportOutcome("duplicate", None)


def test_import_failure_without_duplicate_is_import_error_with_garmin_message(tmp_path):
    FakeGarmin.import_result = {
        "detailedImportResult": {
            "successes": [],
            "failures": [{"messages": [{"code": 500, "content": "Corrupt file."}]}],
        }
    }
    uploader = make_uploader(tmp_path)

    with pytest.raises(GarminImportError, match="Corrupt file."):
        uploader.import_fit(tmp_path / "x.fit", START)


def test_import_without_id_polls_activity_list(tmp_path):
    FakeGarmin.import_result = {"detailedImportResult": {"successes": [], "failures": []}}
    FakeGarmin.activity_lists = [[], [{"activityId": 777, "startTimeGMT": "2026-10-30 18:00:10"}]]
    sleeper = Sleeper()
    uploader = make_uploader(tmp_path, sleeper)

    outcome = uploader.import_fit(tmp_path / "x.fit", START)

    assert outcome == ImportOutcome("uploaded", 777)
    assert sleeper.calls == [5, 5]
    polls = [c for c in FakeGarmin.instances[0].calls if c[0] == "get_activities_by_date"]
    assert polls == [("get_activities_by_date", "2026-10-29", "2026-10-31")] * 2


def test_import_without_id_finds_ride_listed_under_neighbouring_day(tmp_path):
    # Ride started 23:59:50 UTC; Garmin may file it under the next (local) day.
    start = datetime(2026, 10, 30, 23, 59, 50, tzinfo=UTC)
    FakeGarmin.import_result = {"detailedImportResult": {"successes": [], "failures": []}}
    FakeGarmin.activity_lists = [[{"activityId": 9, "startTimeGMT": "2026-10-31 00:00:05"}]]
    uploader = make_uploader(tmp_path)

    assert uploader.import_fit(tmp_path / "x.fit", start) == ImportOutcome("uploaded", 9)
    poll = [c for c in FakeGarmin.instances[0].calls if c[0] == "get_activities_by_date"][0]
    assert poll == ("get_activities_by_date", "2026-10-29", "2026-10-31")


def test_import_without_id_ignores_activities_with_other_start_time(tmp_path):
    FakeGarmin.import_result = {"detailedImportResult": {"successes": [], "failures": []}}
    FakeGarmin.activity_lists = [
        [
            {"activityId": 1, "startTimeGMT": "2026-10-30 12:00:00"},
            {"activityId": 2, "startTimeGMT": "2026-10-30 18:02:30"},
            {"activityId": 3, "startTimeGMT": "2026-10-30 18:01:00"},
        ]
    ]
    uploader = make_uploader(tmp_path)

    outcome = uploader.import_fit(tmp_path / "x.fit", START)

    assert outcome == ImportOutcome("uploaded", 3)


def test_import_without_id_gives_up_after_polling(tmp_path):
    FakeGarmin.import_result = {"detailedImportResult": {"successes": [], "failures": []}}
    FakeGarmin.activity_lists = [[]]
    sleeper = Sleeper()
    uploader = make_uploader(tmp_path, sleeper)

    outcome = uploader.import_fit(tmp_path / "x.fit", START)

    assert outcome == ImportOutcome("uploaded", None)
    polls = [c for c in FakeGarmin.instances[0].calls if c[0] == "get_activities_by_date"]
    assert len(polls) == 6


def test_other_import_errors_are_import_errors(tmp_path):
    FakeGarmin.import_result = http_error("Import error: 500", 500)
    uploader = make_uploader(tmp_path)

    with pytest.raises(GarminImportError, match="Import error: 500"):
        uploader.import_fit(tmp_path / "x.fit", START)


def test_import_error_status_is_read_from_wrapped_cause(tmp_path):
    # garminconnect re-raises HTTP failures as a new error without .response.
    wrapper = GarminConnectConnectionError("Import error: API Error 500")
    wrapper.__cause__ = http_error("API Error 500", 500)
    FakeGarmin.import_result = wrapper
    uploader = make_uploader(tmp_path)

    with pytest.raises(GarminImportError):
        uploader.import_fit(tmp_path / "x.fit", START)


def test_import_http_429_is_unavailable(tmp_path):
    FakeGarmin.import_result = http_error("API Error 429", 429)
    uploader = make_uploader(tmp_path)

    with pytest.raises(GarminUnavailableError):
        uploader.import_fit(tmp_path / "x.fit", START)


@pytest.mark.parametrize(
    "error",
    [
        GarminConnectTooManyRequestsError("429"),
        GarminConnectConnectionError("Import error: connection reset"),
    ],
)
def test_import_rate_limit_or_network_error_is_unavailable(tmp_path, error):
    FakeGarmin.import_result = error
    uploader = make_uploader(tmp_path)

    with pytest.raises(GarminUnavailableError):
        uploader.import_fit(tmp_path / "x.fit", START)


def test_set_name_calls_library(tmp_path):
    uploader = make_uploader(tmp_path)

    uploader.set_name(123, "MyWhoosh - Base Miles #3")

    assert ("set_activity_name", "123", "MyWhoosh - Base Miles #3") in FakeGarmin.instances[
        0
    ].calls


def test_interactive_login_prompts_for_mfa(tmp_path):
    FakeGarmin.mfa_to_call = True
    prompts: list[str] = []

    def prompt(text: str) -> str:
        prompts.append(text)
        return "123456"

    interactive_login(
        "me@example.com", "pw-secret", tmp_path / "tokens", prompt=prompt, client_factory=FakeGarmin
    )

    client = FakeGarmin.instances[0]
    assert client.mfa_code == "123456"
    assert prompts == ["MFA-Code: "]
    assert client.calls == [("login", str(tmp_path / "tokens"))]
    assert (tmp_path / "tokens").is_dir()


@pytest.mark.parametrize(
    "error",
    [
        GarminConnectTooManyRequestsError("429"),
        GarminConnectConnectionError("connection reset"),
    ],
)
def test_poll_failure_after_upload_still_reports_uploaded(tmp_path, error, caplog):
    FakeGarmin.import_result = {"detailedImportResult": {"successes": [], "failures": []}}
    uploader = make_uploader(tmp_path)
    client = uploader._logged_in_client()
    polls = []

    def failing_poll(startdate, enddate):
        polls.append(startdate)
        raise error

    client.get_activities_by_date = failing_poll

    with caplog.at_level("WARNING"):
        outcome = uploader.import_fit(tmp_path / "x.fit", START)

    assert outcome == ImportOutcome("uploaded", None)
    assert len(polls) == 1
    assert any(r.levelname == "WARNING" for r in caplog.records)


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("via_cause", [False, True])
def test_rejected_session_drops_client_and_is_unavailable(tmp_path, status, via_cause):
    if via_cause:
        error = GarminConnectConnectionError("Import error: API Error")
        error.__cause__ = http_error("API Error", status)
    else:
        error = http_error("API Error", status)
    uploader = make_uploader(tmp_path)
    uploader.import_fit(tmp_path / "first.fit", START)  # the cached session worked before
    FakeGarmin.import_result = error

    with pytest.raises(GarminUnavailableError):
        uploader.import_fit(tmp_path / "x.fit", START)

    FakeGarmin.import_result = {
        "detailedImportResult": {"successes": [{"internalId": 5}], "failures": []}
    }
    uploader.ensure_login()

    assert len(FakeGarmin.instances) == 2
    assert uploader.import_fit(tmp_path / "x.fit", START) == ImportOutcome("uploaded", 5)
    assert len(FakeGarmin.instances) == 2


def test_import_requests_exception_is_unavailable(tmp_path):
    FakeGarmin.import_result = requests.ConnectionError("down")
    uploader = make_uploader(tmp_path)

    with pytest.raises(GarminUnavailableError):
        uploader.import_fit(tmp_path / "x.fit", START)


@pytest.mark.parametrize(
    "error",
    [ValueError("no json"), AttributeError("unexpected"), RuntimeError("boom")],
)
def test_unexpected_library_exception_is_import_error(tmp_path, error):
    FakeGarmin.import_result = error
    uploader = make_uploader(tmp_path)

    with pytest.raises(GarminImportError):
        uploader.import_fit(tmp_path / "x.fit", START)


@pytest.mark.parametrize(
    "result",
    [
        pytest.param(["x"], id="list"),
        pytest.param({"detailedImportResult": ["x"]}, id="detailed-is-list"),
        pytest.param({"detailedImportResult": {"successes": [42]}}, id="success-not-dict"),
        pytest.param({"detailedImportResult": {"successes": [], "failures": [7]}}, id="failure-not-dict"),
    ],
)
def test_unexpected_result_shape_is_import_error(tmp_path, result):
    FakeGarmin.import_result = result
    uploader = make_uploader(tmp_path)

    with pytest.raises(GarminImportError):
        uploader.import_fit(tmp_path / "x.fit", START)


def test_unexpected_poll_exception_still_reports_uploaded(tmp_path, caplog):
    FakeGarmin.import_result = {"detailedImportResult": {"successes": [], "failures": []}}
    uploader = make_uploader(tmp_path)
    client = uploader._logged_in_client()

    def failing_poll(startdate, enddate):
        raise ValueError("kein JSON")

    client.get_activities_by_date = failing_poll

    with caplog.at_level("WARNING"):
        outcome = uploader.import_fit(tmp_path / "x.fit", START)

    assert outcome == ImportOutcome("uploaded", None)
    assert any(r.levelname == "WARNING" for r in caplog.records)


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_fresh_session_is_import_error(tmp_path, status):
    # A session created for this very import cannot be "stale": the ride itself is refused.
    FakeGarmin.import_result = http_error("API Error", status)
    uploader = make_uploader(tmp_path)

    with pytest.raises(GarminImportError):
        uploader.import_fit(tmp_path / "x.fit", START)


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_cached_session_after_success_is_unavailable_and_dropped(tmp_path, status):
    uploader = make_uploader(tmp_path)
    assert uploader.import_fit(tmp_path / "a.fit", START) == ImportOutcome("uploaded", 123)

    FakeGarmin.import_result = http_error("API Error", status)
    with pytest.raises(GarminUnavailableError):
        uploader.import_fit(tmp_path / "b.fit", START)

    uploader.ensure_login()
    assert len(FakeGarmin.instances) == 2
