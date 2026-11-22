"""Command line entry point: continuous sync, single run, dry run and Garmin login."""

import argparse
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import FrameType
from zoneinfo import ZoneInfo

from mywhoosh_garmin_sync.config import ConfigError, OpenTriSettings, Settings
from mywhoosh_garmin_sync.garmin import (
    GarminAuthError,
    GarminUnavailableError,
    GarminUploader,
    interactive_login,
)
from mywhoosh_garmin_sync.garmin_workouts import GarminWorkouts
from mywhoosh_garmin_sync.mywhoosh import MyWhooshClient, MyWhooshError
from mywhoosh_garmin_sync.mywhoosh_workout_json import (
    ZwoError,
    parse_zwo,
    to_mywhoosh_json,
    workout_name,
)
from mywhoosh_garmin_sync.mywhoosh_workout_state import MyWhooshWorkoutStateStore
from mywhoosh_garmin_sync.mywhoosh_workout_sync import sync_mywhoosh_workouts
from mywhoosh_garmin_sync.mywhoosh_workouts import AppCoachingAuth, MyWhooshWorkouts
from mywhoosh_garmin_sync.opentri import OpenTriClient, OpenTriError, OpenTriExportError
from mywhoosh_garmin_sync.state import StateStore
from mywhoosh_garmin_sync.sync import dry_run, run_once
from mywhoosh_garmin_sync.workout_fit import read_bundle_report
from mywhoosh_garmin_sync.workout_json import to_garmin_json
from mywhoosh_garmin_sync.workout_state import WorkoutStateStore
from mywhoosh_garmin_sync.workout_sync import sync_workouts

logger = logging.getLogger(__name__)

_TIMEZONE = ZoneInfo("Europe/Berlin")
_OPENTRI_COMMANDS = ("login-opentri", "workouts-dry-run", "mywhoosh-workouts-dry-run")

_QUIET_LOGGERS = ("garminconnect", "urllib3", "requests")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    command = args.command or "run"
    opentri_only = command in _OPENTRI_COMMANDS

    try:
        settings = Settings.from_env(
            os.environ,
            require_garmin=command not in ("dry-run", *_OPENTRI_COMMANDS),
            require_mywhoosh=not opentri_only,
        )
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2

    _configure_logging(settings.log_level)
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    if opentri_only:
        if settings.opentri is None:
            print(
                "Konfigurationsfehler: open-tri ist nicht konfiguriert, "
                "OPENTRI_API_URL und OPENTRI_KEYCLOAK_URL fehlen",
                file=sys.stderr,
            )
            return 2
        client = _opentri_client(settings.opentri, settings.data_dir)
        if command == "login-opentri":
            return _login_opentri(client)
        if command == "mywhoosh-workouts-dry-run":
            return _mywhoosh_workouts_dry_run(client, settings.opentri, settings.data_dir)
        return _workouts_dry_run(client, settings.opentri, settings.data_dir)

    if command == "login-garmin":
        return _login_garmin(settings)

    assert settings.mywhoosh_email is not None and settings.mywhoosh_password is not None
    source = MyWhooshClient(
        settings.mywhoosh_email,
        settings.mywhoosh_password,
        settings.data_dir,
        settings.payload_key,
    )
    if command == "dry-run":
        return _dry_run(source, settings, args.file_id)

    assert settings.garmin_email is not None and settings.garmin_password is not None
    garmin = GarminUploader(
        settings.garmin_email, settings.garmin_password, settings.data_dir / "garmin"
    )
    store = StateStore(settings.data_dir / "state.json")
    work_dir = settings.data_dir / "work"
    workouts = (
        _WorkoutStep(
            _opentri_client(settings.opentri, settings.data_dir),
            GarminWorkouts(garmin),
            WorkoutStateStore(settings.data_dir / "workouts.json"),
            settings.opentri,
        )
        if settings.opentri
        else None
    )
    mywhoosh_workouts = (
        _MyWhooshWorkoutStep(
            _opentri_client(settings.opentri, settings.data_dir),
            MyWhooshWorkouts(AppCoachingAuth(source)),
            MyWhooshWorkoutStateStore(settings.data_dir / "mywhoosh_workouts.json"),
            settings.opentri,
        )
        if settings.mywhoosh_workouts and settings.opentri
        else None
    )
    if command == "once":
        return _once(source, garmin, store, work_dir, workouts, mywhoosh_workouts)
    return _run_forever(
        source, garmin, store, work_dir, settings.interval_minutes, workouts, mywhoosh_workouts
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mywhoosh-garmin-sync",
        description="Überträgt MyWhoosh-Fahrten nach Garmin Connect.",
    )
    commands = parser.add_subparsers(dest="command", metavar="BEFEHL")
    commands.add_parser("run", help="dauerhaft synchronisieren (Standard)")
    commands.add_parser("once", help="einen Lauf ausführen und beenden")
    dry = commands.add_parser(
        "dry-run", help="eine Fahrt herunterladen und umschreiben, ohne Upload"
    )
    dry.add_argument("--file-id", help="activityFileId der Fahrt (Standard: die neueste)")
    commands.add_parser("login-garmin", help="interaktive Garmin-Anmeldung (MFA)")
    commands.add_parser("login-opentri", help="einmalige open-tri-Anmeldung per Geräte-Login")
    commands.add_parser(
        "workouts-dry-run",
        help="geplante open-tri-Trainings holen und als Garmin-JSON ablegen, ohne Garmin zu berühren",
    )
    commands.add_parser(
        "mywhoosh-workouts-dry-run",
        help="geplante Radtrainings holen und als MyWhoosh-JSON ablegen, ohne MyWhoosh zu berühren",
    )
    return parser


