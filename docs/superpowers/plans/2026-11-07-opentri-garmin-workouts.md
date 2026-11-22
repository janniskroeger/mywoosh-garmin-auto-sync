# open-tri-Trainings nach Garmin Connect – Implementierungsplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Der Sync-Container überträgt die geplanten Schwimm-, Rad- und Laufeinheiten des aktiven open-tri-Plans für die nächsten 14 Tage als eingeplante Trainings nach Garmin Connect und hält sie mit open-tri synchron.

**Architecture:** Ein zweiter Schritt in der bestehenden Sync-Schleife. Ein open-tri-Client holt das ZIP des bestehenden FIT-Exports. Ein FIT-Leser macht daraus Workouts mit Seriennummer und Datum, ein Umrechner erzeugt Garmins Workout-JSON. Ein Abgleich vergleicht das mit einer eigenen Zustandsdatei und dem Garmin-Kalender und ruft eine dünne Garmin-Hülle auf, die die vorhandene Garmin-Sitzung mitbenutzt. Der Abgleich kennt open-tri und Garmin nur über Protokolle und wird mit Fakes getestet.

**Tech Stack:** Python ≥ 3.12, uv, requests, fit-tool, garminconnect 0.3.x, pytest, responses.

**Spec:** `docs/superpowers/specs/2026-11-07-opentri-garmin-workouts-design.md`. Das erste Design (`docs/superpowers/specs/2026-10-30-mywhoosh-garmin-sync-design.md`) und der bestehende Code unter `src/mywhoosh_garmin_sync/` bleiben gültig.

## Global Constraints

- Alle Regeln des bestehenden Projekts gelten weiter: Bezeichner und Docstrings Englisch, Log-Meldungen und README Deutsch, Commits mit expliziter Pfadangabe, `tests/fixtures/local/` ist gitignored.
- Niemals Passwörter, Access- oder Refresh-Tokens von open-tri oder Garmin loggen oder in Exception-Texte schreiben.
- open-tri wird nicht geändert. Genutzt werden nur `GET /athletes/{athleteId}/training-plans/active` und `GET /athletes/{athleteId}/training-plans/{planId}/fit?from&to&schedule=true`.
- Keycloak: eigener öffentlicher Client `mywhoosh-garmin-sync`, OAuth 2.0 Device Authorization Grant, Scope `openid offline_access`, einmalig per `login-opentri`. Danach nur Refresh-Token, rotierte Tokens sofort speichern. Kein open-tri-Passwort in der Konfiguration. Athleten-ID = `sub` des Access-Tokens.
- Schlüssel jedes Trainings ist `file_id.serial_number` des FIT-Workouts. Das Datum kommt aus `Schedules.fit`, nicht aus dem Dateinamen.
- Datum "heute" in `Europe/Berlin`. Fenster: heute bis heute + `WORKOUT_SYNC_DAYS` − 1, Standard 14.
- Nur Sportarten swimming, cycling, running. Andere: WARNING, überspringen.
- Markierung in der Garmin-Beschreibung: `open-tri:<serial>`. Sie zählt nicht zum Inhalts-Hash. Garmin-Trainings ohne Markierung werden nie angefasst.
- Beckenlänge Schwimmen Standard 25 m (`WORKOUT_POOL_LENGTH_M`).
- Neue Variablen: `OPENTRI_API_URL`, `OPENTRI_KEYCLOAK_URL` (beide oder keine), `OPENTRI_KEYCLOAK_REALM` (Standard `open-tri`), `OPENTRI_CLIENT_ID` (Standard `mywhoosh-garmin-sync`), `WORKOUT_SYNC_DAYS` (14), `WORKOUT_POOL_LENGTH_M` (25).
- Ein Fehler im Trainings-Schritt darf den Aktivitäten-Schritt nie verhindern, und umgekehrt.

## Review Focus

1. **Zwei Trainings am selben Tag** (Koppeleinheit, oder zweimal dasselbe Training): Beide erscheinen getrennt in Garmin, weil sie verschiedene Seriennummern haben. Test in Task 7.
2. **Der Export enthält ein Workout ohne Eintrag in `Schedules.fit`,** oder `Schedules.fit` fehlt: Dieses Workout wird mit WARNING übersprungen, die anderen laufen weiter. Test in Task 2.
3. **open-tri liefert eine nicht-ZIP-Antwort mit Status 200,** etwa eine HTML-Fehlerseite hinter einem Proxy: Der Trainings-Schritt bricht mit ERROR ab und löscht **nichts** in Garmin. Das darf nicht wie ein leerer Plan behandelt werden. Test in Task 4 und Task 7.
4. **Der Lauf kurz nach Mitternacht:** Ein Training von gestern ist jetzt vergangen und wird nicht mehr angefasst. Eines von heute bleibt aktiv. Test in Task 7 mit injiziertem `today`.
5. **Garmin-Kalender enthält markierte Trainings, die nicht (mehr) im Zustand stehen,** und sie sind auch nicht in open-tri: Ein Eintrag ab heute wird entfernt, ein vergangener bleibt stehen. Test in Task 7.

---

## File Structure

