# /// script
# requires-python = ">=3.12"
# dependencies = ["requests"]
# ///
"""Throwaway probe: MyWhoosh coaching workout API (undocumented).

Run interactively with ``uv run scripts/probe_mywhoosh_workouts.py``.
``--self-check`` makes no network calls: it shows the redacted upload body it would send.
Passwords are read via getpass and never printed; of tokens only claim names are printed.
Self-contained (only ``requests``), so the PEP-723 header is kept.

Phases (``weiter?`` after each):
1. app login, GET my-workouts / my-credits, token claim names
2. service20 login (only if phase 1 got 401/403)
3. upload probe workout from tests/fixtures/local/editor_upload.json
4. second upload + 120-character name
5. delete everything the probe created (Description starts with ``open-tri:probe``)
"""

from __future__ import annotations

import base64
import copy
import getpass
import json
import os
import random
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import requests

BASE = "https://coaching.mywhoosh.com/api/v2"
LOGIN_URL = "https://services.mywhoosh.com/http-service/api/login"
SERVICE20_LOGIN_URL = "https://service20.mywhoosh.com/api/auth/login"
# Upload path is a guess: take the real one from the browser network tab and override on prompt.
DEFAULT_UPLOAD_PATH = "/client/custom-workout-upload"
DELETE_PATH = "/client/custom-workout-upload/0/{workout_id}"
TIMEOUT = 30

PROBE_NAME = "PROBE open-tri sync"
PROBE_DESCRIPTION = "open-tri:probe"
ROOT = Path(__file__).resolve().parent.parent
BODY_PATH = ROOT / "tests/fixtures/local/editor_upload.json"

HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://event.mywhoosh.com",
    "Referer": "https://event.mywhoosh.com/",
}

REDACT_KEYS = {
    "UserId", "userId", "Email", "email", "Username", "author", "Author", "OwnerId",
    "ownerId", "DisplayName", "displayName", "FullName", "fullName", "AccessToken",
    "token", "Token",
}


SECRET_KEY_PARTS = ("token", "jwt", "secret", "password", "authorization")


def _secret_key(key):
    return key in REDACT_KEYS or any(part in str(key).lower() for part in SECRET_KEY_PARTS)


def _redact_str(value):
    """Strip the query of URLs that look presigned or carry a query string."""
    if value.startswith("http") and ("X-Amz-" in value or "Signature=" in value or "?" in value):
        parts = urlsplit(value)
        return f"{parts.scheme}://{parts.netloc}{parts.path}<query redacted>"
    return value


