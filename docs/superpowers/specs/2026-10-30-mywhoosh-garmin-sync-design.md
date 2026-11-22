# MyWhoosh → Garmin Connect Auto-Sync

Stand: 2026-10-30 · Status: Entwurf

## Worum es geht

Der Betreiber fährt indoor auf MyWhoosh und will jede Fahrt in Garmin Connect haben, und zwar so, dass Garmin sie wie eine eigene Edge-Aufzeichnung behandelt und in Trainingsstatus, Belastung und VO2max einrechnet. MyWhoosh hat keine native Garmin-Verknüpfung, und Garmin wertet Fremdgeräte nicht voll aus.

Heute läuft das von Hand in drei Schritten:

1. FIT-Datei im MyWhoosh-Webportal herunterladen
2. Bei fitfiletools.com mit dem "Device Changer" das Gerät auf einen Garmin Edge 1050 umschreiben
3. Die umgeschriebene Datei in Garmin Connect importieren

Ein Docker-Container soll diese Kette vollständig übernehmen: regelmäßig bei MyWhoosh nach neuen Fahrten schauen, jede neue Fahrt herunterladen, das Gerät umschreiben und zu Garmin Connect hochladen, ohne dass der Betreiber nach der Fahrt etwas tun muss.

Es gibt nur einen Nutzer, den Betreiber selbst.

## Ziele

- Jede neue MyWhoosh-Radfahrt landet ohne Zutun innerhalb von etwa 15 Minuten nach Fahrtende in Garmin Connect.
- Die Datei in Garmin ist die **Original-FIT-Datei von MyWhoosh**, nur mit geändertem Gerät (Garmin Edge 1050). Messdaten, Runden und Sitzungswerte bleiben unverändert.
- Die Aktivität trägt in Garmin den MyWhoosh-Titel (z. B. "MyWhoosh - Base Miles #3").
- Nach der einmaligen Einrichtung läuft der Container dauerhaft ohne Handarbeit. Insbesondere kein wiederkehrendes manuelles Erneuern von Tokens.
- Ein Neustart des Containers verliert keinen Zustand und lädt nichts doppelt hoch.

## Nicht-Ziele

- **Kein Nachholen alter Fahrten.** Synchronisiert wird nur, was nach dem ersten Start des Containers neu hinzukommt. Ältere Fahrten hat der Betreiber schon von Hand hochgeladen.
- **Kein Strava.** Strava wurde als Quelle geprüft und verworfen (siehe Entscheidungen).
- **Keine Benachrichtigungen.** Fehler stehen vorerst nur im Container-Log.
- Keine Weboberfläche, kein Mehrbenutzerbetrieb.
- Keine anderen Sportarten als Radfahren (siehe Entscheidungen).

## Anti-Ziele (gelten als Fehlschlag, auch wenn es technisch funktioniert)

- Eine Fahrt landet doppelt in Garmin Connect.
- Beim ersten Start werden alle alten Fahrten auf einmal hochgeladen.
- Passwort, Access-Token, Refresh-Token oder die signierte Download-URL stehen im Log.
- Ein dauerhafter Fehler (z. B. eine kaputte Datei) erzeugt alle 15 Minuten einen neuen Upload-Versuch in Garmin, ohne Ende.
- Das Skript verändert Messwerte der Fahrt (Leistung, Puls, Zeit, Distanz).

## Rahmenbedingungen

### MyWhoosh: inoffizielle Endpunkte

MyWhoosh hat keine öffentliche API. Die folgenden Endpunkte wurden am 2026-10-30 mit dem Konto des Betreibers geprüft. Ein Wegwerf-Skript hat Login, Liste und Download erfolgreich durchlaufen.

**Login (App-Endpunkt, ohne Captcha)**

Das Webportal `event.mywhoosh.com` verlangt beim Login ein reCAPTCHA und ist deshalb ungeeignet. Die MyWhoosh-App nutzt einen anderen Login ohne Captcha:

