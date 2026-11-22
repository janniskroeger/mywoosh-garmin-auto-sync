# Geplante Trainings aus open-tri nach Garmin Connect

Stand: 2026-11-07 · Status: Entwurf

## Worum es geht

Der Betreiber plant sein Training in seiner eigenen App **open-tri** (Nachbarprojekt `../open-tri`, Spring Boot + Keycloak). Heute lädt er einmal pro Woche die Trainings der kommenden Woche als ZIP mit FIT-Workout-Dateien aus open-tri herunter. Die Dateien kopiert er per USB in den Ordner `newFiles` seiner Garmin-Uhr.

Das soll der bestehende Sync-Container übernehmen. Er hat bereits eine funktionierende Garmin-Connect-Anmeldung. Die geplanten Trainings sollen ohne Handarbeit in Garmin Connect stehen: in der Trainingsbibliothek, am geplanten Tag im Garmin-Kalender und von dort auf der Uhr. Unter der Woche verschiebt der Betreiber Trainings in open-tri auch mal. Garmin muss solche Änderungen nachziehen.

**open-tri ist die einzige Quelle der Wahrheit.** Was in Garmin von open-tri abweicht, wird auf den Stand von open-tri zurückgesetzt.

Dieses Dokument erweitert das bestehende Projekt (`docs/superpowers/specs/2026-10-30-mywhoosh-garmin-sync-design.md`). Ein späteres, getrenntes Vorhaben ist der Sync geplanter Radtrainings nach MyWhoosh (siehe Nicht-Ziele).

## Ziele

- Alle geplanten Schwimm-, Rad- und Laufeinheiten des **aktiven Trainingsplans** in open-tri stehen für die nächsten **14 Tage** (einstellbar) als strukturierte Trainings in Garmin Connect und sind am jeweiligen Tag eingeplant.
- Neue, geänderte, verschobene und gelöschte Trainings in open-tri werden beim nächsten Lauf in Garmin nachgezogen.
- Die eingeplanten Trainings werden an die Geräte des Betreibers geschickt, die Uhr holt sie über die Garmin-App oder per WLAN. Der USB-Schritt entfällt.
- Ist der Tag eines Trainings vorbei, bleibt der Kalendereintrag in Garmin als Verlauf stehen. Das zugehörige Training in der Garmin-Bibliothek wird gelöscht, damit sich dort nicht jede Woche neue Einträge ansammeln. Bedingung siehe "Vergangene Trainings".
- Der bestehende Aktivitäten-Sync läuft unverändert weiter, auch wenn der Trainings-Sync scheitert, und umgekehrt.

## Nicht-Ziele

- **Kein Krafttraining.** open-tri exportiert Krafteinheiten heute nicht als FIT. Kommt später eine andere Sportart im Export an, wird sie mit WARNING übersprungen und das Vorhaben dann erweitert.
- **Kein Sync nach MyWhoosh.** Geplante Radtrainings in MyWhoosh sind ein eigenes, späteres Vorhaben mit eigener Erkundung (das Portal hat einen Workout-Import für Zwift-Dateien, open-tri erzeugt `.zwo` bereits).
- **Keine Änderungen an open-tri.** Der Sync nutzt nur bestehende Endpunkte. Siehe "Offene Prüfpunkte" für den Fall, dass die Anmeldung ohne Browser im Deployment nicht freigeschaltet ist.
- Keine Trainingspläne außer dem aktiven, keine Notizen, Rennen oder Vereinbarungen aus open-tri.
- Kein Rückweg von Garmin nach open-tri.

## Anti-Ziele (gelten als Fehlschlag, auch wenn es technisch funktioniert)

