# Virtual Environment Management

Zwei komplementäre Ansätze für Python venvs: Development vs Production.

---

## Development: Lokale `.venv`

**Ziel:** Schnelles lokales Setup für Entwicklung und Testing.

**Verwendung:**

```bash
# Linux/macOS
./scripts/setup_venv.sh
source .venv/bin/activate

# Windows
.\scripts\setup_venv.ps1
.\.venv\Scripts\Activate.ps1
```

**Features:**
- ✅ Erstellt `.venv` im Projekt-Root
- ✅ Installiert Dependencies aus `requirements.txt`
- ✅ User-Level (kein sudo)
- ✅ Plattform-übergreifend (Windows + Linux)
- ✅ VS Code erkennt automatisch

**Optionen:**

```bash
# Venv neu erstellen (überschreibt existierende)
./scripts/setup_venv.sh --force

# Test überspringen
./scripts/setup_venv.sh --skip-test
```

---

## Production: Datierte venvs mit Rotation

**Ziel:** Zero-downtime Updates, Rollback-Fähigkeit, Audit-Trail.

**Verwendung:**

```bash
# Neue venv erstellen (z.B. venv-20260424-1530)
sudo /opt/apps/venv_rotate.sh /opt/apps/pcloud-tools

# Mit Optionen
sudo /opt/apps/venv_rotate.sh --keep 3 --req /tmp/requirements.lock /opt/apps/pcloud-tools
```

**Struktur:**

```
/opt/apps/pcloud-tools/
├── venv → venv-20260424-1530  # Symlink (aktuell)
├── venv-20260424-1530/        # Neueste
├── venv-20260420-1045/        # Vorherige
└── venv-20260415-0900/        # Älteste (wird bei --keep 2 gelöscht)
```

**Features:**
- ✅ Datierte venvs (`venv-YYYYmmdd-HHMM`)
- ✅ Symlink `venv` → neueste Version
- ✅ Automatische Rotation (behält N neueste)
- ✅ Rollback: Symlink umhängen
- ✅ Zero-downtime: Services nutzen Symlink

**Optionen:**

| Flag | Default | Beschreibung |
|------|---------|--------------|
| `--keep N` | `3` | So viele venvs behalten |
| `--python PATH` | autodetect | Python-Interpreter |
| `--req FILE` | `main/requirements.txt` | Requirements-Datei |
| `--dry-run` | - | Nur anzeigen, nichts ausführen |

**Beispiele:**

```bash
# Standard (behält 3 venvs)
sudo /opt/apps/venv_rotate.sh /opt/apps/pcloud-tools

# Nur 2 behalten
sudo /opt/apps/venv_rotate.sh --keep 2 /opt/apps/entropywatcher

# Custom requirements
sudo /opt/apps/venv_rotate.sh --req /tmp/requirements.lock /opt/apps/safe-ops-cli

# Dry-run (testen ohne Änderungen)
sudo /opt/apps/venv_rotate.sh --dry-run /opt/apps/pcloud-tools
```

---

## Quick Switch: `venv_switch.sh`

**Ziel:** Schnell zwischen Projekten wechseln (cd + activate in einem Befehl).

**Verwendung:**

```bash
# Aktiviere venv + wechsle ins Verzeichnis
source venv_switch.sh pcloud-tools

# Oder mit vollständigem Pfad
source venv_switch.sh /opt/apps/entropywatcher

# Status anzeigen
source venv_switch.sh --status

# Deaktivieren
source venv_switch.sh --deactivate
```

**Features:**
- ✅ Folgt venv-Symlink (zeigt auf aktuellste Version)
- ✅ `cd` ins Projekt-Verzeichnis
- ✅ Deaktiviert vorherige venv automatisch
- ✅ Kurzformen ohne `/opt/apps/` Präfix

**Wichtig:** Muss mit `source` aufgerufen werden (nicht `./`), damit `cd` funktioniert!

**Beispiel-Session:**

```bash
# Start: irgendwo im System
$ pwd
/home/user

# In pcloud-tools wechseln + venv aktivieren
$ source venv_switch.sh pcloud-tools
✓ Aktiviere: venv-20260424-1530
✓ Venv aktiv: venv-20260424-1530
✓ Verzeichnis: /opt/apps/pcloud-tools
  Python 3.11.2

# Jetzt automatisch im richtigen Verzeichnis
(.venv) $ pwd
/opt/apps/pcloud-tools

# Zu anderem Projekt wechseln (deaktiviert automatisch alte venv)
(.venv) $ source venv_switch.sh entropywatcher
ℹ Deaktiviere vorherige venv: venv-20260424-1530
✓ Aktiviere: venv-20260420-1045
✓ Venv aktiv: venv-20260420-1045
✓ Verzeichnis: /opt/apps/entropywatcher
```

---

## Vergleich

| Aspekt | Development (`.venv`) | Production (`venv_rotate`) |
|--------|----------------------|---------------------------|
| **Zweck** | Lokale Entwicklung | Server-Deployments |
| **Ort** | `.venv` im Projekt | `/opt/apps/<project>/venv-*` |
| **Permissions** | User-Level | sudo (root) |
| **Rotation** | Nein | Ja (--keep N) |
| **Rollback** | Nein | Ja (Symlink umhängen) |
| **Windows** | ✅ Ja | ❌ Linux only |
| **Audit** | Nein | Ja (datierte Namen) |

