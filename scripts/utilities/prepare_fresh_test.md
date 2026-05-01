# prepare_fresh_test.sh

## Kurzbeschreibung

Bereitet einen Clean-State für Workflow-Tests vor:
- Behält genau **einen** lokalen Snapshot (`--keep-snapshot`)
- Löscht alle anderen Snapshot-Ordner unter `RTB_BASE`
- Setzt den `latest`-Symlink auf den Keep-Snapshot
- Leert Archive (`indexes` immer, `manifests` abhängig von `--keep-local-manifest`)
- Räumt Temp-Files auf (`/tmp/pcloud_index_*.json`)

**Sicherheitsmodell:**  
Standard ist `--dry-run` — es werden keine Änderungen vorgenommen.  
Für echte Ausführung muss `--execute` explizit angegeben werden.

---

## Parameter

| Parameter | Pflicht | Default | Beschreibung |
|-----------|---------|---------|--------------|
| `--keep-snapshot <SNAPSHOT>` | ✅ ja | — | Snapshot der lokal behalten wird |
| `--keep-local-manifest <yes\|no>` | nein | `yes` | Lokales Manifest `<SNAPSHOT>.json` behalten |
| `--keep-lokal-manifest <yes\|no>` | nein | — | Alias für `--keep-local-manifest` |
| `--dry-run` | nein | **Default** | Nur Vorschau, keine Änderungen |
| `--execute` | nein | — | Führt Änderungen wirklich aus (destruktiv!) |
| `--yes` | nein | — | Ohne Rückfrage (nur mit `--execute` sinnvoll) |
| `--rtb-base <PATH>` | nein | `/mnt/backup/rtb_nas` | RTB Basis-Pfad |
| `--archive-base <PATH>` | nein | `/srv/pcloud-archive` | Archiv-Basis |
| `-h`, `--help` | nein | — | Hilfe anzeigen |

---

## Beispielaufrufe

### Dry-Run: Manifest behalten (Standard-Workflow)

```bash
bash /opt/apps/pcloud-tools/main/scripts/utilities/prepare_fresh_test.sh \
  --keep-snapshot 2026-04-27-173201 \
  --keep-local-manifest yes \
  --dry-run
```

**Was passiert:** Zeigt den Soll-Zustand — Manifest `2026-04-27-173201.json` bleibt erhalten,
alle anderen Manifests und alle Indexes werden gelöscht.  
Nützlich wenn ein Turbo/Delta-Upload getestet werden soll (Basis-Manifest schon vorhanden).

---

### Dry-Run: Manifest löschen (Full-Upload-Test)

```bash
bash /opt/apps/pcloud-tools/main/scripts/utilities/prepare_fresh_test.sh \
  --keep-snapshot 2026-04-27-173201 \
  --keep-local-manifest no \
  --dry-run
```

**Was passiert:** Zeigt den Soll-Zustand — alle Manifests und Indexes werden gelöscht.  
Nützlich wenn ein komplett frischer Full-Upload ohne Basis-Manifest getestet werden soll.

---

### Execute: Manifest behalten

```bash
bash /opt/apps/pcloud-tools/main/scripts/utilities/prepare_fresh_test.sh \
  --keep-snapshot 2026-04-27-173201 \
  --keep-local-manifest yes \
  --execute
```

---

### Execute: Manifest löschen (Full-Upload-Test)

```bash
bash /opt/apps/pcloud-tools/main/scripts/utilities/prepare_fresh_test.sh \
  --keep-snapshot 2026-04-27-173201 \
  --keep-local-manifest no \
  --execute
```

---

### Execute ohne Rückfrage (Automation/Scripting)

```bash
bash /opt/apps/pcloud-tools/main/scripts/utilities/prepare_fresh_test.sh \
  --keep-snapshot 2026-04-27-173201 \
  --keep-local-manifest yes \
  --execute \
  --yes
```

---

### Nach Cleanup: Upload starten

