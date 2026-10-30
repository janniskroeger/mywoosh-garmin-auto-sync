"""Command line entry point: continuous sync, single run, dry run and Garmin login."""

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path
from types import FrameType

from mywhoosh_garmin_sync.config import ConfigError, Settings
from mywhoosh_garmin_sync.garmin import (
    GarminAuthError,
    GarminUnavailableError,
    GarminUploader,
    interactive_login,
)
from mywhoosh_garmin_sync.mywhoosh import MyWhooshClient, MyWhooshError
from mywhoosh_garmin_sync.state import StateStore
from mywhoosh_garmin_sync.sync import dry_run, run_once

logger = logging.getLogger(__name__)

_QUIET_LOGGERS = ("garminconnect", "urllib3", "requests")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    command = args.command or "run"

    try:
        settings = Settings.from_env(os.environ, require_garmin=command != "dry-run")
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2

    _configure_logging(settings.log_level)
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    if command == "login-garmin":
        return _login_garmin(settings)

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
    if command == "once":
        return _once(source, garmin, store, work_dir)
    return _run_forever(source, garmin, store, work_dir, settings.interval_minutes)


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


def _once(
    source: MyWhooshClient, garmin: GarminUploader, store: StateStore, work_dir: Path
) -> int:
    try:
        summary = run_once(source, garmin, store, work_dir)
    except Exception:
        logger.exception("Unerwarteter Fehler im Lauf")
        return 1
    return 1 if summary.aborted else 0


def _run_forever(
    source: MyWhooshClient,
    garmin: GarminUploader,
    store: StateStore,
    work_dir: Path,
    interval_minutes: int,
) -> int:
    previous_handler = signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    logger.info("Starte Synchronisation, Intervall %d Minuten", interval_minutes)
    try:
        while True:
            try:
                run_once(source, garmin, store, work_dir)
            except Exception:
                logger.exception("Unerwarteter Fehler im Lauf, nächster Versuch nach dem Intervall")
            time.sleep(interval_minutes * 60)
    except KeyboardInterrupt:
        logger.info("Beende Synchronisation")
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


def _raise_keyboard_interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt
