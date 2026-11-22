# Geplante Radtrainings nach MyWhoosh – Implementierungsplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Der Sync-Container legt die geplanten Radeinheiten des aktiven open-tri-Plans für die nächsten 14 Tage als Custom Workouts in MyWhoosh an, mit dem Datum im Namen. Er zieht Änderungen nach und räumt vergangene Workouts wieder ab.

**Architecture:** Ein dritter Schritt in der bestehenden Lauf-Schleife, nach dem Garmin-Trainingsschritt.
- Quelle: aktiver Plan aus open-tri und pro Training die `.zwo` (`OpenTriClient`).
- Umrechnung: `.zwo` → MyWhoosh-JSON (neues Modul `mywhoosh_workout_json.py`).
- Ziel: Coaching-API von MyWhoosh (neues Modul `mywhoosh_workouts.py`).
- Abgleich: neues Modul `mywhoosh_workout_sync.py`, mit eigener Zustandsdatei `/data/mywhoosh_workouts.json` (neues Modul `mywhoosh_workout_state.py`).

Aufbau und Fehlerverhalten folgen dem Garmin-Trainingssync (`workout_sync.py`, `workout_state.py`).

**Tech Stack:** Python 3.11+, `requests`, `xml.etree.ElementTree` (Standardbibliothek), `pytest`, `responses`, `uv`.

**Spec:** `docs/superpowers/specs/2026-11-08-opentri-mywhoosh-workouts-design.md`

## Global Constraints

- open-tri ist die einzige Quelle der Wahrheit. Keine Änderungen an open-tri.
- Nur Workouts mit Markierung `open-tri:<id der geplanten Einheit>` in `Description` werden gelöscht. Gelöscht wird nur, wenn **alle** Einträge mit dieser `WorkoutId` in der gerade gelesenen Liste markiert sind.
- Kein Kauf, kein Abo, keine `/payment/*`- und keine `/custom-training-plan`-Aufrufe.
- Den reCAPTCHA nicht umgehen. Ist ein Captcha nötig, stoppt das Vorhaben nach Task 1.
- Name: `<Wochentag> <TT.MM.> W<n>: [<brickOrder+1>. ]<Trainingsname>`. Wochentage: `Mo Di Mi Do Fr Sa So`. `W<n>` = `floorDiv((date - startDate).days, 7) + 1`. Gekürzt wird nur der Trainingsname.
- Fenster: heute bis heute + (`WORKOUT_SYNC_DAYS` − 1), Datum in `Europe/Berlin`.
- Soll-Menge: die frühesten Radeinheiten im Fenster nach (Datum, `brickOrder` mit None zuerst), höchstens `CurrentCredits` + Anzahl der eigenen Workouts.
- Schreibaufrufe nacheinander: Jeder Upload und jedes DELETE wird über `my-workouts` bestätigt (höchstens 30 Sekunden), bevor der nächste folgt. Ist er nicht bestätigt, endet das Schreiben für diesen Lauf.
- Die `WorkoutId` vergibt der Sync selbst (`workout_id_for(planned_id)`). Eine Änderung ist ein erneuter Upload mit derselben `Id` (Upsert, kein Slot).
- Ergebnisse der Probe und feste Feldwerte: `docs/superpowers/specs/2026-11-08-mywhoosh-workout-probe-results.md`, Abschnitt „Für den Builder“.
- Schalter `MYWHOOSH_WORKOUT_SYNC=true`. Gesetzt ohne `OPENTRI_API_URL`/`OPENTRI_KEYCLOAK_URL` → Konfigurationsfehler, Exit-Code 2.
- Keine Tokens, Passwörter oder Presigned-URLs im Log. Logtexte deutsch, wie im Rest des Projekts.
- Fehler im MyWhoosh-Schritt stoppen weder den Aktivitäten- noch den Garmin-Sync.

## Review Focus

- **Dasselbe Training zweimal im Fenster** (zwei geplante Einheiten, eine `.zwo`): Es entstehen zwei Workouts mit eigenen Markierungen und Namen, die `.zwo` wird nur einmal geholt. Test in Task 6.
- **Lauf kurz nach Mitternacht:** Das Workout von gestern wird gelöscht, das von heute bleibt. Maßgeblich ist `today`, das der Aufrufer in `Europe/Berlin` bestimmt. Test in Task 6 und Task 7.
- **Markiertes Workout ohne Eintrag im Zustand und außerhalb der Soll-Menge** (zum Beispiel aus einem älteren Lauf mit verlorener Zustandsdatei): Es wird gelöscht und nicht ewig mitgeschleppt. Test in Task 6.
- **Ein Schreibaufruf wird nie sichtbar** (wie das verlorene zweite DELETE in der Probe): Das Schreiben endet für diesen Lauf. Kein Fehlerzähler, keine Exception, eine WARNING, und der Zustand gibt nur Bestätigtes wieder. Test in Task 6.
- **MyWhoosh fällt mitten im Lauf aus**, nachdem schon gelöscht wurde: Jede Schreibaktion ist sofort im Zustand gespeichert. Der nächste Lauf legt nichts doppelt an und vergisst nichts. Test in Task 6.

---

## File Structure