```
scripts/probe_workouts.py            Task 1, Wegwerf-Prüfskript (nicht im Image)
src/mywhoosh_garmin_sync/
  workout_fit.py      FIT-Workouts und Schedules.fit aus dem ZIP lesen
  workout_json.py     FitWorkout -> Garmin-Workout-JSON, Inhalts-Hash, Markierung
  opentri.py          Keycloak-Anmeldung, Token-Cache, Export abrufen
  garmin_workouts.py  dünne Hülle um garminconnect für Trainings und Kalender
  workout_state.py    Zustandsdatei workouts.json
  workout_sync.py     Abgleich pro Lauf
  garmin.py           (ändern) öffentlicher Zugriff auf die eingeloggte Sitzung
  config.py, cli.py   (ändern) neue Variablen, zweiter Schritt in der Schleife
tests/
  workoutfactory.py   synthetische FIT-Workouts und Schedules.fit im Format von open-tri
  test_workout_fit.py test_workout_json.py test_opentri.py test_garmin_workouts.py
  test_workout_state.py test_workout_sync.py
  (test_config.py, test_cli.py erweitern)
docs/superpowers/specs/2026-11-07-workout-probe-results.md   Task 1, Ergebnisse
```

---

### Task 1: Prüfpunkte am echten Konto klären

Dieser Task braucht den Betreiber. Der Controller schreibt das Skript, der Betreiber führt es lokal mit seinen Zugangsdaten aus, der Controller hält die Ergebnisse fest. Ohne diese Ergebnisse beginnen Task 5 und Task 7 nicht.

**Files:**
- Create: `scripts/probe_workouts.py`, `docs/superpowers/specs/2026-11-07-workout-probe-results.md`

**Interfaces:**
- Produces: das Ergebnisdokument mit den tatsächlichen Antwortformen. Task 5 und Task 7 lesen es.

- [ ] **Step 1: Skript schreiben**

`uv run scripts/probe_workouts.py` mit Inline-Abhängigkeiten (`garminconnect`, `requests`). Es fragt Garmin- und open-tri-Zugangsdaten sowie die Keycloak- und API-URL interaktiv ab, Passwörter per `getpass`. Tokens gibt es nur gekürzt aus.

Garmin-Teil, alles mit einem Testtraining namens `PROBE open-tri sync` und der Beschreibung `open-tri:probe`:
1. Ein Lauftraining mit drei Schritten hochladen: Aufwärmen 10 min mit Puls 120–140 bpm, eine Repeat-Gruppe 3× aus 1000 m mit Pace 4:30–4:45 /km und 2 min Erholung offen, Auslaufen per Lap-Taste. Dazu ein Radtraining mit 20 min bei 180–200 W und ein Schwimmtraining mit 4× 100 m, Beckenlänge 25 m. Ziele so, wie in der Spec unter "Mapping FIT → Garmin" beschrieben.
2. Jedes Training mit `get_workout_by_id` zurücklesen und die JSON-Antwort ausgeben, ohne IDs des Nutzers. Prüfen, ob Ziele, Beckenlänge und Beschreibung so ankommen.
3. Alle drei für morgen einplanen. Die vollständige Antwort von `schedule_workout` ausgeben (Schlüssel der Schedule-ID).
4. `get_scheduled_workouts_by_period(morgen, morgen)` ausgeben (Feldnamen für Schedule-ID, Workout-ID, Datum, Name, Beschreibung).
5. `reschedule_workout` auf übermorgen, erneut lesen.
6. Das Lauftraining mit `delete_workout` löschen und den Zeitraum erneut lesen: **Bleibt der Kalendereintrag?**
7. Aufräumen: restliche Einträge ausplanen und Trainings löschen.

open-tri-Teil:
1. Device Login mit dem Client `mywhoosh-garmin-sync` (ursprünglich Password Grant mit `open-tri-web`, im Deployment gesperrt). Ausgeben: Status, ob ein `refresh_token` kam, dessen `typ`-Claim (`Offline` erwartet), Ablauf des Access-Tokens.
2. Refresh mit dem Refresh-Token, Status ausgeben.
3. `GET /athletes/{sub}/training-plans/active`: Status und die Schlüssel der Antwort.
4. Export für heute bis heute + 13 mit `schedule=true`: Status, Content-Type, Dateinamen im ZIP. Das ZIP unter `tests/fixtures/local/opentri_bundle.zip` speichern (gitignored), für den optionalen Echtdaten-Test in Task 2.

- [ ] **Step 2: Betreiber führt das Skript aus**

Der Controller startet es in einem Terminal-Tab und bittet den Betreiber, die Zugangsdaten einzugeben.

Run: `uv run scripts/probe_workouts.py`
Expected: alle Schritte mit Status 2xx, Ausgabe vollständig.

- [ ] **Step 3: Ergebnisse festhalten**

Der Controller schreibt `docs/superpowers/specs/2026-11-07-workout-probe-results.md` mit den gekürzten Antwortformen und den Antworten auf die vier Prüfpunkte der Spec. Weicht etwas von der Spec ab, entscheidet der Controller und vermerkt es dort. Beispiele: Password Grant abgelehnt, oder Garmin will Ziele anders. Sind die Kalendereinträge nach dem Löschen weg, wird `DELETE_PAST_LIBRARY_WORKOUTS = False` festgelegt.

- [ ] **Step 4: Commit**

```bash
git add scripts/probe_workouts.py docs/superpowers/specs/2026-11-07-workout-probe-results.md
git commit -m "chore: Probe Garmin workout and open-tri export behaviour" -- scripts/probe_workouts.py docs/superpowers/specs/2026-11-07-workout-probe-results.md
```

---

### Task 2: FIT-Workouts aus dem Export lesen

