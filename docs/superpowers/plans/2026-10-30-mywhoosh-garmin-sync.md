# MyWhoosh → Garmin Connect Auto-Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ein Docker-Container, der neue MyWhoosh-Radfahrten automatisch herunterlädt, das Gerät in der FIT-Datei auf Garmin Edge 1050 umschreibt und sie in Garmin Connect importiert.

**Architecture:** Ein Python-Paket mit klar getrennten Modulen: MyWhoosh-Client (inoffizielle API mit AES-Payload), FIT-Patch (`fit-tool`), Garmin-Uploader (`garminconnect`), Zustandsdatei, Orchestrierung und CLI. Die Orchestrierung kennt MyWhoosh und Garmin nur über zwei kleine Protokolle und wird mit Fakes getestet. Der Container läuft dauerhaft mit einer eigenen Schleife.

**Tech Stack:** Python ≥ 3.12, uv, requests, pycryptodome, garminconnect 0.3.x, fit-tool 0.9.x, pytest, responses, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-30-mywhoosh-garmin-sync-design.md`. Die Endpunkte, Request-Formate und Beispielantworten stehen dort und werden hier nicht wiederholt.

## Global Constraints

- Python `>=3.12`. Abhängigkeiten: `requests`, `pycryptodome`, `garminconnect>=0.3.20,<0.4`, `fit-tool>=0.9.17,<0.10`. Dev: `pytest`, `responses`.
- Paketname `mywhoosh_garmin_sync` unter `src/`, Konsolenbefehl `mywhoosh-garmin-sync`.
- Bezeichner und Docstrings Englisch, Log-Meldungen und README Deutsch.
- Niemals Passwörter, Access-/Refresh-Tokens, Garmin-Tokens oder die Query-Parameter der S3-URL loggen oder in Exception-Texte schreiben.
- Garmin: `manufacturer = 1`, `product = 4440` (Edge 1050).
- MyWhoosh-Passphrase Standard `D9436E508087E863`, überschreibbar per `MYWHOOSH_PAYLOAD_KEY`.
- Body der service14-Endpunkte: verschlüsselter String **als JSON-String** (`json.dumps(ciphertext)`), `Content-Type: application/json`.
- Login: `POST https://services.mywhoosh.com/http-service/api/login`, `"Platform": "Android"`, `"Action": 1001`.
- Nur `sportType == "cycling"` wird hochgeladen.
- Upload ausschließlich per `Garmin.import_activity`, nie `upload_activity`.
- `MAX_ATTEMPTS = 5` pro Fahrt.
- Standardwerte: `DATA_DIR=/data`, `SYNC_INTERVAL_MINUTES=15`, `LOG_LEVEL=INFO`.
- Echte Fahrtdateien kommen nicht ins Repository. `tests/fixtures/local/` ist gitignored.

## Review Focus

1. **Ein Listeneintrag ohne `activityFileId` oder ohne `title`.** Er darf den Lauf nicht abbrechen. Er wird mit WARNING übersprungen und nicht in den Zustand geschrieben. Test in Task 3.
2. **Die Fahrt steht schon in der Liste, die FIT-Datei ist aber noch nicht fertig** (Download liefert Fehler oder Nicht-FIT-Bytes). Das zählt als Versuch der Fahrt, der nächste Lauf versucht es erneut. Test in Task 6.
3. **Zustandsdatei kaputt** (leer, kein JSON, von Hand verhunzt). Der Lauf bricht mit ERROR ab und legt **keine** neue Baseline an, denn eine stille Neu-Baseline würde unerkannt Fahrten verschlucken. Test in Task 4.
4. **Import klappt, Titel setzen scheitert.** Die Fahrt gilt als `uploaded` und wird nicht erneut importiert. Es gibt nur eine WARNING. Test in Task 6.
5. **Zwischengespeichertes MyWhoosh-Token ist abgelaufen oder Müll.** Der Client meldet sich transparent neu an, ohne Fehler im Lauf. Test in Task 3.

---

## File Structure

```
pyproject.toml
.gitignore  .dockerignore  .env.example  Dockerfile  docker-compose.yml  README.md
src/mywhoosh_garmin_sync/
  __init__.py
  crypto.py      CryptoJS-kompatible AES-Payload-Verschlüsselung
  fit_patch.py   Gerätewechsel in FIT-Bytes
  mywhoosh.py    MyWhoosh-Client: Login, Token-Cache, Liste, Download
  state.py       Zustandsdatei
  garmin.py      Garmin-Login, Import, Titel
  sync.py        run_once und dry_run
  config.py      Einstellungen aus Umgebungsvariablen
  cli.py         Befehle run, once, dry-run, login-garmin
tests/
  fitfactory.py  erzeugt synthetische MyWhoosh-artige FIT-Dateien
  test_crypto.py test_fit_patch.py test_mywhoosh.py test_state.py
  test_garmin.py test_sync.py test_config.py test_cli.py
  fixtures/local/  (gitignored, optional echte Datei)
```

---

### Task 1: Projektgerüst und Payload-Verschlüsselung

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/mywhoosh_garmin_sync/__init__.py`, `src/mywhoosh_garmin_sync/crypto.py`
- Test: `tests/test_crypto.py`

**Interfaces:**
- Produces: `encrypt_payload(payload: dict[str, Any], passphrase: str) -> str`, `decrypt_payload(ciphertext: str, passphrase: str) -> dict[str, Any]`

- [ ] **Step 1: Branch anlegen und Gerüst erstellen**

```bash
git checkout -b feat/sync
```

`pyproject.toml`: Build-Backend `hatchling`, `requires-python = ">=3.12"`, Abhängigkeiten und Dev-Gruppe laut Global Constraints, `[project.scripts] mywhoosh-garmin-sync = "mywhoosh_garmin_sync.cli:main"`, `[tool.pytest.ini_options] testpaths = ["tests"]`. `.gitignore`: `.venv/`, `__pycache__/`, `.env`, `data/`, `tests/fixtures/local/`, `.idea/workspace.xml`. Danach `uv sync`.

- [ ] **Step 2: Failing tests schreiben**

```python
from mywhoosh_garmin_sync.crypto import decrypt_payload, encrypt_payload

