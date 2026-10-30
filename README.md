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

Fehlt eine Pflichtvariable, beendet sich der Dienst mit Exit-Code 2 und nennt die Variable auf stderr.

Befehle: `run` (Standard, Endlosschleife), `once` (ein Lauf), `dry-run [--file-id ID]`, `login-garmin`. Alle Befehle lesen die Variablen aus der `.env`; auch `login-garmin` verlangt daher die MyWhoosh-Variablen, obwohl es sie nicht benutzt.

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