**Files:**
- Create: `src/mywhoosh_garmin_sync/workout_fit.py`, `tests/workoutfactory.py`
- Test: `tests/test_workout_fit.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class FitStep: name: str | None; notes: str | None; intensity: Literal["warmup","active","recovery","cooldown","rest"]; duration: Literal["time","distance","open"]; duration_value: float | None` (Sekunden oder Meter) `; target: Literal["open","heart_rate","power","speed"]; target_low: float | None; target_high: float | None` (bpm, Watt bzw. m/s, bereits ohne FIT-Offset)
  - `@dataclass(frozen=True) class FitRepeat: steps: tuple[FitStep, ...]; iterations: int`
  - `@dataclass(frozen=True) class FitWorkout: serial: int; name: str; sport: Literal["swimming","cycling","running"]; items: tuple[FitStep | FitRepeat, ...]`
  - `@dataclass(frozen=True) class PlannedWorkout: workout: FitWorkout; date: date`
  - `class WorkoutBundleError(ValueError)`: das ZIP selbst ist kaputt oder kein ZIP
  - `read_bundle(zip_bytes: bytes) -> list[PlannedWorkout]`
  - `parse_workout(fit_bytes: bytes) -> FitWorkout | None`: `None` bei nicht unterstützter Sportart (mit WARNING)
  - `parse_schedule(fit_bytes: bytes) -> dict[int, date]`
- Produces (Tests): `tests/workoutfactory.py: make_workout_fit(serial, name, sport, steps) -> bytes`, `make_schedule_fit(entries: dict[int, date]) -> bytes`, `make_bundle(files: dict[str, bytes]) -> bytes`. `steps` ist eine Liste von Dicts mit genau den `workout_step`-Feldern, die open-tri schreibt (siehe Spec, Abschnitt "Die FIT-Workouts von open-tri"), inklusive FIT-Offsets (Puls + 100, Leistung + 1000).

- [ ] **Step 1: Failing tests schreiben**

```python
def test_parses_steps_targets_and_offsets():
    raw = make_workout_fit(42, "W3: Grundlage", "running", [
        {"workout_step_name": "Einlaufen", "intensity": "warmup", "duration_type": "time", "duration_time": 600,
         "target_type": "heart_rate", "target_value": 0, "custom_target_heart_rate_low": 220, "custom_target_heart_rate_high": 240},
        {"workout_step_name": "Hart", "intensity": "active", "duration_type": "distance", "duration_distance": 1000,
         "target_type": "speed", "target_value": 0, "custom_target_speed_low": 3.5, "custom_target_speed_high": 3.7},
        {"workout_step_name": "Locker", "intensity": "recovery", "duration_type": "open", "target_type": "open"},
        {"duration_type": "repeat_until_steps_cmplt", "duration_step": 1, "target_repeat_steps": 3, "target_type": "open"},
    ])
    w = parse_workout(raw)
    assert (w.serial, w.name, w.sport) == (42, "W3: Grundlage", "running")
    assert w.items[0] == FitStep("Einlaufen", None, "warmup", "time", 600, "heart_rate", 120, 140)
    rep = w.items[1]
    assert isinstance(rep, FitRepeat) and rep.iterations == 3 and len(rep.steps) == 2
    assert rep.steps[0].target == "speed" and rep.steps[0].target_low == pytest.approx(3.5)
    assert rep.steps[1].duration == "open"

def test_power_offset_removed():
    # custom_target_power_low/high 1180/1200 -> 180/200 W
    ...

def test_steps_after_repeat_stay_top_level():
    # open-tri writes the final round of a skip-last-step group after the repeat step
    # -> items: [Repeat(...), FitStep(...)]
    ...

def test_unsupported_sport_returns_none_with_warning(caplog): ...  # sport "training"

def test_schedule_local_time_to_date():
    raw = make_schedule_fit({42: date(2026, 11, 10), 43: date(2026, 11, 11)})
    assert parse_schedule(raw) == {42: date(2026, 11, 10), 43: date(2026, 11, 11)}

def test_read_bundle_pairs_workouts_with_dates():
    bundle = make_bundle({"2026-11-10-Lauf.fit": make_workout_fit(42, ...), "2026-11-11-Rad.fit": make_workout_fit(43, ...),
                          "Schedules.fit": make_schedule_fit({42: date(2026, 11, 10), 43: date(2026, 11, 11)})})
    assert [(p.workout.serial, p.date) for p in read_bundle(bundle)] == [(42, date(2026, 11, 10)), (43, date(2026, 11, 11))]

def test_workout_without_schedule_entry_is_skipped(caplog): ...   # Review Focus 2
def test_bundle_without_schedules_file_skips_all_with_warning(caplog): ...  # Review Focus 2
def test_broken_single_workout_is_skipped(caplog): ...            # garbage bytes for one .fit, the other survives
@pytest.mark.parametrize("raw", [b"", b"<html>nope</html>"])
def test_not_a_zip_raises(raw): ...                               # WorkoutBundleError, Review Focus 3

REAL = Path("tests/fixtures/local/opentri_bundle.zip")
@pytest.mark.skipif(not REAL.exists(), reason="no local open-tri export")
def test_real_export_parses():
    planned = read_bundle(REAL.read_bytes())
    assert planned and all(p.workout.items for p in planned)
```

- [ ] **Step 2: Tests laufen lassen, sie schlagen fehl**

Run: `uv run pytest tests/test_workout_fit.py -v`
Expected: FAIL mit `ImportError`

- [ ] **Step 3: `workout_fit.py` implementieren**

- `fit-tool` liest `WorkoutStepMessage`. Die Felder heißen dort `workout_step_name`, `notes`, `intensity`, `duration_type`, `duration_time`, `duration_distance`, `duration_step`, `target_type`, `target_value`, `target_repeat_steps` und `custom_target_*_low/high`. Die Unterfelder (`duration_time`, `duration_step`, `target_repeat_steps`, `custom_target_heart_rate_low` …) liefert fit-tool abhängig von `duration_type` bzw. `target_type`.
- Repeat-Schritt: Die Schritte ab `duration_step` bis vor den Repeat werden aus der flachen Liste in eine `FitRepeat` mit `target_repeat_steps` Iterationen verschoben.
- `scheduled_time` ist FIT-Local-Time in Sekunden seit 1989-12-31 00:00 und wird als UTC-Datum gelesen.
- Das Datum kommt aus `Schedules.fit` über `serial_number`. Dateinamen werden nicht ausgewertet.

