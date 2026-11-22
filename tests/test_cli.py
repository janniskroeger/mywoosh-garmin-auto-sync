import json
import logging
import signal
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from mywhoosh_garmin_sync import cli
from mywhoosh_garmin_sync.garmin import GarminAuthError, GarminUnavailableError
from mywhoosh_garmin_sync.mywhoosh import MyWhooshUnavailableError
from mywhoosh_garmin_sync.mywhoosh_workout_sync import MyWhooshWorkoutRunSummary
from mywhoosh_garmin_sync.opentri import (
    OpenTriAuthError,
    OpenTriExportError,
    OpenTriUnavailableError,
    PlannedBike,
)
from mywhoosh_garmin_sync.sync import RunSummary
from mywhoosh_garmin_sync.workout_sync import WorkoutRunSummary
from workoutfactory import make_bundle, make_schedule_fit, make_workout_fit

ENV_NAMES = [
    "MYWHOOSH_EMAIL",
    "MYWHOOSH_PASSWORD",
    "GARMIN_EMAIL",
    "GARMIN_PASSWORD",
    "SYNC_INTERVAL_MINUTES",
    "MYWHOOSH_PAYLOAD_KEY",
    "DATA_DIR",
    "LOG_LEVEL",
    "OPENTRI_API_URL",
    "OPENTRI_KEYCLOAK_URL",
    "OPENTRI_KEYCLOAK_REALM",
    "OPENTRI_CLIENT_ID",
    "WORKOUT_SYNC_DAYS",
    "WORKOUT_POOL_LENGTH_M",
    "MYWHOOSH_WORKOUT_SYNC",
]


_REAL_CONFIGURE_LOGGING = cli._configure_logging


