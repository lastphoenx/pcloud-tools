# Pool-Pipeline — Vorgehensplan (Rollout)

> Status: **DRAFT zur Review** (2026-05-30). Enthält **keine** ausgeführten Operationen.
> Nichts hier wurde an Services/Wrappern auf der pi verändert. Architektur-Details:
> siehe `POOL_MODE_2026_REBUILD.md`.

## 0. Leitprinzip (begründet Punkt c)

Im Pool-Modell ist **jeder Snapshot eigenständig**: seine Stubs zeigen per `pool_fileid`
direkt in den Pool. Es gibt **keine Abhängigkeit** zwischen Snapshots.

Daraus folgt direkt:
- **Gap-Logik ist überflüssig.** Ein fehlender (älterer) Snapshot beschädigt spätere
  nicht — es gibt keine „chain". Fehlt etwas, lädt man es einfach (in beliebiger
  Reihenfolge) nach. Reihenfolge/„Lücken" sind irrelevant.
- **`--retention-sync` (1to1) ist überflüssig.** Kein Anchor-Promoting nötig. Platz
  freigeben macht später der **GC** (`pcloud_pool_gc.py`).
- Der „Catch-up verpasster Läufe" = schlicht: *für jeden lokalen Snapshot ohne
  `.upload_complete` remote → hochladen*. Mehr braucht es nicht.

## 1. Zielzustand der Pipeline

```
RTB erstellt Snapshot  →  rtb_pool_wrapper.sh (Orchestrator, hält Lock)
                              └─ ruft wrapper_pcloud_pool_sync_1to1.sh (pool-Upload)
                                    └─ pro fehlendem Snapshot:
                                        pcloud_json_pool_manifest.py (scan→manifest)
                                        pcloud_push_json_pool_manifest_to_pcloud.py --snapshot-mode pool
```
- **dest-root:** `/Backup/rtb_pool` (neu, sauber).
- **Modus:** immer `pool`.
- **Kein** `--retention-sync`, **keine** Gap-/Chain-Behandlung.

## 2. Snapshot-für-Snapshot

### Snapshot 1 (initial) — erledigt/laufend
- Voll-Upload nach `/Backup/rtb_pool` (Full-Pool-Mode, da remote leer).
- Ergebnis prüfen: `_pool/` befüllt, `_snapshots/<snap>/…`, `_index/content_index.json`
  (enriched), `.upload_complete` gesetzt.

### Snapshot 2 — via `wrapper_pcloud_pool_sync_1to1.sh` (zu finalisieren)
- Scout findet jetzt **1 remote Basis** → **Turbo-Delta-Mode** (copyfolder + Diff).
- Damit wird der Delta-/Scout-/Bulk-Delete-Pfad zum ersten Mal echt geübt.
- **Vor dem Lauf** Wrapper anpassen — siehe §3.

### Snapshot 3+ — via `rtb_pool_wrapper.sh` (neu zu erstellen)
- Orchestrator, der RTB-Snapshot-Erstellung und pCloud-Sync unter **einem** Lock bündelt.
- Ruft intern `wrapper_pcloud_pool_sync_1to1.sh` mit `BACKUP_PIPELINE_LOCKED=1`.
- Entwurf siehe §4.

## 3. `wrapper_pcloud_pool_sync_1to1.sh` — Finalisierung

**3.1 Defaults korrigieren**
```bash
PCLOUD_DEST=${PCLOUD_DEST:-/Backup/rtb_pool}   # statt /Backup/rtb_1to1
```

**3.2 Retention-Sync entfernen** (im Pool-Modell sinnlos)
- Block `need_retention_sync()` und die Zeile
  `[[ "$(need_retention_sync)" == "YES" ]] && RET="--retention-sync"` streichen;
  `$RET` aus dem Push-Aufruf entfernen.

**3.3 Gap-/Chain-Logik durch einfachen Catch-up-Loop ersetzen**
Der gesamte Block „Gap-Strategie / validate_snapshot_integrity / delete_remote_snapshot /
rebuild" (ca. Zeilen 359–423 und 753–903) entfällt. Ersatz — simpel und korrekt fürs Pool-Modell:
```bash
# Catch-up: jeder lokale Snapshot ohne remote .upload_complete wird hochgeladen.
# Reihenfolge egal (eigenständige Snapshots). Keine Gap-/Chain-Behandlung nötig.
mapfile -t local_snaps < <(local_snapshot_names)
mapfile -t remote_snaps < <(remote_snapshot_names)   # nur .upload_complete-Snapshots

uploaded_count=0
for s in "${local_snaps[@]}"; do
  [[ -n "$TARGET_SNAPSHOT" && "$s" != "$TARGET_SNAPSHOT" ]] && continue
  if [[ "$(is_remote_cached "$s")" == "NO" ]]; then
    _log INFO "Lade fehlenden Snapshot: $s"
    build_and_push "$RTB/$s" || exit 1
    if [[ "$(remote_snapshot_exists "$s")" == "NO" ]]; then
      _log ERROR "Upload von $s fertig, aber .upload_complete fehlt → FAILED"
      exit 1
    fi
    uploaded_count=$((uploaded_count + 1))
  fi
done
[[ $uploaded_count -eq 0 ]] && _log INFO "Alle Snapshots bereits auf pCloud."
```
> `remote_snapshot_names`/`load_remote_snapshots` (zählt nur Snapshots **mit**
> `.upload_complete`) bleibt — das ist die korrekte „ist remote vollständig da?"-Quelle.
> `build_and_push` bleibt unverändert (Manifest-Generierung smart-mode + Pool-Push).