- [ ] **Step 4: Tests laufen lassen**

Run: `uv run pytest tests/test_workout_fit.py -v`
Expected: alle bestanden. Der Echtdaten-Test läuft, wenn Task 1 das ZIP abgelegt hat.

- [ ] **Step 5: Commit**

```bash
git add src/mywhoosh_garmin_sync/workout_fit.py tests/workoutfactory.py tests/test_workout_fit.py
git commit -m "feat: Read planned workouts from the open-tri FIT export" -- src/mywhoosh_garmin_sync/workout_fit.py tests/workoutfactory.py tests/test_workout_fit.py
```

---

### Task 3: Umrechnung nach Garmin-Workout-JSON

**Files:**
- Create: `src/mywhoosh_garmin_sync/workout_json.py`
- Test: `tests/test_workout_json.py`

**Interfaces:**
- Consumes: `FitWorkout`, `FitStep`, `FitRepeat` (Task 2), das Ergebnisdokument von Task 1 für die genaue JSON-Form
- Produces:
  - `MARKER_PREFIX = "open-tri:"`
  - `marker(serial: int) -> str` gibt `"open-tri:<serial>"` zurück
  - `serial_from_description(description: str | None) -> int | None`
  - `to_garmin_json(workout: FitWorkout, pool_length_m: float) -> dict[str, Any]`: Garmin-Workout ohne IDs, `description` = Markierung
  - `content_hash(workout_json: dict[str, Any]) -> str`: SHA-256 über das kanonisch sortierte JSON **ohne** `description`

- [ ] **Step 1: Failing tests schreiben**

```python
def test_running_workout_shape():
    j = to_garmin_json(run_workout(serial=42), pool_length_m=25)
    assert j["workoutName"] == "W3: Grundlage" and j["description"] == "open-tri:42"
    assert j["sportType"]["sportTypeId"] == 1
    steps = j["workoutSegments"][0]["workoutSteps"]
    assert steps[0]["stepType"]["stepTypeKey"] == "warmup"
    assert steps[0]["endCondition"]["conditionTypeKey"] == "time" and steps[0]["endConditionValue"] == 600
    assert steps[0]["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert (steps[0]["targetValueOne"], steps[0]["targetValueTwo"]) == (120, 140) and "zoneNumber" not in steps[0]
    assert steps[1]["type"] == "RepeatGroupDTO" and steps[1]["numberOfIterations"] == 3

def test_step_order_is_global_and_increasing(): ...   # stepOrder 1..n over nested steps
def test_intensity_mapping(): ...                     # warmup/active/recovery/cooldown/rest -> warmup/interval/recovery/cooldown/rest
def test_open_duration_is_lap_button(): ...
def test_distance_in_meters(): ...
def test_power_target_watts(): ...                    # power.zone, 180/200, no zoneNumber
def test_run_speed_becomes_pace_target(): ...         # pace.zone, m/s values as given
def test_bike_speed_becomes_speed_target(): ...       # speed.zone
def test_open_target_is_no_target(): ...
def test_swim_pool_length():
    j = to_garmin_json(swim_workout(), pool_length_m=25)
    assert j["sportType"]["sportTypeId"] == 4 and j["poolLength"] == 25
def test_step_name_and_notes_go_to_description(): ...  # step description = name, plus notes if present
def test_hash_ignores_marker():
    a = to_garmin_json(run_workout(serial=42), 25); b = dict(a, description="open-tri:99")
    assert content_hash(a) == content_hash(b)
def test_hash_changes_with_content(): ...
def test_serial_from_description():
    assert serial_from_description("open-tri:42") == 42
    assert serial_from_description("Mein Training") is None and serial_from_description(None) is None
```

- [ ] **Step 2: Tests laufen lassen, sie schlagen fehl**

Run: `uv run pytest tests/test_workout_json.py -v`
Expected: FAIL mit `ImportError`

- [ ] **Step 3: `workout_json.py` implementieren**

Ergebnis als reines Dict. Die IDs und Keys von Sportart, Schritt-Typ, Endbedingung und Ziel kommen aus `garminconnect.workout` (`SportType`, `StepType`, `ConditionType`, `TargetType`, dazu die `target_type_key`-Strings der Target-Modelle, etwa `"heart.rate.zone"`, `"power.zone"`, `"pace.zone"` und `"speed.zone"`). `pydantic` wird dafür nicht gebraucht. Wo Task 1 eine andere JSON-Form gezeigt hat (z. B. Feldnamen für die Beckenlänge, `poolLengthUnit`), gilt das Ergebnisdokument. `estimatedDurationInSecs` ist die Summe der Zeit-Schritte mal Iterationen, Distanz- und Lap-Schritte zählen 0.

- [ ] **Step 4: Tests laufen lassen**

Run: `uv run pytest tests/test_workout_json.py -v`
Expected: alle bestanden

- [ ] **Step 5: Commit**

```bash
git add src/mywhoosh_garmin_sync/workout_json.py tests/test_workout_json.py
git commit -m "feat: Convert FIT workouts to Garmin workout JSON" -- src/mywhoosh_garmin_sync/workout_json.py tests/test_workout_json.py
```

---

### Task 4: open-tri-Client

**Files:**
- Create: `src/mywhoosh_garmin_sync/opentri.py`
- Test: `tests/test_opentri.py`