```
POST https://services.mywhoosh.com/http-service/api/login
Content-Type: application/json

{
  "Username": "<email>",
  "Password": "<passwort>",
  "Platform": "Android",
  "Action": 1001,
  "CorrelationId": "<uuid4>",
  "DeviceId": "<uuid4>",
  "Authorization": ""
}
```

Antwort (HTTP 200): `Success` (bool), `Message`, `WhooshId`, `AccessToken` (JWT), `RefreshToken` (JWT). Bei falschen Zugangsdaten ist `Success` false. Die Laufzeit des Access-Tokens ist nicht bekannt, sie steht im `exp`-Claim des JWT. Ein Refresh-Endpunkt ist nicht bekannt.

Gefunden wurde dieser Login im Projekt `github.com/marcelorodrigo/mywhoosh-to-garmin`. Der Rest jenes Projekts rät bei Liste und Download herum und ist keine Vorlage.

**Payload-Verschlüsselung**

Die Endpunkte auf `service14.mywhoosh.com` erwarten den Request-Body AES-verschlüsselt, genau wie das Webportal ihn schickt:

- Verfahren: `CryptoJS.AES.encrypt(JSON.stringify(payload), passphrase).toString()`, also OpenSSL-kompatibles Format: zufälliges 8-Byte-Salt, Schlüssel und IV per `EVP_BytesToKey` mit MD5, AES-256-CBC, PKCS7-Padding, Ergebnis Base64 von `"Salted__" + salt + ciphertext`.
- Passphrase: `D9436E508087E863` (steht als `VUE_APP_PAYLOAD_ENCRYPTION_KEY` im JS-Bundle des Portals).
- Der Body ist dieser Base64-String **als JSON-String kodiert**, also in Anführungszeichen, mit `Content-Type: application/json`. Der rohe String ohne Anführungszeichen wird mit 400 "Body is not valid JSON" abgelehnt.
- Unverschlüsseltes JSON nimmt der Listen-Endpunkt ebenfalls an. Das ist nicht garantiert, deshalb immer verschlüsseln.

Gegenprobe: `openssl enc -aes-256-cbc -d -md md5 -a -A -pass pass:D9436E508087E863` entschlüsselt den erzeugten String.

**Aktivitätenliste**

```
POST https://service14.mywhoosh.com/v2/rider/profile/activities
Authorization: Bearer <AccessToken>
Content-Type: application/json
Body: verschlüsselt {"type": "", "page": 1, "sortDate": "DESC"}
```

Antwort: `{"error": false, "code": ..., "message": "Rider Profile Activities", "display": ..., "data": {"results": [...], "totalCount", "totalPages", "currentPage"}}`. Eine Seite hat 10 Einträge, neueste zuerst. Felder pro Eintrag (Beispiel):

```json
{
  "id": "6ae4e5c7ce79013b34b71eb8",
  "date": 1793383209,
  "type": "ride",
  "title": "MyWhoosh - Locker 45 Min (Regeneration)",
  "sportType": "cycling",
  "routeName": "Hollywood Hills",
  "distance": 15.4,
  "elevation": 204,
  "watt": 114,
  "totalWorkout": 310,
  "wattPerKg": 1.84,
  "heartrate": 132,
  "rideDuration": "00:45:09.000",
  "startDatetime": "2026-10-30T18:00:09.000Z",
  "createdAt": "2026-10-30T18:45:27.006Z",
  "activityFileId": "6ae4e5be7b1cb9f1280c676e"
}
```

Andere Werte für `sportType` als `cycling` sind beim Betreiber bisher nicht aufgetreten.

**Download**

```
POST https://service14.mywhoosh.com/v2/rider/profile/download-activity-file
Authorization: Bearer <AccessToken>
Body: verschlüsselt {"fileId": "<activityFileId>"}
```

Antwort: `data` ist eine **vorsignierte S3-URL** (`https://mywhooshprod.s3.eu-west-1.amazonaws.com/ride/<WhooshId>/<name>.fit?...`). Sie läuft ab, also sofort per GET ohne Auth laden. Ergebnis: eine FIT-Datei (`application/octet-stream`, Header-Bytes 8–11 = `.FIT`).