- Ein Training steht in Garmin doppelt im Kalender oder doppelt in der Bibliothek.
- Ein in open-tri verschobenes Training steht in Garmin weiter am alten Tag.
- Der Sync löscht oder ändert Garmin-Trainings, die er nicht selbst angelegt hat, zum Beispiel selbst erstellte Trainings des Betreibers oder Garmin-Coach-Pläne.
- Der Sync löscht vergangene Kalendereinträge. Einzige Ausnahme: Das Training wurde in open-tri auf heute oder später verschoben (siehe "Vergangene Trainings").
- Nach dem Verlust der Zustandsdatei legt der Sync alle Trainings im Fenster ein zweites Mal an.
- Ein Fehler im Trainings-Sync stoppt den Aktivitäten-Sync, oder umgekehrt.
- Der Sync ruft Garmin in jedem Lauf für jedes Training auf, obwohl sich nichts geändert hat.
- Zugangsdaten oder Tokens von open-tri stehen im Log.

## Rahmenbedingungen

### open-tri: Export und Daten

Geprüft am Quelltext von open-tri am 2026-11-07.

- Basis-URL der API mit Context-Path `/api`, zum Beispiel `https://<host>/api`. Alle Endpunkte erwarten `Authorization: Bearer <Keycloak-JWT>`, und die Athleten-ID in der URL muss dem `sub` des Tokens entsprechen (`AthleteAuthentication.verifyMatchesJwt`).
- **Aktiver Plan:** `GET /athletes/{athleteId}/training-plans/active` liefert den Plan als JSON (Feld mit der Plan-ID) oder **204**, wenn kein Plan aktiv ist.
- **Export:** `GET /athletes/{athleteId}/training-plans/{planId}/fit?from=YYYY-MM-DD&to=YYYY-MM-DD&schedule=true` liefert ein ZIP. Bei keiner exportierbaren Einheit im Zeitraum antwortet er mit **404**.
  - Pro geplanter Schwimm-, Rad- oder Laufeinheit eine FIT-Workout-Datei. Der Dateiname enthält Datum und Trainingsnamen, darauf soll sich der Sync aber nicht verlassen.
  - Mit `schedule=true` liegt zusätzlich `Schedules.fit` bei: pro Workout ein `schedule`-Eintrag mit `serial_number`, `time_created` und `scheduled_time`. `scheduled_time` ist Mitternacht des Tages als FIT-Local-Time, also Sekunden seit 1989-12-31 als lokale Zeit gelesen.
  - Krafteinheiten fehlen im Export.
- **Stabile Kennung:** `file_id.serial_number` jedes Workouts ist aus der ID der geplanten Einheit abgeleitet (`PlannedTrainingId`: XOR der beiden UUID-Hälften, auf 32 Bit maskiert, 0 wird 1). Dieselbe geplante Einheit hat also bei jedem Export dieselbe Seriennummer, auch nach einer Verschiebung. **Das ist der Schlüssel des Syncs.**
- `file_id.time_created` ändert sich bei **jedem** Export (Export-Zeitpunkt plus Index). Für die Erkennung von Inhaltsänderungen ist er also unbrauchbar.
- Der Workout-Name ist bereits sprechend, zum Beispiel "W3: Grundlage" oder "W3: 2. Lauf" bei Koppeleinheiten.

### Die FIT-Workouts von open-tri

Geprüft an `FitWorkoutEncoderAdapter` in open-tri. Die Dateien nutzen nur diesen Ausschnitt des FIT-Formats:

- `file_id`: `type=workout`, `manufacturer=development`, `product=0`, `serial_number`, `time_created`
- `workout`: `wkt_name`, `sport` (swimming, cycling, running), `sub_sport` (lap_swimming bei Schwimmen, sonst generic), `num_valid_steps`
- `workout_step` in Reihenfolge, `message_index` fortlaufend:
  - `wkt_step_name` (höchstens 50 Zeichen), `notes` (voller Text, wenn der Name gekürzt wurde)
  - `intensity`: warmup, active, recovery, cooldown, rest
  - Dauer: `duration_type` time (`duration_time` in s), distance (`duration_distance` in m) oder open (Lap-Taste)
  - Wiederholung: `duration_type=repeat_until_steps_cmplt` mit `duration_step` (Index des ersten Schritts der Gruppe) und `repeat_steps` (Anzahl Runden)
  - Ziel: `target_type` open, oder `heart_rate`, `power`, `speed` mit `target_value=0` und Bereich:
    - `custom_target_heart_rate_low/high` als bpm + 100
    - `custom_target_power_low/high` als Watt + 1000
    - `custom_target_speed_low/high` in m/s