KEY = "D9436E508087E863"

def test_roundtrip():
    payload = {"type": "", "page": 1, "sortDate": "DESC"}
    assert decrypt_payload(encrypt_payload(payload, KEY), KEY) == payload

def test_openssl_salted_format():
    ct = encrypt_payload({"fileId": "abc"}, KEY)
    assert ct.startswith("U2FsdGVkX1")  # base64 of "Salted__"

def test_random_salt():
    assert encrypt_payload({"a": 1}, KEY) != encrypt_payload({"a": 1}, KEY)

def test_decrypts_known_cryptojs_ciphertext():
    # erzeugt am 2026-10-30, per openssl enc -d gegengeprüft
    ct = "U2FsdGVkX1+7JfTMlxQ6pSQCosKA/3AEWSnzP7gIPfP1rsX9rtIBv7pX7xDm5EMXY1yMmOaKJ5ZxJdG12RSAfQ=="
    assert decrypt_payload(ct, KEY) == {"type": "", "page": 1, "sortDate": "DESC"}
```

- [ ] **Step 3: Tests laufen lassen, sie schlagen fehl**

Run: `uv run pytest tests/test_crypto.py -v`
Expected: FAIL mit `ModuleNotFoundError`/`ImportError`

- [ ] **Step 4: `crypto.py` implementieren**

OpenSSL-Format laut Spec: 8 Byte Salt, `EVP_BytesToKey` mit MD5 (Blöcke `md5(prev + passphrase + salt)` bis 48 Byte, davon 32 Key und 16 IV), AES-256-CBC aus `Crypto.Cipher.AES`, PKCS7 über `Crypto.Util.Padding`. Payload kompakt serialisieren mit `json.dumps(payload, separators=(",", ":"))`.

- [ ] **Step 5: Tests laufen lassen**

Run: `uv run pytest tests/test_crypto.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock .gitignore src tests
git commit -m "feat: Add project skeleton and MyWhoosh payload encryption"
```

---

### Task 2: Gerätewechsel in der FIT-Datei

**Files:**
- Create: `src/mywhoosh_garmin_sync/fit_patch.py`, `tests/fitfactory.py`
- Test: `tests/test_fit_patch.py`

**Interfaces:**
- Produces: `GARMIN_MANUFACTURER = 1`, `EDGE_1050_PRODUCT = 4440`, `DEFAULT_SOFTWARE_VERSION = 29.19`, `class FitPatchError(ValueError)`, `patch_device(fit_bytes: bytes, software_version: float = DEFAULT_SOFTWARE_VERSION) -> bytes`
- Produces (Tests): `tests/fitfactory.py: make_mywhoosh_like_fit(n_records: int = 60) -> bytes`

- [ ] **Step 1: Testfabrik schreiben**

`make_mywhoosh_like_fit` baut mit `FitFileBuilder(auto_define=True)` eine Aktivität wie MyWhoosh: `FileIdMessage` (type activity, manufacturer 331, product 3570, serial 3313379353, time_created = Start), `n_records` `RecordMessage` im Sekundentakt mit heart_rate, cadence, power, speed, distance, position_lat/long, ein `LapMessage`, ein `SessionMessage` (sport 2, sub_sport 58, start_time, total_elapsed_time, total_distance), ein `ActivityMessage`. **Keine** `DeviceInfoMessage`. Startzeit `1793383209000` ms. Entwicklerfelder lässt die Fabrik bewusst weg, weil `fit-tool` sie nur umständlich erzeugen kann. Dass sie erhalten bleiben, prüft der Test mit der echten Datei. Das weicht vom Testabschnitt der Spec ab.

- [ ] **Step 2: Failing tests schreiben**

```python
import os
from pathlib import Path
import pytest
from fit_tool import FitFile
from fit_tool.profile.messages.device_info_message import DeviceInfoMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from mywhoosh_garmin_sync.fit_patch import FitPatchError, patch_device
from fitfactory import make_mywhoosh_like_fit

def data_messages(raw, skip=("DefinitionMessage", "DeviceInfoMessage")):
    out = []
    for rec in FitFile.from_bytes(raw).records:
        m = rec.message
        if type(m).__name__ in skip:
            continue
        fields = tuple(sorted((f.name, str(f.get_values())) for f in m.fields if f.is_valid()))
        dev = tuple(sorted((d.name, str(d.get_value())) for d in (m.developer_fields or []) if d.is_valid())) if hasattr(m, "developer_fields") else ()
        out.append((type(m).__name__, fields, dev))
    return out

def messages_of(raw, cls):
    return [r.message for r in FitFile.from_bytes(raw).records if isinstance(r.message, cls)]

def test_sets_garmin_edge_1050_in_file_id():
    fid = messages_of(patch_device(make_mywhoosh_like_fit()), FileIdMessage)[0]
    assert (fid.manufacturer, fid.product, fid.serial_number) == (1, 4440, 3313379353)

def test_adds_exactly_one_creator_device_info_after_file_id():
    raw = patch_device(make_mywhoosh_like_fit())
    infos = messages_of(raw, DeviceInfoMessage)
    assert len(infos) == 1
    d = infos[0]
    assert (d.device_index, d.manufacturer, d.product, d.serial_number) == (0, 1, 4440, 3313379353)
    assert d.timestamp == 1793383209000
    names = [type(r.message).__name__ for r in FitFile.from_bytes(raw).records if type(r.message).__name__ != "DefinitionMessage"]
    assert names[:2] == ["FileIdMessage", "DeviceInfoMessage"]