### Die MyWhoosh-FIT-Datei

Geprüft an der Fahrt vom 2026-10-30 (68 KB, 45 Minuten):

- `file_id`: `type=4` (activity), `manufacturer=331` (MYWHOOSH), `product=3570`, `serial_number` gesetzt
- `file_creator`, ein `developer_data_id` und vier `field_description` (MyWhoosh-Felder `UUID`, `Title`, `CurrentRouteId`, `IsEvent` auf der Session)
- **keine** `device_info`-Nachricht
- 2709 `record` im Sekundentakt mit `position_lat/long` (virtuelle Strecke), `altitude` (0), `heart_rate`, `cadence`, `distance`, `speed`, `power`, `enhanced_speed`, `enhanced_altitude`
- 3 `lap`, 1 `session` (`sport=2` cycling, `sub_sport=58` virtual_activity), 1 `activity`, 2 `event`

### Garmin Connect

- Garmins offizielle Personal API ist nur lesend und kann keine Aktivitäten hochladen. Der Upload läuft deshalb über die inoffizielle Garmin-Connect-Schnittstelle.
- Bibliothek: **`garminconnect`** (github.com/cyberjunky/python-garminconnect), Version 0.3.20 vom 2026-10-23, Python ≥ 3.12. Eigener Mobile-SSO-Login ohne Browser, MFA per Callback oder `return_on_mfa=True` + `resume_login()`, Tokens in einer JSON-Datei, automatische Erneuerung. Relevante Methoden: `import_activity(path)`, `upload_activity(path)`, `set_activity_name(activity_id, title)`.
- **`garth`** ist seit Garmins Auth-Umbau eingestellt, neue Logins funktionieren damit nicht. Nicht verwenden.
- Für Java/Kotlin und TypeScript gibt es keine gepflegte Bibliothek, die den aktuellen Garmin-Login kann. Daraus folgt Python.

### FIT-Bibliothek

**`fit-tool`** (github.com/shaonianche/python_fit_tool, Community-Fork), Version 0.9.17 vom 2026-10-28, keine Laufzeit-Abhängigkeiten, Profil 21.218.0. Liest und schreibt FIT, kennt `GarminProduct.EDGE_1050 = 4440` und `Manufacturer.GARMIN = 1`. Hat Encode-Modi `PRESERVE` (Standard) und `CANONICAL`.

Geprüft an der echten MyWhoosh-Datei (2026-10-30):

- `FitFile.from_file(...).to_bytes()` im Modus `PRESERVE` ohne Änderung ergibt eine **byte-identische** Datei.
- Werden `manufacturer` und `product` im geladenen `FileIdMessage` direkt gesetzt und die Datei mit `PRESERVE` geschrieben, unterscheidet sich danach genau diese eine Nachricht. Alle 2723 Datennachrichten und die Entwicklerfelder der Session (`UUID`, `Title`, `CurrentRouteId`, `IsEvent`) bleiben erhalten.
- Der Neuaufbau über `FitFileBuilder`, wie ihn mywhoosh-to-garmin macht, ist **nicht** nötig und erzeugt mehr Änderungen als gewollt.
- `validate()` meldet an der **unveränderten Originaldatei** 2714 Fehler: alle `record`, `lap` und die `session` kodieren mehr Bytes als ihre Definition, und der Session fehlen `message_index` und `first_lap_index`. Die umgeschriebene Datei meldet dieselben Fehler. Garmin nimmt die Originaldatei nach dem fitfiletools-Umschreiben heute schon an. Maßstab für Tests ist deshalb "keine neuen Fehler gegenüber dem Original", nicht "keine Fehler".

## Entscheidungen

### Quelle: MyWhoosh direkt, nicht Strava