- Alle Ziele sind absolute Werte, gegen die aktuellen Schwellen des Athleten aufgelöst. Garmin-Zonen werden nie benutzt.

### Garmin Connect: Trainings

Geprüft am Quelltext von `garminconnect` 0.3.20. Diese Bibliothek nutzt der Container schon.

- Garmin Connect nimmt Trainings **nicht als FIT** an, sondern als eigenes JSON. `upload_workout(json)` legt ein Training in der Bibliothek an und liefert die `workoutId`. `update_workout(workout_id, json)` ändert es, `delete_workout(workout_id)` löscht es.
- `schedule_workout(workout_id, "YYYY-MM-DD")` plant ein Training an einem Tag ein und liefert eine Schedule-ID. `unschedule_workout(schedule_id)` entfernt den Kalendereintrag, ohne das Training zu löschen. Laut Docstring um Zeile 486 von `api/workouts.py` gibt es zusätzlich eine Methode zum Umplanen eines bestehenden Eintrags (`PUT /workout-service/schedule/{scheduleId}`).
- Lesen: Die Methode um Zeile 417 von `api/workouts.py` ruft `GET /workout-service/schedule/workoutScheduleByPeriod?startDate&endDate` auf und liefert die eingeplanten Trainings eines Zeitraums. `get_workout_by_id` liefert ein einzelnes Training.
- `send_scheduled_workouts_to_devices()` schickt alle eingeplanten Trainings an die Geräte.
- `garminconnect.workout` enthält Pydantic-Modelle für das JSON (Extra `garminconnect[workout]`, braucht `pydantic`), mit allen benötigten IDs:
  - Sportarten: running=1, cycling=2, swimming=4
  - Schritt-Typen: warmup=1, cooldown=2, interval=3, recovery=4, rest=5, repeat=6, main=8
  - Endbedingungen: lap_button=1, time=2, distance=3, iterations=7
  - Ziele: no_target=1, power_zone=2, heart_rate_zone=4, speed_zone=5, pace_zone=6
  - Modelle mit eigenem Bereich: `CustomPowerTarget`, `CustomHeartRateTarget`, `SpeedTarget`, `PaceTarget`
- Schwimmtrainings verlangen in Garmin eine Beckenlänge. open-tri liefert keine.

## Entscheidungen

### Quelle: der bestehende FIT-Export von open-tri

Der Sync ruft denselben Export ab, den der Betreiber heute im Browser herunterlädt, und rechnet die FIT-Workouts in Garmins JSON um. So bleibt die gesamte Auflösung der Zielwerte (FTP, Schwellenpace, Schwellenpuls) in open-tri, und open-tri muss nicht geändert werden. Ein eigener JSON-Endpunkt in open-tri wäre sauberer, wurde aber verworfen, weil er Änderungen an zwei Projekten verlangt.

### Anmeldung bei open-tri

Der Betreiber hat am 2026-11-07 entschieden. Hintergrund: Das deployte Keycloak lehnt den Password Grant für `open-tri-web` ab (`unauthorized_client`).

- Der Sync bekommt einen **eigenen öffentlichen Keycloak-Client `mywhoosh-garmin-sync`**. Darauf ist nur die **OAuth 2.0 Device Authorization Grant** aktiviert, Standard Flow und Direct Access Grants sind aus. Den Client legt der Betreiber in der Keycloak-Admin-Konsole an. open-tri prüft nur den Issuer des Tokens und nicht den Client, deshalb muss open-tri nicht geändert werden.
- **Einmalige Anmeldung:** Der Befehl `mywhoosh-garmin-sync login-opentri` (Portainer-Konsole) fordert per `POST <keycloak>/realms/<realm>/protocol/openid-connect/auth/device` mit Scope `openid offline_access` einen Code an. Er zeigt dann den Link `verification_uri_complete` und pollt den Token-Endpunkt mit `grant_type=urn:ietf:params:oauth:grant-type:device_code`, bis der Betreiber sich im Browser angemeldet hat. Das kann auch per Strava sein.
- Das **Offline-Refresh-Token** wird unter `/data` gespeichert. Jeder Lauf holt damit ein Access-Token. Gibt Keycloak ein neues Refresh-Token zurück (Rotation), wird es sofort gespeichert.
- Wird das Refresh-Token abgelehnt, etwa weil die Offline-Sitzung nach langer Pause abgelaufen ist, gibt es eine ERROR-Zeile mit dem Hinweis auf `login-opentri`. Der Trainings-Schritt pausiert dann, der Aktivitäten-Sync läuft weiter. **Kein Passwort von open-tri in der Konfiguration.**
- Die Athleten-ID ist der `sub` des Access-Tokens.

