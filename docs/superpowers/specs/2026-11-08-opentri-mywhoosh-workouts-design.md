# Geplante Radtrainings aus open-tri nach MyWhoosh

Stand: 2026-11-08 · Status: Entwurf

## Worum es geht

Der Betreiber fährt seine Rolleneinheiten in **MyWhoosh**. Heute lädt er dafür jede Radeinheit einzeln als `.zwo` aus open-tri herunter und importiert sie im Web-Editor `workout.mywhoosh.com`. Auf der Rolle sucht er die Einheit dann in seiner Workout-Liste von Hand heraus.

Das soll der bestehende Sync-Container übernehmen, so ähnlich wie den Sync der geplanten Trainings nach Garmin (`docs/superpowers/specs/2026-11-07-opentri-garmin-workouts-design.md`, im Folgenden „Garmin-Spec“). Die geplanten Radeinheiten der nächsten Tage sollen ohne Handarbeit als Custom Workouts in MyWhoosh stehen. Was in open-tri neu dazukommt, geändert, verschoben oder gelöscht wird, zieht der Sync nach.

**open-tri ist die einzige Quelle der Wahrheit.** Was in MyWhoosh von open-tri abweicht, wird auf den Stand von open-tri gebracht.

MyWhoosh hat für Custom Workouts **keinen Kalender mit Datum**. Die Trainingspläne im Editor bestehen nur aus „Woche n, Tag 1–7“ ohne Datum. Damit der Betreiber die richtige Einheit findet, beginnt der Name jedes Workouts mit Wochentag und Datum.

## Ziele

- Alle geplanten **Radeinheiten** (`sport = BIKE`) des aktiven open-tri-Plans für die nächsten **14 Tage** stehen als Custom Workouts in MyWhoosh. Das Fenster ist dasselbe wie beim Garmin-Sync, `WORKOUT_SYNC_DAYS`.
- Der Name zeigt den Tag, zum Beispiel `Mo 09.11. W3: Grundlage`.
- Neue, geänderte, verschobene und in open-tri gelöschte Einheiten werden beim nächsten Lauf nachgezogen.
- Ist der Tag einer Einheit vorbei, löscht der Sync das Workout. So wird der Slot wieder frei (siehe „Slots“).
- Garmin-Sync und Aktivitäten-Sync laufen unverändert weiter, auch wenn der MyWhoosh-Schritt scheitert, und umgekehrt.

## Nicht-Ziele

- **Keine Unterscheidung zwischen Rolle und draußen.** Eine geplante Einheit hat in open-tri keinen Ort, den gibt es nur bei den festen Trainingstagen und Verfügbarkeiten. Der Betreiber hat am 2026-11-08 entschieden: Jede geplante Radeinheit kommt nach MyWhoosh, auch eine Ausfahrt draußen.
- **Keine MyWhoosh-Trainingspläne** (`/custom-training-plan`). Sie haben kein Datum, kosten ebenfalls Slots und bringen nichts gegenüber einem Workout mit Datum im Namen.
- **Kein Kauf und keine Abos.** Der Sync bleibt im kostenlosen Rahmen von 10 Slots.
- **Keine Änderungen an open-tri.** Der Sync nutzt nur bestehende Endpunkte.
- Kein Schwimmen, Laufen oder Krafttraining in MyWhoosh. Kein Rückweg von MyWhoosh nach open-tri.

## Anti-Ziele (gelten als Fehlschlag, auch wenn es technisch funktioniert)