def test_all_other_messages_unchanged():
    orig = make_mywhoosh_like_fit()
    before, after = data_messages(orig), data_messages(patch_device(orig))
    assert len(before) == len(after)
    diffs = [b[0] for b, a in zip(before, after) if b != a]
    assert diffs == ["FileIdMessage"]

def test_idempotent():
    once = patch_device(make_mywhoosh_like_fit())
    twice = patch_device(once)
    assert len(messages_of(twice, DeviceInfoMessage)) == 1
    assert data_messages(once, skip=("DefinitionMessage",)) == data_messages(twice, skip=("DefinitionMessage",))

def test_no_new_validation_errors():
    orig = make_mywhoosh_like_fit()
    assert len(FitFile.from_bytes(patch_device(orig)).validate().errors) <= len(FitFile.from_bytes(orig).validate().errors)

@pytest.mark.parametrize("raw", [b"", b"not a fit file at all", b"\x0e\x10" + b"\x00" * 30])
def test_rejects_non_fit(raw):
    with pytest.raises(FitPatchError):
        patch_device(raw)

REAL = Path(os.environ.get("MYWHOOSH_SAMPLE_FIT", "tests/fixtures/local/mywhoosh_sample.fit"))

@pytest.mark.skipif(not REAL.exists(), reason="no local MyWhoosh sample file")
def test_real_file_only_file_id_changes_and_dev_fields_kept():
    orig = REAL.read_bytes()
    before, after = data_messages(orig), data_messages(patch_device(orig))
    assert [b[0] for b, a in zip(before, after) if b != a] == ["FileIdMessage"]
    assert any(m[2] for m in after)  # MyWhoosh-Entwicklerfelder der Session sind noch da
```

- [ ] **Step 3: Tests laufen lassen, sie schlagen fehl**

Run: `uv run pytest tests/test_fit_patch.py -v`
Expected: FAIL mit `ImportError`

- [ ] **Step 4: `patch_device` implementieren**

Am 2026-10-30 an der echten Datei verifizierter Weg:

```python
fit = FitFile.from_bytes(fit_bytes)          # Parse-Fehler -> FitPatchError
records = fit.records
fid_idx = index of the first record whose message is FileIdMessage  # fehlt -> FitPatchError
fid.manufacturer, fid.product = GARMIN_MANUFACTURER, EDGE_1050_PRODUCT
existing = first DeviceInfoMessage with device_index == 0
if existing: set manufacturer/product/serial_number/software_version on it
else:
    start = SessionMessage.start_time, else fid.time_created
    local_id = smallest of range(16) not used by any record   # MyWhoosh nutzt nur 0
    di = DeviceInfoMessage(local_id=local_id); set timestamp, device_index=0, manufacturer, product, serial_number=fid.serial_number, software_version
    records.insert(fid_idx + 1, Record.from_message(DefinitionMessage.from_data_message(di)))
    records.insert(fid_idx + 2, Record.from_message(di))
    fit.mark_dirty()
return fit.to_bytes()
```

Leere Bytes, fehlender `.FIT`-Header (Bytes 8–11) und jede Ausnahme von `fit-tool` werden zu `FitPatchError`.

- [ ] **Step 5: Tests laufen lassen**

Run: `uv run pytest tests/test_fit_patch.py -v`
Expected: alle bestanden, der Real-File-Test übersprungen

- [ ] **Step 6: Optional mit der echten Datei prüfen**

Die Testdatei vom 2026-10-30 liegt, solange die Session existiert, unter `/tmp/claude-1000/-home-jannis-IdeaProjects-mywoosh-garmin-auto-sync/04b431a8-ef74-4463-9e21-72a39c33d355/scratchpad/mywhoosh_test.fit`. Nach `tests/fixtures/local/mywhoosh_sample.fit` kopieren und erneut laufen lassen.
Expected: alle bestanden, inklusive Real-File-Test. Fehlt die Datei, den Schritt überspringen.

- [ ] **Step 7: Commit**

```bash
git add src/mywhoosh_garmin_sync/fit_patch.py tests/fitfactory.py tests/test_fit_patch.py
git commit -m "feat: Rewrite FIT device to Garmin Edge 1050"
```

---

### Task 3: MyWhoosh-Client

**Files:**
- Create: `src/mywhoosh_garmin_sync/mywhoosh.py`
- Test: `tests/test_mywhoosh.py`

**Interfaces:**
- Consumes: `encrypt_payload`, `decrypt_payload` (Task 1)
- Produces:
  - `@dataclass(frozen=True) class MyWhooshActivity: file_id: str; title: str; sport_type: str; start: datetime` (`start` UTC-aware, aus `startDatetime`)
  - `class MyWhooshError(Exception)`, `class MyWhooshAuthError(MyWhooshError)` (Zugangsdaten abgelehnt), `class MyWhooshUnavailableError(MyWhooshError)` (Netz, Timeout, Nicht-2xx bei Login oder Liste, unerwartete Struktur), `class MyWhooshDownloadError(MyWhooshError)` (Fehler beim Download einer bestimmten Fahrt)
  - `class MyWhooshClient(email: str, password: str, data_dir: Path, payload_key: str, session: requests.Session | None = None)` mit `list_recent() -> list[MyWhooshActivity]` (Seite 1, neueste zuerst, so wie geliefert) und `download_fit(file_id: str) -> bytes`

- [ ] **Step 1: Failing tests schreiben**

Mit `responses` und einem `tmp_path` als `data_dir`. Hilfsfunktion `jwt(exp: int) -> str` baut ein unsigniertes JWT (`header.payload.sig`, Base64url) mit `exp`. Der Body jedes service14-Calls wird im Test mit `json.loads` und `decrypt_payload` geprüft.

```python
def test_list_recent_logs_in_and_parses(client, rsps): ...
    # Login liefert AccessToken=jwt(now+3600); Liste liefert zwei Einträge aus der Spec
    # assert: zwei MyWhooshActivity, erste file_id "6ae4e5be7b1cb9f1280c676e",
    #         start == datetime(2026,10,30,18,0,9,tzinfo=UTC), title wie geliefert
    # assert: Listen-Request hatte Authorization "Bearer <token>" und
    #         decrypt_payload(json.loads(body), KEY) == {"type": "", "page": 1, "sortDate": "DESC"}
    # assert: Login-Body hat Platform "Android", Action 1001, Username, und eine DeviceId