```bash
# venv aktivieren
source /opt/apps/safe-ops-cli/main/tools/venv_switch.sh pcloud-tools

# 2a) Test-Lauf (read-only) via rtb_wrapper
/opt/apps/rtb/rtb_wrapper.sh --check-only

# 2b) Produktions-Lauf via rtb_wrapper
/opt/apps/rtb/rtb_wrapper.sh

# 3a) Hinweis: --check-only nur im rtb_wrapper vorhanden

# 3b) Direkter pcloud-tools Produktions-Lauf
/opt/apps/pcloud-tools/main/wrapper_pcloud_sync_1to1.sh /mnt/backup/rtb_nas/2026-04-27-173201

# 3c) Direkter pcloud-tools Dry-Run
/opt/apps/pcloud-tools/main/wrapper_pcloud_sync_1to1.sh /mnt/backup/rtb_nas/2026-04-27-173201 --dry-run

# 3d) Copy/Turbo erzwingen (direkter Wrapper)
/opt/apps/pcloud-tools/main/wrapper_pcloud_sync_1to1.sh /mnt/backup/rtb_nas/2026-04-27-173201 --use-delta-copy
```

**Modus-Hinweis:**
- Default: Smart-Logik im `wrapper_pcloud_sync_1to1.sh` (automatische Strategie-Auswahl).
- Copy-Modus: maximale Geschwindigkeit, hoeherer Speicherverbrauch.
- Smart-Logik: balanciert Upload-Zeit und Speicherverbrauch (empfohlen).

**Wie Modus explizit forcieren?**
- `rtb_wrapper.sh`: kein direkter Modus-Parameter fuer Smart/Copy; nutzt den pcloud-wrapper-Default (Smart).
- `wrapper_pcloud_sync_1to1.sh`: unterstuetzt jetzt `--dry-run` und `--use-delta-copy` (Passthrough an das Push-Tool).
- Explizites Forcieren von Copy/Turbo geht aktuell nur im Low-Level Push-Tool:

```bash
/opt/apps/pcloud-tools/venv/bin/python /opt/apps/pcloud-tools/main/pcloud_push_json_manifest_to_pcloud.py \
  --manifest /tmp/pcloud_mani.2026-04-27-173201.json \
  --dest-root /Backup/rtb_1to1 \
  --snapshot-mode 1to1 \
  --use-delta-copy \
  --env-file /opt/apps/pcloud-tools/main/.env
```

**Wichtige Parameter (Kurzuebersicht):**
- `rtb_wrapper.sh`:
  - `--check-only` (read-only Check)
  - `--force` (Safety-Gate umgehen)
  - `--upload-only /mnt/backup/rtb_nas/<SNAPSHOT>` (nur Upload, kein neues RTB-Backup)
- `wrapper_pcloud_sync_1to1.sh`:
  - `wrapper_pcloud_sync_1to1.sh [SNAPSHOT|/path/to/SNAPSHOT] [--dry-run] [--use-delta-copy]`
  - kein `--check-only` (das bleibt rtb_wrapper-only)
  - wichtige ENVs: `PCLOUD_MANIFEST_MODE=smart|full`, `PCLOUD_GAP_STRATEGY=conservative|optimistic|aggressive`

---

## Was wird gelöscht

| Bereich | Aktion |
|---------|--------|
| RTB-Snapshots | Alle außer `--keep-snapshot` werden gelöscht |
| `latest`-Symlink | Wird auf `--keep-snapshot` gesetzt |
| `manifests/*.json` | Bei `--keep-local-manifest yes`: alle außer `<keep-snapshot>.json`; bei `no`: alle |
| `indexes/*.json` | Immer alle gelöscht |
| `/tmp/pcloud_index_*.json` | Immer gelöscht |
| **pCloud (remote)** | **Nicht berührt** — muss manuell geleert werden |

> **Hinweis:** pCloud selbst (`Backup/rtb_1to1/_snapshots/`) muss manuell geleert werden,
> bevor ein sauberer Full-Upload-Test möglich ist.

---

## Use Cases

**Turbo/Delta-Upload testen** (Basis-Manifest schon lokal vorhanden):
```bash
# Clean-State mit behaltenem Manifest
bash /opt/apps/pcloud-tools/main/scripts/utilities/prepare_fresh_test.sh --keep-snapshot 2026-04-27-173201 --keep-local-manifest yes --execute

# Upload — Smart-Controller wählt Turbo/Delta
/opt/apps/pcloud-tools/main/wrapper_pcloud_sync_1to1.sh /mnt/backup/rtb_nas/2026-04-27-173201
```