| Datei | Aufgabe |
|---|---|
| `scripts/probe_mywhoosh_workouts.py` (neu) | Wegwerf-Probe für die Coaching-API, interaktiv |
| `docs/superpowers/specs/2026-11-08-mywhoosh-workout-probe-results.md` (neu) | Ergebnisse der Probe |
| `src/mywhoosh_garmin_sync/mywhoosh_workout_json.py` (neu) | `.zwo` lesen, Name bilden, MyWhoosh-JSON bauen, Markierung, Hash |
| `src/mywhoosh_garmin_sync/opentri.py` (ändern) | Radeinheiten des aktiven Plans und `.zwo` holen |
| `src/mywhoosh_garmin_sync/mywhoosh_workouts.py` (neu) | Coaching-API: lesen, anlegen, löschen, Slots |
| `src/mywhoosh_garmin_sync/mywhoosh.py` (ändern, je nach Probe) | Token für die Coaching-API bereitstellen |
| `src/mywhoosh_garmin_sync/mywhoosh_workout_state.py` (neu) | Zustandsdatei pro geplanter Einheit |
| `src/mywhoosh_garmin_sync/mywhoosh_workout_sync.py` (neu) | Abgleich eines Laufs |
| `src/mywhoosh_garmin_sync/config.py`, `cli.py` (ändern) | Schalter, Schritt in der Schleife, Dry-Run |
| `README.md`, `docker-compose.yml`, `deployment/docker-compose.yml`, `.env.example` (ändern) | neue Variable und neuer Befehl |
| `tests/zwofactory.py` (neu) | `.zwo`-Testdaten bauen |
| `tests/test_mywhoosh_workout_json.py`, `tests/test_mywhoosh_workouts.py`, `tests/test_mywhoosh_workout_state.py`, `tests/test_mywhoosh_workout_sync.py` (neu); `tests/test_opentri.py`, `tests/test_config.py`, `tests/test_cli.py` (ändern) | Tests |

---

### Task 1: Probe der Coaching-API (mit dem Betreiber, Gate)

Dieser Task schreibt keinen Produktcode. Er beantwortet die Prüfpunkte 1 bis 4 der Spec und legt die Konstanten fest, die die Tasks 2 und 4 brauchen. **Zeigt die Probe eines der Stopp-Ergebnisse, endet der Plan hier und geht zurück an den Betreiber.**

**Files:**
- Create: `scripts/probe_mywhoosh_workouts.py`
- Create: `docs/superpowers/specs/2026-11-08-mywhoosh-workout-probe-results.md`

**Interfaces:**
- Produces (im Ergebnisdokument, als Abschnitt „Für den Builder“):
  - `AUTH_VARIANT`: `"app"` (App-Token aus `MyWhooshClient` geht) oder `"service20"` (Login an `service20.mywhoosh.com/api/auth/login` ohne Captcha geht)
  - Herkunft der `UserId`
  - das vollständige Feldset eines angelegten Workouts
  - `StepType` und Felder für Rampe und FreeRide
  - `MAX_NAME_LENGTH`
  - ob die Anlege-Antwort die `WorkoutId` liefert, und wo
  - Antwort bei fehlendem Slot, falls gesehen

- [ ] **Step 1: Probe-Skript schreiben**

Aufbau wie `scripts/probe_workouts.py`: PEP-723-Kopf mit `requests`, Passwort per `getpass`, Tokens nur gekürzt ausgeben, persönliche IDs über `redact()` schwärzen. Basis `https://coaching.mywhoosh.com/api/v2`. Nacheinander, nach jeder Phase `input("weiter?")`:
1. App-Login wie `MyWhooshClient._login` (Payload und URL von dort kopieren), dann `GET /workout-builder/my-workouts` und `GET /client/my-credits` mit diesem Token. Status und Body ausgeben. Die Claim-Namen des Tokens ausgeben, ohne Werte.
2. Nur wenn 1 mit 401/403 scheitert: `POST https://service20.mywhoosh.com/api/auth/login` mit `{"email", "password", "role": "player"}` ohne `token`. Status und Meldung ausgeben. Scheitert das mit einem Captcha-Hinweis, „STOPP: Captcha nötig“ ausgeben und beenden.
3. Ein Probe-Workout `PROBE open-tri sync` mit `Description` `open-tri:probe` anlegen. Grundlage ist ein Workout-Dict, das der Betreiber vorher im Editor aus `tests/fixtures/zwo/probe.zwo` erzeugt hat (siehe Step 2), mit den Blöcken SteadyState, Ramp aufwärts, Ramp abwärts und FreeRide. Den Request-Body nimmt der Betreiber im Browser-Netzwerk-Tab auf und speichert ihn als `tests/fixtures/local/editor_upload.json`. Das Skript schickt diesen Body mit geändertem Namen und geänderter Description. Danach antwortet es mit der Body-Ausgabe und liest `my-workouts`.
4. Ein zweites Probe-Workout gleichen Inhalts anlegen. Prüfen, ob die beiden `WorkoutId`s verschieden sind und ob `Description` unverändert zurückkommt. Einen Namen mit 120 Zeichen probieren und ausgeben, wie viel davon gespeichert wird.
5. Beide Probe-Workouts per `DELETE /client/custom-workout-upload/0/<WorkoutId>` löschen. `my-credits` und `my-workouts` alle 2 Sekunden lesen, höchstens 30 Sekunden lang, und ausgeben, wann der Slot zurück ist.