- Eine Einheit steht doppelt in MyWhoosh.
- Ein Workout steht unter altem Datum oder mit altem Inhalt in MyWhoosh, obwohl open-tri sich geändert hat.
- Der Sync löscht oder ändert ein Workout, das er nicht selbst angelegt hat.
- Der Sync kauft etwas oder schließt ein Abo ab.
- Vergangene Workouts sammeln sich an und belegen Slots, bis keine neuen mehr gehen.
- Nach dem Verlust der Zustandsdatei legt der Sync alle Einheiten im Fenster ein zweites Mal an.
- Ein Fehler im MyWhoosh-Schritt stoppt den Aktivitäten- oder den Garmin-Sync, oder umgekehrt.
- Der Sync schreibt in jedem Lauf in MyWhoosh, obwohl sich nichts geändert hat.
- Zugangsdaten oder Tokens stehen im Log.

## Rahmenbedingungen

### open-tri: geplante Einheiten und `.zwo`

Geprüft am Quelltext von open-tri am 2026-11-08.

- `GET /athletes/{athleteId}/training-plans/active` (das holt der Garmin-Sync schon in `OpenTriClient.fetch_bundle`) liefert den aktiven Plan als `TrainingPlanResponse`, oder 204, wenn kein Plan aktiv ist. Darin stehen:
  - `startDate` des Plans
  - `plannedTrainings[]` als `PlannedTrainingResponse` mit `id` (ID der geplanten Einheit, UUID), `date`, `brickOrder` (bei Koppeleinheiten) und `training` (`TrainingResponse` mit `id`, `sport`, `name`)
- `GET /athletes/{athleteId}/trainings/{trainingId}/zwo` liefert die Radeinheit als Zwift-Datei (`ZwoWorkoutEncoderAdapter`). Andere Sportarten ergeben einen Fehler. Fehlt dem Athleten die FTP, schlägt der Export ebenfalls fehl.
- Die `.zwo` gehört zum **Training**, nicht zur geplanten Einheit. Zwei geplante Einheiten mit demselben Training liefern dieselbe Datei. Der Name in der Datei ist der Trainingsname ohne Wochenpräfix.
- Inhalt der `.zwo`: `<workout_file>` mit `author`, `name`, `sportType=bike` und in `<workout>` nur drei Blockarten:
  - `SteadyState Duration Power [Cadence]`
  - `Ramp Duration PowerLow PowerHigh [Cadence]`, wobei PowerLow der Start und PowerHigh das Ende ist, auch wenn es abwärts geht
  - `FreeRide Duration [Cadence]`
  - Leistung als Anteil der FTP mit drei Nachkommastellen, Dauer in ganzen Sekunden, keine Intervall-Kurzformen und keine Textereignisse.
- Die Planwoche ist `floorDiv(Tage zwischen startDate und date, 7) + 1` (`TrainingPlan.weekOf`). So entsteht auch das „W3“ im Garmin-Export.
- Anmeldung, Token und Athleten-ID: wie in der Garmin-Spec, der vorhandene `OpenTriClient` wird wiederverwendet.

### MyWhoosh: Custom Workouts

Erkundet am 2026-11-08 am Konto des Betreibers, über das JS-Bundle des Editors (`workout.mywhoosh.com/static/js/main.*.js`) und mit Aufrufen aus der angemeldeten Editor-Seite. Basis-URL: `https://coaching.mywhoosh.com/api/v2`. Alle Aufrufe mit `Authorization: Bearer <token>`.

