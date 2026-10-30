from pathlib import Path

import pytest

from mywhoosh_garmin_sync.config import ConfigError, Settings

FULL_ENV = {
    "MYWHOOSH_EMAIL": "mw@example.com",
    "MYWHOOSH_PASSWORD": "mw-secret",
    "GARMIN_EMAIL": "g@example.com",
    "GARMIN_PASSWORD": "g-secret",
}


def test_from_env_reads_all_values():
    env = {
        **FULL_ENV,
        "SYNC_INTERVAL_MINUTES": "5",
        "MYWHOOSH_PAYLOAD_KEY": "ABCDEF0123456789",
        "DATA_DIR": "/tmp/x",
        "LOG_LEVEL": "debug",
    }

    s = Settings.from_env(env, require_garmin=True)

    assert s == Settings(
        mywhoosh_email="mw@example.com",
        mywhoosh_password="mw-secret",
        garmin_email="g@example.com",
        garmin_password="g-secret",
        interval_minutes=5,
        payload_key="ABCDEF0123456789",
        data_dir=Path("/tmp/x"),
        log_level="DEBUG",
    )


def test_defaults():
    s = Settings.from_env(
        {"MYWHOOSH_EMAIL": "a", "MYWHOOSH_PASSWORD": "b", "GARMIN_EMAIL": "c", "GARMIN_PASSWORD": "d"},
        require_garmin=True,
    )

    assert (s.interval_minutes, s.payload_key, s.data_dir, s.log_level) == (
        15,
        "D9436E508087E863",
        Path("/data"),
        "INFO",
    )


def test_missing_values_listed_without_secrets():
    with pytest.raises(ConfigError) as e:
        Settings.from_env(
            {"MYWHOOSH_EMAIL": "a", "MYWHOOSH_PASSWORD": "geheim"}, require_garmin=True
        )

    assert "GARMIN_EMAIL" in str(e.value) and "GARMIN_PASSWORD" in str(e.value)
    assert "geheim" not in str(e.value)


def test_empty_value_counts_as_missing():
    with pytest.raises(ConfigError, match="MYWHOOSH_EMAIL"):
        Settings.from_env({**FULL_ENV, "MYWHOOSH_EMAIL": ""}, require_garmin=True)


def test_garmin_optional_for_dry_run():
    s = Settings.from_env({"MYWHOOSH_EMAIL": "a", "MYWHOOSH_PASSWORD": "b"}, require_garmin=False)

    assert s.garmin_email is None and s.garmin_password is None


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1.5"])
def test_invalid_interval(value):
    with pytest.raises(ConfigError, match="SYNC_INTERVAL_MINUTES"):
        Settings.from_env({**FULL_ENV, "SYNC_INTERVAL_MINUTES": value}, require_garmin=True)


def test_invalid_log_level():
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        Settings.from_env({**FULL_ENV, "LOG_LEVEL": "LOUD"}, require_garmin=True)