### Gleicher Container, eigener Ablauf

Der Trainings-Sync ist ein zweiter Schritt in derselben Schleife und nutzt dieselbe Garmin-Sitzung wie der Aktivitäten-Sync. Er hat eine eigene Zustandsdatei. Fehler im einen Schritt brechen nur diesen Schritt ab. Ist open-tri nicht konfiguriert, läuft nur der Aktivitäten-Sync, wie bisher.

### Mapping FIT → Garmin

- Sportart: swimming → 4, cycling → 2, running → 1. Andere werden mit WARNING übersprungen.
- Intensität → Schritt-Typ: warmup → warmup, active → interval, recovery → recovery, cooldown → cooldown, rest → rest. *Entscheidung des Designers:* `active` wird zu `interval` und nicht zu `main`, wie Garmin-eigene Intervalltrainings es zeigen. Der Builder darf `main` nehmen, falls Garmin `interval` außerhalb von Wiederholungen schlecht darstellt.
- Dauer: time → Endbedingung time mit Sekunden, distance → Endbedingung distance mit Metern, open → lap_button.
- Wiederholung: Die Schritte von `duration_step` bis vor den Repeat-Schritt werden zu einer Garmin-Repeat-Gruppe mit `repeat_steps` Iterationen. open-tri schreibt keine verschachtelten Wiederholungen.
- Ziele:
  - heart_rate → eigener Pulsbereich in bpm, also FIT-Wert − 100
  - power → eigener Leistungsbereich in Watt, also FIT-Wert − 1000
  - speed → bei Laufen und Schwimmen als Pace-Bereich, bei Rad als Geschwindigkeitsbereich, Einheit wie Garmin sie im JSON erwartet
  - open → kein Ziel
- Schrittname und Notiz aus `wkt_step_name` und `notes`. Trainingsname aus `wkt_name`.
- Schwimmen: *Entscheidung des Designers:* Beckenlänge 25 m, einstellbar per Konfiguration.

### Zuordnung und Änderungen

- Schlüssel ist die `serial_number` des Workouts.
- Inhaltsvergleich: ein Hash über das erzeugte Garmin-JSON ohne IDs. Nicht über die FIT-Bytes, weil sich dort `time_created` bei jedem Export ändert.
- Pro Lauf:
  1. Fenster bestimmen: heute bis heute + (Tage − 1), Datum in `Europe/Berlin`.
  2. Aktiven Plan holen. Ist keiner aktiv (204) oder der Export leer (404), gilt das Fenster als leer.
  3. Export abrufen, Workouts und Termine aus `Schedules.fit` lesen.
  4. Mit dem Zustand vergleichen:
     - **neu** → anlegen und einplanen
     - **Inhalt geändert** → `update_workout`
     - **Tag geändert** → umplanen
     - **vergangen und wieder im Fenster** → vergangenen Eintrag entfernen, am neuen Tag einplanen, Training bei Bedarf neu anlegen
     - **in open-tri verschwunden**, letzter bekannter Tag ab heute → Kalendereintrag entfernen und Training löschen
  5. *Entscheidung des Designers:* Einmal pro Lauf die eingeplanten Garmin-Trainings im Fenster lesen. Fehlt ein vom Sync angelegter Eintrag oder steht er an einem anderen Tag, wird das korrigiert. Das deckt Verschiebungen und Löschungen direkt in Garmin ab, mit nur einem Lesezugriff pro Lauf. Inhaltliche Änderungen direkt in Garmin werden nicht erkannt und erst überschrieben, wenn sich das Training in open-tri ändert.
  6. Gab es mindestens eine Änderung, `send_scheduled_workouts_to_devices()`.