@pytest.fixture(autouse=True)
def keep_test_logging(monkeypatch):
    # _configure_logging uses force=True and would remove pytest's caplog handler;
    # it is tested on its own in test_configure_logging_overrides_setup_done_by_libraries.
    monkeypatch.setattr(cli, "_configure_logging", lambda level: None)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MYWHOOSH_EMAIL", "mw@example.com")
    monkeypatch.setenv("MYWHOOSH_PASSWORD", "mw-secret")
    monkeypatch.setenv("GARMIN_EMAIL", "g@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "g-secret")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def run_once_calls(monkeypatch):
    calls = []

    def fake_run_once(source, garmin, store, work_dir):
        calls.append((source, garmin, store, work_dir))
        return RunSummary()

    monkeypatch.setattr(cli, "run_once", fake_run_once)
    return calls


def test_once_runs_single_sync_and_returns_0(data_dir, run_once_calls):
    assert cli.main(["once"]) == 0

    assert len(run_once_calls) == 1
    _, _, store, work_dir = run_once_calls[0]
    assert work_dir == data_dir / "work"


def test_once_returns_1_when_aborted(data_dir, monkeypatch):
    monkeypatch.setattr(cli, "run_once", lambda *a: RunSummary(aborted="x"))

    assert cli.main(["once"]) == 1


def test_once_returns_1_on_unexpected_exception(data_dir, monkeypatch, caplog):
    def boom(*a):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(cli, "run_once", boom)

    with caplog.at_level(logging.ERROR):
        assert cli.main(["once"]) == 1

    assert "Unerwarteter Fehler" in caplog.text


def test_run_is_the_default_command(data_dir, monkeypatch, run_once_calls):
    def stop(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", stop)

    assert cli.main([]) == 0
    assert len(run_once_calls) == 1


def test_run_loops_and_survives_exceptions(data_dir, monkeypatch, caplog):
    instances = []
    outcomes = [RuntimeError("kaputt"), RunSummary()]

    def fake_run_once(source, garmin, store, work_dir):
        instances.append((source, garmin, store))
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_once", fake_run_once)
    monkeypatch.setattr(cli.time, "sleep", fake_sleep)

    with caplog.at_level(logging.ERROR):
        assert cli.main(["run"]) == 0

    assert sleeps == [900, 900]
    assert len(instances) == 2
    # The same client, uploader and store serve every run (login pause must survive).
    assert all(a is b for a, b in zip(instances[0], instances[1]))
    assert "Unerwarteter Fehler" in caplog.text


def test_run_uses_configured_interval(data_dir, monkeypatch, run_once_calls):
    monkeypatch.setenv("SYNC_INTERVAL_MINUTES", "5")
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)

    cli.main(["run"])

    assert sleeps == [300]


def test_run_restores_sigterm_handler_and_sigterm_stops_loop(data_dir, monkeypatch):
    before = signal.getsignal(signal.SIGTERM)

    def fake_run_once(*a):
        signal.raise_signal(signal.SIGTERM)
        return RunSummary()

    monkeypatch.setattr(cli, "run_once", fake_run_once)
    monkeypatch.setattr(cli.time, "sleep", lambda s: pytest.fail("loop must end on SIGTERM"))

    assert cli.main(["run"]) == 0
    assert signal.getsignal(signal.SIGTERM) == before


def test_dry_run_does_not_need_garmin(data_dir, monkeypatch, capsys):
    monkeypatch.delenv("GARMIN_EMAIL")
    monkeypatch.delenv("GARMIN_PASSWORD")
    calls = []

    def fake_dry_run(source, out_dir, file_id=None):
        calls.append((out_dir, file_id))
        return out_dir / "7.original.fit", out_dir / "7.edge1050.fit"

    monkeypatch.setattr(cli, "dry_run", fake_dry_run)

    assert cli.main(["dry-run", "--file-id", "abc"]) == 0

    assert calls == [(data_dir / "dry-run", "abc")]
    out = capsys.readouterr().out.splitlines()
    assert out == [
        str(data_dir / "dry-run" / "7.original.fit"),
        str(data_dir / "dry-run" / "7.edge1050.fit"),
    ]


def test_dry_run_defaults_to_newest_ride(data_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli, "dry_run", lambda source, out_dir, file_id=None: calls.append(file_id) or (Path("a"), Path("b"))
    )

    assert cli.main(["dry-run"]) == 0
    assert calls == [None]


def test_dry_run_unknown_file_id_exits_1(data_dir, monkeypatch, capsys):
    def fake_dry_run(source, out_dir, file_id=None):
        raise ValueError("Fahrt zzz steht nicht in der MyWhoosh-Liste")

    monkeypatch.setattr(cli, "dry_run", fake_dry_run)

    assert cli.main(["dry-run", "--file-id", "zzz"]) == 1

    captured = capsys.readouterr()
    assert "zzz" in captured.err and captured.out == ""


def test_dry_run_mywhoosh_error_exits_1_without_traceback(data_dir, monkeypatch, capsys):
    def fake_dry_run(source, out_dir, file_id=None):
        raise MyWhooshUnavailableError("MyWhoosh nicht erreichbar")

    monkeypatch.setattr(cli, "dry_run", fake_dry_run)

    assert cli.main(["dry-run"]) == 1

    err = capsys.readouterr().err
    assert "MyWhoosh nicht erreichbar" in err and "Traceback" not in err


def test_login_garmin_calls_interactive_login(data_dir, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "interactive_login", lambda *a: calls.append(a))

    assert cli.main(["login-garmin"]) == 0

    assert calls == [("g@example.com", "g-secret", data_dir / "garmin")]
    assert "erfolgreich" in capsys.readouterr().out


@pytest.mark.parametrize("error", [GarminAuthError("abgelehnt"), GarminUnavailableError("offline")])
def test_login_garmin_failure_exits_1(data_dir, monkeypatch, capsys, error):
    def fail(*a):
        raise error

    monkeypatch.setattr(cli, "interactive_login", fail)

    assert cli.main(["login-garmin"]) == 1

    assert str(error) in capsys.readouterr().err


def test_config_error_exits_2_with_message(data_dir, monkeypatch, capsys):
    monkeypatch.delenv("GARMIN_PASSWORD")

    assert cli.main(["once"]) == 2

    err = capsys.readouterr().err
    assert "GARMIN_PASSWORD" in err and "g-secret" not in err


def test_data_dir_is_created(tmp_path, data_dir, monkeypatch, run_once_calls):
    target = tmp_path / "nested" / "data"
    monkeypatch.setenv("DATA_DIR", str(target))

    assert cli.main(["once"]) == 0
    assert target.is_dir()


@pytest.fixture
def root_logger():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield root
    root.handlers[:] = handlers
    root.setLevel(level)


def test_configure_logging_overrides_setup_done_by_libraries(root_logger):
    # fit-tool calls logging.basicConfig(format="%(asctime)s %(message)s") on import,
    # which leaves the root logger at WARNING with its own handler.
    root_logger.handlers[:] = []
    logging.basicConfig(format="%(asctime)s %(message)s")

    _REAL_CONFIGURE_LOGGING("INFO")

    assert root_logger.level == logging.INFO
    assert [h.formatter._fmt for h in root_logger.handlers] == [
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ]


@pytest.fixture
def opentri_env(monkeypatch):
    monkeypatch.setenv("OPENTRI_API_URL", "https://api.example.com")
    monkeypatch.setenv("OPENTRI_KEYCLOAK_URL", "https://auth.example.com")


@pytest.fixture
def sync_workouts_calls(monkeypatch):
    calls = []

    def fake(source, garmin, store, today, days, pool_length_m, now):
        calls.append((source, garmin, store, today, days, pool_length_m))
        return WorkoutRunSummary()

    monkeypatch.setattr(cli, "sync_workouts", fake)
    return calls


def _stop_after(monkeypatch, intervals):
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == intervals:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    return sleeps


def test_run_calls_both_steps_each_interval(
    data_dir, opentri_env, monkeypatch, run_once_calls, sync_workouts_calls
):
    monkeypatch.setenv("WORKOUT_SYNC_DAYS", "7")
    monkeypatch.setenv("WORKOUT_POOL_LENGTH_M", "50")
    _stop_after(monkeypatch, 2)

    assert cli.main(["run"]) == 0

    assert len(run_once_calls) == 2 and len(sync_workouts_calls) == 2
    source, garmin, store, today, days, pool = sync_workouts_calls[0]
    assert (days, pool) == (7, 50.0)
    assert isinstance(today, date)
    # the workout step shares the activity uploader and the state survives between runs
    assert garmin._uploader is run_once_calls[0][1]
    assert sync_workouts_calls[1][:3] == (source, garmin, store)
    assert store._path == data_dir / "workouts.json"


def test_workout_step_skipped_without_opentri(
    data_dir, monkeypatch, run_once_calls, sync_workouts_calls
):
    _stop_after(monkeypatch, 1)

    assert cli.main(["run"]) == 0

    assert len(run_once_calls) == 1 and sync_workouts_calls == []


def test_workout_step_failure_does_not_stop_activity_step(
    data_dir, opentri_env, monkeypatch, run_once_calls, caplog
):
    def boom(*a):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(cli, "sync_workouts", boom)
    _stop_after(monkeypatch, 2)

    with caplog.at_level(logging.ERROR):
        assert cli.main(["run"]) == 0

    assert len(run_once_calls) == 2
    assert "Trainings-Abgleich" in caplog.text


def test_activity_step_failure_does_not_stop_workout_step(
    data_dir, opentri_env, monkeypatch, sync_workouts_calls
):
    def boom(*a):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(cli, "run_once", boom)
    _stop_after(monkeypatch, 1)

    assert cli.main(["run"]) == 0

    assert len(sync_workouts_calls) == 1


def test_once_runs_both_steps(data_dir, opentri_env, run_once_calls, sync_workouts_calls):
    assert cli.main(["once"]) == 0

    assert len(run_once_calls) == 1 and len(sync_workouts_calls) == 1


@pytest.mark.parametrize("activity_aborted,workout_aborted", [(True, False), (False, True)])
def test_once_returns_1_if_either_aborted(
    data_dir, opentri_env, monkeypatch, activity_aborted, workout_aborted
):
    monkeypatch.setattr(
        cli, "run_once", lambda *a: RunSummary(aborted="x" if activity_aborted else None)
    )
    monkeypatch.setattr(
        cli, "sync_workouts", lambda *a: WorkoutRunSummary(aborted="y" if workout_aborted else None)
    )

    assert cli.main(["once"]) == 1


def test_once_returns_1_if_workout_step_raises_but_still_runs_activity(
    data_dir, opentri_env, monkeypatch, run_once_calls
):
    def boom(*a):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(cli, "sync_workouts", boom)

    assert cli.main(["once"]) == 1
    assert len(run_once_calls) == 1


def test_login_opentri_calls_device_login(data_dir, opentri_env, monkeypatch, capsys):
    monkeypatch.delenv("MYWHOOSH_EMAIL")
    monkeypatch.delenv("GARMIN_EMAIL")
    shown = []
    monkeypatch.setattr(cli.OpenTriClient, "device_login", lambda self, show: show("Link") or shown.append(show))

    assert cli.main(["login-opentri"]) == 0

    assert shown == [print]
    out = capsys.readouterr().out
    assert "Link" in out and "erfolgreich" in out


def test_login_opentri_failure_exits_1(data_dir, opentri_env, monkeypatch, capsys):
    def fail(self, show):
        raise OpenTriAuthError("abgelehnt")

    monkeypatch.setattr(cli.OpenTriClient, "device_login", fail)

    assert cli.main(["login-opentri"]) == 1
    assert "abgelehnt" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["login-opentri", "workouts-dry-run"])
def test_opentri_commands_exit_2_when_not_configured(data_dir, capsys, command):
    assert cli.main([command]) == 2

    assert "OPENTRI_API_URL" in capsys.readouterr().err


def test_partial_opentri_config_exits_2(data_dir, monkeypatch, capsys):
    monkeypatch.setenv("OPENTRI_API_URL", "https://api.example.com")

    assert cli.main(["workouts-dry-run"]) == 2
    assert "OPENTRI_KEYCLOAK_URL" in capsys.readouterr().err


def test_workouts_dry_run_writes_json_without_garmin(data_dir, opentri_env, monkeypatch, capsys):
    for name in ("MYWHOOSH_EMAIL", "MYWHOOSH_PASSWORD", "GARMIN_EMAIL", "GARMIN_PASSWORD"):
        monkeypatch.delenv(name)
    steps = [{"name": "A", "intensity": "active", "duration_type": "time", "duration_time": 600}]
    bundle = make_bundle({
        "a.fit": make_workout_fit(42, "Lauf", "running", steps),
        "Schedules.fit": make_schedule_fit({42: date(2026, 11, 10)}),
    })
    requested = []
    monkeypatch.setattr(
        cli.OpenTriClient, "fetch_bundle", lambda self, start, end: requested.append((start, end)) or bundle
    )
    monkeypatch.setattr(cli, "GarminUploader", lambda *a, **k: pytest.fail("Garmin must not be touched"))

    assert cli.main(["workouts-dry-run"]) == 0

    path = data_dir / "workouts-dry-run" / "2026-11-10-42.json"
    assert capsys.readouterr().out.splitlines() == [str(path)]
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["workoutName"]
    assert path.read_text(encoding="utf-8").startswith("{\n")
    start, end = requested[0]
    assert (end - start).days == 13