`tests/fixtures/zwo/probe.zwo` anlegen:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<workout_file><author>open-tri</author><name>Probe</name><sportType>bike</sportType><workout>
<SteadyState Duration="300" Power="0.600" Cadence="90"/>
<Ramp Duration="300" PowerLow="0.500" PowerHigh="0.800"/>
<Ramp Duration="300" PowerLow="0.800" PowerHigh="0.500"/>
<FreeRide Duration="120"/>
</workout></workout_file>
```

- [ ] **Step 2: Mit dem Betreiber ausführen**

Der Betreiber importiert `tests/fixtures/zwo/probe.zwo` im Editor, nimmt den Upload-Body auf (Step 1, Phase 3) und löscht das Editor-Workout wieder. Danach führt er `uv run scripts/probe_mywhoosh_workouts.py` aus. `tests/fixtures/local/` steht bereits in `.gitignore`.
Erwartet: Ausgabe aller fünf Phasen, oder „STOPP“.

- [ ] **Step 3: Ergebnisse festhalten und Stopp prüfen**

Das Ergebnisdokument schreiben, im Aufbau wie `2026-11-07-workout-probe-results.md`, mit dem Abschnitt „Für den Builder“ (siehe Interfaces).
**STOPP**, Rückmeldung an den Betreiber und keine weiteren Tasks, wenn eines davon zutrifft:
- Ein Captcha ist nötig.
- Zwei angelegte Workouts haben dieselbe `WorkoutId`.
- `Description` kommt verändert zurück.

Außerdem den Betreiber bitten, Prüfpunkt 5 an der Rolle zu prüfen (erscheint ein neues Workout ohne Neustart der App?), und das Ergebnis eintragen.

- [ ] **Step 4: Commit**

```bash
git add scripts/probe_mywhoosh_workouts.py tests/fixtures/zwo/probe.zwo docs/superpowers/specs/2026-11-08-mywhoosh-workout-probe-results.md
git commit -m "chore: Probe the MyWhoosh coaching workout API"
```

---

### Task 2: `.zwo` → MyWhoosh-JSON

**Files:**
- Create: `src/mywhoosh_garmin_sync/mywhoosh_workout_json.py`
- Create: `tests/zwofactory.py`, `tests/test_mywhoosh_workout_json.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class ZwoBlock:
      kind: Literal["steady", "ramp", "free"]
      seconds: int
      power: float | None        # steady
      start_power: float | None  # ramp
      end_power: float | None    # ramp
      cadence: int | None

  @dataclass(frozen=True)
  class ZwoWorkout:
      name: str
      blocks: tuple[ZwoBlock, ...]

  class ZwoError(ValueError): ...
  MAX_NAME_LENGTH = 100
  MARKER_PREFIX = "open-tri:"
  def parse_zwo(data: bytes) -> ZwoWorkout
  def workout_name(day: date, week: int, brick_order: int | None, training_name: str) -> str
  def workout_id_for(planned_id: str) -> int     # 100_000_000 + int(sha256(planned_id)) % 900_000_000
  def to_mywhoosh_json(workout: ZwoWorkout, name: str, planned_id: str) -> dict[str, Any]
      # "Id" = workout_id_for(planned_id)
  def marker(planned_id: str) -> str
  def planned_id_from_description(description: object) -> str | None
  def content_hash(workout_json: dict[str, Any]) -> str
  ```
- `tests/zwofactory.py`: `make_zwo(name: str, blocks: list[str]) -> bytes`. `blocks` sind rohe XML-Elemente wie `'<SteadyState Duration="600" Power="0.650"/>'`.

- [ ] **Step 1: Failing Tests schreiben**

```python
def test_parse_steady_ramp_free_with_cadence():
    w = parse_zwo(make_zwo("Grundlage", [
        '<SteadyState Duration="600" Power="0.650" Cadence="90"/>',
        '<Ramp Duration="300" PowerLow="0.800" PowerHigh="0.500"/>',
        '<FreeRide Duration="120"/>']))
    assert w.name == "Grundlage"
    assert w.blocks == (
        ZwoBlock("steady", 600, 0.65, None, None, 90),
        ZwoBlock("ramp", 300, None, 0.8, 0.5, None),
        ZwoBlock("free", 120, None, None, None, None))

@pytest.mark.parametrize("blocks", [[], ['<IntervalsT Repeat="3" OnDuration="60" OffDuration="60"/>'],
                                    ['<SteadyState Power="0.6"/>']])
def test_parse_rejects_unknown_empty_or_incomplete(blocks):
    with pytest.raises(ZwoError):
        parse_zwo(make_zwo("X", blocks))

def test_parse_rejects_non_xml():
    with pytest.raises(ZwoError):
        parse_zwo(b"<html>proxy</html")

def test_workout_name_plain_and_brick():
    assert workout_name(date(2026, 11, 9), 3, None, "Grundlage") == "Mo 09.11. W3: Grundlage"
    assert workout_name(date(2026, 11, 15), 4, 1, "Rad") == "So 15.11. W4: 2. Rad"

def test_workout_name_cuts_training_name_only():
    name = workout_name(date(2026, 11, 9), 3, None, "Ü" * 300)
    assert len(name) == MAX_NAME_LENGTH
    assert name.startswith("Mo 09.11. W3: ÜÜ")

def test_json_steps_and_totals():
    w = parse_zwo(make_zwo("G", ['<SteadyState Duration="600" Power="0.650" Cadence="90"/>',
                                 '<Ramp Duration="300" PowerLow="0.500" PowerHigh="0.800"/>']))
    j = to_mywhoosh_json(w, "Mo 09.11. W3: G", "abc-uuid")
    assert j["Name"] == "Mo 09.11. W3: G"
    assert j["Description"] == "open-tri:abc-uuid"
    assert j["Time"] == 900 and j["StepCount"] == 2
    s1, s2 = j["WorkoutStepsArray"]
    assert (s1["Id"], s1["Power"], s1["Time"], s1["Rpm"]) == (1, 0.65, 600, 90)
    assert (s2["Id"], s2["StartPower"], s2["EndPower"], s2["Power"]) == (2, 0.5, 0.8, 0)
    assert (s1["StepType"], s2["StepType"]) == ("E_Normal", "E_WarmUp")
    assert j["Id"] == workout_id_for("abc-uuid")

def test_json_down_ramp_and_free_ride():
    w = parse_zwo(make_zwo("G", ['<Ramp Duration="300" PowerLow="0.800" PowerHigh="0.500"/>',
                                 '<FreeRide Duration="120"/>']))
    s1, s2 = to_mywhoosh_json(w, "n", "p")["WorkoutStepsArray"]
    assert (s1["StepType"], s1["StartPower"], s1["EndPower"]) == ("E_CoolDown", 0.8, 0.5)
    assert (s2["StepType"], s2["Power"], s2["StartPower"], s2["EndPower"]) == ("E_FreeRide", 0, 0, 0)

def test_json_matches_editor_import_fields():
    # tests/fixtures/zwo/probe.zwo → same keys and fixed values as the editor body in the probe results
    j = to_mywhoosh_json(parse_zwo(Path("tests/fixtures/zwo/probe.zwo").read_bytes()), "n", "p")
    assert (j["Mode"], j["Type"], j["CategoryId"], j["SubcategoryId"], j["WokoutAssociationId"]) == \
        ("E_Ride", "E_Normal", 200000000, 1, 0)
    assert j["IF"] == pytest.approx(0.6467, abs=1e-3)
    assert j["TSS"] == pytest.approx(11.85, abs=0.01)
    assert j["KJ"] == 0

def test_workout_id_is_stable_and_in_range():
    assert workout_id_for("abc") == workout_id_for("abc") != workout_id_for("abd")
    assert 100_000_000 <= workout_id_for("abc") <= 999_999_999

def test_marker_roundtrip_and_foreign_descriptions():
    assert planned_id_from_description(marker("abc")) == "abc"
    for other in (None, "", "Locker!", "open-tri:", 5):
        assert planned_id_from_description(other) is None

def test_hash_ignores_description_but_not_name():
    w = parse_zwo(make_zwo("G", ['<SteadyState Duration="600" Power="0.650"/>']))
    a = to_mywhoosh_json(w, "Mo 09.11. W3: G", "a")
    assert content_hash(a) == content_hash(to_mywhoosh_json(w, "Mo 09.11. W3: G", "b"))
    assert content_hash(a) != content_hash(to_mywhoosh_json(w, "Di 10.11. W3: G", "a"))
