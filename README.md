# mywhoosh-garmin-sync

Der Dienst fragt in einem festen Intervall die MyWhoosh-Fahrten ab, lädt neue Fahrten als FIT-Datei herunter und schreibt sie so um, dass sie als Aufzeichnung eines Garmin Edge 1050 gelten. Anschließend lädt er sie nach Garmin Connect hoch und übernimmt den MyWhoosh-Titel als Aktivitätsnamen.

Er läuft als Docker-Container und hält seinen Zustand (Zugangs-Tokens, bereits verarbeitete Fahrten) in einem Datenverzeichnis.

## Einrichtung

```bash
cp .env.example .env     # Zugangsdaten für MyWhoosh und Garmin eintragen
mkdir data               # als eigener Benutzer anlegen, der Container läuft mit UID 1000
docker compose run --rm sync dry-run   # Prüfung ohne Upload
docker compose up -d
```

- `mkdir data` muss vor dem ersten Start passieren. Legt Docker das Verzeichnis selbst an, gehört es `root` und der Container kann nicht hineinschreiben. Hat der Benutzer auf dem Host nicht die UID 1000, hilft `sudo chown 1000:1000 data`.
- `dry-run` braucht nur die MyWhoosh-Zugangsdaten. Er nimmt die neueste Fahrt auf Seite 1 (oder mit `--file-id <ID>` eine bestimmte), lädt sie herunter, schreibt sie um und legt Original und Ergebnis unter `data/dry-run/` ab. Er berührt weder Garmin noch `state.json`, meldet sich aber bei MyWhoosh an und kann dabei die MyWhoosh-Sitzungsdatei in `data/` aktualisieren. Die umgeschriebene Datei (`*.edge1050.fit`) lässt sich z. B. in fitfiletools prüfen.
- **Erster Start:** Der Dienst merkt sich nur die bereits vorhandenen Fahrten (Baseline) und lädt davon nichts hoch. Hochgeladen werden erst Fahrten, die danach entstehen.
- Beim ersten Start meldet sich der Container mit `GARMIN_EMAIL` und `GARMIN_PASSWORD` bei Garmin an und speichert die Tokens unter `data/garmin/`. Er meldet sich nur neu an, wenn Garmin die Tokens ablehnt. Das Garmin-Passwort steht dafür bewusst in der `.env`.

### Image

GitHub Actions baut bei jedem Push auf `main` die Tests, erzeugt per semantic-release eine neue Version, sofern die Commits eine verlangen (`feat:` oder `fix:`), und veröffentlicht das Image als `ghcr.io/janniskroeger/mywhoosh-garmin-sync` mit der Versionsnummer und `latest`. Auf dem Server reicht dann:

```bash
docker compose pull
docker compose up -d
```

Ohne Registry baut `docker compose build` das Image lokal unter demselben Namen. Ist das Paket in GitHub privat, vorher mit `docker login ghcr.io` anmelden.

### Deployment mit Portainer

`deployment/docker-compose.yml` ist für einen Portainer-Stack gedacht und zieht nur das fertige Image.

1. In Portainer unter **Stacks → Add stack** den Inhalt der Datei in den Web-Editor kopieren.
2. Unter **Environment variables** eintragen: `MYWHOOSH_EMAIL`, `MYWHOOSH_PASSWORD`, `GARMIN_EMAIL`, `GARMIN_PASSWORD`. Optional `IMAGE_TAG` (z. B. `1.0.0` statt `latest`), `SYNC_INTERVAL_MINUTES`, `LOG_LEVEL` sowie die open-tri-Variablen (siehe unten).
3. Ist das Paket in GitHub privat, unter **Registries** `ghcr.io` mit GitHub-Benutzer und einem Token mit `read:packages` anlegen.
4. **Deploy the stack.**