- Der Sync fasst nur Garmin-Trainings an, die in seinem Zustand stehen.

### Vergangene Trainings

Der Betreiber hat entschieden: **Der Garmin-Kalender bildet open-tri genau ab.** Daraus folgt:

- Ist der geplante Tag vorbei (vor heute), bleibt der Kalendereintrag als Verlauf stehen. Inhaltliche Änderungen in open-tri werden nicht mehr nachgezogen.
- **Verpasst und nach vorne verschoben:** Taucht dieselbe Seriennummer später wieder im Fenster auf, an einem Tag ab heute, wird der vergangene Kalendereintrag entfernt und das Training am neuen Tag eingeplant. Ist das Training in der Bibliothek schon gelöscht, wird es neu angelegt. Ein verpasstes und verschobenes Training verschwindet also von seinem alten Tag. Das ist so gewollt.
- **In die Vergangenheit verschoben,** zum Beispiel zum Nachtragen: Für den Sync sieht das aus wie eine Löschung, weil der Export erst bei heute beginnt. Das Training wird wie gelöscht behandelt: Der Eintrag am alten Tag ab heute verschwindet, in der Vergangenheit entsteht kein neuer.
- In open-tri gelöschte vergangene Trainings sind im Export nicht sichtbar und bleiben in Garmin stehen.
- Das Training in der Bibliothek wird nach seinem Tag gelöscht, **wenn** der Kalendereintrag dabei erhalten bleibt. Das ist ungeprüft (siehe "Offene Prüfpunkte"). Löscht Garmin den Kalendereintrag mit, bleiben die Trainings in der Bibliothek, und der Sync löscht sie nicht.

### Erkennung eigener Trainings

*Entscheidung des Designers:* Jedes vom Sync angelegte Training bekommt in seiner Beschreibung eine Markierung mit der Seriennummer, zum Beispiel `open-tri:1234567890`. Die Markierung zählt nicht zum Inhalts-Hash. Fehlt die Zustandsdatei, erkennt der Sync seine Trainings über diese Markierung im einmaligen Lesezugriff auf die eingeplanten Trainings wieder und übernimmt sie in den Zustand, statt sie doppelt anzulegen. Garmin-Trainings ohne Markierung fasst der Sync nie an. Eine **kaputte** Zustandsdatei bricht den Trainings-Schritt dagegen ab, wie beim Aktivitäten-Sync.

### Konfiguration (zusätzliche Umgebungsvariablen)

| Variable | Pflicht | Bedeutung |
|---|---|---|
| `OPENTRI_API_URL` | für den Trainings-Sync | z. B. `https://<host>/api` |
| `OPENTRI_KEYCLOAK_URL` | für den Trainings-Sync | z. B. `https://<keycloak-host>` |
| `OPENTRI_KEYCLOAK_REALM` | nein | Standard `open-tri` |
| `OPENTRI_CLIENT_ID` | nein | Standard `mywhoosh-garmin-sync` |
| `WORKOUT_SYNC_DAYS` | nein | Standard 14 |
| `WORKOUT_POOL_LENGTH_M` | nein | Standard 25 |

Fehlen alle `OPENTRI_*`-Pflichtwerte, ist der Trainings-Sync aus. Fehlt nur ein Teil, ist das ein Konfigurationsfehler mit Exit-Code 2, wie bei den bestehenden Variablen. Das Portainer-Compose und die README bekommen die neuen Variablen.

### Zustand

Eigene Datei `/data/workouts.json`, atomar geschrieben wie `state.json`. Pro Seriennummer:

- Garmin-`workoutId` und Schedule-ID
- eingeplanter Tag, Inhalts-Hash, Trainingsname
- Status: `active` (Tag ab heute), `past` (Tag vorbei, Eintrag bleibt als Verlauf) oder `removed`
- ob das Training in der Bibliothek noch existiert
- Zeitpunkt der letzten Änderung

