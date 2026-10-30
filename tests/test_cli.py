import logging
import signal
from pathlib import Path

import pytest

from mywhoosh_garmin_sync import cli
from mywhoosh_garmin_sync.garmin import GarminAuthError, GarminUnavailableError
from mywhoosh_garmin_sync.mywhoosh import MyWhooshUnavailableError
from mywhoosh_garmin_sync.sync import RunSummary

ENV_NAMES = [
    "MYWHOOSH_EMAIL",
    "MYWHOOSH_PASSWORD",
    "GARMIN_EMAIL",
    "GARMIN_PASSWORD",
    "SYNC_INTERVAL_MINUTES",
    "MYWHOOSH_PAYLOAD_KEY",
    "DATA_DIR",
    "LOG_LEVEL",
]


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