def _configure_logging(level: str) -> None:
    # force=True: fit-tool already calls logging.basicConfig on import, which would
    # otherwise turn this call into a no-op and leave the root logger at WARNING.
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True
    )
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def _login_garmin(settings: Settings) -> int:
    assert settings.garmin_email is not None and settings.garmin_password is not None
    try:
        interactive_login(
            settings.garmin_email, settings.garmin_password, settings.data_dir / "garmin"
        )
    except (GarminAuthError, GarminUnavailableError) as exc:
        print(f"Garmin-Anmeldung fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    print("Garmin-Anmeldung erfolgreich, die Tokens sind gespeichert.")
    return 0


def _dry_run(source: MyWhooshClient, settings: Settings, file_id: str | None) -> int:
    try:
        original, patched = dry_run(source, settings.data_dir / "dry-run", file_id)
    except (ValueError, MyWhooshError) as exc:
        print(f"Dry-Run fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    print(original)
    print(patched)
    return 0


@dataclass
class _WorkoutStep:
    """The planned-workout half of a run; shares the Garmin login with the activity sync."""

    source: OpenTriClient
    garmin: GarminWorkouts
    store: WorkoutStateStore
    settings: OpenTriSettings

    def run(self) -> bool:
        """Run one reconciliation; True when it aborted or failed. Never raises."""
        try:
            summary = sync_workouts(
                self.source,
                self.garmin,
                self.store,
                datetime.now(_TIMEZONE).date(),
                self.settings.days,
                self.settings.pool_length_m,
                lambda: datetime.now(_TIMEZONE),
            )
        except Exception:
            logger.exception("Unerwarteter Fehler im Trainings-Abgleich")
            return True
        return summary.aborted is not None


@dataclass
class _MyWhooshWorkoutStep:
    """The planned-bike half of a run; shares the MyWhoosh login with the activity sync."""

    source: OpenTriClient
    library: MyWhooshWorkouts
    store: MyWhooshWorkoutStateStore
    settings: OpenTriSettings

    def run(self) -> bool:
        """Run one reconciliation; True when it aborted or failed. Never raises."""
        try:
            summary = sync_mywhoosh_workouts(
                self.source,
                self.library,
                self.store,
                datetime.now(_TIMEZONE).date(),
                self.settings.days,
                lambda: datetime.now(_TIMEZONE),
            )
        except Exception:
            logger.exception("Unerwarteter Fehler im MyWhoosh-Trainings-Abgleich")
            return True
        return summary.aborted is not None


def _opentri_client(settings: OpenTriSettings, data_dir: Path) -> OpenTriClient:
    return OpenTriClient(
        settings.api_url, settings.keycloak_url, settings.realm, settings.client_id, data_dir
    )


def _login_opentri(client: OpenTriClient) -> int:
    try:
        client.device_login(print)
    except OpenTriError as exc:
        print(f"open-tri-Anmeldung fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    print("open-tri-Anmeldung erfolgreich, das Token ist gespeichert.")
    return 0


def _workouts_dry_run(client: OpenTriClient, settings: OpenTriSettings, data_dir: Path) -> int:
    today = datetime.now(_TIMEZONE).date()
    try:
        bundle = client.fetch_bundle(today, today + timedelta(days=settings.days - 1))
        report = read_bundle_report(bundle) if bundle is not None else None
    except (OpenTriError, ValueError) as exc:
        print(f"Trainings-Dry-Run fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    if report is None or not report.planned:
        print("Keine geplanten Trainings im Zeitraum (kein aktiver Plan oder leerer Export).")
        return 0
    out_dir = data_dir / "workouts-dry-run"
    out_dir.mkdir(parents=True, exist_ok=True)
    for planned in report.planned:
        path = out_dir / f"{planned.date.isoformat()}-{planned.workout.serial}.json"
        payload = to_garmin_json(planned.workout, settings.pool_length_m)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(path)
    if report.unreadable:
        print(
            f"{len(report.unreadable)} Training(s) waren nicht lesbar und wurden übersprungen.",
            file=sys.stderr,
        )
    return 0


def _mywhoosh_workouts_dry_run(
    client: OpenTriClient, settings: OpenTriSettings, data_dir: Path
) -> int:
    today = datetime.now(_TIMEZONE).date()
    try:
        bikes = client.fetch_planned_bikes(today, today + timedelta(days=settings.days - 1))
    except OpenTriError as exc:
        print(f"MyWhoosh-Trainings-Dry-Run fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    if not bikes:
        print("Keine geplanten Radtrainings im Zeitraum (kein aktiver Plan oder keine Radeinheit).")
        return 0
    out_dir = data_dir / "mywhoosh-workouts-dry-run"
    for bike in bikes:
        try:
            zwo = parse_zwo(client.fetch_zwo(bike.training_id))
            name = workout_name(bike.day, bike.week, bike.brick_order, bike.training_name)
            payload = to_mywhoosh_json(zwo, name, bike.planned_id)
        except (OpenTriExportError, ZwoError) as exc:
            print(
                f"Übersprungen: {bike.day.isoformat()} {bike.training_name} "
                f"({bike.planned_id}): {exc}",
                file=sys.stderr,
            )
            continue
        except OpenTriError as exc:
            print(f"MyWhoosh-Trainings-Dry-Run fehlgeschlagen: {exc}", file=sys.stderr)
            return 1
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{bike.day.isoformat()}-{bike.planned_id}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(path)
    return 0


def _once(
    source: MyWhooshClient,
    garmin: GarminUploader,
    store: StateStore,
    work_dir: Path,
    workouts: _WorkoutStep | None = None,
    mywhoosh_workouts: _MyWhooshWorkoutStep | None = None,
) -> int:
    failed = False
    try:
        summary = run_once(source, garmin, store, work_dir)
        failed = bool(summary.aborted)
    except Exception:
        logger.exception("Unerwarteter Fehler im Lauf")
        failed = True
    if workouts is not None and workouts.run():
        failed = True
    if mywhoosh_workouts is not None and mywhoosh_workouts.run():
        failed = True
    return 1 if failed else 0


def _run_forever(
    source: MyWhooshClient,
    garmin: GarminUploader,
    store: StateStore,
    work_dir: Path,
    interval_minutes: int,
    workouts: _WorkoutStep | None = None,
    mywhoosh_workouts: _MyWhooshWorkoutStep | None = None,
) -> int:
    previous_handler = signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    logger.info("Starte Synchronisation, Intervall %d Minuten", interval_minutes)
    if workouts is not None:
        logger.info(
            "Trainings-Abgleich mit open-tri aktiv, Zeitfenster %d Tage", workouts.settings.days
        )
    else:
        logger.info("Trainings-Abgleich mit open-tri aus, OPENTRI_* ist nicht gesetzt")
    if mywhoosh_workouts is not None:
        logger.info(
            "MyWhoosh-Trainings-Abgleich aktiv, Zeitfenster %d Tage", mywhoosh_workouts.settings.days
        )
    else:
        logger.info("MyWhoosh-Trainings-Abgleich aus, MYWHOOSH_WORKOUT_SYNC ist nicht true")
    try:
        while True:
            try:
                run_once(source, garmin, store, work_dir)
            except Exception:
                logger.exception("Unerwarteter Fehler im Lauf, nächster Versuch nach dem Intervall")
            if workouts is not None:
                workouts.run()
            if mywhoosh_workouts is not None:
                mywhoosh_workouts.run()
            time.sleep(interval_minutes * 60)
    except KeyboardInterrupt:
        logger.info("Beende Synchronisation")
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


def _raise_keyboard_interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt
