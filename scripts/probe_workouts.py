# /// script
# requires-python = ">=3.11"
# dependencies = ["garminconnect>=0.3.20,<0.4", "requests"]
# ///
"""Throwaway probe: clarify real Garmin Connect and open-tri API behaviour.

Run interactively with ``uv run scripts/probe_workouts.py`` (or
``uv run python scripts/probe_workouts.py`` in the project venv).
``--self-check`` only builds the three workout dicts and prints them (no network).
Passwords are read via getpass and never printed; tokens only shortened.
"""

from __future__ import annotations

import base64
import getpass
import json
import sys
import time
import traceback
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

from garminconnect.workout import ConditionType, SportType, StepType, TargetType

PROBE_NAME = "PROBE open-tri sync"
PROBE_DESCRIPTION = "open-tri:probe"
BUNDLE_PATH = Path("tests/fixtures/local/opentri_bundle.zip")

REDACT_KEYS = {
    "ownerId", "author", "userProfilePK", "userProfileId", "userProfileID",
    "ownerDisplayName", "ownerFullName", "ownerProfileImageUrlSmall",
    "ownerProfileImageUrlMedium", "ownerProfileImageUrlLarge", "displayName",
    "profileImageUrl", "fullName", "userId",
}


# ---------------------------------------------------------------- helpers

def redact(obj):
    """Return a copy with personal ids replaced by '<redacted:type>'."""
    if isinstance(obj, dict):
        return {
            k: (f"<redacted:{type(v).__name__}>" if k in REDACT_KEYS else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def show(label, obj):
    print(f"--- {label}")
    try:
        print(json.dumps(redact(obj), indent=2, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001
        print(repr(obj))


def short(token):
    if not token:
        return "<none>"
    return f"{token[:8]}...{token[-4:]}"


def ask_yes(prompt):
    return input(f"{prompt} [J/n] ").strip().lower() in ("", "j", "ja", "y", "yes")


def ask_no(prompt):
    return input(f"{prompt} [j/N] ").strip().lower() in ("j", "ja", "y", "yes")


def section(title):
    print(f"\n===== {title} =====")


def step_guard(label, fn, *args, **kwargs):
    """Run one probe step; print the error and return None on failure."""
    print(f"\n>> {label}")
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        print(f"   FEHLER in '{label}': {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=2)
        return None


# ------------------------------------------------------- workout builders

def _stype(sid, key, order):
    return {"sportTypeId": sid, "sportTypeKey": key, "displayOrder": order}


def _step_type(sid, key):
    return {"stepTypeId": sid, "stepTypeKey": key, "displayOrder": sid}


def _end(cid, key):
    return {"conditionTypeId": cid, "conditionTypeKey": key,
            "displayOrder": cid, "displayable": True}


def _target(tid, key):
    return {"workoutTargetTypeId": tid, "workoutTargetTypeKey": key,
            "displayOrder": tid}


NO_TARGET = _target(TargetType.NO_TARGET, "no.target")
END_TIME = _end(ConditionType.TIME, "time")
END_DISTANCE = _end(ConditionType.DISTANCE, "distance")
END_LAP = _end(ConditionType.LAP_BUTTON, "lap.button")
END_ITER = _end(ConditionType.ITERATIONS, "iterations")


def _exec(order, step_type, end, end_value, target=None, v1=None, v2=None,
          description=None):
    step = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": step_type,
        "endCondition": end,
        "endConditionValue": end_value,
        "targetType": target or NO_TARGET,
    }
    if v1 is not None:
        step["targetValueOne"] = v1
        step["targetValueTwo"] = v2
    if description:
        step["description"] = description
    return step


def _workout(sport, segment_steps, extra=None):
    w = {
        "workoutName": PROBE_NAME,
        "description": PROBE_DESCRIPTION,
        "sportType": sport,
        "workoutSegments": [
            {"segmentOrder": 1, "sportType": sport, "workoutSteps": segment_steps}
        ],
    }
    if extra:
        w.update(extra)
    return w


def build_run_workout():
    sport = _stype(SportType.RUNNING, "running", 1)
    warm = _step_type(StepType.WARMUP, "warmup")
    interval = _step_type(StepType.INTERVAL, "interval")
    recovery = _step_type(StepType.RECOVERY, "recovery")
    cool = _step_type(StepType.COOLDOWN, "cooldown")
    # pace targets in m/s: 4:30/km = 1000/270, 4:45/km = 1000/285 (slower first)
    repeat = {
        "type": "RepeatGroupDTO",
        "stepOrder": 2,
        "stepType": _step_type(StepType.REPEAT, "repeat"),
        "numberOfIterations": 3,
        "endCondition": END_ITER,
        "endConditionValue": 3.0,
        "smartRepeat": False,
        "workoutSteps": [
            _exec(3, interval, END_DISTANCE, 1000.0,
                  _target(TargetType.PACE_ZONE, "pace.zone"),
                  1000.0 / 285.0, 1000.0 / 270.0),
            _exec(4, recovery, END_TIME, 120.0),
        ],
    }
    steps = [
        _exec(1, warm, END_TIME, 600.0,
              _target(TargetType.HEART_RATE_ZONE, "heart.rate.zone"), 120.0, 140.0),
        repeat,
        _exec(5, cool, END_LAP, None),
    ]
    steps[2].pop("endConditionValue")
    return _workout(sport, steps)


def build_bike_workout():
    sport = _stype(SportType.CYCLING, "cycling", 2)
    steps = [
        _exec(1, _step_type(StepType.INTERVAL, "interval"), END_TIME, 1200.0,
              _target(TargetType.POWER_ZONE, "power.zone"), 180.0, 200.0),
    ]
    return _workout(sport, steps)


def build_swim_workout():
    sport = _stype(SportType.SWIMMING, "swimming", 4)
    repeat = {
        "type": "RepeatGroupDTO",
        "stepOrder": 1,
        "stepType": _step_type(StepType.REPEAT, "repeat"),
        "numberOfIterations": 4,
        "endCondition": END_ITER,
        "endConditionValue": 4.0,
        "smartRepeat": False,
        "workoutSteps": [
            _exec(2, _step_type(StepType.INTERVAL, "interval"), END_DISTANCE, 100.0),
        ],
    }
    return _workout(sport, [repeat], extra={
        "poolLength": 25.0,
        "poolLengthUnit": {"unitId": 1, "unitKey": "meter", "factor": 100.0},
    })


def build_all():
    return {"run": build_run_workout(), "bike": build_bike_workout(),
            "swim": build_swim_workout()}


# ------------------------------------------------------------ Garmin part

ID_KEYS = ("workoutId", "workout_id")
SCHEDULE_KEYS = ("workoutScheduleId", "scheduleId", "scheduledWorkoutId", "id")


def pick(d, keys):
    if isinstance(d, dict):
        for k in keys:
            if d.get(k) is not None:
                return d[k]
    return None


def garmin_part():
    from garminconnect import Garmin

    section("Garmin")
    email = input("Garmin E-Mail: ").strip()
    password = getpass.getpass("Garmin Passwort: ")
    client = Garmin(email, password, prompt_mfa=lambda: input("MFA-Code: "))
    client.login()
    print("Garmin-Login ok")

    workouts = build_all()
    created: dict[str, int] = {}      # name -> workoutId
    schedules: dict[str, object] = {}  # name -> scheduleId
    deleted_templates: set[str] = set()
    tomorrow = date.today() + timedelta(days=1)
    day_after = tomorrow + timedelta(days=1)
    t_str, d_str = tomorrow.isoformat(), day_after.isoformat()

    try:
        # 1. upload
        for name, payload in workouts.items():
            resp = step_guard(f"1. upload_workout({name})",
                              client.upload_workout, payload)
            if resp is None:
                continue
            show(f"upload response ({name})", resp)
            wid = pick(resp, ID_KEYS)
            if wid is not None:
                created[name] = wid
            else:
                print("   WARNUNG: keine workoutId in der Antwort gefunden")

        # 2. read back
        for name, wid in created.items():
            back = step_guard(f"2. get_workout_by_id({name})",
                              client.get_workout_by_id, wid)
            if back is not None:
                show(f"readback ({name})", back)
                print(f"   description zurueck: {back.get('description')!r}")
                print(f"   poolLength/Unit: {back.get('poolLength')!r} / "
                      f"{back.get('poolLengthUnit')!r}")

        # 3. schedule for tomorrow
        for name, wid in created.items():
            resp = step_guard(f"3. schedule_workout({name}, {t_str})",
                              client.schedule_workout, wid, t_str)
            if resp is None:
                continue
            show(f"schedule response ({name})", resp)
            sid = pick(resp, SCHEDULE_KEYS)
            if sid is not None:
                schedules[name] = sid
            else:
                print("   WARNUNG: keine Schedule-ID erkannt, siehe Antwort oben")

        # 4. read period
        period = step_guard(f"4. get_scheduled_workouts_by_period({t_str}, {t_str})",
                            client.get_scheduled_workouts_by_period, t_str, t_str)
        if period is not None:
            show("period tomorrow", period)

        # 5. reschedule
        if schedules:
            name, sid = next(iter(schedules.items()))
            resp = step_guard(f"5. reschedule_workout({name}, {d_str})",
                              client.reschedule_workout, sid, d_str)
            show("reschedule response", resp)
            period2 = step_guard(f"5b. period {t_str}..{d_str}",
                                 client.get_scheduled_workouts_by_period,
                                 t_str, d_str)
            if period2 is not None:
                show("period after reschedule", period2)

        # 6. delete run template, does the calendar entry stay?
        if "run" in created:
            res = step_guard("6. delete_workout(run)", client.delete_workout,
                             created["run"])
            print(f"   delete response: {res!r}")
            deleted_templates.add("run")
            period3 = step_guard(f"6b. period {t_str}..{d_str} nach delete",
                                 client.get_scheduled_workouts_by_period,
                                 t_str, d_str)
            if period3 is not None:
                show("period after deleting run template", period3)
                run_id = created["run"]
                stays = [e for e in period3 if isinstance(e, dict)
                         and (pick(e, ID_KEYS) == run_id
                              or pick(e.get("workout") or {}, ID_KEYS) == run_id)]
                print(f"   KALENDEREINTRAG BLEIBT NACH LOESCHEN? "
                      f"{'JA' if stays else 'NEIN (kein Eintrag mit der run-workoutId)'}"
                      f" (Eintraege gesamt: {len(period3)})")
    finally:
        section("Garmin Aufraeumen")
        # unschedule everything we know plus anything found in the period
        schedule_ids = set(str(s) for s in schedules.values())
        try:
            for entry in client.get_scheduled_workouts_by_period(t_str, d_str) or []:
                if not isinstance(entry, dict):
                    continue
                desc = entry.get("description") or (entry.get("workout") or {}).get(
                    "description")
                wid = pick(entry, ID_KEYS) or pick(entry.get("workout") or {}, ID_KEYS)
                if desc == PROBE_DESCRIPTION or wid in created.values():
                    sid = pick(entry, SCHEDULE_KEYS)
                    if sid is not None:
                        schedule_ids.add(str(sid))
        except Exception as exc:  # noqa: BLE001
            print(f"   Zeitraum fuer Aufraeumen nicht lesbar: {exc}")
        for sid in sorted(schedule_ids):
            try:
                client.unschedule_workout(sid)
                print(f"   ausgeplant: schedule {sid}")
            except Exception as exc:  # noqa: BLE001
                print(f"   FEHLER beim Ausplanen {sid}: {exc}")
        for name, wid in created.items():
            if name in deleted_templates:
                continue
            try:
                client.delete_workout(wid)
                print(f"   geloescht: {name} (workoutId {wid})")
            except Exception as exc:  # noqa: BLE001
                print(f"   FEHLER beim Loeschen {name} ({wid}): {exc}")
        print("   Aufraeumen beendet. Bitte im Garmin-Kalender/der Bibliothek "
              f"nach '{PROBE_NAME}' pruefen.")


# ---------------------------------------------------------- open-tri part

def jwt_payload(token):
    part = token.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


DEFAULT_KEYCLOAK = "https://auth.janniskroeger.net"
DEFAULT_API = "https://open-tri.janniskroeger.net/api"


def _with_scheme(url):
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = "https://" + url
    return url


def _print_kc_error(resp):
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        print(f"   Antwort kein JSON: {resp.text[:200]!r}")
        return
    if isinstance(body, dict):
        print(f"   error: {body.get('error')}")
        print(f"   error_description: {body.get('error_description')}")
    else:
        print(f"   Antwort: {type(body).__name__}")


def _local_time(ts):
    if ts is None:
        return "<keine>"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def opentri_part():
    import requests

    section("open-tri")
    kc = _with_scheme(input(f"Keycloak Basis-URL [{DEFAULT_KEYCLOAK}]: ") or DEFAULT_KEYCLOAK)
    realm = input("Realm [open-tri]: ").strip() or "open-tri"
    client_id = input("Client-ID [mywhoosh-garmin-sync]: ").strip() or "mywhoosh-garmin-sync"
    api = _with_scheme(input(f"open-tri API-URL [{DEFAULT_API}]: ") or DEFAULT_API)
    base = f"{kc}/realms/{realm}/protocol/openid-connect"
    device_url = f"{base}/auth/device"
    print(f"   Keycloak: {kc!r}  Realm: {realm!r}  Client: {client_id!r}  API: {api!r}")
    token_url = f"{base}/token"

    # 1. device authorization request
    r = step_guard("1. Device Authorization Request", requests.post, device_url,
                   data={"client_id": client_id, "scope": "openid offline_access"},
                   timeout=30)
    if r is None:
        return
    print(f"   Status: {r.status_code}")
    if r.status_code != 200:
        _print_kc_error(r)
        return
    dev = r.json()
    link = dev.get("verification_uri_complete") or (
        f"{dev.get('verification_uri')} (Code: {dev.get('user_code')})")
    expires_in = dev.get("expires_in") or 600
    interval = dev.get("interval") or 5
    print(f"   Link: {link}")
    print(f"   expires_in: {dev.get('expires_in')}s, interval: {dev.get('interval')}s")
    print("   Bitte den Link im Browser oeffnen und einloggen.")

    # 2. poll token endpoint
    print("\n>> 2. Token-Polling (warte auf Login)")
    deadline = time.monotonic() + expires_in
    body = None
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            t = requests.post(token_url, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id, "device_code": dev.get("device_code")},
                timeout=30)
        except Exception as exc:  # noqa: BLE001
            print(f"\n   FEHLER beim Polling: {type(exc).__name__}: {exc}")
            return
        if t.status_code == 200:
            body = t.json()
            break
        try:
            err = t.json().get("error")
        except Exception:  # noqa: BLE001
            err = None
        if err == "authorization_pending":
            print(".", end="", flush=True)
            continue
        if err == "slow_down":
            interval += 5
            print("s", end="", flush=True)
            continue
        print(f"\n   Status: {t.status_code}")
        _print_kc_error(t)
        return
    if body is None:
        print("\n   Device Code abgelaufen, abgebrochen.")
        return
    print("\n   Login erfolgreich")

    access, refresh = body.get("access_token"), body.get("refresh_token")
    print(f"   Antwort-Schluessel: {sorted(body)}")
    print(f"   refresh_token vorhanden: {bool(refresh)}")
    sub = None
    try:
        claims = jwt_payload(access)
        sub = claims.get("sub")
        print(f"   Access-Token exp (lokal): {_local_time(claims.get('exp'))}")
        print(f"   azp: {claims.get('azp')!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"   Access-Token nicht lesbar: {exc}")
    if refresh:
        try:
            print(f"   refresh typ-Claim: {jwt_payload(refresh).get('typ')!r} "
                  "(erwartet 'Offline')")
        except Exception as exc:  # noqa: BLE001
            print(f"   Refresh-Token nicht lesbar: {exc}")
    print(f"   access_token: {short(access)}  refresh_token: {short(refresh)}")

    # 3. refresh once
    if refresh:
        r = step_guard("3. Refresh", requests.post, token_url, data={
            "grant_type": "refresh_token", "client_id": client_id,
            "refresh_token": refresh}, timeout=30)
        if r is not None:
            print(f"   Status: {r.status_code}")
            if r.ok:
                rb = r.json()
                new_rt = rb.get("refresh_token")
                print(f"   neuer refresh_token (Rotation): "
                      f"{'JA' if new_rt and new_rt != refresh else 'NEIN'}"
                      f" ({short(new_rt)})")
                print(f"   neuer access_token: {short(rb.get('access_token'))}")
                access = rb.get("access_token") or access
            else:
                _print_kc_error(r)

    if not sub:
        print("   ohne sub keine weiteren API-Aufrufe")
        return

    headers = {"Authorization": f"Bearer {access}"}

    # 3. active plan
    r = step_guard("3. GET training-plans/active", requests.get,
                   f"{api}/athletes/{sub}/training-plans/active",
                   headers=headers, timeout=30)
    plan_id = None
    if r is not None:
        print(f"   Status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print(f"   Schluessel: {sorted(data) if isinstance(data, dict) else type(data)}")
            plan_id = data.get("id") if isinstance(data, dict) else None
        elif r.status_code == 204:
            print("   kein aktiver Plan (204)")
    if plan_id is None:
        print("   ohne Plan-ID kein Export")
        return

    # 4. export
    today = date.today()
    r = step_guard("4. GET fit export", requests.get,
                   f"{api}/athletes/{sub}/training-plans/{plan_id}/fit",
                   params={"from": today.isoformat(),
                           "to": (today + timedelta(days=13)).isoformat(),
                           "schedule": "true"},
                   headers=headers, timeout=60)
    if r is None:
        return
    print(f"   Status: {r.status_code}, Content-Type: {r.headers.get('Content-Type')}, "
          f"Bytes: {len(r.content)}")
    if r.status_code != 200:
        return
    BUNDLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BUNDLE_PATH.write_bytes(r.content)
    print(f"   gespeichert: {BUNDLE_PATH}")
    with zipfile.ZipFile(BUNDLE_PATH) as zf:
        for n in zf.namelist():
            print(f"   - {n}")


def _keys(resp):
    try:
        data = resp.json()
        return sorted(data) if isinstance(data, dict) else type(data).__name__
    except Exception:  # noqa: BLE001
        return "<kein JSON>"


# ------------------------------------------------------------------- main

def main():
    if "--self-check" in sys.argv:
        for name, w in build_all().items():
            assert isinstance(w, dict)
            show(f"{name} workout", w)
        print("self-check ok")
        return
    if ask_no("Garmin-Teil ausführen?"):
        try:
            garmin_part()
        except Exception as exc:  # noqa: BLE001
            print(f"Garmin-Teil abgebrochen: {type(exc).__name__}: {exc}")
    if ask_yes("open-tri-Teil ausführen?"):
        try:
            opentri_part()
        except Exception as exc:  # noqa: BLE001
            print(f"open-tri-Teil abgebrochen: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