```

Die Feldwerte stammen aus „Für den Builder“ der Probe-Ergebnisse.
- `IF`: vierte Wurzel des zeitgewichteten Mittels von Leistung⁴ über alle Schritte außer FreeRide. Für eine Rampe von a nach b ist das Mittel von x⁴ gleich `(b⁵ − a⁵) / (5 · (b − a))`, bei a == b gleich a⁴.
- `TSS` = `Time / 3600 · IF² · 100`, mit `Time` inklusive FreeRide.
- `IF` auf 3 und `TSS` auf 2 Nachkommastellen gerundet. `TSS` wird aus dem ungerundeten `IF` berechnet.
- Ohne Schritt mit Leistung sind `IF` und `TSS` 0.

- [ ] **Step 2: Laufen lassen, Fehlschlag prüfen**

Run: `uv run pytest tests/test_mywhoosh_workout_json.py -q`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implementieren**

- `parse_zwo` mit `xml.etree.ElementTree.fromstring`. Ein `ParseError` wird zu `ZwoError`. Leistung als `float`, Dauer als `int`.
- `to_mywhoosh_json`: übrige Felder mit den festen Werten der Probe als Modulkonstante `_WORKOUT_DEFAULTS`, Schrittfelder als `_STEP_DEFAULTS`. Schritt-Ids ab 1.
- `workout_id_for`: `int.from_bytes(hashlib.sha256(planned_id.encode()).digest()[:8], "big") % 900_000_000 + 100_000_000`.
- `content_hash`: SHA-256 über `json.dumps(..., sort_keys=True, separators=(",", ":"))` ohne `Description`.
- Wochentage über eine eigene Liste, nicht über `strftime` (Locale).

- [ ] **Step 4: Tests grün**

Run: `uv run pytest tests/test_mywhoosh_workout_json.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mywhoosh_garmin_sync/mywhoosh_workout_json.py tests/zwofactory.py tests/test_mywhoosh_workout_json.py
git commit -m "feat: Convert open-tri Zwift workouts to MyWhoosh workouts"
```

---

### Task 3: Radeinheiten und `.zwo` aus open-tri

**Files:**
- Modify: `src/mywhoosh_garmin_sync/opentri.py` (`fetch_bundle` holt den aktiven Plan über die neue Hilfsmethode)
- Test: `tests/test_opentri.py`

**Interfaces:**
- Produces in `opentri.py`:
  ```python
  @dataclass(frozen=True)
  class PlannedBike:
      planned_id: str
      day: date
      brick_order: int | None
      training_id: str
      training_name: str
      week: int          # floorDiv((day - plan.startDate).days, 7) + 1

  def OpenTriClient.fetch_planned_bikes(self, start: date, end: date) -> list[PlannedBike] | None
      # None: no active plan (204). Sorted by (day, brick_order with None first).
  def OpenTriClient.fetch_zwo(self, training_id: str) -> bytes
  ```
- `fetch_zwo` wirft `OpenTriUnavailableError` bei Netzfehler oder 5xx. Bei 4xx wirft es die neue Klasse `OpenTriExportError(OpenTriError)` (eine einzelne Einheit nicht exportierbar, etwa wegen fehlender FTP). So kann der Abgleich nur diese Einheit überspringen.

- [ ] **Step 1: Failing Tests schreiben** (mit `responses`, wie die bestehenden open-tri-Tests)
  - `test_fetch_planned_bikes_filters_sport_and_window`: Plan mit `startDate` 2026-10-26 und Einheiten BIKE 2026-11-09, RUN 2026-11-09, BIKE 2026-11-30 (außerhalb), BIKE 2026-11-08 (davor). Fenster 2026-11-09..2026-11-22. Erwartet genau `[PlannedBike(id, date(2026,11,9), None, training_id, "Grundlage", 3)]`.
  - `test_fetch_planned_bikes_sorts_bricks`: zwei BIKE am selben Tag mit `brickOrder` 1 und `None` → `None` zuerst.
  - `test_fetch_planned_bikes_no_active_plan_returns_none` (204).
  - `test_fetch_planned_bikes_malformed_plan_raises_unavailable`: Einheit ohne `id`, `date` nicht ISO oder `startDate` fehlt → `OpenTriUnavailableError`.
  - `test_fetch_zwo_returns_bytes_and_uses_training_path`: `GET {api}/athletes/{sub}/trainings/{trainingId}/zwo`.
  - `test_fetch_zwo_4xx_raises_export_error_5xx_unavailable`.
- [ ] **Step 2: Laufen lassen, Fehlschlag prüfen.** Run: `uv run pytest tests/test_opentri.py -q`. Expected: FAIL bei den neuen Tests.
- [ ] **Step 3: Implementieren.** `_active_plan() -> dict | None` als Hilfsmethode. `fetch_bundle` nutzt sie und verhält sich unverändert. `_api_get` lässt 4xx außer 401/404 heute als Unavailable durch. Für `fetch_zwo` bekommt es deshalb einen Parameter `client_errors: type[OpenTriError] | None = None`, der bei 4xx geworfen wird.
- [ ] **Step 4: Alle open-tri-Tests grün.** Run: `uv run pytest tests/test_opentri.py -q`. Expected: PASS
- [ ] **Step 5: Commit**

```bash
git add src/mywhoosh_garmin_sync/opentri.py tests/test_opentri.py
git commit -m "feat: Read planned bike sessions and their Zwift files from open-tri"
```

---

### Task 4: Client für die Coaching-API

**Files:**
- Create: `src/mywhoosh_garmin_sync/mywhoosh_workouts.py`
- Modify: `src/mywhoosh_garmin_sync/mywhoosh.py` (Token für die Coaching-API, siehe unten)
- Test: `tests/test_mywhoosh_workouts.py`

**Interfaces:**
- Produces:
  ```python
  COACHING_URL = "https://coaching.mywhoosh.com/api/v2"

  @dataclass(frozen=True)
  class RemoteWorkout:
      workout_id: int
      name: str
      description: str | None

  class MyWhooshWorkoutError(MyWhooshError): ...      # this one workout was rejected
  class MyWhooshNoSlotsError(MyWhooshWorkoutError): ...

  class CoachingAuth(Protocol):
      def token(self) -> str: ...
      def user_id(self) -> str: ...
      def invalidate(self) -> None: ...

  class MyWhooshWorkouts:
      def __init__(self, auth: CoachingAuth, session: requests.Session | None = None) -> None
      def list(self) -> list[RemoteWorkout]
      def credits(self) -> int
      def upload(self, workout_json: dict[str, Any]) -> None   # create or overwrite by workout_json["Id"]; 201, no id in response
      def delete(self, workout_id: int) -> None
  ```
- Fehler:
  - Transport → `MyWhooshConnectionError`.
  - 5xx oder unerwartete Struktur → `MyWhooshUnavailableError`.
  - 401 → einmal `auth.invalidate()` und Wiederholung, danach `MyWhooshAuthError`.
  - 4xx beim Upload → `MyWhooshNoSlotsError`, wenn die Meldung `credit` oder `slot` enthält, ohne Rücksicht auf Groß- und Kleinschreibung. Sonst `MyWhooshWorkoutError` mit der Meldung von MyWhoosh.
- `CoachingAuth`: Laut Probe nimmt die API das App-Token an.
  - `MyWhooshClient` bekommt `coaching_token() -> str` (das vorhandene `_access_token()`), `coaching_user_id() -> str` (Claim `userId` des Tokens) und `invalidate_token() -> None` (`_store_token(None)`).
  - Dazu kommt ein kleiner Adapter `AppCoachingAuth(client: MyWhooshClient)` in `mywhoosh_workouts.py`. `user_id()` wirft `MyWhooshUnavailableError`, wenn der Claim fehlt.

- [ ] **Step 1: Failing Tests schreiben** (mit `responses`)
  - `test_list_parses_workouts_and_skips_malformed_entries`: Eintrag ohne `WorkoutId` → WARNING, die übrigen werden geliefert.
  - `test_list_unexpected_structure_raises_unavailable`
  - `test_credits_reads_current_credits`
  - `test_upload_posts_user_id_and_workout`: `POST …/client/custom-workout-upload`, Body `{"UserId": <auth.user_id()>, "SportsModeType": 0, "WorkoutsData": [json]}`, Antwort 201 `{"status": true, "message": "Custom workout is being uploaded."}` → kein Fehler
  - `test_upload_no_slots_raises_no_slots_error` (4xx mit „Not enough Slot Credits“), `test_upload_other_4xx_raises_workout_error_with_message`, `test_upload_status_false_raises_workout_error` (2xx mit `"status": false`)
  - `test_delete_uses_zero_prefix_path`: `DELETE …/client/custom-workout-upload/0/1794152572`
  - `test_401_invalidates_and_retries_once_then_auth_error`
  - `test_token_never_logged` (caplog enthält das Token nicht)
  - `test_app_auth_uses_client_token_and_user_id_claim`, `test_app_auth_missing_user_id_claim_raises_unavailable` (in `tests/test_mywhoosh_workouts.py`, mit einem Fake-Token wie in `tests/test_mywhoosh.py`)
- [ ] **Step 2: Laufen lassen, Fehlschlag prüfen.** Run: `uv run pytest tests/test_mywhoosh_workouts.py -q`. Expected: FAIL
- [ ] **Step 3: Implementieren.** Header wie `_API_HEADERS` in `mywhoosh.py`, aber `Origin`/`Referer` `https://workout.mywhoosh.com`. Timeout 30 Sekunden.
- [ ] **Step 4: Tests grün.** Run: `uv run pytest tests/test_mywhoosh_workouts.py tests/test_mywhoosh.py -q`. Expected: PASS
- [ ] **Step 5: Commit**

```bash
git add src/mywhoosh_garmin_sync/mywhoosh_workouts.py src/mywhoosh_garmin_sync/mywhoosh.py tests/test_mywhoosh_workouts.py
git commit -m "feat: Add a client for MyWhoosh custom workouts"
```

---

### Task 5: Zustandsdatei