- **Lesen:** `GET /workout-builder/my-workouts` liefert `{"data": [...], "status": true}`. Pro Workout unter anderem `_id`, `UserId`, `WorkoutId` (Zahl), `Name`, `Description`, `Time`, `WorkoutStepsArray`, `createdAt`.
- **Anlegen:** `POST /client/custom-workout-upload` mit
  ```json
  {"UserId": "<userId aus dem Token>", "SportsModeType": 0, "WorkoutsData": [<Workout>]}
  ```
  Der Editor liest die `.zwo` im Browser und schickt danach nur dieses JSON. Das genaue Feldset von `<Workout>` beim Anlegen ist noch zu prüfen (Prüfpunkt 2). Ein gelesenes Workout sieht so aus:
  ```json
  {"Name": "…", "Description": "…", "Mode": "E_Ride", "ERGMode": "E_OFF", "FTPMode": "E_NoFTP",
   "Time": 2700, "CategoryId": 1, "SubcategoryId": 0, "Type": "E_Custom", "DisplayType": "E_byWatts",
   "StepCount": 3, "WorkoutStepsArray": [
     {"IntervalId": 0, "StepType": "E_Normal", "Id": 1, "WorkoutMessage": [], "Rpm": 0,
      "Power": 0.5, "Pace": 1, "StartPower": 0, "EndPower": 0, "Time": 600,
      "IsManualGrade": false, "ManualGradeValue": 0, "ShowAveragePower": false, "FlatRoad": 0}]}
  ```
  Eine Rampe sieht man an einem von MyWhoosh angelegten Workout: `StepType` `E_WarmUp` oder `E_CoolDown` mit `StartPower` und `EndPower`, `Power: 0`. Leistung ist ein Anteil der FTP.
- **Löschen:** `DELETE /client/custom-workout-upload/0/<WorkoutId>` antwortet `{"status": true, "message": "Custom workout is being deleted."}`. Das Löschen läuft asynchron und ist nach wenigen Sekunden sichtbar. Bei zwei Einträgen mit derselben `WorkoutId` wurde nur einer gelöscht.
- **Bearbeiten durch Upsert** (Probe vom 2026-11-08, siehe `2026-11-08-mywhoosh-workout-probe-results.md`): Die `WorkoutId` ist das Feld `Id`, das der Absender im Workout mitschickt. Ein Upload mit einer vorhandenen `Id` überschreibt dieses Workout und kostet keinen Slot. Die Anlege-Antwort enthält keine ID. Zwei DELETEs direkt hintereinander: nur das erste wirkte.
- **Slots:** `GET https://coaching.mywhoosh.com/api/v2/client/my-credits` liefert `CurrentCredits`, `IsSubscriptionExists` und `UnlockedWorkoutIds`. Kostenlos gibt es **10 Slots**. Jedes Workout belegt einen. Beim Test am 2026-11-08 gab das Löschen den Slot nach wenigen Sekunden zurück (7 → 8). Ohne freien Slot lehnt der Editor das Anlegen ab („Not enough Slot Credits“). Wie die API dann antwortet, ist ungeprüft. Abos (Basic 2,99 €/Monat für 20 Slots und andere) bleiben außen vor.
- **Anmeldung:** Der Editor meldet sich mit `POST https://service20.mywhoosh.com/api/auth/login` an (`email`, `password`, reCAPTCHA-`token`, `role: "player"`). Er bekommt ein JWT mit `iss` `http://service20.mywhoosh.com/api/auth/login`, Claims unter anderem `userId` und `sub`, und einer Laufzeit von **360 Stunden**. Der Aktivitäten-Sync meldet sich dagegen über `services.mywhoosh.com/http-service/api/login` an (App-Login ohne Captcha). Die Probe hat gezeigt, dass der Coaching-Dienst dieses App-Token annimmt. Die `UserId` steht im Claim `userId`. Der Sync nutzt deshalb den App-Login.
- Der Betreiber hat am 2026-11-08 alle eigenen Workouts aus MyWhoosh gelöscht. Alle 10 Slots stehen also dem Sync zur Verfügung.

## Entscheidungen

### Quelle: `.zwo` pro geplanter Radeinheit

Der Betreiber hat am 2026-11-08 zugestimmt. Der Sync liest die geplanten Einheiten aus dem aktiven Plan, filtert `training.sport == "BIKE"` und das Fenster und holt pro Einheit die `.zwo` ihres Trainings. Die `.zwo` beschreibt die Leistung schon als Anteil der FTP, so wie MyWhoosh sie erwartet. Die Auflösung bleibt damit in open-tri, und MyWhoosh rechnet mit der FTP im MyWhoosh-Profil, genau wie beim heutigen Import von Hand. Die FIT-Dateien aus dem Plan-Bundle mit absoluten Watt werden verworfen, weil der Sync dafür die FTP selbst kennen müsste.

