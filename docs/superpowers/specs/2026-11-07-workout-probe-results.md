# Prüfergebnisse: Garmin-Trainings und open-tri-Export

Stand: 2026-11-07. Geprüft mit `scripts/probe_workouts.py` am Konto des Betreibers. Gehört zu `2026-11-07-opentri-garmin-workouts-design.md` und zu Task 1 des Plans.

## Antworten auf die Prüfpunkte der Spec

1. **Löschen aus der Bibliothek entfernt auch den Kalendereintrag.** Nach `delete_workout` war der eingeplante Eintrag weg. Ein späteres `unschedule_workout` darauf ergab 404 "No workout found for workout schedule". Daraus folgt `DELETE_PAST_LIBRARY_WORKOUTS = False`: Vergangene Trainings bleiben in der Bibliothek.
2. **Eigene Bereiche funktionieren ohne Zonennummer.** Garmin speichert sie so, wie sie gesendet werden:
   - Puls: `heart.rate.zone` (ID 4) mit `targetValueOne`/`Two` in bpm, z. B. 120/140
   - Leistung: `power.zone` (ID 2) in Watt, z. B. 180/200
   - Pace beim Laufen: `pace.zone` (ID 6) in m/s, `targetValueOne` = langsamere Grenze (3.5088 = 4:45 /km), `targetValueTwo` = schnellere (3.7037 = 4:30 /km)
   - `zoneNumber` bleibt `null`
3. **Anmeldung an open-tri:** Der Password Grant mit `open-tri-web` ist im Deployment gesperrt (`unauthorized_client: Client not allowed for direct access grants`). Der Betreiber hat deshalb den öffentlichen Client `mywhoosh-garmin-sync` mit nur "OAuth 2.0 Device Authorization Grant" angelegt. Mit ihm funktionierte der ganze Weg:
   - Device Login (`/auth/device` liefert `device_code`, `user_code`, `verification_uri_complete`, `expires_in: 600`, `interval: 5`)
   - Token, `GET /training-plans/active` und Export
   - open-tri nimmt das Access-Token des neuen Clients an.
   - Nicht gesehen: Ob das Refresh-Token `typ: Offline` hat und ob Keycloak beim Refresh rotiert. Die Terminal-Ausgabe war beim Auslesen gelöscht. Task 4 muss beides robust behandeln: immer `offline_access` anfordern und ein neues Refresh-Token immer speichern.
4. **Koppeleinheiten:** im aktuellen Export keine enthalten. Zwei Trainings am selben Tag (2026-11-15: Lauf und Schwimmen) haben verschiedene Seriennummern und eigene `Schedules.fit`-Einträge.

## Garmin: Antwortformen

### Hochladen

`upload_workout(dict)` liefert das Training mit `workoutId`. Akzeptiert wurde:
- ein Lap-Taste-Schritt mit `endCondition` `lap.button` (ID 1) und `endConditionValue: null`
- ein Training ohne `estimatedDurationInSecs`, Garmin berechnet es selbst, wo möglich
- Repeat-Gruppen: `type: RepeatGroupDTO`, `stepType` `repeat` (ID 6), `numberOfIterations`, `endCondition` `iterations` (ID 7) mit `endConditionValue` = Anzahl, `smartRepeat: false`. Die Kindschritte bekommen von Garmin `childStepId: 1`.
- `stepOrder` fortlaufend über alle Ebenen: Aufwärmen 1, Repeat 2, Kinder 3 und 4, Auslaufen 5
- ohne Ziel: `no.target` (ID 1)
- Schwimmen: **auf Workout-Ebene** `poolLength: 25.0` und `poolLengthUnit: {"unitId": 1, "unitKey": "meter", "factor": 100.0}`. Im Segment bleiben die Felder `null`.
- `description` kommt unverändert zurück, z. B. `open-tri:probe`

### Einplanen und Kalender lesen

- `schedule_workout(workoutId, "YYYY-MM-DD")` liefert die Schedule-ID unter **`workoutScheduleId`**. Das Skript hat sie über diesen Schlüssel gefunden, er stand als erster in der Suchliste.
- `get_scheduled_workouts_by_period(start, end)` liefert eine Liste. Jeder Eintrag:
  ```json
  {"workoutScheduleId": 1833138814,
   "workout": {"workoutId": 1754186325, "workoutName": "…", "description": "open-tri:probe",
               "sportType": {...}, "workoutSegments": [...], "...": "..."},
   "calendarDate": "2026-11-08", "createdDate": "2026-11-07", "...": "..."}
  ```
  Die Beschreibung steckt also schon im Kalender-Eintrag. Für das Wiedererkennen eigener Trainings braucht es **kein** zusätzliches `get_workout_by_id`.
- `reschedule_workout(workoutScheduleId, "YYYY-MM-DD")` liefert `null` und verschiebt den Eintrag.
- `unschedule_workout(workoutScheduleId)` funktioniert. 404 heißt, der Eintrag ist schon weg.

## open-tri-Export: echte Dateien

Export vom 2026-11-07 (heute bis +13 Tage, `schedule=true`): 9 Workouts und `Schedules.fit`. Abgelegt unter `tests/fixtures/local/opentri_bundle.zip` (gitignored).

**Wichtig für Task 2:** `fit-tool` 0.9.17 liefert die Felder dieser Dateien **ohne aufgelöste Unterfelder**, als Rohwerte unter den Hauptnamen. Die Namen sind die des FIT-SDK:

- `FileIdMessage`: `type` 5 (workout) bzw. 7 (schedules), `manufacturer` 255 (development), `serial_number`, `time_created`
- `WorkoutMessage`: `wkt_name`, `sport`, `sub_sport`, `num_valid_steps`
  - `sport`: **1 = running, 2 = cycling, 5 = swimming**
  - `sub_sport` 17 = lap_swimming bei Schwimmen, sonst 0
- `WorkoutStepMessage`: `message_index`, `wkt_step_name`, `notes` (falls vorhanden), `intensity`, `duration_type`, `duration_value`, `target_type`, `target_value`, `custom_target_value_low`, `custom_target_value_high`
  - `intensity`: **0 active, 1 rest, 2 warmup, 3 cooldown, 4 recovery**
  - `duration_type`: **0 time** (`duration_value` in **ms**), **1 distance** (`duration_value` in **cm**, 40000 = 400 m), **5 open** (Lap-Taste), **6 repeat_until_steps_cmplt** (`duration_value` = Index des ersten Schritts, `target_value` = Anzahl Runden)
  - `target_type`: **0 speed** (`custom_target_value_low/high` in **mm/s**, 850 = 0,85 m/s), **1 heart_rate** (bpm + 100), **4 power** (Watt + 1000), **2 open**
  - Ungültige Werte stehen als **4294967295**, z. B. `target_value` und `custom_target_value_*` bei einem Schritt ohne Ziel. Sie gelten als nicht gesetzt.
- `ScheduleMessage`: `serial_number`, `time_created`, `completed`, `type` 0 (workout), `scheduled_time` als FIT-Local-Time. 1162944000 + 631065600 = 1794009600 = 2026-11-07 00:00 UTC.

Beispiel CSS-Test Schwimmen: Einschwimmen 400 m, Repeat 4× [50 m steigernd, 20 s Pause], 100 m locker, 400 m mit Tempo 0,850–0,883 m/s, 200 m locker, 200 m mit 0,883–0,917 m/s, 300 m locker.

Heute geplante Trainings (2026-11-07) sind im Export enthalten, weil `from` = heute ist.
