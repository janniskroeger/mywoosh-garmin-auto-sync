from pathlib import Path

import pytest

from mywhoosh_garmin_sync.config import ConfigError, OpenTriSettings, Settings

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


OPENTRI_ENV = {
    "OPENTRI_API_URL": "https://api.example.com",
    "OPENTRI_KEYCLOAK_URL": "https://auth.example.com",
}


def test_opentri_settings_absent_when_no_vars():
    assert Settings.from_env(FULL_ENV, require_garmin=True).opentri is None


def test_opentri_settings_defaults():
    s = Settings.from_env({**FULL_ENV, **OPENTRI_ENV}, require_garmin=True)

    assert s.opentri == OpenTriSettings(
        api_url="https://api.example.com",
        keycloak_url="https://auth.example.com",
        realm="open-tri",
        client_id="mywhoosh-garmin-sync",
        days=14,
        pool_length_m=25.0,
    )


def test_opentri_settings_all_values():
    env = {
        **FULL_ENV,
        **OPENTRI_ENV,
        "OPENTRI_KEYCLOAK_REALM": "r",
        "OPENTRI_CLIENT_ID": "c",
        "WORKOUT_SYNC_DAYS": "7",
        "WORKOUT_POOL_LENGTH_M": "33.3",
    }

    o = Settings.from_env(env, require_garmin=True).opentri

    assert (o.realm, o.client_id, o.days, o.pool_length_m) == ("r", "c", 7, 33.3)


@pytest.mark.parametrize("present,missing", [
    ("OPENTRI_API_URL", "OPENTRI_KEYCLOAK_URL"),
    ("OPENTRI_KEYCLOAK_URL", "OPENTRI_API_URL"),
])
def test_partial_opentri_vars_is_config_error(present, missing):
    with pytest.raises(ConfigError, match=missing):
        Settings.from_env({**FULL_ENV, present: "https://x"}, require_garmin=True)


@pytest.mark.parametrize("name,value", [
    ("WORKOUT_SYNC_DAYS", "0"),
    ("WORKOUT_SYNC_DAYS", "x"),
    ("WORKOUT_SYNC_DAYS", "1.5"),
    ("WORKOUT_POOL_LENGTH_M", "-1"),
    ("WORKOUT_POOL_LENGTH_M", "0"),
    ("WORKOUT_POOL_LENGTH_M", "nan"),
    ("WORKOUT_POOL_LENGTH_M", "x"),
])
def test_invalid_workout_values(name, value):
    with pytest.raises(ConfigError, match=name):
        Settings.from_env({**FULL_ENV, **OPENTRI_ENV, name: value}, require_garmin=True)


def test_mywhoosh_optional_when_not_required():
    s = Settings.from_env(OPENTRI_ENV, require_garmin=False, require_mywhoosh=False)

    assert s.mywhoosh_email is None and s.opentri is not None


def test_mywhoosh_workout_sync_default_off():
    assert Settings.from_env(FULL_ENV, require_garmin=True).mywhoosh_workouts is False
    s = Settings.from_env({**FULL_ENV, "MYWHOOSH_WORKOUT_SYNC": ""}, require_garmin=True)
    assert s.mywhoosh_workouts is False


@pytest.mark.parametrize("value,expected", [("true", True), ("TRUE", True), ("False", False)])
def test_mywhoosh_workout_sync_true_with_opentri(value, expected):
    env = {**FULL_ENV, **OPENTRI_ENV, "MYWHOOSH_WORKOUT_SYNC": value}

    assert Settings.from_env(env, require_garmin=True).mywhoosh_workouts is expected


def test_mywhoosh_workout_sync_true_without_opentri_is_config_error():
    env = {**FULL_ENV, "MYWHOOSH_WORKOUT_SYNC": "true"}

    with pytest.raises(
        ConfigError,
        match="MYWHOOSH_WORKOUT_SYNC=true braucht OPENTRI_API_URL und OPENTRI_KEYCLOAK_URL",
    ):
        Settings.from_env(env, require_garmin=True)


@pytest.mark.parametrize("value", ["yes", "1", "ja"])
def test_mywhoosh_workout_sync_invalid_value(value):
    env = {**FULL_ENV, **OPENTRI_ENV, "MYWHOOSH_WORKOUT_SYNC": value}

    with pytest.raises(ConfigError, match="MYWHOOSH_WORKOUT_SYNC muss true oder false sein"):
        Settings.from_env(env, require_garmin=True)