def redact(obj):
    """Return a copy with personal ids and secrets replaced, presigned URLs stripped."""
    if isinstance(obj, str):
        return _redact_str(obj)
    if isinstance(obj, dict):
        return {
            k: (f"<redacted:{type(v).__name__}>" if _secret_key(k) else redact(v))
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


def section(title):
    print(f"\n===== {title} =====")


def pause():
    if input("weiter? [J/n] ").strip().lower() in ("n", "nein"):
        print("Abbruch durch Betreiber.")
        sys.exit(0)


def token_claims(token):
    """Claims of an unverified JWT (values stay in memory, only names are printed)."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    seg = parts[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
    except ValueError:
        return {}
    return claims if isinstance(claims, dict) else {}


def find_key(obj, wanted):
    """First value for any key in ``wanted`` (case-insensitive), searching nested data."""
    wanted = {w.lower() for w in wanted}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in wanted and v not in (None, ""):
                return v
        for v in obj.values():
            hit = find_key(v, wanted)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = find_key(v, wanted)
            if hit is not None:
                return hit
    return None


def walk_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_dicts(v)


def is_probe(entry):
    return str(entry.get("Description", "")).startswith(PROBE_DESCRIPTION)


def body_of(response):
    try:
        return response.json()
    except ValueError:
        return response.text[:500]


def report(label, response):
    print(f"\n>> {label}: HTTP {response.status_code}")
    show("body", body_of(response))


def build_body(template, name, description, user_id, workout_id=None):
    """Copy of the recorded editor body with name, description and UserId replaced."""
    body = copy.deepcopy(template)
    workouts = body.get("WorkoutsData")
    if not isinstance(workouts, list) or not workouts or not isinstance(workouts[0], dict):
        raise SystemExit("editor_upload.json hat kein WorkoutsData[0]")
    workouts[0]["Name"] = name
    workouts[0]["Description"] = description
    if workout_id is not None:
        workouts[0]["Id"] = workout_id
    if user_id is not None:
        body["UserId"] = user_id
    return body


class Probe:
    def __init__(self):
        self.http = requests.Session()
        self.token = None
        self.user_id = None
        self.user_id_source = None
        self.auth_variant = None
        self.created = {}  # WorkoutId -> name
        self.baseline_credits = None

    def auth(self):
        return {**HEADERS, "Authorization": f"Bearer {self.token}"}

    def get(self, path):
        return self.http.get(BASE + path, headers=self.auth(), timeout=TIMEOUT)

    # ---- phase 1
    def app_login(self, email, password):
        response = self.http.post(
            LOGIN_URL,
            json={
                "Username": email,
                "Password": password,
                "Platform": "Android",
                "Action": 1001,
                "CorrelationId": str(uuid.uuid4()),
                "DeviceId": str(uuid.uuid4()),
                "Authorization": "",
            },
            timeout=TIMEOUT,
        )
        print(f"App-Login: HTTP {response.status_code}")
        body = body_of(response)
        token = body.get("AccessToken") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            print("Kein AccessToken im App-Login.")
            return False
        self.token = token
        claims = token_claims(token)
        print("Token-Claims (nur Namen):", sorted(claims))
        uid = find_key(claims, {"userId", "user_id", "uid", "sub", "id"})
        if uid is not None:
            self.user_id, self.user_id_source = uid, "app token claims"
        return True

    def phase1(self, email, password):
        section("Phase 1: App-Login")
        if not self.app_login(email, password):
            print("App-Login ohne Token, weiter mit Phase 2.")
            return "phase2"
        return self.check_access("app")

    def check_access(self, variant):
        """Read the coaching GETs; return 'ok', 'phase2' (401/403) or 'unclear'."""
        statuses = []
        for path in ("/workout-builder/my-workouts", "/client/my-credits"):
            response = self.get(path)
            report(f"GET {path}", response)
            statuses.append(response.status_code)
            if path.endswith("my-workouts") and response.ok:
                existing = [e for e in walk_dicts(body_of(response)) if e.get("WorkoutId") is not None]
                if existing:
                    print(f"STOPP: my-workouts enthält schon {len(existing)} Eintrag/Einträge. "
                          "Bibliothek erst leeren (saubere Credit-Zahlen), dann neu starten.")
                    return "unclear"
            if path.endswith("my-credits") and response.ok:
                self.baseline_credits = find_key(body_of(response), {"CurrentCredits"})
                print(f"Baseline CurrentCredits (vor jedem Upload): {self.baseline_credits!r}")
        if any(s in (401, 403) for s in statuses):
            print(f"{variant}-Token wird abgelehnt (401/403).")
            return "phase2"
        if not all(200 <= s < 300 for s in statuses):
            print(f"unklar: Status {statuses} (kein 401/403), AUTH_VARIANT nicht gesetzt.")
            return "unclear"
        self.auth_variant = variant
        print(f"AUTH_VARIANT = {variant}")
        return "ok"

    # ---- phase 2
    def phase2(self, email, password):
        section("Phase 2: service20-Login")
        response = self.http.post(
            SERVICE20_LOGIN_URL,
            json={"email": email, "password": password, "role": "player"},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        body = body_of(response)
        print(f"service20-Login: HTTP {response.status_code}")
        show("body", body)
        if "captcha" in json.dumps(body, default=str).lower():
            print("STOPP: Captcha nötig")
            sys.exit(1)
        token = find_key(body, {"token", "accessToken", "access_token"})
        if not isinstance(token, str):
            print("Kein Token im service20-Login.")
            return False
        self.token = token
        print("Token-Claims (nur Namen):", sorted(token_claims(token)))
        uid = find_key(body, {"userId", "user_id"})
        if uid is not None:
            self.user_id, self.user_id_source = uid, "service20 login userId"
        return self.check_access("service20") == "ok"

    # ---- upload / list
    def upload(self, upload_path, name, workout_id=None, mutate=None):
        """POST the editor body with a fresh (or given) Id; the Id is recorded before the POST."""
        if workout_id is None:
            workout_id = random.randint(900_000_000, 999_999_999)
        self.created[workout_id] = name
        template = json.loads(BODY_PATH.read_text())
        body = build_body(template, name, PROBE_DESCRIPTION, self.user_id, workout_id)
        if mutate:
            mutate(body["WorkoutsData"][0])
        response = self.http.post(BASE + upload_path, json=body, headers=self.auth(), timeout=TIMEOUT)
        report(f"POST {upload_path} (Id {workout_id}, Name {name!r})", response)
        return response, workout_id

    def credits(self):
        return find_key(body_of(self.get("/client/my-credits")), {"CurrentCredits"})

    def listing(self):
        response = self.get("/workout-builder/my-workouts")
        return body_of(response)

    def entry(self, listing, workout_id):
        for e in walk_dicts(listing):
            if str(e.get("WorkoutId")) == str(workout_id):
                return e
        return None

    def wait_listed(self, workout_id, name=None, limit=30):
        """Poll every 2 s until the entry is listed (with ``name`` if given); return (entry, seconds)."""
        start = time.monotonic()
        while True:
            elapsed = time.monotonic() - start
            entry = self.entry(self.listing(), workout_id)
            credits = self.credits()
            listed = entry is not None and (name is None or entry.get("Name") == name)
            print(f"t={elapsed:4.1f}s CurrentCredits={credits!r} gelistet={'ja' if listed else 'nein'}")
            if listed:
                return entry, elapsed
            if elapsed >= limit:
                print(f"Nach {limit} s nicht gelistet.")
                return None, elapsed
            time.sleep(2)

    def phase3(self, upload_path):
        section("Phase 3: Workout A anlegen")
        print(f"UserId-Herkunft: {self.user_id_source or 'UNBEKANNT (UserId aus Body unverändert)'}")
        response, self.id_a = self.upload(upload_path, "PROBE A")
        if not response.ok:
            return False
        entry, seconds = self.wait_listed(self.id_a)
        print(f"A sichtbar nach {seconds:.1f}s" if entry else "A nicht sichtbar")
        return entry is not None

    def phase4(self, upload_path):
        section("Phase 4: Workout B und Update von A")
        response, id_b = self.upload(upload_path, "PROBE B")
        entry_b, seconds = self.wait_listed(id_b) if response.ok else (None, 0)
        print(f"B sichtbar nach {seconds:.1f}s" if entry_b else "B nicht sichtbar")
        print("WorkoutIds verschieden:", "ja" if str(self.id_a) != str(id_b) else "nein")
        # (b) re-upload A with the same Id, new name and first step Power 0.7
        before = self.listing()
        count_before = sum(1 for e in walk_dicts(before) if is_probe(e))
        entry_a = self.entry(before, self.id_a)
        uid_before = find_key(entry_a, {"_id"}) if entry_a else None
        credits_before = self.credits()
        changed = {}

        def mutate(workout):
            for d in walk_dicts(workout):
                for key, value in d.items():
                    if "power" in key.lower() and isinstance(value, (int, float)) and not isinstance(value, bool):
                        changed["key"], changed["old"] = key, value
                        d[key] = 0.7
                        return

        response, _ = self.upload(upload_path, "PROBE A geändert", self.id_a, mutate)
        print(f"Geänderter Power-Schlüssel: {changed.get('key')!r} (alt {changed.get('old')!r}, neu 0.7)")
        entry, seconds = self.wait_listed(self.id_a, "PROBE A geändert") if response.ok else (None, 0)
        after = self.listing()
        count_after = sum(1 for e in walk_dicts(after) if is_probe(e))
        credits_after = self.credits()
        print(f"Name aktualisiert nach {seconds:.1f}s" if entry else "Name nicht aktualisiert")
        print(f"Eintragsanzahl gleich geblieben: {'ja' if count_before == count_after else 'nein'} "
              f"({count_before} -> {count_after})")
        uid_after = find_key(entry, {"_id"}) if entry else None
        print(f"_id gleich geblieben: {'ja' if uid_before is not None and uid_before == uid_after else 'nein'}")
        stored = None
        if entry and changed.get("key"):
            stored = find_key(entry, {changed["key"]})
        print(f"Schrittänderung gespeichert: {'ja' if stored == 0.7 else 'nein'} (Wert {stored!r})")
        print(f"Credits vorher/nachher: {credits_before!r} / {credits_after!r} -> "
              f"Update verbraucht Slot: {'nein' if credits_before == credits_after else 'ja'}")

    def phase5(self):
        section("Phase 5: Aufräumen")
        created = {str(w): n for w, n in self.created.items()}
        todo = {}
        for e in walk_dicts(self.listing()):
            wid = e.get("WorkoutId")
            if wid is None:
                continue
            by_description = is_probe(e)
            by_creation = str(wid) in created and str(e.get("Name", "")).startswith("PROBE")
            if by_description or by_creation:
                todo[str(wid)] = e
        print(f"Zu löschen: {len(todo)} (Description {PROBE_DESCRIPTION!r} oder selbst angelegt)")
        deleted = set()
        for wid in todo:
            try:
                response = self.http.delete(
                    BASE + DELETE_PATH.format(workout_id=wid), headers=self.auth(), timeout=TIMEOUT
                )
            except requests.RequestException as exc:
                print(f"DELETE WorkoutId {wid}: {type(exc).__name__}")
                continue
            report(f"DELETE WorkoutId {wid}", response)
            if response.ok:
                deleted.add(wid)
        for wid, name in created.items():
            if wid not in deleted:
                print(f"MANUELL löschen: {wid} {name}")
        self.poll_credits()

    def poll_credits(self):
        print(f"Warte auf Credits == Baseline {self.baseline_credits!r} und keine Probe-Einträge (max. 60 s)")
        start = time.monotonic()
        while True:
            elapsed = time.monotonic() - start
            try:
                credits = self.credits()
                left = [e for e in walk_dicts(self.listing()) if is_probe(e)]
            except requests.RequestException as exc:
                print(f"t={elapsed:4.1f}s Lesefehler {type(exc).__name__}")
                credits, left = None, [None]
            else:
                print(f"t={elapsed:4.1f}s CurrentCredits={credits!r} Probe-Einträge={len(left)}")
            if self.baseline_credits is not None and credits == self.baseline_credits and not left:
                print(f"Credits zurück auf Baseline und Bibliothek leer nach {elapsed:.1f}s")
                return
            if elapsed >= 60:
                print("Nach 60 s NICHT zurück auf Baseline (oder Baseline unbekannt) bzw. Einträge übrig.")
                return
            time.sleep(2)


def self_check():
    if not BODY_PATH.exists():
        print(f"{BODY_PATH} fehlt: Body im Editor-Netzwerk-Tab aufnehmen und dort speichern.")
        print("self-check ok (ohne Body)")
        return
    body = build_body(json.loads(BODY_PATH.read_text()), PROBE_NAME, PROBE_DESCRIPTION, "<UserId aus Login>")
    show("Upload-Body (geschwärzt)", body)
    assert body["WorkoutsData"][0]["Name"] == PROBE_NAME
    assert body["WorkoutsData"][0]["Description"] == PROBE_DESCRIPTION
    print("self-check ok")


def main():
    if "--self-check" in sys.argv:
        self_check()
        return
    if not BODY_PATH.exists():
        raise SystemExit(f"{BODY_PATH} fehlt: erst den Editor-Upload-Body aufnehmen.")
    email = os.environ.get("MYWHOOSH_EMAIL") or input("MyWhoosh-E-Mail: ").strip()
    password = os.environ.get("MYWHOOSH_PASSWORD") or getpass.getpass("MyWhoosh-Passwort: ")
    upload_path = input(f"Upload-Pfad relativ zu {BASE} [{DEFAULT_UPLOAD_PATH}]: ").strip() or DEFAULT_UPLOAD_PATH
    probe = Probe()
    try:
        state = probe.phase1(email, password)
        pause()
        if state == "unclear":
            print("STOPP: Antwort unklar, Betreiber prüft die Ausgabe.")
            return
        if state == "ok":
            print("Phase 2 übersprungen (App-Token geht).")
        else:
            if not probe.phase2(email, password):
                print("STOPP: weder App-Token noch service20-Token funktionieren.")
                return
            pause()
        if probe.phase3(upload_path):
            pause()
            probe.phase4(upload_path)
            pause()
        else:
            print("Upload fehlgeschlagen, springe zum Aufräumen.")
    finally:
        if probe.token:
            probe.phase5()
    print(f"\nAUTH_VARIANT={probe.auth_variant} UserId-Herkunft={probe.user_id_source}")


if __name__ == "__main__":
    main()