**Fresh Full-Upload testen** (ohne jegliches lokales Manifest):
```bash
# Clean-State, alle Archive leer
bash /opt/apps/pcloud-tools/main/scripts/utilities/prepare_fresh_test.sh --keep-snapshot 2026-04-27-173201 --keep-local-manifest no --execute

# Upload — Smart-Controller wählt SAFE-Mode (Full-Upload)
/opt/apps/pcloud-tools/main/wrapper_pcloud_sync_1to1.sh /mnt/backup/rtb_nas/2026-04-27-173201
```

**Nach pCloud-Problemen (Re-Upload):**
```bash
bash /opt/apps/pcloud-tools/main/scripts/utilities/prepare_fresh_test.sh --keep-snapshot 2026-04-27-173201 --execute
/opt/apps/pcloud-tools/main/wrapper_pcloud_sync_1to1.sh /mnt/backup/rtb_nas/2026-04-27-173201
```

---

## Output-Beispiel (Dry-Run)

Der Dry-Run zeigt im **[5/5] Final Check** den erwarteten **Soll-Zustand** nach `--execute`,
nicht den aktuellen Ist-Zustand. Das ermöglicht eine vollständige Prüfung vor der echten Ausführung.

```
════════════════════════════════════════════════════════════════
🧹 pCloud-Tools: Fresh Test Preparation
════════════════════════════════════════════════════════════════

Aktion: Clean-State für Workflow-Test
  - Modus: DRY-RUN (keine Änderungen)
  - Behalte Snapshot: 2026-04-27-173201
  - Lösche alle anderen lokalen Snapshots (1)
  - Setze latest → 2026-04-27-173201
  - Leere Archive (indexes immer, manifests abhängig von --keep-local-manifest=yes)
  - Cleanup Temp-Files

ℹ️  Dry-Run aktiv: Es werden keine Dateien gelöscht und keine Symlinks geändert.

════════════════════════════════════════════════════════════════
[1/5] RTB-Snapshots aufräumen
════════════════════════════════════════════════════════════════
📋 Aktueller Stand:
drwxr-xr-x 15 root   root   4.0K Apr  6 15:58 2026-04-27-173201
drwxr-xr-x 16 root   root   4.0K Apr  6 15:58 2026-05-01-003410
lrwxrwxrwx  1 root   root     17 May  1 00:34 latest -> 2026-05-01-003410

[dry] rm -rf /mnt/backup/rtb_nas/2026-05-01-003410

════════════════════════════════════════════════════════════════
[2/5] Latest-Symlink setzen
════════════════════════════════════════════════════════════════
[dry] rm -f /mnt/backup/rtb_nas/latest
[dry] ln -s 2026-04-27-173201 /mnt/backup/rtb_nas/latest
🔗 (dry) Latest würde gesetzt auf → 2026-04-27-173201

════════════════════════════════════════════════════════════════
[3/5] Archive leeren (manifests + indexes)
════════════════════════════════════════════════════════════════
🧹 Lösche Manifests außer: 2026-04-27-173201.json
[dry] rm -f /srv/pcloud-archive/manifests/2026-05-01-003410.json
   ✓ Keep-Manifest behalten: 2026-04-27-173201.json
🗑️  Lösche 1 Indexes
[dry] rm -f /srv/pcloud-archive/indexes/*.json

════════════════════════════════════════════════════════════════
[4/5] Temp-Files aufräumen
════════════════════════════════════════════════════════════════
○  Keine Temp-Files gefunden

════════════════════════════════════════════════════════════════
[5/5] Final Check
════════════════════════════════════════════════════════════════
📊 Erwarteter Soll-Zustand nach --execute:

RTB-Snapshots:
  2026-04-27-173201 (behalten)
  2026-05-01-003410 → gelöscht

Latest-Symlink:
  → 2026-04-27-173201

Archive:
  manifests/: 1 Datei(en) (nur 2026-04-27-173201.json)
  indexes/:   0 Dateien (alle gelöscht)

  → Führe '--execute' aus um diesen Zustand herzustellen

════════════════════════════════════════════════════════════════
✅ Dry-Run abgeschlossen (keine Änderungen durchgeführt)
════════════════════════════════════════════════════════════════
```