def test_workouts_dry_run_without_plan_exits_0(data_dir, opentri_env, monkeypatch, capsys):
    monkeypatch.setattr(cli.OpenTriClient, "fetch_bundle", lambda self, start, end: None)

    assert cli.main(["workouts-dry-run"]) == 0

    assert "Keine geplanten Trainings" in capsys.readouterr().out
    assert not (data_dir / "workouts-dry-run").exists()


def test_workouts_dry_run_opentri_error_exits_1(data_dir, opentri_env, monkeypatch, capsys):
    def fail(self, start, end):
        raise OpenTriAuthError("Anmeldung abgelaufen")

    monkeypatch.setattr(cli.OpenTriClient, "fetch_bundle", fail)

    assert cli.main(["workouts-dry-run"]) == 1
    assert "Anmeldung abgelaufen" in capsys.readouterr().err


def test_startup_logs_workout_sync_active_with_window(
    data_dir, opentri_env, monkeypatch, caplog, run_once_calls, sync_workouts_calls
):
    monkeypatch.setenv("WORKOUT_SYNC_DAYS", "7")
    _stop_after(monkeypatch, 1)

    with caplog.at_level(logging.INFO):
        assert cli.main(["run"]) == 0

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("Trainings-Abgleich mit open-tri aktiv" in m and "7 Tage" in m for m in messages)


def test_startup_logs_workout_sync_off_without_opentri(
    data_dir, monkeypatch, caplog, run_once_calls, sync_workouts_calls
):
    _stop_after(monkeypatch, 1)

    with caplog.at_level(logging.INFO):
        assert cli.main(["run"]) == 0

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("Trainings-Abgleich mit open-tri aus" in m and "OPENTRI_" in m for m in messages)


@pytest.fixture
def mywhoosh_env(opentri_env, monkeypatch):
    monkeypatch.setenv("MYWHOOSH_WORKOUT_SYNC", "true")