def test_device_id_is_persisted_and_reused(tmp_path, rsps):
    # zwei Client-Instanzen nacheinander mit leerem Token-Cache -> beide Logins senden dieselbe DeviceId
    # und data_dir/mywhoosh_session.json enthält sie

def test_cached_token_is_reused_without_login(tmp_path, rsps):
    # Session-Datei mit gültigem Token (exp = now+3600) vorbereiten -> list_recent ruft den Login NICHT auf

def test_expired_or_garbage_cached_token_triggers_login(tmp_path, rsps):  # Review Focus 5
    # parametrisiert: exp = now-10, exp = now+60 (innerhalb der 5-Minuten-Marge), Token "garbage"
    # -> genau ein Login, dann Liste OK

def test_401_relogs_once_and_retries(client, rsps):
    # Liste antwortet zuerst 401, dann 200 -> genau ein zusätzlicher Login, Ergebnis OK
    # antwortet die Liste auch nach Re-Login 401 -> MyWhooshUnavailableError

def test_bad_credentials_raise_auth_error(client, rsps):
    # Login 200 mit {"Success": false, "Message": "Invalid"} -> MyWhooshAuthError, Passwort nicht im str(exc)

def test_server_error_on_list_raises_unavailable(client, rsps):
    # Liste 502 -> MyWhooshUnavailableError; ConnectionError -> MyWhooshUnavailableError

def test_malformed_entries_are_skipped(client, rsps, caplog):  # Review Focus 1
    # Liste mit drei Einträgen: einer ohne activityFileId, einer mit title None, einer korrekt
    # -> genau ein Ergebnis, eine WARNING pro übersprungenem Eintrag

def test_download_fit(client, rsps):
    # download-activity-file liefert {"data": "https://mywhooshprod.s3.eu-west-1.amazonaws.com/ride/x/y.fit?X-Amz-Signature=secret"}
    # GET der URL liefert b"FITBYTES" -> Rückgabe b"FITBYTES"
    # assert decrypt_payload(json.loads(body), KEY) == {"fileId": "6ae4e5be7b1cb9f1280c676e"}
    # assert "secret" nicht in caplog.text

def test_download_errors_are_download_errors(client, rsps):
    # parametrisiert: download-activity-file 400, data fehlt/kein http-String, S3-GET 403, S3-GET 500
    # -> MyWhooshDownloadError, "X-Amz" nicht in str(exc)
```

- [ ] **Step 2: Tests laufen lassen, sie schlagen fehl**

Run: `uv run pytest tests/test_mywhoosh.py -v`
Expected: FAIL mit `ImportError`

- [ ] **Step 3: `mywhoosh.py` implementieren**

- Session-Datei `data_dir/mywhoosh_session.json` mit `device_id` und `access_token`. `device_id` wird beim ersten Mal als `uuid4` erzeugt. Die Datei wird atomar geschrieben (tmp + `os.replace`), Modus `0o600`.
- Token gilt als nutzbar, wenn der `exp`-Claim mehr als 300 s in der Zukunft liegt. Ohne lesbares `exp` gilt er als nicht nutzbar.
- Ein 401 bei Liste oder Download-Endpunkt führt zu genau einem Re-Login und einer Wiederholung.
- Timeouts: 30 s für API-Calls, 60 s für den S3-Download.
- Header für service14 wie im Spike: `Authorization`, `Accept: application/json`, `Content-Type: application/json`, `Origin: https://event.mywhoosh.com`, `Referer: https://event.mywhoosh.com/`.
- S3-URL nur als `url.split("?")[0]` loggen.

- [ ] **Step 4: Tests laufen lassen**

Run: `uv run pytest tests/test_mywhoosh.py -v`
Expected: alle bestanden

- [ ] **Step 5: Commit**

```bash
git add src/mywhoosh_garmin_sync/mywhoosh.py tests/test_mywhoosh.py
git commit -m "feat: Add MyWhoosh client with app login and token cache"
```

---

### Task 4: Zustandsdatei

**Files:**
- Create: `src/mywhoosh_garmin_sync/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Produces:
  - `Status = Literal["baseline", "uploaded", "duplicate", "skipped", "failed", "pending"]`, `DONE_STATUSES = {"baseline", "uploaded", "duplicate", "skipped", "failed"}`
  - `@dataclass class Entry: status: Status; title: str; start: str; updated_at: str; attempts: int = 0; garmin_activity_id: int | None = None; last_error: str | None = None`
  - `class StateCorruptError(Exception)`
  - `class StateStore(path: Path)` mit `exists() -> bool`, `get(file_id: str) -> Entry | None`, `put(file_id: str, entry: Entry) -> None` (speichert sofort), `is_done(file_id: str) -> bool`

- [ ] **Step 1: Failing tests schreiben**

```python
def test_missing_file_means_no_state(tmp_path):
    store = StateStore(tmp_path / "state.json")
    assert not store.exists() and store.get("x") is None and not store.is_done("x")

def test_put_persists_across_instances(tmp_path):
    p = tmp_path / "state.json"
    StateStore(p).put("a", Entry(status="uploaded", title="T", start="2026-10-30T18:00:09+00:00", updated_at="2026-10-30T19:00:00+00:00", garmin_activity_id=42))
    again = StateStore(p)
    assert again.exists() and again.get("a").garmin_activity_id == 42 and again.is_done("a")