**Files:**
- Create: `src/mywhoosh_garmin_sync/mywhoosh_workout_state.py`
- Test: `tests/test_mywhoosh_workout_state.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class MyWhooshWorkoutEntry:
      planned_id: str
      name: str
      day: date
      workout_id: int | None      # None: not (yet) in MyWhoosh
      content_hash: str | None
      attempts: int
      last_error: str | None
      updated_at: str

  class MyWhooshWorkoutStateStore:
      def __init__(self, path: Path) -> None
      def exists(self) -> bool
      def all(self) -> dict[str, MyWhooshWorkoutEntry]
      def put(self, entry: MyWhooshWorkoutEntry) -> None
      def remove(self, planned_id: str) -> None
  ```
- Dateiformat `{"version": 1, "workouts": {"<planned_id>": {...}}}`. Atomares Schreiben wie `WorkoutStateStore._write`. Eine kaputte Datei oder falsche Typen ergeben `StateCorruptError` aus `state.py`.

- [ ] **Step 1: Failing Tests schreiben:** `test_roundtrip_put_all_remove`, `test_missing_file_is_empty_and_not_exists`, `test_corrupt_json_raises_state_corrupt`, `test_wrong_field_type_raises_state_corrupt`, `test_write_is_atomic_no_tmp_left`.
- [ ] **Step 2: Fehlschlag prüfen.** Run: `uv run pytest tests/test_mywhoosh_workout_state.py -q`. Expected: FAIL
- [ ] **Step 3: Implementieren**, nach dem Muster von `workout_state.py`.
- [ ] **Step 4: Tests grün.** Run: dieselbe Zeile. Expected: PASS
- [ ] **Step 5: Commit**

```bash
git add src/mywhoosh_garmin_sync/mywhoosh_workout_state.py tests/test_mywhoosh_workout_state.py
git commit -m "feat: Persist the mapping of planned sessions to MyWhoosh workouts"
```

---

### Task 6: Abgleich

**Files:**
- Create: `src/mywhoosh_garmin_sync/mywhoosh_workout_sync.py`
- Test: `tests/test_mywhoosh_workout_sync.py`

**Interfaces:**
- Consumes: `PlannedBike`, `fetch_planned_bikes`, `fetch_zwo`, `OpenTriExportError` (Task 3); `parse_zwo`, `workout_name`, `workout_id_for`, `to_mywhoosh_json`, `planned_id_from_description`, `content_hash`, `ZwoError` (Task 2); `MyWhooshWorkouts`-Methoden und Fehler (Task 4); `MyWhooshWorkoutStateStore` (Task 5).
- Produces:
  ```python
  MAX_WORKOUT_ATTEMPTS = 5
  CONFIRM_TIMEOUT_SECONDS = 30
  CONFIRM_POLL_SECONDS = 3

  class BikeSource(Protocol):
      def fetch_planned_bikes(self, start: date, end: date) -> list[PlannedBike] | None: ...
      def fetch_zwo(self, training_id: str) -> bytes: ...

  class WorkoutLibrary(Protocol):
      def list(self) -> list[RemoteWorkout]: ...
      def credits(self) -> int: ...
      def upload(self, workout_json: dict[str, Any]) -> None: ...
      def delete(self, workout_id: int) -> None: ...

  @dataclass
  class MyWhooshWorkoutRunSummary:
      created: int = 0
      updated: int = 0
      deleted: int = 0
      adopted: int = 0
      skipped: int = 0
      deferred: int = 0       # outside the wanted set for lack of slots
      failed: int = 0
      unconfirmed: bool = False   # a write was not visible within CONFIRM_TIMEOUT_SECONDS; writing stopped
      aborted: str | None = None

  def sync_mywhoosh_workouts(source: BikeSource, library: WorkoutLibrary,
                             store: MyWhooshWorkoutStateStore, today: date, days: int,
                             now: Callable[[], datetime],
                             sleep: Callable[[float], None] = time.sleep,
                             clock: Callable[[], float] = time.monotonic) -> MyWhooshWorkoutRunSummary
  ```
- Wirft nie bei erwarteten Fehlern. Abbruch (mit `aborted` gesetzt, ERROR-Zeile, Zustand konsistent) bei `StateCorruptError`, `OpenTriError` außer `OpenTriExportError`, `MyWhooshAuthError` und `MyWhooshUnavailableError`.
- Ein unbestätigter Schreibaufruf ist kein Abbruch: `unconfirmed = True`, eine WARNING, danach keine weiteren Schreibaufrufe in diesem Lauf. `aborted` bleibt `None`.

Algorithmus (die Reihenfolge ist wichtig, die Tests sichern sie ab):