Die Daten liegen im benannten Volume `mywhoosh-garmin-sync-data`, deshalb ist kein `data/`-Ordner nötig. Prüfung und Notfall-Login laufen über **Containers → mywhoosh-garmin-sync → Console** (Command `/bin/sh`):

- `mywhoosh-garmin-sync dry-run` legt Original und umgeschriebene Datei unter `/data/dry-run/` im Volume ab.
- `mywhoosh-garmin-sync login-garmin` fragt den MFA-Code ab. Danach den Container in Portainer neu starten.

Das Image wird auf dem Self-hosted Runner gebaut und hat dessen Architektur (derzeit ARM64). Es läuft also nur auf einem Server mit derselben Architektur.

### Konfiguration

| Variable | Pflicht | Bedeutung |
|---|---|---|
| `MYWHOOSH_EMAIL` | ja | MyWhoosh-Konto |
| `MYWHOOSH_PASSWORD` | ja | |
| `GARMIN_EMAIL` | ja | Garmin-Connect-Konto |
| `GARMIN_PASSWORD` | ja | |
| `SYNC_INTERVAL_MINUTES` | nein | Abstand zwischen zwei Läufen, Standard 15 |
| `MYWHOOSH_PAYLOAD_KEY` | nein | Standard `D9436E508087E863`, falls MyWhoosh den Schlüssel ändert |
| `DATA_DIR` | nein | Standard `/data` (im Container nicht ändern) |
| `LOG_LEVEL` | nein | Standard `INFO` |
| `OPENTRI_API_URL` | nein | Adresse der open-tri-API. Schaltet die Trainings-Synchronisation ein, nur zusammen mit `OPENTRI_KEYCLOAK_URL` |
| `OPENTRI_KEYCLOAK_URL` | nein | Adresse des Keycloak-Servers von open-tri |
| `OPENTRI_KEYCLOAK_REALM` | nein | Standard `open-tri` |
| `OPENTRI_CLIENT_ID` | nein | Standard `mywhoosh-garmin-sync` |
| `WORKOUT_SYNC_DAYS` | nein | Zeitfenster in Tagen ab heute, Standard 14 |
| `WORKOUT_POOL_LENGTH_M` | nein | Beckenlänge für Schwimmtrainings in Metern, Standard 25 |
| `MYWHOOSH_WORKOUT_SYNC` | nein | `true` schaltet die Übertragung geplanter Radtrainings nach MyWhoosh ein, Standard `false`. Braucht open-tri |

Fehlt eine Pflichtvariable, beendet sich der Dienst mit Exit-Code 2 und nennt die Variable auf stderr.

Befehle: `run` (Standard, Endlosschleife), `once` (ein Lauf), `dry-run [--file-id ID]`, `login-garmin`, `login-opentri`, `workouts-dry-run`, `mywhoosh-workouts-dry-run`. Alle Befehle lesen die Variablen aus der `.env`; auch `login-garmin` verlangt daher die MyWhoosh-Variablen, obwohl es sie nicht benutzt. `login-opentri`, `workouts-dry-run` und `mywhoosh-workouts-dry-run` brauchen dagegen nur die open-tri-Variablen; fehlen `OPENTRI_API_URL` und `OPENTRI_KEYCLOAK_URL`, enden sie mit Exit-Code 2.

## Geplante Trainings aus open-tri

Ist open-tri konfiguriert, gleicht der Dienst in jedem Lauf nach den MyWhoosh-Fahrten die geplanten Trainings mit Garmin Connect ab. Sie landen im Garmin-Kalender und gelangen mit dem nächsten Sync der Garmin-App auf die Uhr.

