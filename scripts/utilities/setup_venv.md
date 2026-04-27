# setup_venv.sh / setup_venv.ps1

## Kurzbeschreibung
Erstellt Python Virtual Environment für pcloud-tools und installiert alle Dependencies aus requirements.txt.

## Parameter

### Bash (setup_venv.sh)
- `--force`: Überschreibt existierende venv
- `--skip-test`: Überspringt Test-Import

### PowerShell (setup_venv.ps1)
- `-Force`: Überschreibt existierende venv
- `-SkipTest`: Überspringt Test-Import

## Beispielaufrufe

### Erstinstallation (Linux/macOS)
```bash
./scripts/utilities/setup_venv.sh
```
Erstellt `.venv/` im Projekt-Root, installiert Dependencies, führt Test-Imports durch.

### Erstinstallation (Windows)
```powershell
.\scripts\utilities\setup_venv.ps1
```
Erstellt `.venv/` im Projekt-Root, installiert Dependencies, führt Test-Imports durch.

### Force-Reinstall (Linux/macOS)
```bash
./scripts/utilities/setup_venv.sh --force
```
Löscht alte venv, erstellt neue, installiert alles neu (für Updates/Reparatur).

### Force-Reinstall (Windows)
```powershell
.\scripts\utilities\setup_venv.ps1 -Force
```
Löscht alte venv, erstellt neue, installiert alles neu (für Updates/Reparatur).

### Nach Setup aktivieren
```bash
# Linux/macOS
source .venv/bin/activate

# Windows
.\.venv\Scripts\Activate.ps1
```
Aktiviert Virtual Environment für Development/Testing.