**Interfaces:**
- Produces:
  - `class OpenTriError(Exception)`, `class OpenTriAuthError(OpenTriError)` (kein oder abgelehntes Refresh-Token, Text enthält den Hinweis auf `login-opentri`), `class OpenTriUnavailableError(OpenTriError)` (Netz, 5xx, unerwartete Antwort)
  - `class OpenTriClient(api_url: str, keycloak_url: str, realm: str, client_id: str, data_dir: Path, session: requests.Session | None = None, sleep: Callable[[float], None] = time.sleep)`
  - `OpenTriClient.device_login(show: Callable[[str], None]) -> None`: Device Authorization Grant. Zeigt über `show` den Link (`verification_uri_complete`, sonst `verification_uri` + `user_code`), pollt im Abstand `interval` (Standard 5 s, bei `slow_down` +5 s) bis zum Erfolg oder `expires_in` und speichert das Refresh-Token.
  - `OpenTriClient.fetch_bundle(start: date, end: date) -> bytes | None`: ZIP-Bytes, oder `None`, wenn kein Plan aktiv ist (204) oder der Export im Zeitraum leer ist (404)

- [ ] **Step 1: Failing tests schreiben**

Mit `responses`, `tmp_path` als `data_dir` und einem unsignierten JWT mit `sub` und `exp`.

```python
def test_device_login_stores_refresh_token(tmp_path, rsps): ...
    # POST .../auth/device form client_id=mywhoosh-garmin-sync, scope="openid offline_access" -> device_code, verification_uri_complete, interval 5, expires_in 600
    # token POST grant_type=urn:ietf:params:oauth:grant-type:device_code: authorization_pending, slow_down, then success
    # assert: show() got the link; sleep() called with 5, then 10 after slow_down; refresh token file written, mode 0600
def test_device_login_expired_or_denied_raises_auth_error(tmp_path, rsps): ...  # expired_token / access_denied -> OpenTriAuthError
def test_fetch_uses_refresh_then_active_plan_then_export(client_with_refresh_token, rsps): ...
    # token POST grant_type=refresh_token, client_id=mywhoosh-garmin-sync
    # GET {api}/athletes/<sub>/training-plans/active -> {"id": "p1", ...}
    # GET {api}/athletes/<sub>/training-plans/p1/fit?from=2026-11-07&to=2026-11-20&schedule=true -> zip bytes (Content-Type application/zip)
    # assert result == zip bytes; Authorization Bearer header on both API calls
def test_rotated_refresh_token_is_saved(tmp_path, rsps): ...           # refresh response carries a new refresh_token -> file updated; next refresh uses it
def test_no_refresh_token_raises_auth_error_with_hint(tmp_path): ...  # no session file -> OpenTriAuthError, "login-opentri" in str(exc), no HTTP call
def test_rejected_refresh_raises_auth_error_with_hint(client_with_refresh_token, rsps): ...  # 400 invalid_grant -> OpenTriAuthError, token not in str(exc)
def test_access_token_reused_until_expiry(client, rsps): ...          # two fetches, one token call
def test_api_401_refreshes_once(client, rsps): ...
def test_no_active_plan_returns_none(client, rsps): ...               # 204
def test_empty_export_returns_none(client, rsps): ...                 # 404
@pytest.mark.parametrize("ctype,body", [("text/html", b"<html>"), ("application/json", b"{}")])
def test_non_zip_200_raises_unavailable(client, rsps, ctype, body): ...  # Review Focus 3
def test_network_error_raises_unavailable(client, rsps): ...
def test_tokens_never_logged(client, rsps, caplog): ...                # DEBUG level; no token/password substrings
```

- [ ] **Step 2: Tests laufen lassen, sie schlagen fehl**

Run: `uv run pytest tests/test_opentri.py -v`
Expected: FAIL mit `ImportError`

- [ ] **Step 3: `opentri.py` implementieren**

- Device-Endpunkt `<keycloak_url>/realms/<realm>/protocol/openid-connect/auth/device`, Token-Endpunkt `<keycloak_url>/realms/<realm>/protocol/openid-connect/token`.
- Refresh-Token in `data_dir/opentri_session.json`, atomar geschrieben (tmp + `os.replace`), Modus `0o600`. Ein rotiertes Refresh-Token wird sofort gespeichert. Ohne gespeichertes Token kein HTTP-Aufruf, sondern `OpenTriAuthError` mit Hinweis auf `docker compose run --rm sync login-opentri` bzw. die Portainer-Konsole.
- Access-Token nur im Speicher, nutzbar bis 60 s vor `exp`. `sub` wird aus dem Payload gelesen (Base64url, ohne Signaturprüfung).
- Timeouts 30 s. Ein 200 ohne ZIP-Signatur (`PK\x03\x04`) gilt als `OpenTriUnavailableError`.

- [ ] **Step 4: Tests laufen lassen**

Run: `uv run pytest tests/test_opentri.py -v`
Expected: alle bestanden

- [ ] **Step 5: Commit**

```bash
git add src/mywhoosh_garmin_sync/opentri.py tests/test_opentri.py
git commit -m "feat: Fetch planned workouts from open-tri via Keycloak" -- src/mywhoosh_garmin_sync/opentri.py tests/test_opentri.py
```

---

### Task 5: Garmin-Hülle für Trainings und Kalender

**Files:**
- Modify: `src/mywhoosh_garmin_sync/garmin.py` (öffentliche Methode `client() -> Garmin` und `drop_session() -> None` am `GarminUploader`)
- Create: `src/mywhoosh_garmin_sync/garmin_workouts.py`
- Test: `tests/test_garmin_workouts.py`, `tests/test_garmin.py` (für die zwei neuen Methoden)