*Entscheidung des Designers:* Verwenden mehrere Einheiten im Fenster dasselbe Training, wird die `.zwo` pro Lauf nur einmal geholt.

### Mapping `.zwo` → MyWhoosh

Geklärt durch die Probe. Felder und Werte genau wie in „Für den Builder“ in `2026-11-08-mywhoosh-workout-probe-results.md`:

- `SteadyState` → `StepType` `E_Normal`, `Power` = Power, `Time` = Duration
- `Ramp` → `StartPower` = PowerLow, `EndPower` = PowerHigh, `Power` 0, `Time` = Duration. Aufwärts (Ende ≥ Start) `E_WarmUp`, abwärts `E_CoolDown`, so wie der Editor importiert.
- `FreeRide` → `E_FreeRide`, alle Leistungen 0
- `Cadence` → `Rpm`, sonst 0
- Schritt-`Id` fortlaufend ab 1, `StepCount` = Anzahl Schritte, `Time` des Workouts = Summe der Schritte
- Workout-`Id` (= `WorkoutId`): *Entscheidung des Designers:* fest aus der ID der geplanten Einheit abgeleitet, eine Zahl von 100 000 000 bis 999 999 999. So landet jeder Upload derselben Einheit auf demselben Workout, auch nach einem Verlust der Zustandsdatei.
- `Name` siehe unten. `Description` = Markierung (siehe „Erkennung eigener Workouts“).
- `IF` und `TSS` *Entscheidung des Designers:* `IF` = vierte Wurzel des zeitgewichteten Mittels von Leistung⁴ über alle Schritte außer FreeRide (Rampen als Mittel von x⁴ über die Rampe), `TSS` = `Time/3600 · IF² · 100`. `KJ` = 0, weil der Sync die FTP nicht kennt. Die Werte dienen nur der Anzeige in der App.
- Die übrigen Felder mit den festen Werten des Editors.
- Eine `.zwo` mit anderen Blockarten oder ohne Schritte: WARNING, die Einheit wird übersprungen.

### Name

Der Betreiber hat am 2026-11-08 zugestimmt: `<Wochentag> <TT.MM.> W<n>: <Trainingsname>`, zum Beispiel `Mo 09.11. W3: Grundlage`.

- Wochentag deutsch und zweistellig abgekürzt (Mo, Di, Mi, Do, Fr, Sa, So)
- `W<n>` aus `startDate` des Plans, wie bei `TrainingPlan.weekOf`
- Bei Koppeleinheiten (`brickOrder` gesetzt) wie im Garmin-Export `W3: 2. Grundlage`, also `brickOrder + 1` und ein Punkt vor dem Namen
- Ist der Name länger als 100 Zeichen, wird der Trainingsname gekürzt, nie das Datum. *Entscheidung des Designers:* 120 Zeichen wurden angenommen, 100 lassen Luft.

### Zuordnung und Änderungen