Dazu das open-tri-Refresh-Token in einer eigenen Datei mit Modus 0600.

### Fehler

- open-tri nicht erreichbar, Token abgelehnt, Export kaputt: ERROR-Zeile, der Trainings-Schritt bricht ab, der Zustand bleibt unverändert. Der nächste Lauf versucht es erneut.
- Ein einzelnes Workout lässt sich nicht umrechnen, etwa wegen eines unbekannten Schritt-Typs: WARNING mit Name und Tag, dieses Workout wird übersprungen, die anderen laufen weiter.
- Garmin lehnt ein einzelnes Training ab: ERROR-Zeile mit Garmins Meldung. Das Training bleibt im Zustand als "nicht angelegt" und wird im nächsten Lauf erneut versucht. Wiederholungen begrenzt der Builder wie beim Aktivitäten-Sync, damit es keine Endlosschleife gibt.
- Garmin-Sitzung abgelehnt oder Garmin nicht erreichbar: dieselbe Behandlung wie im Aktivitäten-Sync (Abbruch des Schritts, kein Fehlerzähler).

## Offene Prüfpunkte (zuerst prüfen)

1. **Löschen aus der Bibliothek:** Bleibt der Kalendereintrag in Garmin bestehen, wenn das Training gelöscht wird? Prüfen mit einem Test-Training am Konto des Betreibers, bevor die Aufräumlogik gebaut wird.
2. **Garmin-JSON für eigene Bereiche:** In welcher Einheit erwartet Garmin Pace- und Geschwindigkeitsbereiche (`targetValueOne`/`targetValueTwo`), und nimmt es eigene Puls- und Leistungsbereiche ohne Zonennummer an? Am besten an einem in Garmin Connect angelegten Beispieltraining per `get_workout_by_id` ablesen.
3. **Anmeldung an open-tri:** Geklärt am 2026-11-07. Der Password Grant ist im Deployment gesperrt, deshalb gibt es jetzt einen eigenen Client mit Device Login (siehe "Anmeldung bei open-tri"). Zu prüfen bleibt nur, dass der Device Login mit dem neuen Client ein Offline-Token liefert und open-tri das Access-Token annimmt.
4. **Koppeleinheiten** (zwei Teile am selben Tag): Garmin bekommt sie als zwei getrennte Trainings am selben Tag. Prüfen, dass beide im Kalender erscheinen.

## Tests

- Umrechnung FIT → Garmin-JSON an synthetisch erzeugten FIT-Workouts im Format von open-tri: jede Dauerart, jedes Ziel, eine Wiederholungsgruppe, eine Gruppe mit übersprungenem letztem Schritt (open-tri schreibt die letzte Runde ausgeschrieben hinter den Repeat-Schritt) und Schwimmen mit Beckenlänge.
- Lesen von `Schedules.fit` (FIT-Local-Time → Datum).
- Abgleich mit Fakes für open-tri und Garmin:
  - neu, Inhalt geändert, verschoben, gelöscht, vergangen
  - verpasst und nach vorne verschoben, in die Vergangenheit verschoben
  - fehlende Zustandsdatei mit vorhandenen markierten Garmin-Trainings (keine Duplikate)
  - Verschiebung direkt in Garmin
  - kein aktiver Plan, leerer Export
  - Fehler im Trainings-Sync lässt den Aktivitäten-Sync laufen
- Keycloak-Anmeldung mit `responses`: Device Login inklusive Warten und `slow_down`, Refresh mit Rotation, abgelehntes Refresh-Token ergibt einen Hinweis auf `login-opentri`, nichts Geheimes im Log.

## Dem Builder überlassen

- Modulstruktur, Namen, ob der Umrechner die Pydantic-Modelle aus `garminconnect.workout` oder eigene Dicts benutzt
- Wie oft Garmin pro Lauf gelesen wird, solange es höchstens ein Lesezugriff für das ganze Fenster plus nötige Schreibzugriffe sind
- Wie Wiederholungsversuche bei abgelehnten Trainings begrenzt werden
- Ob ein Umplanen über die Umplanen-Methode oder über Entfernen und neu Einplanen läuft