Strava wurde ernsthaft erwogen: Die offizielle Strava-API liefert die Datenströme, und der Betreiber hat eine Strava-API-App (Projekt open-tri). Verworfen, weil die FIT-Datei dann nachgebaut werden müsste. Der direkte Weg liefert die Originaldatei, braucht deutlich weniger Code und entspricht genau dem heutigen Handablauf. Der Preis: das MyWhoosh-Passwort liegt in der Container-Konfiguration, und die Endpunkte sind undokumentiert. Das Garmin-Passwort liegt ebenfalls dort (siehe Einrichtung).

### Gerätewechsel

So wie der fitfiletools Device Changer:

- `file_id.manufacturer` → `1` (Garmin), `file_id.product` → `4440` (Edge 1050). `serial_number`, `time_created` und `type` bleiben.
- Eine `device_info`-Nachricht für das Aufzeichnungsgerät wird ergänzt (`device_index=0` creator, Garmin, Edge 1050, `serial_number` wie in `file_id`, `timestamp` = Fahrtbeginn, eine plausible Edge-1050-Firmwareversion). *Entscheidung des Designers:* Echte Edge-Dateien haben diese Nachricht, MyWhoosh liefert keine.
- Alle anderen Nachrichten bleiben unverändert und in derselben Reihenfolge, einschließlich der MyWhoosh-Entwicklerfelder.
- Umsetzung: Datei laden, Felder im vorhandenen `FileIdMessage` setzen, die `device_info` samt Definition direkt hinter `file_id` bzw. `file_creator` einfügen, im Modus `PRESERVE` schreiben. Kein Neuaufbau der ganzen Datei.

### Upload als Import

*Entscheidung des Designers:* `import_activity` statt `upload_activity`. Das entspricht dem heutigen manuellen "Importieren" in Garmin Connect. Laut Bibliothek wird ein Import nicht an verbundene Dienste wie Strava weitergereicht. Das ist gewollt, denn MyWhoosh schickt die Fahrt schon selbst zu Strava, ein Weiterreichen von Garmin würde sie dort verdoppeln.

### Nur Radfahrten

*Entscheidung des Designers:* Synchronisiert werden nur Einträge mit `sportType == "cycling"`. Andere werden als erledigt markiert und mit einer Info-Zeile im Log übersprungen. Eine Laufeinheit mit "Edge 1050" als Gerät wäre unplausibel.

### Sprache und Betrieb

- Python 3.12+, Abhängigkeiten über `uv` und `pyproject.toml`.
- Ein dauerhaft laufender Container mit eigener Schleife (kein Cron im Container). Intervall per Umgebungsvariable, Standard 15 Minuten.
- `docker-compose.yml` und `.env.example` im Repo. Persistenter Zustand in einem Volume unter `/data`.

## Verhalten im Detail

### Ein Lauf

1. Bei MyWhoosh anmelden oder ein gültiges zwischengespeichertes Access-Token nutzen.
2. Seite 1 der Aktivitätenliste holen.
3. Für jeden Eintrag, dessen `activityFileId` nicht im Zustand steht, von alt nach neu:
   1. Nicht `cycling` → als `skipped` markieren.
   2. FIT herunterladen, Gerät umschreiben.
   3. Zu Garmin importieren.
   4. Bei Erfolg die neue Garmin-Aktivitäts-ID ermitteln und den Titel setzen. Als `uploaded` mit Garmin-ID markieren.
   5. Meldet Garmin ein Duplikat, als `duplicate` markieren. Kein Fehler.
   6. Bei einem Fehler den Fehlerzähler erhöhen. Der nächste Lauf versucht es erneut.
4. Zustand nach jedem Eintrag speichern, nicht erst am Ende.

Zehn neue Fahrten innerhalb eines Intervalls sind ausgeschlossen, deshalb reicht Seite 1.

### Erster Start

Ist noch kein Zustand vorhanden, werden alle Einträge der aktuellen Seite 1 als `baseline` markiert und **nicht** hochgeladen. Ab dem nächsten Lauf gilt der normale Ablauf.

### Fehler und Wiederholung

