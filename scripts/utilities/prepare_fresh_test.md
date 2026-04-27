# prepare_fresh_test.sh

## Kurzbeschreibung
Bereitet einen Clean-State für Workflow-Tests vor. Löscht fehlerhafte Snapshots, leert Archive (manifests + indexes), setzt latest-Symlink auf Test-Snapshot und räumt Temp-Files auf. Ideal nach massiven Code-Umbauten oder für systematische Integrationstests.

## Parameter

Keine Parameter erforderlich - Script ist interaktiv und fragt vor Ausführung um Bestätigung.

**Konfiguriert im Script:**
- `KEEP_SNAPSHOT`: Snapshot der behalten wird (Default: `2026-04-10-075334`)
- `DELETE_SNAPSHOTS`: Array von Snapshots die gelöscht werden
- `RTB_BASE`: RTB-Root-Verzeichnis (Default: `/mnt/backup/rtb_nas`)
- `ARCHIVE_BASE`: pCloud-Archive-Verzeichnis (Default: `/srv/pcloud-archive`)

**Wichtig:** Vor Verwendung die Snapshot-Namen im Script anpassen!

## Beispielaufrufe

### Standard-Verwendung (interaktiv)
```bash
cd /opt/apps/pcloud-tools
./scripts/utilities/prepare_fresh_test.sh
```
- Zeigt Übersicht was gelöscht wird
- Fragt um Bestätigung vor Ausführung
- Löscht fehlerhafte Snapshots: `2026-04-17-235901`, `2026-04-18-004404`
- Behält Test-Snapshot: `2026-04-10-075334`
- Leert `/srv/pcloud-archive/manifests/` und `/srv/pcloud-archive/indexes/`
- Setzt `latest` → `2026-04-10-075334`
- Räumt `/tmp/pcloud_index_*.json` auf
- Zeigt finale Status-Übersicht

### Nach Cleanup: Upload starten
```bash
# Variante 1: Direkt mit wrapper_pcloud_sync_1to1.sh
source venv_switch.sh pcloud-tools
./wrapper_pcloud_sync_1to1.sh /mnt/backup/rtb_nas/2026-04-10-075334

# Variante 2: Via rtb_wrapper.sh (--upload-only mode)
/opt/apps/rtb/rtb_wrapper.sh --upload-only /mnt/backup/rtb_nas/2026-04-10-075334
```
Nach dem Cleanup kann der Test-Upload sofort gestartet werden (Full-Mode, da keine Reference vorhanden).

### Custom Snapshot-Namen (Script bearbeiten)
```bash
# Im Script anpassen (Zeile 26-28):
KEEP_SNAPSHOT="2026-05-01-120000"
DELETE_SNAPSHOTS=("2026-04-28-235901" "2026-04-29-004404")

# Dann ausführen:
./scripts/utilities/prepare_fresh_test.sh
```
Snapshot-Namen müssen vor Ausführung im Script konfiguriert werden (keine CLI-Argumente).

## Use Cases

**Nach großem Refactoring:**
```bash
# Clean-State erstellen für Test des neuen Codes
./scripts/utilities/prepare_fresh_test.sh

# Workflow-Test durchführen
source venv_switch.sh pcloud-tools
./wrapper_pcloud_sync_1to1.sh /mnt/backup/rtb_nas/2026-04-10-075334

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
./scripts/utilities/prepare_fresh_test.sh

# Re-Upload (via rtb_wrapper mit --upload-only)
/opt/apps/rtb/rtb_wrapper.sh --upload-only /mnt/backup/rtb_nas/2026-04-10-075334
```

**Vor Production-Release:**
```bash
# Kompletten Backup-Workflow testen
./scripts/utilities/prepare_fresh_test.sh

# Upload via production workflow
/opt/apps/rtb/rtb_wrapper.sh --upload-only /mnt/backup/rtb_nas/2026-04-10-075334

# Logs überwachen
tail -f /var/log/backup/pcloud_sync.log
```

## Was wird gelöscht

Das Script führt folgende Cleanup-Aktionen durch:

1. **RTB-Snapshots:** 
   - Löscht: `2026-04-17-235901`, `2026-04-18-004404`
   - Behält: `2026-04-10-075334`

2. **Latest-Symlink:**
   - Neu gesetzt: `latest` → `2026-04-10-075334`

3. **Archive:**
   - Leert: `/srv/pcloud-archive/manifests/*.json`
   - Leert: `/srv/pcloud-archive/indexes/*.json`

4. **Temp-Files:**
   - Löscht: `/tmp/pcloud_index_*.json`

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