- Schlüssel ist die **`id` der geplanten Einheit** aus open-tri. Sie bleibt auch nach einer Verschiebung gleich.
- Inhalts-Hash über das erzeugte MyWhoosh-JSON ohne Markierung. Der Name gehört dazu, also ändert sich der Hash auch bei Verschiebung und Umbenennung.
- Pro Lauf:
  1. Fenster bestimmen: heute bis heute + (Tage − 1), Datum in `Europe/Berlin`, wie beim Garmin-Sync.
  2. Aktiven Plan holen. Ist keiner aktiv, ist das Fenster leer.
  3. Radeinheiten im Fenster bestimmen und ihre `.zwo` holen und umrechnen.
  4. `my-workouts` **einmal** lesen.
  5. Abgleich:
     - **neu** → hochladen
     - **Hash geändert** (Inhalt, Tag oder Name) → mit derselben `Id` erneut hochladen (Upsert, kein Slot)
     - **in open-tri verschwunden oder aus dem Fenster in die Vergangenheit verschoben** → löschen
     - **Tag vorbei** (vor heute) → löschen
     - **im Zustand, aber in `my-workouts` nicht mehr da** (in MyWhoosh von Hand gelöscht) → neu hochladen, wenn es noch in der Soll-Menge ist
     - sonst nichts tun
  6. *Entscheidung des Designers:* Erst löschen, dann hochladen, damit die frei werdenden Slots für neue Workouts reichen.
  7. **Jeder Schreibaufruf wird bestätigt, bevor der nächste folgt** (Probe: ein zweites DELETE direkt nach dem ersten ging verloren). Nach jedem Upload oder DELETE liest der Sync `my-workouts` alle paar Sekunden, bis die Änderung zu sehen ist, höchstens 30 Sekunden. Bei einem Upload heißt das: die `Id` ist gelistet, mit dem neuen Namen. Bei einem DELETE: die `Id` ist nicht mehr gelistet. Ist die Änderung nach 30 Sekunden nicht zu sehen, schreibt der Sync eine WARNING und hört für diesen Lauf mit dem Schreiben auf. Der Zustand gibt dann nur wieder, was bestätigt ist, und der nächste Lauf setzt dort fort.
- Der Sync fasst nur Workouts an, die er selbst angelegt hat (siehe unten).
- *Entscheidung des Designers:* Eine Änderung in open-tri wird auch dann nachgezogen, wenn die Einheit schon begonnen hat. Ein laufendes MyWhoosh-Workout sollte das nicht stören, geprüft ist es aber nicht.

### Slots

- Vor dem Anlegen liest der Sync `CurrentCredits`.
- *Entscheidung des Designers:* Der Sync bestimmt eine **Soll-Menge**. Das sind die frühesten Einheiten im Fenster, sortiert nach Datum und dann nach `brickOrder`, höchstens so viele, wie Slots verfügbar sind. Verfügbar heißt: freie Slots plus die vom Sync selbst belegten. Workouts des Syncs, die nicht mehr in der Soll-Menge sind, werden gelöscht, auch wenn ihre Einheit noch im Fenster liegt. Kommt also eine frühere Einheit dazu, verdrängt sie die späteste. Die verdrängte kommt wieder, sobald Slots frei werden.
- Für Einheiten außerhalb der Soll-Menge schreibt der Sync **eine** WARNING pro Lauf mit der Anzahl und dem Datum der ersten fehlenden Einheit.
- Lehnt MyWhoosh das Anlegen wegen fehlender Slots ab, bricht der Sync das Anlegen in diesem Lauf ab, ohne Fehlerzähler und ohne Wiederholung pro Workout.

### Erkennung eigener Workouts

*Entscheidung des Designers:* Jedes vom Sync angelegte Workout bekommt als `Description` die Markierung `open-tri:<id der geplanten Einheit>`.