```
1. bikes = source.fetch_planned_bikes(today, today + days - 1) or []
2. desired: für jede Einheit json = to_mywhoosh_json(parse_zwo(zwo[training_id]), workout_name(...), planned_id),
   hash = content_hash(json), wid = workout_id_for(planned_id)
   - zwo pro training_id nur einmal holen
   - OpenTriExportError/ZwoError → WARNING, Einheit ist "unrenderbar" (skipped += 1)
3. remote = library.list(); by_id = remote-Einträge je WorkoutId; own = Einträge mit
   planned_id_from_description != None
4. Zustand abgleichen:
   - eigenes remote-Workout mit wid == workout_id_for(seiner planned_id), ohne Zustandseintrag
     → Eintrag (workout_id=wid, content_hash=None, erzwingt Vergleich), adopted += 1
   - eigenes remote-Workout mit anderer WorkoutId als workout_id_for(planned_id) → Löschliste
   - mehrere Einträge mit derselben wid, alle markiert → ein delete in die Löschliste;
     der Zustandseintrag bekommt content_hash=None, damit danach neu hochgeladen wird
   - Zustandseintrag mit workout_id, die in remote fehlt → workout_id=None (von Hand gelöscht)
5. available = library.credits() + Anzahl eigener remote-Einträge
   Einheiten, die belegen dürfen: wanted = die ersten Einheiten aus bikes (schon sortiert), bis available
   erreicht ist. Unrenderbare mit vorhandenem Workout und Tag >= today bleiben stehen und zählen mit,
   unrenderbare ohne Workout zählen nicht.
   deferred = übrige renderbare Einheiten → eine WARNING mit Anzahl und erstem Datum
6. Löschen (vor allen Uploads): eigene Workouts, deren planned_id nicht in wanted ist
   (also: aus open-tri verschwunden, Tag vorbei, verdrängt), plus die Löschliste aus 4.
   Pro Löschung: nur wenn alle by_id[workout_id] markiert sind, sonst WARNING und überspringen.
   delete → bestätigen (Einträge mit dieser WorkoutId weniger geworden) → sofort store.remove bzw.
   store.put(workout_id=None) für Einheiten, die in wanted bleiben.
7. Uploads in wanted-Reihenfolge, für Einheiten mit attempts < MAX, deren Zustand nicht passt:
   - workout_id None → neu (braucht einen Slot): free = library.credits(), einmal vor dem ersten neuen
     Upload gelesen und pro Neuanlage heruntergezählt; ist free 0 → restliche Neuen als deferred,
     WARNING, weiter mit Updates
   - content_hash != desired hash → Update mit derselben wid (kein Slot)
   - Liegt wid in by_id auf einem unmarkierten Eintrag → WARNING, überspringen (fremdes Workout)
   upload → bestätigen (Eintrag mit WorkoutId wid und Name == json["Name"] gelistet) →
   sofort store.put(workout_id=wid, content_hash=hash, attempts=0)
   MyWhooshNoSlotsError → restliche Neuen als deferred, kein attempts++, weiter mit Updates
   MyWhooshWorkoutError → ERROR, attempts += 1, last_error, failed += 1, weiter
8. Bestätigen: alle CONFIRM_POLL_SECONDS library.list() lesen, bis die Bedingung gilt, höchstens
   CONFIRM_TIMEOUT_SECONDS (gemessen mit clock). Kein Erfolg → unconfirmed = True, WARNING, Schritte 6
   und 7 enden sofort, der Zustand dieses Aufrufs bleibt unverändert.
9. Zustandseinträge, deren Tag < today ist und die kein Workout mehr haben → store.remove.
```

- [ ] **Step 1: Failing Tests schreiben**

Fakes im Test:
- `FakeSource`: Einheiten und `.zwo` pro `training_id`. Zählt `fetch_zwo`-Aufrufe und kann pro ID `OpenTriExportError` werfen.
- `FakeLibrary`: Workouts als Liste von `RemoteWorkout`, `credits` = 10 − Anzahl Einträge. `upload` ist ein Upsert über `Id`. Schreibaufrufe werden protokolliert. Fehler bei `upload`/`delete` sind konfigurierbar. `visible_after_polls` steuert, nach wie vielen `list()`-Aufrufen ein Schreibaufruf sichtbar wird. `drop_writes` steuert, dass ein Schreibaufruf nie sichtbar wird (wie das verlorene DELETE in der Probe).
- `sleep` als Liste, die die Wartezeiten sammelt, `clock` als Zähler, den `sleep` weiterschiebt.
- Mit `TODAY = date(2026, 11, 9)`.

Tests (Name → Kernaussage):
- `test_creates_new_sessions_with_dated_names_and_derived_ids` → zwei `upload` mit `Name` `Mo 09.11. W3: …` und `Id == workout_id_for(planned_id)`, im Zustand mit `workout_id`
- `test_second_run_without_change_writes_nothing` → kein `upload`/`delete`
- `test_moved_session_is_updated_in_place` → genau ein `upload` mit derselben `Id` und neuem Namen, kein `delete`, `updated == 1`
- `test_changed_content_is_updated_in_place`
- `test_session_gone_from_opentri_is_deleted_and_forgotten`
- `test_past_day_is_deleted` (Eintrag für `day(-1)`, Lauf an TODAY)
- `test_manually_deleted_workout_is_recreated`
- `test_lost_state_adopts_marked_workouts_without_duplicates` → kein `upload` bei gleichem Inhalt, `adopted == 2`
- `test_marked_workout_with_foreign_id_is_deleted` (Markierung für Einheit p, aber `WorkoutId != workout_id_for(p)`)
- `test_two_entries_same_id_delete_one_then_reupload`
- `test_unmarked_workouts_are_never_touched` (auch wenn sie Slots belegen)
- `test_delete_skipped_when_workout_id_shared_with_unmarked_entry` → WARNING, kein `delete`
- `test_derived_id_taken_by_unmarked_workout_is_skipped` → WARNING, kein `upload`
- `test_slots_limit_creates_earliest_and_warns_once` (12 Einheiten, 10 Slots → 10 `upload`, `deferred == 2`, genau eine WARNING)
- `test_earlier_session_pushes_out_latest` (10 Slots voll, neue Einheit an TODAY → `delete` der spätesten, danach `upload` der neuen, in dieser Reihenfolge)
- `test_each_write_is_confirmed_before_the_next` (`visible_after_polls=2` → zwischen zwei Schreibaufrufen liegen die `list()`-Aufrufe, `sleep == [3, 3, …]`)
- `test_dropped_delete_stops_writing_and_keeps_state` (`drop_writes` für das erste DELETE → `unconfirmed`, kein zweiter Schreibaufruf, der Zustand hält die `workout_id`, `aborted is None`) *(Review Focus)*
- `test_update_does_not_need_a_free_slot` (0 freie Slots, Inhalt geändert → `upload` trotzdem)
- `test_same_training_twice_fetches_zwo_once_and_creates_two` *(Review Focus)*
- `test_run_after_midnight_deletes_yesterday_keeps_today` *(Review Focus)*
- `test_marked_orphan_outside_wanted_is_deleted` *(Review Focus)*
- `test_unavailable_after_delete_keeps_state_consistent` (`upload` wirft `MyWhooshUnavailableError` nach einem bestätigten `delete` → `aborted` gesetzt, der Zustand des gelöschten Eintrags ist entfernt. Ein zweiter Lauf mit heiler Library lädt genau einmal hoch.) *(Review Focus)*
- `test_unrenderable_session_is_skipped_and_keeps_existing_workout`
- `test_rejected_workout_counts_attempts_and_stops_after_max`
- `test_no_slots_error_defers_new_without_attempts_but_still_updates`
- `test_no_active_plan_deletes_own_future_workouts` (Fenster leer, open-tri ist die Quelle der Wahrheit)
- `test_corrupt_state_aborts_without_writes`
- `test_opentri_auth_error_aborts_without_writes`