**3.4 `finalize_index_fileids` (1to1) im Bootstrap entfernen**
- Der enriched Index hält fileids bereits → der `finalize_index_fileids`-Aufruf aus
  `pcloud_push_json_manifest_to_pcloud` (1to1-Modul) ist im Pool-Modell obsolet.

**3.5 Optional (Komfort):** sobald Auto-Mode-Detect im Push-Tool ist (siehe Rebuild-Doku
offener Punkt 2), kann `--snapshot-mode pool` im Wrapper entfallen.

## 4. `rtb_pool_wrapper.sh` — Entwurf (neu)

Analog zum bestehenden `rtb_wrapper.sh` (1to1), aber ruft den **Pool**-Wrapper.
Aufgaben: RTB-Snapshot triggern/abwarten → globales Lock → Pool-Sync.

```bash
#!/usr/bin/env bash
set -euo pipefail
# rtb_pool_wrapper.sh — Orchestrator: RTB-Snapshot + pCloud-Pool-Sync unter EINEM Lock.

MAIN_DIR=${MAIN_DIR:-/opt/apps/pcloud-tools/main}
LOCKFILE=${LOCKFILE:-/run/backup_pipeline.lock}
WAIT_SEC=${WAIT_SEC:-7200}
POOL_WRAPPER=${POOL_WRAPPER:-${MAIN_DIR}/wrapper_pcloud_pool_sync_1to1.sh}

exec 9>"$LOCKFILE"
flock -w "$WAIT_SEC" 9 || { echo "Lock busy"; exit 0; }
export BACKUP_PIPELINE_LOCKED=1   # innerer Wrapper überspringt eigenes Lock

# 1) (falls hier orchestriert) RTB-Snapshot erstellen — sonst entfällt dieser Schritt,
#    wenn RTB separat läuft und nur 'latest' aktualisiert.
#    <RTB-Snapshot-Kommando hier, projektspezifisch>

# 2) pCloud-Pool-Sync (lädt alle fehlenden Snapshots nach)
exec "$POOL_WRAPPER" "$@"
```
> **Zu klären (wach):** Erstellt `rtb_pool_wrapper.sh` selbst den RTB-Snapshot, oder läuft
> RTB getrennt und der Wrapper synct nur? Das bestimmt Schritt 1. Vorlage ist das vorhandene
> `rtb_wrapper.sh` (1to1) — beim Finalisieren 1:1 dessen RTB-Teil übernehmen, nur den
> pCloud-Aufruf auf den Pool-Wrapper umstellen.

## 5. systemd / Services (Checkliste — NICHT ausgeführt)

> Inventar nötig auf der pi: `systemctl list-unit-files | grep -i -E 'pcloud|rtb|backup'`
> und Inhalt von `pcloud-tools/systemd/`.

- [ ] **Service-Unit** auf `rtb_pool_wrapper.sh` umstellen (statt 1to1-Wrapper):
      `ExecStart=/opt/apps/pcloud-tools/main/rtb_pool_wrapper.sh`
- [ ] **Env/Default** prüfen: `PCLOUD_DEST=/Backup/rtb_pool` (Unit-Env oder `.env`).
- [ ] Alten 1to1-Service **disablen** (nicht löschen), neuen **enablen**:
      `systemctl disable --now <alt>` / `systemctl enable --now <neu>` (bzw. `.timer`).
- [ ] Timer-Kadenz übernehmen (gleicher Zeitplan wie bisher).
- [ ] Erst **manuell** testen (`rtb_pool_wrapper.sh --dry-run` bzw. ein Snapshot), dann Timer scharf.
- [ ] Logs/Monitoring prüfen (`/var/log/backup/pcloud_sync.log`, JSONL, ggf. MariaDB-Run-History).

## 6. Cutover alt → neu (separat)

- Bisheriger Prod-Pool lag unter `/Backup/rtb_1to1` (bzw. fälschlich Root-`/_pool`).
- Neuer sauberer Pfad: `/Backup/rtb_pool`.
- Optionaler Spar-Weg (kein 8h-Reupload) für vorhandene Daten: das **Root-`/_pool`**
  server-seitig nach `/Backup/rtb_pool/_pool` **moven** (renamefolder = O(1)); dann nur
  Stubs/Index neu. (Eigene Aktion, siehe frühere Diskussion — erst Restore-Pfad fileid-vs-path
  verifizieren.)

## 7. Reihenfolge der Umsetzung (Empfehlung)

1. Snapshot 1 fertig verifizieren (Pfade, `.upload_complete`, 1 Restore-Stichprobe).
2. `wrapper_pcloud_pool_sync_1to1.sh` finalisieren (§3) → Snapshot 2 als **Delta** testen
   (übt Scout/copyfolder/Diff/Bulk-Delete).
3. `rtb_pool_wrapper.sh` erstellen (§4) → Snapshot 3 testen.
4. Offene Punkte aus Rebuild-Doku angehen (Template-Save, Auto-Mode, GC-„drop empty refs").
5. Services umstellen + enablen (§5).