- Ein Fehler bei einer Fahrt hält die anderen nicht auf.
- *Entscheidung des Designers:* Nach 5 fehlgeschlagenen Versuchen wird die Fahrt als `failed` markiert, mit einer ERROR-Zeile geloggt und nicht mehr angefasst. Wieder freigeben geht durch Entfernen des Eintrags aus der Zustandsdatei. Das verhindert endlose Upload-Versuche.
- Verbindungsfehler, Timeouts und Fehler beim Login oder beim Abruf der Liste brechen den Lauf ab, ohne den Fehlerzähler einer Fahrt zu erhöhen. Der nächste Lauf versucht es normal.
- *Entscheidung des Designers:* Eine HTTP-Fehlerantwort, auch 5xx, beim Download oder beim Garmin-Import **einer bestimmten Fahrt** zählt als Fehler dieser Fahrt. Garmin antwortet auf kaputte Dateien teils mit 500, das darf nicht endlos wiederholt werden.
- 401 von MyWhoosh: einmal neu anmelden und den Schritt wiederholen.
- Falsche MyWhoosh-Zugangsdaten (`Success: false`): ERROR-Zeile mit klarer Handlungsanweisung, Container läuft weiter und versucht es im nächsten Intervall.
- Garmin-Tokens abgelehnt: automatisch mit Passwort neu anmelden (siehe Einrichtung). Scheitert das, ERROR-Zeile, die Fahrten bleiben unverarbeitet und ohne Erhöhung ihres Fehlerzählers liegen.

### Zustand

Eine JSON-Datei unter `/data/state.json`, atomar geschrieben (temporäre Datei + rename). Pro `activityFileId`: Status (`baseline`, `uploaded`, `duplicate`, `skipped`, `failed`), Titel, Startzeit, Garmin-Aktivitäts-ID falls vorhanden, Zeitpunkt, Fehlerzähler und letzte Fehlermeldung.

### MyWhoosh-Sitzung

*Entscheidung des Designers:* Der Login nutzt den App-Endpunkt mit `"Platform": "Android"`. Ob ein Login dort die laufende Sitzung in der MyWhoosh-App beendet, ist nicht geprüft. Um das Risiko klein zu halten, mitten in einer Fahrt aus der App zu fliegen:

- Die `DeviceId` wird einmal erzeugt und unter `/data` gespeichert, nicht pro Login neu gewürfelt. Der Container erscheint MyWhoosh damit als ein festes Gerät.
- Das Access-Token wird **zwingend** unter `/data` zwischengespeichert und bis kurz vor Ablauf laut `exp`-Claim wiederverwendet. Neu angemeldet wird nur bei Ablauf oder 401, nicht pro Lauf.
- Bei der ersten echten Fahrt nach Inbetriebnahme achtet der Betreiber darauf, ob die App die Sitzung verliert. Falls ja, muss das Design nachgeschärft werden.

### Einrichtung

1. `.env` aus `.env.example` anlegen, Zugangsdaten für MyWhoosh **und Garmin** eintragen.
2. `docker compose up -d`.

Der Betreiber hat entschieden: Das Garmin-Passwort liegt in der Konfiguration, damit der Container sich selbst neu anmelden kann, wenn die gespeicherte Sitzung abläuft oder von Garmin verworfen wird. Kein wiederkehrender manueller Login.

- Der Container meldet sich beim ersten Start mit `GARMIN_EMAIL` und `GARMIN_PASSWORD` an und speichert die Tokens unter `/data/garmin/`. Danach nutzt er die Tokens und meldet sich nur neu an, wenn Garmin sie ablehnt.
- Verlangt Garmin beim Login einen MFA-Code, kann der Container das nicht allein. Dann gibt es eine ERROR-Zeile mit Hinweis auf den interaktiven Befehl `docker compose run --rm sync login-garmin`, der den Code abfragt und die Tokens speichert. Dieser Befehl ist nur die Notlösung, nicht Teil der normalen Einrichtung.
- Lehnt Garmin die Zugangsdaten ab, wird nicht in jedem Lauf erneut versucht. *Entscheidung des Designers:* Nach einem abgelehnten Passwort pausiert die Garmin-Anmeldung, bis der Container neu gestartet wird, damit Garmin das Konto nicht wegen wiederholter Fehlversuche sperrt.