**Interfaces:**
- Consumes: `GarminUploader`, `GarminAuthError`, `GarminUnavailableError`, `_http_status` aus `garmin.py`; Feldnamen aus dem Ergebnisdokument von Task 1
- Produces:
  - `GarminUploader.client() -> Garmin`: `ensure_login()` und dann die Sitzung zurückgeben. `GarminUploader.drop_session() -> None`: Sitzung verwerfen, nicht pausieren.
  - `class GarminWorkoutError(Exception)`: Garmin lehnt eine einzelne Operation ab (HTTP-Fehler außer 401/403/429)
  - `@dataclass(frozen=True) class ScheduledEntry: schedule_id: int; workout_id: int; date: date; name: str`
  - `class GarminWorkouts(uploader: GarminUploader)` mit:
    - `create(workout_json: dict) -> int` (workoutId)
    - `update(workout_id: int, workout_json: dict) -> None`
    - `delete(workout_id: int) -> None` (404 gilt als Erfolg)
    - `schedule(workout_id: int, day: date) -> int` (scheduleId)
    - `reschedule(schedule_id: int, day: date) -> None`
    - `unschedule(schedule_id: int) -> None` (404 gilt als Erfolg)
    - `scheduled(start: date, end: date) -> list[ScheduledEntry]`
    - `description(workout_id: int) -> str | None` (über `get_workout_by_id`)
    - `send_to_devices() -> None`

- [ ] **Step 1: Failing tests schreiben**

Mit einem `FakeGarmin` hinter einem `GarminUploader` mit `client_factory`, wie in `tests/test_garmin.py`.

```python
def test_create_returns_workout_id(): ...                 # upload_workout -> {"workoutId": 77, ...} -> 77
def test_schedule_returns_schedule_id(): ...             # Schlüssel laut Ergebnisdokument
def test_scheduled_maps_entries(): ...                   # get_scheduled_workouts_by_period -> ScheduledEntry list
def test_reschedule_uses_iso_date(): ...
def test_delete_and_unschedule_tolerate_404(): ...
def test_http_error_is_workout_error(): ...              # 400/500 with .response -> GarminWorkoutError
@pytest.mark.parametrize("status", [401, 403])
def test_rejected_session_drops_session_and_is_unavailable(status): ...  # GarminUnavailableError, next call logs in again
def test_rate_limit_and_network_are_unavailable(): ...   # 429, no response, requests.ConnectionError
def test_auth_error_propagates(): ...                    # GarminAuthError from ensure_login passes through unchanged
```

- [ ] **Step 2: Tests laufen lassen, sie schlagen fehl**

Run: `uv run pytest tests/test_garmin_workouts.py tests/test_garmin.py -v`
Expected: FAIL mit `ImportError` bzw. `AttributeError`

- [ ] **Step 3: Implementieren**

In `garmin.py` nur die zwei öffentlichen Methoden ergänzen, der bestehende Import-Code bleibt unverändert. `GarminWorkouts` ruft für jede Operation `uploader.client()` auf und ordnet die Fehler wie in der Interface-Liste zu. Bei 401/403 ruft es `uploader.drop_session()` auf.

- [ ] **Step 4: Tests laufen lassen**

Run: `uv run pytest tests/test_garmin_workouts.py tests/test_garmin.py -v`
Expected: alle bestanden

- [ ] **Step 5: Commit**

```bash
git add src/mywhoosh_garmin_sync/garmin.py src/mywhoosh_garmin_sync/garmin_workouts.py tests/test_garmin_workouts.py tests/test_garmin.py
git commit -m "feat: Wrap Garmin workout and calendar operations" -- src/mywhoosh_garmin_sync/garmin.py src/mywhoosh_garmin_sync/garmin_workouts.py tests/test_garmin_workouts.py tests/test_garmin.py
```

---

### Task 6: Zustandsdatei für Trainings

**Files:**
- Create: `src/mywhoosh_garmin_sync/workout_state.py`
- Test: `tests/test_workout_state.py`

**Interfaces:**
- Consumes: `StateCorruptError` aus `state.py` (wiederverwenden, nicht neu definieren)
- Produces:
  - `WorkoutStatus = Literal["active", "past", "removed"]`
  - `@dataclass(frozen=True) class WorkoutEntry: serial: int; name: str; day: date; status: WorkoutStatus; workout_id: int | None; schedule_id: int | None; content_hash: str | None; in_library: bool; attempts: int; last_error: str | None; updated_at: str`
  - `class WorkoutStateStore(path: Path)` mit `exists() -> bool`, `load() -> None` (wirft `StateCorruptError`), `all() -> dict[int, WorkoutEntry]`, `put(entry: WorkoutEntry) -> None`, `put_many(entries: list[WorkoutEntry]) -> None` (eine atomare Schreibung, legt die Datei auch leer an)

- [ ] **Step 1: Failing tests schreiben**

```python
def test_roundtrip_all_fields(tmp_path): ...           # incl. date, None ids, attempts, last_error
def test_put_many_empty_creates_file(tmp_path): ...
def test_atomic_write_no_tmp_left(tmp_path): ...
@pytest.mark.parametrize("content", ["", "{x", "[]", '{"version": 1}', '{"version": 1, "workouts": {"1": {"status": "nope"}}}'])
def test_corrupt_raises(tmp_path, content): ...
def test_put_on_corrupt_does_not_overwrite(tmp_path): ...
```

- [ ] **Step 2: Tests laufen lassen, sie schlagen fehl**

Run: `uv run pytest tests/test_workout_state.py -v`
Expected: FAIL mit `ImportError`

- [ ] **Step 3: `workout_state.py` implementieren**

Format `{"version": 1, "workouts": {"<serial>": {...}}}`, `day` als ISO-Datum. Schreiben wie `state.py` (tmp + `os.replace`). Typen beim Laden prüfen, Abweichungen ergeben `StateCorruptError`.

