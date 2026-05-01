# prepare_fresh_test.sh

## Kurzbeschreibung
Bereitet einen Clean-State fuer Workflow-Tests vor. Behaelt genau einen lokalen Snapshot, loescht alle anderen lokalen Snapshots, setzt latest auf den Keep-Snapshot, leert Archive (indexes immer) und raeumt Temp-Files auf.

## Parameter

Pflichtparameter:
- --keep-snapshot <SNAPSHOT>

Optionale Parameter:
- --keep-local-manifest <yes|no> (Default: yes)
- --keep-lokal-manifest <yes|no> (Alias)
- --dry-run (Default, nur Vorschau)
- --execute (fuehrt echte Loeschungen/Änderungen aus)
- --rtb-base <PATH> (Default: /mnt/backup/rtb_nas)
- --archive-base <PATH> (Default: /srv/pcloud-archive)
- --yes (ohne Rueckfrage)
- --help

## Beispielaufrufe

### Standard (interaktiv, Keep-Manifest behalten)
```bash
cd /opt/apps/pcloud-tools
./scripts/utilities/prepare_fresh_test.sh \
   --keep-snapshot 2026-04-27-173201
```

Hinweis:
- Standard ist Dry-Run. Es wird nur angezeigt, was geloescht/gesetzt wuerde.
- Fuer echte Ausfuehrung muss zusaetzlich --execute gesetzt werden.

### Keep-Manifest explizit behalten
```bash
./scripts/utilities/prepare_fresh_test.sh \
   --keep-snapshot 2026-04-27-173201 \
   --keep-local-manifest yes
```

### Keep-Manifest loeschen
```bash
./scripts/utilities/prepare_fresh_test.sh \
   --keep-snapshot 2026-04-27-173201 \
   --keep-local-manifest no
```

### Ohne Rueckfrage (Automation)
```bash
./scripts/utilities/prepare_fresh_test.sh \
   --keep-snapshot 2026-04-27-173201 \
   --keep-local-manifest yes \
   --execute \
   --yes
```

### Echte Ausfuehrung (destruktiv)
```bash
./scripts/utilities/prepare_fresh_test.sh \
   --keep-snapshot 2026-04-27-173201 \
   --keep-local-manifest yes \
   --execute
```

### Nach Cleanup: Upload starten
```bash
# Variante 1: Direkt mit wrapper_pcloud_sync_1to1.sh
source venv_switch.sh pcloud-tools
./wrapper_pcloud_sync_1to1.sh /mnt/backup/rtb_nas/2026-04-27-173201

# Variante 2: Via rtb_wrapper.sh (--upload-only mode)
/opt/apps/rtb/rtb_wrapper.sh --upload-only /mnt/backup/rtb_nas/2026-04-27-173201
```
Nach dem Cleanup kann der Test-Upload sofort gestartet werden (Full-Mode, falls pCloud bereits geleert wurde).

## Use Cases

**Nach großem Refactoring:**
```bash
# Clean-State erstellen für Test des neuen Codes
./scripts/utilities/prepare_fresh_test.sh --keep-snapshot 2026-04-27-173201

# Workflow-Test durchführen
source venv_switch.sh pcloud-tools
./wrapper_pcloud_sync_1to1.sh /mnt/backup/rtb_nas/2026-04-27-173201

# Beobachten:
# ✓ Thread-Count (parallele Uploads)
# ✓ Debug-Output (neue CLI-Logs)
# ✓ Upload-Speed & Fortschritt
# ✓ Timeouts (keine Hänger)
# ✓ Manifest-Erstellung
# ✓ Index-Update
```

**Nach pCloud-Problemen (Re-Upload):**
```bash
# Archive leeren + Test-Snapshot erneut hochladen
./scripts/utilities/prepare_fresh_test.sh --keep-snapshot 2026-04-27-173201

# Re-Upload (via rtb_wrapper mit --upload-only)
/opt/apps/rtb/rtb_wrapper.sh --upload-only /mnt/backup/rtb_nas/2026-04-27-173201
```

**Vor Production-Release:**
```bash
# Kompletten Backup-Workflow testen
./scripts/utilities/prepare_fresh_test.sh --keep-snapshot 2026-04-27-173201

# Upload via production workflow
/opt/apps/rtb/rtb_wrapper.sh --upload-only /mnt/backup/rtb_nas/2026-04-27-173201

# Logs überwachen
tail -f /var/log/backup/pcloud_sync.log
```

## Was wird gelöscht

Das Script führt folgende Cleanup-Aktionen durch:

1. **RTB-Snapshots:** 
   - Behaelt: den via --keep-snapshot angegebenen Snapshot
   - Loescht: alle anderen Snapshot-Ordner unter RTB_BASE

2. **Latest-Symlink:**
   - Neu gesetzt: latest -> Keep-Snapshot

3. **Archive:**
   - indexes/*.json: immer geloescht
   - manifests/*.json:
     - bei --keep-local-manifest yes: alle ausser <keep-snapshot>.json
     - bei --keep-local-manifest no: alle geloescht

4. **Temp-Files:**
   - Loescht: /tmp/pcloud_index_*.json

5. **pCloud (manuell):**
   - Script löscht NICHT auf pCloud
   - `Backup/rtb_1to1/_snapshots/` muss manuell geleert werden

## Output-Beispiel

```
════════════════════════════════════════════════════════════════
🧹 pCloud-Tools: Fresh Test Preparation
════════════════════════════════════════════════════════════════

Aktion: Clean-State für Workflow-Test
  - Lösche fehlerhafte Snapshots (2026-04-17-235901, 2026-04-18-004404)
  - Setze latest → 2026-04-10-075334
  - Leere Archive (manifests + indexes)
  - Cleanup Temp-Files

▶ Fortfahren? [y/N] y

════════════════════════════════════════════════════════════════
[1/5] RTB-Snapshots aufräumen
════════════════════════════════════════════════════════════════
📋 Aktueller Stand:
drwxr-xr-x 15 root root 4.0K Apr  6 15:58 2026-04-10-075334
drwxr-xr-x 15 root root 4.0K Apr  6 15:58 2026-04-17-235901
drwxr-xr-x 15 root root 4.0K Apr  6 15:58 2026-04-18-004404
lrwxrwxrwx  1 root root   17 Apr 18 00:44 latest -> 2026-04-18-004404

🗑️  Lösche: 2026-04-17-235901
   ✓ Gelöscht
🗑️  Lösche: 2026-04-18-004404
   ✓ Gelöscht

════════════════════════════════════════════════════════════════
[2/5] Latest-Symlink setzen
════════════════════════════════════════════════════════════════
🔗 Latest → 2026-04-10-075334
   Kontrolle: latest zeigt auf 2026-04-10-075334

[...weitere Schritte...]

════════════════════════════════════════════════════════════════
✅ Clean-State vorbereitet!
════════════════════════════════════════════════════════════════

🚀 Nächste Schritte:

1. In pcloud-tools wechseln + venv aktivieren (in EINEM Befehl):
   source venv_switch.sh pcloud-tools

2. Echter Upload (Full-Mode):
   ./wrapper_pcloud_sync_1to1.sh /mnt/backup/rtb_nas/2026-04-10-075334

Good luck! 🍀
```