- [ ] **Step 2: Fehlschlag prüfen.** Run: `uv run pytest tests/test_mywhoosh_workout_sync.py -q`. Expected: FAIL
- [ ] **Step 3: Implementieren** nach dem Algorithmus oben. Zusammenfassung als INFO-Zeile wie `workout_sync._describe`, zum Beispiel „MyWhoosh-Abgleich beendet: 2 angelegt, 1 aktualisiert, 1 gelöscht“.
- [ ] **Step 4: Tests grün.** Run: `uv run pytest -q`. Expected: PASS
- [ ] **Step 5: Commit**

```bash
git add src/mywhoosh_garmin_sync/mywhoosh_workout_sync.py tests/test_mywhoosh_workout_sync.py
git commit -m "feat: Reconcile planned bike sessions with MyWhoosh workouts"
```

---

### Task 7: Konfiguration, Schleife, Dry-Run, Doku

**Files:**
- Modify: `src/mywhoosh_garmin_sync/config.py`, `src/mywhoosh_garmin_sync/cli.py`
- Modify: `README.md`, `docker-compose.yml`, `deployment/docker-compose.yml`, `.env.example`
- Test: `tests/test_config.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `sync_mywhoosh_workouts` (Task 6), `MyWhooshWorkouts` und die passende `CoachingAuth` (Task 4), `MyWhooshWorkoutStateStore` (Task 5), `fetch_planned_bikes`/`fetch_zwo` (Task 3), Umrechnung (Task 2).
- Produces:
  - `Settings.mywhoosh_workouts: bool = False`, gelesen aus `MYWHOOSH_WORKOUT_SYNC`: `true`/`false` ohne Rücksicht auf Groß- und Kleinschreibung, leer heißt `false`, alles andere ist ein `ConfigError("MYWHOOSH_WORKOUT_SYNC muss true oder false sein")`. `true` ohne open-tri ergibt `ConfigError("MYWHOOSH_WORKOUT_SYNC=true braucht OPENTRI_API_URL und OPENTRI_KEYCLOAK_URL")`.
  - In `cli.py`: `_MyWhooshWorkoutStep` mit `run() -> bool` wie `_WorkoutStep`, Aufruf mit `datetime.now(_TIMEZONE).date()`. `_once` und `_run_forever` bekommen den Parameter `mywhoosh_workouts: _MyWhooshWorkoutStep | None = None` und rufen ihn nach `workouts.run()` auf. Beim Start gibt es eine INFO-Zeile, ob der Schritt aktiv ist.
  - Befehl `mywhoosh-workouts-dry-run`, in `_OPENTRI_COMMANDS`. Er schreibt `/data/mywhoosh-workouts-dry-run/<day>-<planned_id>.json`, gibt die Pfade aus und meldet übersprungene Einheiten über stderr. Er ruft MyWhoosh nicht auf.

- [ ] **Step 1: Failing Tests schreiben**
  - config: `test_mywhoosh_workout_sync_default_off`, `test_mywhoosh_workout_sync_true_with_opentri`, `test_mywhoosh_workout_sync_true_without_opentri_is_config_error`, `test_mywhoosh_workout_sync_invalid_value`
  - cli: `test_once_runs_mywhoosh_step_after_garmin_step` (Reihenfolge über Fakes oder Monkeypatch, wie die bestehenden cli-Tests), `test_mywhoosh_step_failure_does_not_stop_other_steps` (Exit-Code 1, die anderen Schritte liefen), `test_mywhoosh_step_off_without_switch`, `test_mywhoosh_workouts_dry_run_writes_json_files`, `test_mywhoosh_step_uses_berlin_date` (Monkeypatch der Uhr auf 2026-11-08 23:30 UTC → `today == date(2026, 11, 9)`) *(Review Focus)*
- [ ] **Step 2: Fehlschlag prüfen.** Run: `uv run pytest tests/test_config.py tests/test_cli.py -q`. Expected: FAIL bei den neuen Tests.
- [ ] **Step 3: Implementieren**, plus Doku:
  - README: Abschnitt „Geplante Radtrainings nach MyWhoosh“ mit Schalter, Voraussetzung (eigene Custom Workouts vorher löschen, 10 kostenlose Slots), Namensschema und Dry-Run.
  - Compose-Dateien und `.env.example`: `MYWHOOSH_WORKOUT_SYNC=false`.
- [ ] **Step 4: Alles grün.** Run: `uv run pytest -q`. Expected: PASS
- [ ] **Step 5: Commit**

```bash
git add src/mywhoosh_garmin_sync/config.py src/mywhoosh_garmin_sync/cli.py tests/test_config.py tests/test_cli.py README.md docker-compose.yml deployment/docker-compose.yml .env.example
git commit -m "feat: Sync planned open-tri bike sessions to MyWhoosh in the run loop"
```

- [ ] **Step 6: Abnahme mit dem Betreiber**

Der Betreiber führt im Container `mywhoosh-workouts-dry-run` aus und schaut sich ein JSON an. Danach setzt er `MYWHOOSH_WORKOUT_SYNC=true`, und es läuft ein Lauf mit `once`. Er prüft in der MyWhoosh-App, dass die Workouts mit Datum im Namen erscheinen und sich fahren lassen. Das Ergebnis kommt ins Probe-Ergebnisdokument.