- [ ] **Step 4: Tests laufen lassen**

Run: `uv run pytest tests/test_workout_state.py -v`
Expected: alle bestanden

- [ ] **Step 5: Commit**

```bash
git add src/mywhoosh_garmin_sync/workout_state.py tests/test_workout_state.py
git commit -m "feat: Add persistent state for synced workouts" -- src/mywhoosh_garmin_sync/workout_state.py tests/test_workout_state.py
```

---

### Task 7: Abgleich

**Files:**
- Create: `src/mywhoosh_garmin_sync/workout_sync.py`
- Test: `tests/test_workout_sync.py`

**Interfaces:**
- Consumes: alles aus Task 2 bis 6, `DELETE_PAST_LIBRARY_WORKOUTS` aus dem Ergebnisdokument von Task 1
- Produces:
  - `class WorkoutSource(Protocol)`: `fetch_bundle(start: date, end: date) -> bytes | None`
  - `class WorkoutTarget(Protocol)`: die Methoden von `GarminWorkouts` (Task 5)
  - `MAX_WORKOUT_ATTEMPTS = 5`
  - `DELETE_PAST_LIBRARY_WORKOUTS: bool` (Wert aus Task 1)
  - `@dataclass class WorkoutRunSummary: created: int = 0; updated: int = 0; moved: int = 0; removed: int = 0; adopted: int = 0; skipped: int = 0; failed: int = 0; aborted: str | None = None`
  - `sync_workouts(source: WorkoutSource, garmin: WorkoutTarget, store: WorkoutStateStore, today: date, days: int, pool_length_m: float, now: Callable[[], datetime]) -> WorkoutRunSummary`

- [ ] **Step 1: Failing tests schreiben**

Fakes: `FakeSource` liefert ein Bundle aus `tests/workoutfactory.py` oder `None` oder wirft. `FakeGarminWorkouts` hält Bibliothek und Kalender im Speicher, protokolliert Aufrufe und kann pro Operation Fehler werfen.

```python
def test_new_workouts_are_created_scheduled_and_sent(): ...        # 2 workouts -> 2 create + 2 schedule + 1 send_to_devices; state active
def test_nothing_changed_makes_no_garmin_writes(): ...             # 2nd run: only scheduled() read, no writes, no send
def test_content_change_updates_workout(): ...                     # same serial, other step -> update(), hash stored
def test_moved_day_reschedules(): ...                              # same serial, new date -> reschedule()
def test_removed_in_opentri_unschedules_and_deletes(): ...         # serial gone, day >= today
def test_past_day_becomes_past_and_is_left_alone(): ...            # Review Focus 4: today injected; yesterday's entry -> status past, no writes
def test_past_library_cleanup_follows_flag(monkeypatch): ...       # DELETE_PAST_LIBRARY_WORKOUTS True -> delete(); False -> nothing; calendar entry never unscheduled
def test_missed_and_moved_forward_replaces_past_entry(): ...       # past serial reappears on a future day -> unschedule old, (re)create if not in_library, schedule new
def test_moved_into_past_is_treated_as_removed(): ...              # serial disappears from window, last day >= today -> removed
def test_calendar_moved_in_garmin_is_corrected(): ...              # FakeGarmin entry on other day -> reschedule back
def test_calendar_entry_deleted_in_garmin_is_rescheduled(): ...
def test_unmarked_garmin_workouts_are_never_touched(): ...
def test_missing_state_adopts_marked_workouts(): ...               # no state file, Garmin has marked entries -> adopted, no create; hash differs -> update
def test_marked_orphan_not_in_opentri(): ...                       # Review Focus 5: future -> removed; past -> left
def test_two_workouts_same_day(): ...                              # Review Focus 1
def test_no_active_plan_removes_future_entries(): ...              # source returns None -> all active entries removed
def test_bundle_error_aborts_without_writes(): ...                 # Review Focus 3: WorkoutBundleError / OpenTriUnavailableError -> aborted, no deletes
def test_opentri_auth_error_aborts(): ...
def test_garmin_unavailable_aborts_without_counting(): ...
def test_single_workout_error_counts_attempts_until_failed(): ...  # GarminWorkoutError on create -> attempts 1..5, then failed (status stays, no more tries), others proceed
def test_corrupt_state_aborts(): ...
def test_skipped_unsupported_sport_counts(): ...
```

- [ ] **Step 2: Tests laufen lassen, sie schlagen fehl**

Run: `uv run pytest tests/test_workout_sync.py -v`
Expected: FAIL mit `ImportError`

- [ ] **Step 3: `workout_sync.py` implementieren**

Reihenfolge eines Laufs:
1. Zustand laden (`StateCorruptError` ⇒ abort).
2. Bundle für das Fenster holen und lesen (Fehler ⇒ abort, ohne Garmin-Schreibzugriffe). `None` ⇒ leere Soll-Liste.
3. Einträge vor heute auf `past` setzen, Bibliothek je nach Flag aufräumen.
4. `garmin.scheduled(fenster_start, fenster_ende)` einmal lesen. Markierte Einträge ohne Zustand über `description()` adoptieren.
5. Soll gegen Ist abgleichen wie in der Spec, Abschnitt "Zuordnung und Änderungen" und "Vergangene Trainings".
6. Bei mindestens einer Schreiboperation `send_to_devices()`.

Fehlerzähler pro Seriennummer wie `MAX_ATTEMPTS` im Aktivitäten-Sync: Bei `GarminWorkoutError` steigt `attempts`, ab 5 wird das Training nicht mehr versucht, bis sich sein Inhalt oder Tag in open-tri ändert. Log pro Training eine INFO-Zeile, pro Lauf eine Zusammenfassung, beides auf Deutsch.