- `my-workouts` liefert die `Description` mit. Fehlt die Zustandsdatei, übernimmt der Sync markierte Workouts in den Zustand, statt sie doppelt anzulegen. Ihr Inhalt wird dabei verglichen und bei Abweichung ersetzt.
- Workouts ohne Markierung fasst der Sync nie an.
- Zwei markierte Workouts mit derselben Einheit (zum Beispiel ein Rest aus einer älteren Version mit anderer `Id`): das mit der abgeleiteten `Id` behalten, die anderen löschen. Gibt es mehrere Einträge mit **derselben** `WorkoutId`, löscht der Sync einen pro Lauf und lädt danach neu hoch. Gelöscht wird über `WorkoutId`, und das entfernt nur einen Eintrag (siehe oben).
- *Entscheidung des Designers:* **Löschen nur bei eindeutiger `WorkoutId`.** Bevor der Sync löscht, prüft er in der gerade gelesenen `my-workouts`-Liste, dass alle Einträge mit dieser `WorkoutId` seine Markierung tragen. Trägt ein Eintrag mit derselben ID keine Markierung, löscht der Sync nicht. Er schreibt eine WARNING und lässt das Workout stehen.
- Die Stopp-Bedingungen der Prüfpunkte 2 und 3 sind nicht eingetreten: `Description` kommt unverändert zurück, und verschiedene `Id`s ergeben verschiedene Workouts.
- *Entscheidung des Designers:* Liegt die abgeleitete `Id` einer Einheit schon auf einem **unmarkierten** Workout, lädt der Sync nicht hoch, denn das würde ein fremdes Workout überschreiben. Er schreibt eine WARNING und überspringt die Einheit.
- Eine **kaputte** Zustandsdatei bricht den Schritt ab, wie bei den anderen Syncs.

### Gleicher Container, eigener Schritt

- Der MyWhoosh-Schritt ist ein dritter Schritt in der Schleife, nach dem Garmin-Trainingsschritt. Er hat eine eigene Zustandsdatei `/data/mywhoosh_workouts.json`, atomar geschrieben wie die anderen.
- Pro Einheit stehen darin die MyWhoosh-`WorkoutId`, Datum, Name, Hash und der Zeitpunkt der letzten Änderung.
- Er nutzt dieselbe open-tri-Anmeldung wie der Garmin-Trainingsschritt und die MyWhoosh-Zugangsdaten des Aktivitäten-Syncs.
- Fehler brechen nur diesen Schritt ab.

### Konfiguration

| Variable | Pflicht | Bedeutung |
|---|---|---|
| `MYWHOOSH_WORKOUT_SYNC` | nein | `true` schaltet den Schritt ein, Standard aus |

*Entscheidung des Designers:* Der Schritt läuft nur, wenn die Variable gesetzt ist **und** open-tri konfiguriert ist. Den Garmin-Trainingsschritt allein abschalten kann man damit nicht, das bleibt wie bisher an `OPENTRI_*` gebunden. Ist sie gesetzt, open-tri aber nicht, ist das ein Konfigurationsfehler mit Exit-Code 2. Das Fenster kommt aus `WORKOUT_SYNC_DAYS`. Compose und README bekommen die Variable.

Wird die Anmeldung über einen eigenen Weg nötig (Prüfpunkt 1), kommen höchstens dafür weitere Variablen oder ein Befehl hinzu.

### Dry-Run

*Entscheidung des Designers:* Analog zu `workouts-dry-run` gibt es `mywhoosh-workouts-dry-run`. Er schreibt das MyWhoosh-JSON jeder Einheit im Fenster nach `/data/mywhoosh-workouts-dry-run/` und ruft MyWhoosh nicht schreibend auf.

### Fehler

- open-tri nicht erreichbar, Token abgelehnt, Plan kaputt: ERROR-Zeile, Schritt bricht ab, Zustand bleibt unverändert.
- `.zwo` einer Einheit fehlt oder lässt sich nicht umrechnen (zum Beispiel weil die FTP fehlt): WARNING mit Name und Tag, die Einheit wird übersprungen, die anderen laufen weiter. Ihr altes Workout bleibt stehen, bis sie wieder umrechenbar ist oder ihr Tag vorbei ist.
- MyWhoosh lehnt ein einzelnes Workout ab: ERROR-Zeile mit der Meldung von MyWhoosh. Die Einheit wird im nächsten Lauf erneut versucht. Wiederholungen begrenzt der Builder wie bei Garmin.
- MyWhoosh nicht erreichbar oder Token abgelehnt: Schritt bricht ab, ohne Fehlerzähler. Bei 401 einmal neu anmelden, wie im Aktivitäten-Client.

## Prüfpunkte