def test_pending_is_not_done(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.put("a", Entry(status="pending", title="T", start="s", updated_at="u", attempts=2, last_error="boom"))
    assert not store.is_done("a")

def test_file_format(tmp_path):
    p = tmp_path / "state.json"
    StateStore(p).put("a", Entry(status="skipped", title="T", start="s", updated_at="u"))
    data = json.loads(p.read_text())
    assert data["version"] == 1 and data["activities"]["a"]["status"] == "skipped"

def test_atomic_write_leaves_no_temp_files(tmp_path):
    StateStore(tmp_path / "state.json").put("a", Entry(status="skipped", title="T", start="s", updated_at="u"))
    assert [f.name for f in tmp_path.iterdir()] == ["state.json"]

@pytest.mark.parametrize("content", ["", "{not json", "[]", '{"version": 1}'])
def test_corrupt_file_raises(tmp_path, content):  # Review Focus 3
    p = tmp_path / "state.json"; p.write_text(content)
    with pytest.raises(StateCorruptError):
        StateStore(p).get("a")
```

- [ ] **Step 2: Tests laufen lassen, sie schlagen fehl**

Run: `uv run pytest tests/test_state.py -v`
Expected: FAIL mit `ImportError`

- [ ] **Step 3: `state.py` implementieren**

Datei wird beim ersten Zugriff geladen. Schreiben über eine temporäre Datei im selben Verzeichnis plus `os.replace`. `exists()` ist True, sobald die Datei existiert, auch wenn sie korrupt ist. Laden einer korrupten Datei wirft `StateCorruptError` mit dem Pfad im Text.

- [ ] **Step 4: Tests laufen lassen**

Run: `uv run pytest tests/test_state.py -v`
Expected: alle bestanden

- [ ] **Step 5: Commit**

```bash
git add src/mywhoosh_garmin_sync/state.py tests/test_state.py
git commit -m "feat: Add persistent sync state"
```

---

### Task 5: Garmin-Uploader

**Files:**
- Create: `src/mywhoosh_garmin_sync/garmin.py`
- Test: `tests/test_garmin.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class ImportOutcome: status: Literal["uploaded", "duplicate"]; activity_id: int | None`
  - `class GarminAuthError(Exception)` (Zugangsdaten abgelehnt oder MFA nötig, Anmeldung pausiert), `class GarminUnavailableError(Exception)` (Netz, 429, 5xx beim Login), `class GarminImportError(Exception)` (Fehler beim Import einer bestimmten Fahrt)
  - `class GarminUploader(email: str, password: str, token_dir: Path, client_factory: Callable[..., Garmin] = Garmin, sleep: Callable[[float], None] = time.sleep)` mit `ensure_login() -> None`, `import_fit(path: Path, start: datetime) -> ImportOutcome`, `set_name(activity_id: int, title: str) -> None`
  - `interactive_login(email: str, password: str, token_dir: Path, prompt: Callable[[str], str] = input, client_factory=Garmin) -> None`

Fakten aus `garminconnect` 0.3.20 (am 2026-10-30 im Quelltext geprüft):
- `Garmin(email, password, prompt_mfa=None, return_on_mfa=False)`, dann `login(tokenstore=str(token_dir))`. Mit `tokenstore` lädt `login` gespeicherte Tokens, erneuert sie bei Bedarf und meldet sich bei abgelehnten Tokens selbst mit dem Passwort neu an. Nach einem Credential-Login schreibt es `garmin_tokens.json` in `token_dir`.
- **`login()` setzt `self.password = None`.** Für jeden neuen Login muss deshalb über `client_factory` eine frische Instanz erzeugt werden.
- MFA ohne `prompt_mfa` wirft `GarminConnectAuthenticationError("MFA Required but no prompt_mfa mechanism supplied")`.
- Fehlerklassen in `garminconnect.exceptions`: `GarminConnectAuthenticationError`, `GarminConnectTooManyRequestsError`, `GarminConnectConnectionError` (mit `.response`).
- `import_activity(path: str) -> dict` liefert Garmins `detailedImportResult`. Bei HTTP 409 wirft es `GarminConnectConnectionError` mit `"Activity already exists (duplicate)"` im Text.
- `get_activities_by_date(startdate: "YYYY-MM-DD", enddate) -> list[dict]`, Einträge haben `activityId` und `startTimeGMT` im Format `"2026-10-30 18:00:09"`.
- `set_activity_name(activity_id, title)`.

- [ ] **Step 1: Failing tests schreiben**

Mit einer `FakeGarmin`-Klasse, die über eine Fabrik injiziert wird, ihre Aufrufe protokolliert und pro Methode konfigurierbare Ergebnisse oder Ausnahmen hat.

```python
def test_ensure_login_uses_tokenstore_and_logs_in_once(tmp_path): ...
    # zweimal ensure_login -> Fabrik einmal aufgerufen mit (email, password), login(tokenstore=str(tmp_path))

def test_bad_credentials_pause_further_logins(tmp_path):
    # login wirft GarminConnectAuthenticationError -> GarminAuthError
    # zweiter ensure_login wirft sofort GarminAuthError, ohne die Fabrik erneut aufzurufen

def test_mfa_required_raises_auth_error_with_hint(tmp_path):
    # Fehlertext "MFA Required ..." -> GarminAuthError, Text enthält "login-garmin"

def test_rate_limit_and_connection_errors_are_unavailable_and_not_paused(tmp_path):
    # GarminConnectTooManyRequestsError / GarminConnectConnectionError beim login -> GarminUnavailableError
    # nächster ensure_login versucht es erneut

def test_import_success_with_internal_id(tmp_path):
    # import_activity -> {"detailedImportResult": {"successes": [{"internalId": 123}], "failures": []}}
    # -> ImportOutcome("uploaded", 123); import_activity bekam str(path)

def test_import_duplicate_409(tmp_path):
    # wirft GarminConnectConnectionError("Activity already exists (duplicate): x.fit") -> ImportOutcome("duplicate", None)

def test_import_duplicate_in_failures(tmp_path):
    # {"detailedImportResult": {"successes": [], "failures": [{"messages": [{"code": 202, "content": "Duplicate Activity."}]}]}}
    # -> ImportOutcome("duplicate", None)

def test_import_without_id_polls_activity_list(tmp_path):
    # successes leer, keine failures; get_activities_by_date liefert erst [], dann
    # [{"activityId": 777, "startTimeGMT": "2026-10-30 18:00:10"}]
    # start = datetime(2026,10,30,18,0,9,tzinfo=UTC) -> ImportOutcome("uploaded", 777), sleep wurde aufgerufen

def test_import_without_id_gives_up_after_polling(tmp_path):
    # Liste bleibt leer -> ImportOutcome("uploaded", None) nach 6 Versuchen

def test_other_import_errors_are_import_errors(tmp_path):
    # GarminConnectConnectionError("Import error: 500") mit .response.status_code == 500 -> GarminImportError

def test_import_rate_limit_or_network_error_is_unavailable(tmp_path):
    # GarminConnectTooManyRequestsError -> GarminUnavailableError
    # GarminConnectConnectionError ohne .response (reiner Netzfehler) -> GarminUnavailableError

def test_set_name_calls_library(tmp_path):
    # set_name(123, "MyWhoosh - Base Miles #3") -> set_activity_name("123", "MyWhoosh - Base Miles #3")

def test_interactive_login_prompts_for_mfa(tmp_path):
    # Fabrik bekommt prompt_mfa; FakeGarmin ruft ihn auf; prompt liefert "123456" -> login(tokenstore=...) ok
```

- [ ] **Step 2: Tests laufen lassen, sie schlagen fehl**

Run: `uv run pytest tests/test_garmin.py -v`
Expected: FAIL mit `ImportError`

- [ ] **Step 3: `garmin.py` implementieren**

- `ensure_login` ist lazy und merkt sich die eingeloggte Instanz. `token_dir` wird mit `mkdir(parents=True, exist_ok=True)` und Modus `0o700` angelegt.
- `GarminConnectAuthenticationError` ⇒ `GarminAuthError` und Pause bis Prozessende. Ist "MFA" im Text, lautet die Meldung: `docker compose run --rm sync login-garmin` ausführen.
- Activity-ID: `detailedImportResult.successes[0].internalId`. Fehlt sie, bis zu 6-mal im Abstand von 5 s `get_activities_by_date(<Startdatum UTC>, <Startdatum UTC>)` und den Eintrag nehmen, dessen `startTimeGMT` höchstens 120 s von `start` abweicht.
- Duplikat: 409-Ausnahme mit "duplicate" im Text oder ein `failures`-Eintrag, dessen Meldung "Duplicate" enthält.
- Andere `failures` ohne Duplikat-Meldung ⇒ `GarminImportError` mit Garmins Meldungstext.
- Beim Import: HTTP-Fehlerantwort (`exc.response` gesetzt, auch 5xx) ⇒ `GarminImportError`. Rate-Limit oder Netzfehler ohne Antwort ⇒ `GarminUnavailableError`, zählt nicht als Versuch der Fahrt.
- Beim Login: alle Fehler außer `GarminConnectAuthenticationError` sind `GarminUnavailableError`.

- [ ] **Step 4: Tests laufen lassen**

Run: `uv run pytest tests/test_garmin.py -v`
Expected: alle bestanden

- [ ] **Step 5: Commit**

```bash
git add src/mywhoosh_garmin_sync/garmin.py tests/test_garmin.py
git commit -m "feat: Add Garmin Connect uploader with import and renaming"
```

---

### Task 6: Sync-Ablauf

**Files:**
- Create: `src/mywhoosh_garmin_sync/sync.py`
- Test: `tests/test_sync.py`

**Interfaces:**
- Consumes: `MyWhooshActivity` und MyWhoosh-Fehlerklassen (Task 3), `patch_device`, `FitPatchError` (Task 2), `StateStore`, `Entry`, `StateCorruptError` (Task 4), `ImportOutcome` und Garmin-Fehlerklassen (Task 5)
- Produces:
  - `class MyWhooshSource(Protocol)`: `list_recent() -> list[MyWhooshActivity]`, `download_fit(file_id: str) -> bytes`
  - `class GarminSink(Protocol)`: `ensure_login() -> None`, `import_fit(path: Path, start: datetime) -> ImportOutcome`, `set_name(activity_id: int, title: str) -> None`
  - `MAX_ATTEMPTS = 5`
  - `@dataclass class RunSummary: new: int = 0; uploaded: int = 0; duplicates: int = 0; skipped: int = 0; failed: int = 0; retry_later: int = 0; baseline: int = 0; aborted: str | None = None`
  - `run_once(source: MyWhooshSource, garmin: GarminSink, store: StateStore, work_dir: Path, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> RunSummary`
  - `dry_run(source: MyWhooshSource, out_dir: Path, file_id: str | None = None) -> tuple[Path, Path]` (Original, umgeschrieben)

- [ ] **Step 1: Failing tests schreiben**

Fakes: `FakeSource(activities, fits: dict[str, bytes | Exception])` und `FakeGarmin` mit konfigurierbaren Ergebnissen pro Aufruf und Aufrufprotokoll. FIT-Bytes kommen aus `make_mywhoosh_like_fit()`.

```python
def test_first_run_creates_baseline_without_uploading(...)
    # kein State, zwei Aktivitäten -> beide "baseline", garmin.ensure_login nie aufgerufen, summary.baseline == 2

def test_new_ride_is_patched_imported_and_renamed(...)
    # State mit Baseline für A; Liste [B (neu), A] -> import_fit einmal für B, die importierte Datei hat file_id product 4440,
    # set_name(123, B.title); State B "uploaded" mit garmin_activity_id 123; work_dir ist danach leer

def test_processes_oldest_first(...)
    # zwei neue Fahrten -> Import-Reihenfolge nach start aufsteigend

def test_nothing_new_does_not_touch_garmin(...)
    # alle Einträge done -> ensure_login nie aufgerufen

def test_non_cycling_is_skipped(...)
    # sport_type "running" -> State "skipped", kein Download, kein Import

def test_duplicate_is_done(...)
    # import -> ImportOutcome("duplicate", None) -> State "duplicate", set_name nicht aufgerufen

def test_rename_failure_keeps_uploaded(...)  # Review Focus 4
    # set_name wirft Exception -> State "uploaded", WARNING im Log, nächster Lauf importiert nicht erneut

def test_upload_without_activity_id_skips_rename(...)
    # ImportOutcome("uploaded", None) -> State "uploaded", set_name nicht aufgerufen, WARNING

def test_ride_errors_count_attempts_until_failed(...)  # Review Focus 2
    # parametrisiert über: download wirft MyWhooshDownloadError, download liefert b"not fit" (FitPatchError), import wirft GarminImportError
    # 4 Läufe -> "pending" mit attempts 1..4 und last_error gesetzt; 5. Lauf -> "failed", ERROR-Log; 6. Lauf -> kein weiterer Versuch

def test_one_failing_ride_does_not_block_others(...)
    # zwei neue, erste scheitert -> zweite trotzdem "uploaded"

def test_garmin_auth_or_unavailable_aborts_without_counting(...)
    # ensure_login wirft GarminAuthError bzw. GarminUnavailableError -> summary.aborted gesetzt, State unverändert (keine attempts)

def test_mywhoosh_list_failure_aborts(...)
    # list_recent wirft MyWhooshUnavailableError bzw. MyWhooshAuthError -> summary.aborted, State unverändert

def test_corrupt_state_aborts_without_new_baseline(...)  # Review Focus 3
    # state.json = "{kaputt" -> summary.aborted, Datei unverändert, kein Import

def test_dry_run_writes_original_and_patched(tmp_path)
    # ohne file_id -> neueste Fahrt; Dateien out_dir/<file_id>.original.fit und .edge1050.fit, patched hat product 4440
    # mit file_id einer anderen Fahrt -> diese; unbekannte file_id -> ValueError
```

- [ ] **Step 2: Tests laufen lassen, sie schlagen fehl**

Run: `uv run pytest tests/test_sync.py -v`
Expected: FAIL mit `ImportError`

- [ ] **Step 3: `sync.py` implementieren**

Ablauf laut Spec, Abschnitt "Ein Lauf", mit diesen Festlegungen:
- Zustand vor der Liste laden. `StateCorruptError` ⇒ `aborted`, ERROR-Log.
- `garmin.ensure_login()` erst aufrufen, wenn mindestens eine Fahrt hochgeladen werden muss.
- Fehler der Fahrt (`MyWhooshDownloadError`, `FitPatchError`, `GarminImportError`) ⇒ `attempts + 1`, `last_error = str(exc)`, Status `pending`, bei `attempts >= MAX_ATTEMPTS` Status `failed` mit ERROR-Log.
- `GarminAuthError`, `GarminUnavailableError`, `MyWhooshUnavailableError`, `MyWhooshAuthError` brechen den Lauf ab, ohne `attempts` zu ändern.
- Die umgeschriebene Datei heißt `work_dir/<file_id>.fit` und wird nach dem Import immer gelöscht.
- Log pro Fahrt: `"<Titel> (<Startzeit lokal>): hochgeladen | Duplikat | übersprungen | Fehler (Versuch n/5): <Meldung>"`. Pro Lauf eine Zusammenfassung.

- [ ] **Step 4: Tests laufen lassen**

Run: `uv run pytest tests/test_sync.py -v`
Expected: alle bestanden

- [ ] **Step 5: Commit**

```bash
git add src/mywhoosh_garmin_sync/sync.py tests/test_sync.py
git commit -m "feat: Add sync run with baseline, retries and dry run"
```

---

### Task 7: Konfiguration und CLI

**Files:**
- Create: `src/mywhoosh_garmin_sync/config.py`, `src/mywhoosh_garmin_sync/cli.py`
- Test: `tests/test_config.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: alles aus Task 3 bis 6
- Produces:
  - `class ConfigError(Exception)`
  - `@dataclass(frozen=True) class Settings: mywhoosh_email: str; mywhoosh_password: str; garmin_email: str | None; garmin_password: str | None; interval_minutes: int = 15; payload_key: str = "D9436E508087E863"; data_dir: Path = Path("/data"); log_level: str = "INFO"`
  - `Settings.from_env(env: Mapping[str, str], require_garmin: bool) -> Settings`
  - `main(argv: list[str] | None = None) -> int`, Befehle `run` (Standard), `once`, `dry-run [--file-id ID]`, `login-garmin`

- [ ] **Step 1: Failing tests schreiben**

```python
def test_from_env_reads_all_values(): ...
    # alle Variablen gesetzt, SYNC_INTERVAL_MINUTES="5", DATA_DIR="/tmp/x" -> Felder entsprechend

def test_defaults():
    s = Settings.from_env({"MYWHOOSH_EMAIL": "a", "MYWHOOSH_PASSWORD": "b", "GARMIN_EMAIL": "c", "GARMIN_PASSWORD": "d"}, require_garmin=True)
    assert (s.interval_minutes, s.payload_key, s.data_dir, s.log_level) == (15, "D9436E508087E863", Path("/data"), "INFO")

def test_missing_values_listed_without_secrets():
    with pytest.raises(ConfigError) as e:
        Settings.from_env({"MYWHOOSH_EMAIL": "a", "MYWHOOSH_PASSWORD": "geheim"}, require_garmin=True)
    assert "GARMIN_EMAIL" in str(e.value) and "GARMIN_PASSWORD" in str(e.value) and "geheim" not in str(e.value)

def test_garmin_optional_for_dry_run():
    assert Settings.from_env({"MYWHOOSH_EMAIL": "a", "MYWHOOSH_PASSWORD": "b"}, require_garmin=False).garmin_email is None

@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_invalid_interval(value): ...  # ConfigError
```

`tests/test_cli.py` patcht `run_once`, `dry_run`, `interactive_login` und `time.sleep` in `mywhoosh_garmin_sync.cli` per `monkeypatch`:

```python
def test_once_runs_single_sync_and_returns_0(...)          # run_once einmal, Rückgabe 0
def test_once_returns_1_when_aborted(...)                  # RunSummary(aborted="x") -> 1
def test_run_loops_and_survives_exceptions(...)            # run_once wirft beim 1. Mal RuntimeError, 2. Mal ok; sleep(900) zwischen den Läufen; Schleife per sleep-Fake nach 2 Läufen mit KeyboardInterrupt beenden -> Rückgabe 0
def test_dry_run_does_not_need_garmin(...)                 # nur MyWhoosh-Variablen gesetzt -> dry_run aufgerufen mit out_dir=<data_dir>/dry-run, file_id durchgereicht
def test_login_garmin_calls_interactive_login(...)         # token_dir=<data_dir>/garmin
def test_config_error_exits_2_with_message(...)            # fehlende Variable -> 2, Variablenname auf stderr
```

- [ ] **Step 2: Tests laufen lassen, sie schlagen fehl**

Run: `uv run pytest tests/test_config.py tests/test_cli.py -v`
Expected: FAIL mit `ImportError`

- [ ] **Step 3: `config.py` und `cli.py` implementieren**

- `argparse` mit Subcommands. Ohne Subcommand gilt `run`.
- Pfade: Zustand `<data_dir>/state.json`, Arbeitsverzeichnis `<data_dir>/work`, Garmin-Tokens `<data_dir>/garmin`, Dry-Run-Ausgabe `<data_dir>/dry-run`.
- Logging: `logging.basicConfig(level=..., format="%(asctime)s %(levelname)s %(name)s: %(message)s")`. Logger `garminconnect` und `urllib3` auf WARNING.
- `run`: Endlosschleife `run_once` → `sleep(interval*60)`. Unerwartete Ausnahmen in einem Lauf werden mit `logger.exception` geloggt, die Schleife läuft weiter. `KeyboardInterrupt` und `SIGTERM` beenden mit 0 (SIGTERM-Handler wirft `KeyboardInterrupt`). Die `GarminUploader`-Instanz lebt über alle Läufe, damit die Login-Pause nach abgelehntem Passwort hält.

- [ ] **Step 4: Alle Tests laufen lassen**

Run: `uv run pytest -v`
Expected: alle bestanden, nur der Real-File-Test eventuell übersprungen

- [ ] **Step 5: Commit**

```bash
git add src/mywhoosh_garmin_sync/config.py src/mywhoosh_garmin_sync/cli.py tests/test_config.py tests/test_cli.py
git commit -m "feat: Add configuration and command line interface"
```

---

### Task 8: Docker, Compose und README

**Files:**
- Create: `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example`, `README.md`

**Interfaces:**
- Consumes: Konsolenbefehl `mywhoosh-garmin-sync` (Task 7)

- [ ] **Step 1: Dateien anlegen**

- `Dockerfile`: Basis `python:3.13-slim`, `uv` per `COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv`, `uv sync --frozen --no-dev`, nicht-root-User `app` (UID 1000), `ENV DATA_DIR=/data PYTHONUNBUFFERED=1`, `VOLUME /data`, `ENTRYPOINT ["mywhoosh-garmin-sync"]`, `CMD ["run"]`.
- `docker-compose.yml`: Service `sync`, `build: .`, `env_file: .env`, `volumes: ["./data:/data"]`, `restart: unless-stopped`, `stdin_open: true` und `tty: true` für `login-garmin`.
- `.env.example`: alle Variablen aus der Spec-Tabelle, Pflichtwerte leer, optionale auskommentiert mit Standardwert.
- `.dockerignore`: `.git`, `.venv`, `data`, `.env`, `tests/fixtures/local`, `.idea`, `docs`.
- `README.md` (Deutsch): Zweck in zwei Sätzen, Einrichtung (`cp .env.example .env`, ausfüllen, `mkdir data`, `docker compose run --rm sync dry-run` zum Prüfen, `docker compose up -d`), Notfall-Login mit MFA, Logs (`docker compose logs -f`), eine fehlgeschlagene Fahrt wieder freigeben (Eintrag aus `data/state.json` löschen), Hinweis auf die inoffiziellen Schnittstellen.

- [ ] **Step 2: Image bauen und Hilfe aufrufen**

Run: `docker compose build && docker compose run --rm sync --help`
Expected: Build erfolgreich, Hilfe listet `run`, `once`, `dry-run`, `login-garmin`

- [ ] **Step 3: Fehlende Konfiguration im Container prüfen**

Run: `docker compose run --rm -e MYWHOOSH_EMAIL= sync once; echo "exit=$?"`
Expected: Meldung nennt `MYWHOOSH_EMAIL`, `exit=2`. Falls `.env` noch nicht existiert, vorher `cp .env.example .env`.

- [ ] **Step 4: Commit**

```bash
git add Dockerfile docker-compose.yml .dockerignore .env.example README.md
git commit -m "feat: Add Docker setup and README"
```

- [ ] **Step 5: Abnahme durch den Betreiber (nicht automatisierbar)**

Braucht echte Zugangsdaten in `.env`, die nur der Betreiber einträgt:
1. `docker compose run --rm sync dry-run`, dann `data/dry-run/*.edge1050.fit` prüfen, z. B. in fitfiletools oder per Upload auf einen Viewer.
2. `docker compose up -d`, im Log steht die Baseline.
3. Nach der nächsten Fahrt: Aktivität erscheint in Garmin Connect als Edge 1050 mit MyWhoosh-Titel. Darauf achten, ob die MyWhoosh-App während der Fahrt die Sitzung verloren hat.