- [ ] **Step 4: Tests laufen lassen**

Run: `uv run pytest tests/test_workout_sync.py -v`
Expected: alle bestanden

- [ ] **Step 5: Commit**

```bash
git add src/mywhoosh_garmin_sync/workout_sync.py tests/test_workout_sync.py
git commit -m "feat: Reconcile planned open-tri workouts with Garmin Connect" -- src/mywhoosh_garmin_sync/workout_sync.py tests/test_workout_sync.py
```

---

### Task 8: Konfiguration, CLI, Deployment und Doku

**Files:**
- Modify: `src/mywhoosh_garmin_sync/config.py`, `src/mywhoosh_garmin_sync/cli.py`, `deployment/docker-compose.yml`, `.env.example`, `README.md`
- Test: `tests/test_config.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `OpenTriClient` (Task 4), `GarminWorkouts` (Task 5), `WorkoutStateStore` (Task 6), `sync_workouts` (Task 7)
- Produces:
  - `@dataclass(frozen=True) class OpenTriSettings: api_url: str; keycloak_url: str; realm: str; client_id: str; days: int; pool_length_m: float`
  - `Settings.opentri: OpenTriSettings | None` (None, wenn weder `OPENTRI_API_URL` noch `OPENTRI_KEYCLOAK_URL` gesetzt ist)
  - neuer Befehl `login-opentri`: ruft `OpenTriClient.device_login(print)` auf, Exit 0 bei Erfolg, 1 mit deutscher Meldung bei `OpenTriError`, 2 wenn open-tri nicht konfiguriert ist
  - neuer Befehl `workouts-dry-run`: holt und rechnet um, schreibt das JSON pro Training nach `<data_dir>/workouts-dry-run/<datum>-<serial>.json` und berührt Garmin nicht

- [ ] **Step 1: Failing tests schreiben**

```python
def test_opentri_settings_absent_when_no_vars(): ...               # opentri is None
def test_opentri_settings_all_values_and_defaults(): ...           # realm open-tri, client mywhoosh-garmin-sync, days 14, pool 25
def test_partial_opentri_vars_is_config_error(): ...               # only one of the two URLs -> names listed
def test_login_opentri_calls_device_login(...): ...                  # exit 0; OpenTriError -> 1; not configured -> 2
@pytest.mark.parametrize("name,value", [("WORKOUT_SYNC_DAYS", "0"), ("WORKOUT_SYNC_DAYS", "x"), ("WORKOUT_POOL_LENGTH_M", "-1")])
def test_invalid_workout_values(name, value): ...
def test_run_calls_both_steps_each_interval(...): ...              # monkeypatch run_once and sync_workouts
def test_workout_step_skipped_without_opentri(...): ...
def test_workout_step_failure_does_not_stop_activity_step(...): ...  # sync_workouts raises -> run_once still called next loop; and vice versa
def test_once_runs_both_and_returns_1_if_either_aborted(...): ...
def test_workouts_dry_run_writes_json_without_garmin(...): ...
```

- [ ] **Step 2: Tests laufen lassen, sie schlagen fehl**

Run: `uv run pytest tests/test_config.py tests/test_cli.py -v`
Expected: FAIL

- [ ] **Step 3: Implementieren**

- `today` für `sync_workouts` ist `datetime.now(ZoneInfo("Europe/Berlin")).date()`.
- Zustand in `<data_dir>/workouts.json`. Die `GarminUploader`-Instanz wird von beiden Schritten geteilt.
- `deployment/docker-compose.yml` bekommt die neuen Variablen als `${…}`, die optionalen mit Standardwert.
- `.env.example` bekommt die neuen Variablen auskommentiert.
- README: Einrichtung des Keycloak-Clients `mywhoosh-garmin-sync` (öffentlich, nur OAuth 2.0 Device Authorization Grant) und einmalig `mywhoosh-garmin-sync login-opentri` in der Portainer-Konsole.
- README: neuer Abschnitt "Geplante Trainings aus open-tri". Er beschreibt, was synchronisiert wird (Fenster, Sportarten, open-tri als Quelle der Wahrheit, Verhalten bei vergangenen und verschobenen Trainings) und die Variablen. Dazu `workouts-dry-run` in der Portainer-Konsole und den Hinweis, dass der Sync nur Trainings mit `open-tri:`-Markierung anfasst.

- [ ] **Step 4: Alle Tests laufen lassen**

Run: `uv run pytest -q`
Expected: alle bestanden

- [ ] **Step 5: Commit**

```bash
git add src/mywhoosh_garmin_sync/config.py src/mywhoosh_garmin_sync/cli.py tests/test_config.py tests/test_cli.py deployment/docker-compose.yml .env.example README.md
git commit -m "feat: Sync planned open-tri workouts to Garmin in the run loop" -- src/mywhoosh_garmin_sync/config.py src/mywhoosh_garmin_sync/cli.py tests/test_config.py tests/test_cli.py deployment/docker-compose.yml .env.example README.md
```

- [ ] **Step 6: Abnahme durch den Betreiber (nicht automatisierbar)**

1. Neues Image ausrollen, im Portainer-Stack `OPENTRI_API_URL` und `OPENTRI_KEYCLOAK_URL` setzen, in der Konsole einmalig `mywhoosh-garmin-sync login-opentri`.
2. In der Konsole `mywhoosh-garmin-sync workouts-dry-run` ausführen und das JSON eines Trainings prüfen.
3. Container neu starten. Im Log stehen die angelegten Trainings, im Garmin-Kalender die nächsten 14 Tage, auf der Uhr nach dem nächsten Sync der Garmin-App.
4. In open-tri ein Training verschieben. Nach höchstens 15 Minuten steht es in Garmin am neuen Tag.
