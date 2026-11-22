# Prüfergebnisse: MyWhoosh Custom Workouts

Stand: 2026-11-08. Geprüft mit `scripts/probe_mywhoosh_workouts.py` in zwei Läufen am Konto des Betreibers, dazu mit dem Upload-Body, den der Editor beim Import von `tests/fixtures/zwo/probe.zwo` schickt. Gehört zu `2026-11-08-opentri-mywhoosh-workouts-design.md` und zu Task 1 des Plans.

## Antworten auf die Prüfpunkte der Spec

1. **Anmeldung:** Der Coaching-Dienst nimmt das **App-Token** aus `services.mywhoosh.com/http-service/api/login` an. Diesen Login nutzt `MyWhooshClient` schon. `my-workouts` und `my-credits` antworteten mit HTTP 200. Ein Captcha ist nicht nötig, der Login über service20 wurde nicht gebraucht. Die Claims des App-Tokens: `deviceId`, `exp`, `iss`, `platform`, `roles`, `sub`, `userId`, `userStatus`. Die **`UserId`** für das Anlegen ist der Claim `userId`.
2. **Feldset beim Anlegen:** siehe „Für den Builder“. Die `Description` kommt **unverändert** zurück. Ein Name mit 120 Zeichen wurde vollständig gespeichert. Die Anlege-Antwort liefert **keine** `WorkoutId`: `201 {"status": true, "message": "Custom workout is being uploaded."}`.
3. **`WorkoutId`:** Die vergibt **der Absender**. Das Feld `Id` des Workouts in `WorkoutsData` wird zur `WorkoutId`. Zwei Uploads mit verschiedenen `Id`s ergeben zwei Workouts. Ein Upload mit einer schon vorhandenen `Id` **überschreibt** dieses Workout (Upsert): `_id` bleibt gleich, `updatedAt` ändert sich, Name und Schritte werden ersetzt, die Anzahl der Einträge bleibt gleich. Das doppelte „Active Recovery“ beim Betreiber stammt vermutlich aus zwei schnell hintereinander geschickten Uploads mit derselben `Id`.
4. **Slots:** Jedes neue Workout belegt einen Slot (10 → 9 → 8). **Ein Update belegt keinen** (8 → 8). Ein Workout war jeweils sofort nach dem Upload gelistet, beim ersten Lesen nach dem Upload. Eine Antwort ohne freien Slot wurde nicht gesehen.

## Wichtig: schnell aufeinanderfolgende Löschungen

Zwei DELETEs direkt hintereinander antworteten beide mit `200 "Custom workout is being deleted."`, gelöscht wurde aber nur das erste Workout. Das zweite war nach 60 Sekunden noch da und belegte weiter seinen Slot. Danach einzeln erneut gelöscht, war es nach 2 Sekunden weg, und der Slot war frei.

Folge für den Sync: **Schreibaufrufe nacheinander und jeder bestätigt.** Nach jedem Upload oder DELETE liest der Sync `my-workouts`, bis die Änderung zu sehen ist, und erst dann folgt der nächste Schreibaufruf. Ein HTTP 200 allein zählt nicht als erledigt.

## Für den Builder

- `AUTH_VARIANT = "app"`. Token, Neuanmeldung und 401-Behandlung kommen aus `MyWhooshClient`, die `UserId` aus dem Claim `userId` des App-Tokens.
- Upload: `POST https://coaching.mywhoosh.com/api/v2/client/custom-workout-upload`, Body `{"UserId": <userId>, "SportsModeType": 0, "WorkoutsData": [<Workout>]}`, Antwort 201 ohne ID.
- Workout-Felder, wie sie der Editor beim Import einer `.zwo` schickt:
  ```json
  {"Id": <WorkoutId>, "Name": "…", "Description": "…", "Mode": "E_Ride", "ERGMode": "E_OFF",
   "IsRecovery": false, "IsIntervals": false, "FTPMode": "E_NoFTP", "IsTT": false, "IsTSS": false,
   "IsIF": false, "FTPMultiplier": 1, "StressPoint": 0, "Time": <Summe s>, "CustomTagDescription": "2",
   "CategoryId": 200000000, "SubcategoryId": 1, "Type": "E_Normal", "DisplayType": "E_byWatts",
   "StepCount": <n>, "IsFavorite": false, "CompletedCount": 0, "WorkoutSteps": [], "AuthorName": "mywhoosh",
   "WokoutAssociationId": 0, "IF": <float>, "TSS": <float>, "KJ": <float>, "WorkoutStepsArray": [...]}
  ```
  `WokoutAssociationId` ist im Original so geschrieben.
- Schritte, alle mit `IntervalId: 0`, `WorkoutMessage: []`, `Pace: 0`, `IsManualGrade: false`, `ManualGradeValue: 0`, `ShowAveragePower: true`, `FlatRoad: 0`, `Id` fortlaufend ab 1:
  - `SteadyState` → `StepType: "E_Normal"`, `Power` = Anteil FTP, `StartPower`/`EndPower` 0, `Rpm` = Cadence oder 0
  - `Ramp` aufwärts → `"E_WarmUp"`, abwärts → `"E_CoolDown"`, `StartPower`/`EndPower`, `Power` 0
  - `FreeRide` → `"E_FreeRide"`, alle Leistungen 0
- Werte des Editors für die Probe-`.zwo` (300 s @ 0.6, Rampe 0.5→0.8 300 s, Rampe 0.8→0.5 300 s, FreeRide 120 s): `IF` 0.653, `TSS` 12.09, `KJ` 75. TSS passt zu `Time/3600 · IF² · 100`. KJ braucht die FTP des Profils.
- `MAX_NAME_LENGTH`: 120 Zeichen wurden angenommen. Eine harte Grenze wurde nicht gefunden.
- Löschen: `DELETE …/client/custom-workout-upload/0/<WorkoutId>`, asynchron. Der Slot ist etwa 2 Sekunden nach dem Verschwinden aus der Liste wieder frei.

## Offen

- Prüfpunkt 5 (erscheint ein neues Workout ohne Neustart in der App?) prüft der Betreiber bei der Abnahme in Task 7.