- Synchronisiert werden die Trainings des **aktiven Plans** für die nächsten 14 Tage (`WORKOUT_SYNC_DAYS`), und zwar Schwimmen, Rad und Laufen.
- **open-tri ist die Quelle der Wahrheit.** Neue Trainings werden angelegt, geänderte aktualisiert, verschobene auf den neuen Tag gesetzt und gestrichene aus dem Kalender entfernt.
- Vergangene Einträge bleiben im Kalender stehen. Ein verpasstes Training, das du in open-tri verschiebst, wandert mit.
- Angefasst werden nur Trainings mit der Markierung `open-tri:` in der Beschreibung. Eigene Garmin-Trainings bleiben unberührt.
- Vergangene Trainings bleiben in der Garmin-Bibliothek, weil das Löschen eines Trainings seinen Kalendereintrag mitnimmt.
- Die Datumsgrenze „heute“ richtet sich nach `Europe/Berlin`. Der Zustand liegt in `/data/workouts.json`.

### Keycloak-Client einrichten

Die Anmeldung läuft ausschließlich über den Geräte-Login, es gibt keine open-tri-Zugangsdaten in der `.env`. Einmalig in Keycloak (Realm `open-tri`):

1. Neuen Client `mywhoosh-garmin-sync` anlegen, **Client authentication** aus (öffentlicher Client).
2. Bei den Authentifizierungsabläufen nur **OAuth 2.0 Device Authorization Grant** aktivieren, alles andere aus.
3. Der Client braucht den Scope `offline_access`, damit das Token dauerhaft gilt.

Dann `OPENTRI_API_URL` und `OPENTRI_KEYCLOAK_URL` im Portainer-Stack setzen, den Stack neu ausrollen und in der Container-Konsole einmalig anmelden:

```bash
mywhoosh-garmin-sync login-opentri
```

Der Befehl zeigt einen Link und einen Code. Nach der Freigabe im Browser speichert er das Token unter `/data/`. Der laufende Container muss danach nicht neu gestartet werden: er liest das gespeicherte Token bei jeder Erneuerung neu ein. Ohne gültige Anmeldung bricht der Trainings-Abgleich mit einer ERROR-Zeile ab, die MyWhoosh-Synchronisation läuft trotzdem weiter.

### Prüfung ohne Garmin

```bash
mywhoosh-garmin-sync workouts-dry-run
```

Der Befehl holt die Trainings des Zeitfensters, rechnet sie in das Garmin-Format um und schreibt pro Training eine JSON-Datei nach `/data/workouts-dry-run/<Datum>-<Seriennummer>.json`. Er berührt Garmin nicht. Gibt es keinen aktiven Plan oder ist der Export leer, meldet er das und endet mit Exit-Code 0.

## Geplante Radtrainings nach MyWhoosh

Optional legt der Dienst die geplanten Radtrainings des aktiven open-tri-Plans als Custom Workouts in MyWhoosh an, damit sie dort direkt fahrbar sind. Der Schritt läuft in jedem Lauf nach dem Garmin-Abgleich und nutzt die MyWhoosh-Anmeldung der Fahrten-Synchronisation mit.

- **Schalter:** `MYWHOOSH_WORKOUT_SYNC=true`, Standard ist `false`. Der Schalter braucht `OPENTRI_API_URL` und `OPENTRI_KEYCLOAK_URL`, sonst endet der Start mit Exit-Code 2. Das Zeitfenster ist dasselbe wie beim Garmin-Abgleich (`WORKOUT_SYNC_DAYS`).
- **Voraussetzung:** MyWhoosh erlaubt im kostenlosen Konto nur **10 Custom Workouts**. Vor dem ersten Lauf die eigenen Custom Workouts in der App löschen, damit die Slots frei sind. Reichen die Slots nicht für alle Einheiten des Fensters, werden die nächsten zuerst angelegt, der Rest wartet und wird mit einer WARNING-Zeile gemeldet.
- **Namensschema:** `Mo 09.11. W3: Name` mit Wochentag, Datum, Planwoche und Trainingsname. Bei Koppeleinheiten steht die Reihenfolge davor, zum Beispiel `So 15.11. W4: 2. Name`.
- **Aktualisierung an Ort und Stelle:** Ändert sich das Training in open-tri, wird das vorhandene Workout in MyWhoosh ersetzt. Verschobene Einheiten bekommen den neuen Tag im Namen.
- **Vergangene Einheiten** werden aus MyWhoosh entfernt, damit die Slots frei werden. Ebenso verschwinden gestrichene Einheiten.
- **Nur eigene Workouts:** Angefasst werden ausschließlich Workouts mit der Markierung `open-tri:` in der Beschreibung. Workouts ohne diese Markierung löscht oder ändert der Dienst nie.
- Der Zustand liegt in `/data/mywhoosh_workouts.json`.