@pytest.fixture
def mywhoosh_sync_calls(monkeypatch):
    calls = []

    def fake(source, library, store, today, days, now):
        calls.append((source, library, store, today, days))
        return MyWhooshWorkoutRunSummary()

    monkeypatch.setattr(cli, "sync_mywhoosh_workouts", fake)
    return calls


def test_once_runs_mywhoosh_step_after_garmin_step(
    data_dir, mywhoosh_env, monkeypatch, run_once_calls
):
    order = []
    monkeypatch.setattr(cli, "run_once", lambda *a: order.append("activity") or RunSummary())
    monkeypatch.setattr(
        cli, "sync_workouts", lambda *a: order.append("garmin") or WorkoutRunSummary()
    )
    monkeypatch.setattr(
        cli,
        "sync_mywhoosh_workouts",
        lambda *a: order.append("mywhoosh") or MyWhooshWorkoutRunSummary(),
    )

    assert cli.main(["once"]) == 0

    assert order == ["activity", "garmin", "mywhoosh"]


def test_run_passes_shared_client_store_and_window(
    data_dir, mywhoosh_env, monkeypatch, run_once_calls, sync_workouts_calls, mywhoosh_sync_calls
):
    monkeypatch.setenv("WORKOUT_SYNC_DAYS", "7")
    _stop_after(monkeypatch, 2)

    assert cli.main(["run"]) == 0

    assert len(mywhoosh_sync_calls) == 2
    _source, library, store, today, days = mywhoosh_sync_calls[0]
    assert days == 7 and isinstance(today, date)
    assert library._auth._client is run_once_calls[0][0]
    assert store._path == data_dir / "mywhoosh_workouts.json"


def test_mywhoosh_step_failure_does_not_stop_other_steps(
    data_dir, mywhoosh_env, monkeypatch, run_once_calls, sync_workouts_calls
):
    def boom(*a):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(cli, "sync_mywhoosh_workouts", boom)

    assert cli.main(["once"]) == 1
    assert len(run_once_calls) == 1 and len(sync_workouts_calls) == 1


def test_mywhoosh_step_abort_returns_1_but_unconfirmed_does_not(
    data_dir, mywhoosh_env, monkeypatch, run_once_calls, sync_workouts_calls
):
    monkeypatch.setattr(
        cli, "sync_mywhoosh_workouts", lambda *a: MyWhooshWorkoutRunSummary(unconfirmed=True)
    )
    assert cli.main(["once"]) == 0
    monkeypatch.setattr(
        cli, "sync_mywhoosh_workouts", lambda *a: MyWhooshWorkoutRunSummary(aborted="x")
    )
    assert cli.main(["once"]) == 1


def test_garmin_step_failure_does_not_stop_mywhoosh_step(
    data_dir, mywhoosh_env, monkeypatch, run_once_calls, mywhoosh_sync_calls
):
    def boom(*a):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(cli, "sync_workouts", boom)

    assert cli.main(["once"]) == 1
    assert len(mywhoosh_sync_calls) == 1


def test_mywhoosh_step_off_without_switch(
    data_dir, opentri_env, monkeypatch, caplog, run_once_calls, sync_workouts_calls, mywhoosh_sync_calls
):
    _stop_after(monkeypatch, 1)

    with caplog.at_level(logging.INFO):
        assert cli.main(["run"]) == 0

    assert mywhoosh_sync_calls == []
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("MyWhoosh-Trainings-Abgleich aus" in m for m in messages)


def test_startup_logs_mywhoosh_step_active(
    data_dir, mywhoosh_env, monkeypatch, caplog, run_once_calls, sync_workouts_calls, mywhoosh_sync_calls
):
    _stop_after(monkeypatch, 1)

    with caplog.at_level(logging.INFO):
        assert cli.main(["run"]) == 0

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("MyWhoosh-Trainings-Abgleich aktiv" in m for m in messages)


def test_mywhoosh_step_uses_berlin_date(
    data_dir, mywhoosh_env, monkeypatch, run_once_calls, sync_workouts_calls, mywhoosh_sync_calls
):
    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 8, 23, 30, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(cli, "datetime", FakeDatetime)

    assert cli.main(["once"]) == 0

    assert mywhoosh_sync_calls[0][3] == date(2026, 11, 9)


_ZWO = (
    b'<workout_file><name>Sweet Spot</name><workout>'
    b'<SteadyState Duration="600" Power="0.9"/></workout></workout_file>'
)