*Entscheidung des Designers:* Ein eigener Testbefehl ohne Upload, z. B. `docker compose run --rm sync dry-run [--file-id <activityFileId>]`. Er nimmt die neueste Fahrt auf Seite 1 oder die angegebene, ignoriert den Zustand, lädt herunter, schreibt um und legt Original und umgeschriebene Datei zur Kontrolle unter `/data/dry-run/` ab. Er braucht keine Garmin-Anmeldung und ändert weder Garmin noch den Zustand. So lässt sich der Gerätewechsel vor der Garmin-Einrichtung prüfen.

### Konfiguration (Umgebungsvariablen)

| Variable | Pflicht | Bedeutung |
|---|---|---|
| `MYWHOOSH_EMAIL` | ja | |
| `MYWHOOSH_PASSWORD` | ja | |
| `GARMIN_EMAIL` | ja | |
| `GARMIN_PASSWORD` | ja | |
| `SYNC_INTERVAL_MINUTES` | nein | Standard 15 |
| `MYWHOOSH_PAYLOAD_KEY` | nein | Standard `D9436E508087E863`, falls MyWhoosh den Schlüssel ändert |
| `DATA_DIR` | nein | Standard `/data` |
| `LOG_LEVEL` | nein | Standard `INFO` |

### Logging

- Eine INFO-Zeile pro Lauf (Anzahl neuer Fahrten) und eine pro verarbeiteter Fahrt mit Titel, Datum und Ergebnis.
- Nie Passwörter, Tokens oder die Query-Parameter der S3-URL loggen.

## Tests

- Unit-Tests für die AES-Verschlüsselung (Entschlüsselung des eigenen Ergebnisses ergibt das Original, Format beginnt mit `U2FsdGVkX1`).
- Unit-Tests für den Gerätewechsel an einer synthetisch erzeugten FIT-Datei mit MyWhoosh-Merkmalen (Hersteller 331, Entwicklerfelder, keine `device_info`): danach Garmin/4440 in `file_id`, genau eine `device_info`, alle Records, Laps, Session und Activity mit identischen Werten.
- Unit-Tests für die Ablauflogik (erster Start, Duplikat, Fehlerzähler, Nicht-Radfahrt) mit gefälschten MyWhoosh- und Garmin-Clients.
- *Entscheidung des Designers:* Echte Fahrtdateien kommen nicht ins Repo. Ein optionaler Test darf eine lokale Datei über eine Umgebungsvariable einlesen und wird sonst übersprungen.

## Risiken

- **MyWhoosh ändert Endpunkte, Login oder Schlüssel.** Dann bricht die Kette mit einer ERROR-Zeile ab. Schlüssel ist konfigurierbar, alles andere braucht eine Code-Anpassung.
- **Garmin ändert seine Anmeldung.** Dann hilft meist ein Update von `garminconnect`.
- **`fit-tool` verändert beim Schreiben Werte.** Für das reine Setzen der `file_id`-Felder ist das an der echten Datei widerlegt. Offen ist, ob das Einfügen der `device_info` im Modus `PRESERVE` sauber geht. Falls nicht, ist eine Datei ohne `device_info` die Rückfallebene, denn die Garmin-Felder in `file_id` sind das, was fitfiletools sicher setzt.

## Dem Builder überlassen

- Modulstruktur, Namen, CLI-Gestaltung
- Wie die Garmin-Aktivitäts-ID nach dem Import ermittelt wird (Antwort des Imports auswerten oder neueste Aktivität mit passender Startzeit suchen) und wie ein Duplikat erkannt wird
- Konkrete Firmwareversion in der `device_info`
- Basis-Image und Aufbau des Dockerfiles