Zur Prüfung ohne MyWhoosh gibt es einen Dry-Run:

```bash
mywhoosh-garmin-sync mywhoosh-workouts-dry-run
```

Er holt die geplanten Radtrainings des Zeitfensters, rechnet sie in das MyWhoosh-Format um und schreibt pro Einheit eine Datei nach `/data/mywhoosh-workouts-dry-run/<Datum>-<ID>.json`. MyWhoosh ruft er nicht auf und braucht dafür keine Zugangsdaten. Einheiten, die sich nicht umrechnen lassen (zum Beispiel wegen fehlender FTP in open-tri), meldet er auf stderr und überspringt sie. Gibt es keinen aktiven Plan oder keine Radeinheit, meldet er das und endet mit Exit-Code 0.

## Notfall-Login mit MFA

Verlangt Garmin beim Login einen MFA-Code, kann der Container das nicht allein. Im Log steht dann eine ERROR-Zeile mit Hinweis auf diesen Befehl, der den Code abfragt und die Tokens speichert:

```bash
docker compose run --rm sync login-garmin
docker compose restart sync
```

Der laufende Container bleibt nach dem Fehler pausiert und nimmt die neuen Tokens erst nach dem Neustart (`docker compose restart sync`) wieder auf. Das ist nur die Notlösung, nicht Teil der normalen Einrichtung. Lehnt Garmin die Zugangsdaten ab, pausiert die Anmeldung ebenfalls bis zum Neustart des Containers, damit das Konto nicht wegen wiederholter Fehlversuche gesperrt wird. Nach Korrektur der `.env` also `docker compose up -d --force-recreate`.

## Logs

```bash
docker compose logs -f
```

Pro Lauf gibt es eine INFO-Zeile mit der Zahl neuer Fahrten und pro verarbeiteter Fahrt eine Zeile mit Titel, Datum und Ergebnis. Passwörter und Tokens werden nie geloggt.

## Fehlgeschlagene Fahrt wieder freigeben

Nach 5 fehlgeschlagenen Versuchen wird eine Fahrt als `failed` markiert, mit einer ERROR-Zeile geloggt und nicht mehr angefasst. Zum erneuten Versuch den Container stoppen, den Eintrag der Fahrt aus `data/state.json` löschen und wieder starten:

```bash
docker compose stop
# Eintrag der Fahrt in data/state.json entfernen
docker compose up -d
```

Die Datei darf nur bei gestopptem Container bearbeitet werden, sonst überschreibt der laufende Dienst die Änderung.

## Hinweise

- Der Dienst nutzt **inoffizielle Schnittstellen** von MyWhoosh (App-Endpunkte, Login, AES-Schlüssel) und Garmin Connect (über die Bibliothek `garminconnect`). Ändert einer der Anbieter etwas, bricht der Ablauf mit einer ERROR-Zeile ab. Bei Garmin hilft meist ein Update von `garminconnect`, bei MyWhoosh braucht es oft eine Code-Anpassung.
- Ob die Anmeldung des Dienstes die laufende Sitzung in der MyWhoosh-App beendet, ist nicht geprüft. Bei der **ersten echten Fahrt** nach der Inbetriebnahme deshalb darauf achten, ob die App mitten in der Fahrt die Sitzung verliert. Die Sitzung wird zwischengespeichert und die Geräte-ID bleibt gleich, um dieses Risiko klein zu halten.