Die Punkte 1 bis 4 sind am 2026-11-08 geprüft worden, die Ergebnisse stehen in `2026-11-08-mywhoosh-workout-probe-results.md`. Punkt 5 prüft der Betreiber bei der Abnahme.

1. **Anmeldung am Coaching-Dienst.** Nimmt `coaching.mywhoosh.com` das App-Token aus `services.mywhoosh.com/http-service/api/login` an (`GET /workout-builder/my-workouts`)? Wenn nicht, nacheinander probieren:
   - a) `service20.mywhoosh.com/api/auth/login` ohne reCAPTCHA-Token, mit `email`, `password` und `role: "player"`
   - b) Lehnt der Dienst ohne Captcha ab, **stoppt das Vorhaben** und geht mit dem Ergebnis zurück an den Betreiber. Der Captcha wird nicht umgangen.

   Wo kommt die `UserId` für das Anlegen her? Beim Editor-Token steht sie im Claim `userId`.
2. **Feldset beim Anlegen.** Eine kleine Test-`.zwo` mit allen drei Blockarten im Editor importieren, das gesendete JSON mitlesen und das Ergebnis per `my-workouts` lesen. Danach mit der eigenen Umrechnung anlegen und vergleichen, dann beide wieder löschen. Zu klären: Felder und Werte, `StepType` und Felder für Rampen mitten im Workout, Darstellung von `FreeRide`, maximale Namenslänge, ob `Description` (die Markierung) unverändert über `my-workouts` zurückkommt, und ob die Antwort die neue `WorkoutId` liefert. Liefert sie sie nicht, findet der Sync sie über die Markierung beim nächsten Lesen von `my-workouts`.
3. **Eindeutige `WorkoutId`.** Bekommen zwei angelegte Workouts verschiedene `WorkoutId`s? Beim Betreiber hatten zwei Einträge dieselbe.
4. **Antwort ohne Slot.** HTTP-Status und Meldung, wenn keine Slots mehr frei sind. Nur prüfen, wenn es ohne großen Aufwand geht. Sonst behandelt der Builder jede Ablehnung beim Anlegen wie in „Fehler“ beschrieben.
5. **In der App:** Erscheint ein neu angelegtes Workout ohne Neustart der MyWhoosh-App in „Custom Workouts“? Das prüft der Betreiber an der Rolle.

## Tests

- Umrechnung `.zwo` → MyWhoosh-JSON: jede Blockart, Rampe auf- und abwärts, Trittfrequenz, unbekannte Blockart, leeres Workout
- Name: Wochentag, Datum, Planwoche, Koppeleinheit, Kürzen
- Abgleich mit Fakes für open-tri und MyWhoosh:
  - neu, Inhalt geändert, verschoben, gelöscht, Tag vorbei, von Hand in MyWhoosh gelöscht
  - fehlende Zustandsdatei mit markierten Workouts (keine Duplikate), doppelt markierte Workouts
  - unmarkierte Workouts bleiben unberührt
  - zu wenig Slots, verzögertes Freiwerden nach dem Löschen
  - kein aktiver Plan, keine Radeinheit, `.zwo` nicht umrechenbar
  - Fehler im MyWhoosh-Schritt lässt die anderen Schritte laufen
- Konfiguration: Schalter ein und aus, Schalter ohne open-tri
- Client mit `responses`: Anlegen, Lesen, Löschen, Slots, 401 mit Neuanmeldung, nichts Geheimes im Log

## Dem Builder überlassen

- Modulstruktur und Namen. Ob der Coaching-Teil in `MyWhooshClient` wandert oder ein eigener Client wird.
- Wie oft während der Bestätigung eines Schreibaufrufs gelesen wird, solange es höchstens 30 Sekunden pro Aufruf sind
- Wie Wiederholungsversuche bei abgelehnten Workouts begrenzt werden
- Ob der open-tri-Client die Planabfrage mit dem Garmin-Schritt im selben Lauf teilt