def _bike(planned_id, day, training_id):
    return PlannedBike(planned_id, day, None, training_id, "Sweet Spot", 3)


def test_mywhoosh_workouts_dry_run_writes_json_files(data_dir, opentri_env, monkeypatch, capsys):
    for name in ("MYWHOOSH_EMAIL", "MYWHOOSH_PASSWORD", "GARMIN_EMAIL", "GARMIN_PASSWORD"):
        monkeypatch.delenv(name)
    requested = []
    bikes = [_bike("p1", date(2026, 11, 10), "t1"), _bike("p2", date(2026, 11, 11), "t2")]
    monkeypatch.setattr(
        cli.OpenTriClient,
        "fetch_planned_bikes",
        lambda self, start, end: requested.append((start, end)) or bikes,
    )

    def fetch_zwo(self, training_id):
        if training_id == "t2":
            raise OpenTriExportError("FTP fehlt")
        return _ZWO

    monkeypatch.setattr(cli.OpenTriClient, "fetch_zwo", fetch_zwo)
    monkeypatch.setattr(
        cli, "MyWhooshClient", lambda *a, **k: pytest.fail("MyWhoosh must not be touched")
    )

    assert cli.main(["mywhoosh-workouts-dry-run"]) == 0

    out_dir = data_dir / "mywhoosh-workouts-dry-run"
    path = out_dir / "2026-11-10-p1.json"
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [str(path)]
    assert "p2" in captured.err and "FTP fehlt" in captured.err
    assert not (out_dir / "2026-11-11-p2.json").exists()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("{\n")
    payload = json.loads(text)
    assert payload["Name"].endswith("Sweet Spot") and "W3" in payload["Name"]
    start, end = requested[0]
    assert (end - start).days == 13


def test_mywhoosh_workouts_dry_run_skips_unreadable_zwo(data_dir, opentri_env, monkeypatch, capsys):
    monkeypatch.setattr(
        cli.OpenTriClient,
        "fetch_planned_bikes",
        lambda self, s, e: [_bike("p1", date(2026, 11, 10), "t1")],
    )
    monkeypatch.setattr(cli.OpenTriClient, "fetch_zwo", lambda self, training_id: b"<nope")

    assert cli.main(["mywhoosh-workouts-dry-run"]) == 0

    assert "p1" in capsys.readouterr().err


@pytest.mark.parametrize("result", [None, []])
def test_mywhoosh_workouts_dry_run_without_sessions_exits_0(
    data_dir, opentri_env, monkeypatch, capsys, result
):
    monkeypatch.setattr(cli.OpenTriClient, "fetch_planned_bikes", lambda self, s, e: result)

    assert cli.main(["mywhoosh-workouts-dry-run"]) == 0

    assert "Keine geplanten Radtrainings" in capsys.readouterr().out
    assert not (data_dir / "mywhoosh-workouts-dry-run").exists()


def test_mywhoosh_workouts_dry_run_opentri_error_exits_1(
    data_dir, opentri_env, monkeypatch, capsys
):
    def fail(self, start, end):
        raise OpenTriAuthError("Anmeldung abgelaufen")

    monkeypatch.setattr(cli.OpenTriClient, "fetch_planned_bikes", fail)

    assert cli.main(["mywhoosh-workouts-dry-run"]) == 1
    assert "Anmeldung abgelaufen" in capsys.readouterr().err


def test_mywhoosh_workouts_dry_run_exits_2_when_not_configured(data_dir, capsys):
    assert cli.main(["mywhoosh-workouts-dry-run"]) == 2


@pytest.mark.parametrize("error", [OpenTriUnavailableError("open-tri nicht erreichbar"), OpenTriAuthError("Anmeldung abgelaufen")])
def test_mywhoosh_workouts_dry_run_fetch_zwo_error_exits_1(
    data_dir, opentri_env, monkeypatch, capsys, error
):
    monkeypatch.setattr(
        cli.OpenTriClient,
        "fetch_planned_bikes",
        lambda self, s, e: [_bike("p1", date(2026, 11, 10), "t1")],
    )

    def fail(self, training_id):
        raise error

    monkeypatch.setattr(cli.OpenTriClient, "fetch_zwo", fail)

    assert cli.main(["mywhoosh-workouts-dry-run"]) == 1
    err = capsys.readouterr().err
    assert "MyWhoosh-Trainings-Dry-Run fehlgeschlagen" in err and str(error) in err