---

## Workflows

### Neues Projekt Setup (Dev)

```bash
# 1. Repo klonen
git clone https://github.com/user/project.git
cd project

# 2. Venv erstellen
./scripts/setup_venv.sh

# 3. Aktivieren
source .venv/bin/activate

# 4. Entwickeln
python scripts/my_script.py
```

### Dependencies Update (Production)

```bash
# 1. Requirements vorbereiten (lokal oder auf Server)
# Option A: Direkt aus Repo
cd /opt/apps/pcloud-tools/main
git pull

# Option B: Frozen requirements
pip freeze > /tmp/requirements.lock

# 2. Neue venv mit neuen Dependencies
sudo /opt/apps/venv_rotate.sh --req /tmp/requirements.lock /opt/apps/pcloud-tools
# → Erstellt venv-20260424-1530, installiert neue Packages

# 3. Test (optional)
/opt/apps/pcloud-tools/venv/bin/python --version
/opt/apps/pcloud-tools/venv/bin/python -c "import pandas; print(pandas.__version__)"

# 4. Systemd Services laden neue venv automatisch (folgen Symlink)
sudo systemctl restart pcloud-backup.service

# 5. Bei Problemen: Rollback
sudo ln -sfn /opt/apps/pcloud-tools/venv-20260420-1045 /opt/apps/pcloud-tools/venv
sudo systemctl restart pcloud-backup.service
```

### Rollback (Production)

```bash
# 1. Liste verfügbare venvs
ls -ldt /opt/apps/pcloud-tools/venv-*

# 2. Symlink auf vorherige Version setzen
sudo ln -sfn /opt/apps/pcloud-tools/venv-20260420-1045 /opt/apps/pcloud-tools/venv

# 3. Services neustarten
sudo systemctl restart pcloud-backup.service
sudo systemctl restart telegram-commander.service
```

---

## Systemd Integration

Services nutzen den venv-Symlink für Zero-Downtime Updates:

```ini
[Service]
Type=oneshot
ExecStart=/opt/apps/pcloud-tools/venv/bin/python /opt/apps/pcloud-tools/main/scripts/backup.py
```

**Nach venv_rotate:**
- ✅ Symlink zeigt auf neue venv
- ✅ Nächste Timer-Execution nutzt neue venv
- ✅ Laufende Services unberührt (bis zum Neustart)

**Manuelle Service-Updates:**

```bash
# Nach venv_rotate
sudo systemctl restart pcloud-backup.service
```

---

## Best Practices

### Development
- ✅ `.venv` in `.gitignore`
- ✅ `requirements.txt` committen
- ✅ Regelmäßig `pip freeze > requirements.txt`
- ✅ VS Code wählt `.venv` automatisch

### Production
- ✅ Minimum 2-3 venvs behalten (`--keep 3`)
- ✅ Test nach Update (vor Service-Restart)
- ✅ Alte venvs = Rollback-Option
- ✅ Installations-Log speichern für Audit

### Beide
- ✅ Pin major versions in `requirements.txt`
- ✅ Use virtual environments IMMER (nie system Python)
- ✅ Dokumentiere Custom-Setups

---

## Troubleshooting

### "Command not found: python"

**Dev:**
```bash
# Venv nicht aktiviert
source .venv/bin/activate
```

**Production:**
```bash
# Vollständiger Pfad verwenden
/opt/apps/pcloud-tools/venv/bin/python script.py
```

### "No module named 'pandas'"

**Check venv:**
```bash
# Dev
which python
# Sollte: /path/to/project/.venv/bin/python

# Production
/opt/apps/pcloud-tools/venv/bin/python -c "import pandas"
```

**Fix:**
```bash
# Dev
pip install -r requirements.txt

# Production
sudo /opt/apps/venv_rotate.sh /opt/apps/pcloud-tools  # Neu erstellen
```

### venv_switch.sh: "Must be sourced"

❌ **Falsch:**
```bash
./venv_switch.sh pcloud-tools
```

✅ **Richtig:**
```bash
source venv_switch.sh pcloud-tools
```

**Grund:** `cd` funktioniert nur im aktuellen Shell-Context (nicht in Sub-Shell).

---

## Tools

| Tool | Repo | Zweck |
|------|------|-------|
| `setup_venv.sh` | pcloud-tools | Dev: Lokale .venv Setup (Linux) |
| `setup_venv.ps1` | pcloud-tools | Dev: Lokale .venv Setup (Windows) |
| `venv_rotate.sh` | Safe-CLI-Helpers | Production: Datierte venvs + Rotation |
| `venv_switch.sh` | Safe-CLI-Helpers | Helper: cd + activate in einem |

**Installation (Safe-CLI-Helpers):**

```bash
# Tools verfügbar machen
sudo ln -s /opt/apps/Safe-CLI-Helpers/tools/venv_rotate.sh /usr/local/bin/
sudo ln -s /opt/apps/Safe-CLI-Helpers/tools/venv_switch.sh /usr/local/bin/

# Dann von überall nutzbar
sudo venv_rotate.sh /opt/apps/pcloud-tools
source venv_switch.sh pcloud-tools
```
