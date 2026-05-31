# Handoff: pCloud Pool-Delta — offener Bug + Leitprinzip

## Leitprinzip (NICHT verletzen)
Der **Legacy-Code funktioniert zu 100%**. Die Pool-Methode ist der Legacy-Code mit
**marginalen** Anpassungen. NICHT neu erfinden — Legacy-Funktionen 1:1 spiegeln und nur
da abweichen, wo Pool es zwingend verlangt (Pool = `_pool/<XX>/<sha256>` dedupliziert,
`_snapshots/<snap>/<relpath>.meta.json` Stubs, `_snapshots/_index/content_index.json`).
Bei Unsicherheit: zuerst die Legacy-Entsprechung lesen, dann spiegeln.

Legacy-Referenzen (im Repo, NICHT loeschen bis Pool stabil):
- `pcloud-tools/pcloud_push_json_manifest_to_pcloud.py`  (1to1 Push-Tool, Klon-Muster bei 2906-2925, OHNE toname)
- `pcloud-tools/wrapper_pcloud_sync_1to1.sh`              (1to1 Wrapper)
- `rtb/rtb_wrapper.sh`                                    (Orchestrator, Snapshot-nur-bei-Aenderung-Gate 85-129)

## Setup
- Pi "pi-nas", pCloud Backup, jetzt POOL-ONLY. Lokale Snapshots: `/mnt/backup/rtb_nas/<YYYY-MM-DD-HHMMSS>/`.
- Remote: `/Backup/rtb_pool/{_pool,_snapshots,_snapshots/_index}`.
- Aktiver Pool-Wrapper: `pcloud-tools/wrapper_pcloud_pool_sync_1to1.sh` (Aufruf: `./wrapper_pcloud_pool_sync_1to1.sh <snapshot>`).
- Pool-Push: `pcloud-tools/pcloud_push_json_pool_manifest_to_pcloud.py`.
- Pool-Manifest-Generator: `pcloud-tools/pcloud_json_pool_manifest.py`.
- Repos getrennt: pcloud-tools (remote lastphoenx/pcloud-tools), rtb (lastphoenx/rtb).
- backup-pipeline.timer + .service sind GESTOPPT/disabled (bis Cutover).
- Kein lokales Python auf Windows -> `py_compile` macht der USER auf dem Pi.

## Gerade gefixt (committed)
- pcloud-tools de08474: Delta-Klon landete im `_snapshots/`-ROOT statt in `_snapshots/<snap>/`.
  Ursache: `copyfolder(to_folderid=snapshots_fid, toname=..., copycontentonly=True)` —
  copycontentonly kopiert KINDER nach to_folderid; to_folderid war der Parent. Fix:
  `ensure_path(dest_snapshot_dir)` -> dest-fid -> `copyfolder(to_folderid=dest_fid, copycontentonly=True)`
  (= Legacy-Muster, kein toname). Stelle: push_pool_delta_mode ~Zeile 1593-1602.
- pcloud-tools 3290d38: UnboundLocalError `index` im Delta-0-Changes-Pfad + alle (auch
  geklonten) SHAs in pool_refs des neuen Snapshots registrieren.

## OFFENER BUG (Hauptaufgabe naechster Chat): Delta meldet faelschlich "+0 -0 Δ0"
Beweis dass es ein Bug ist: rtb erstellt einen Snapshot NUR bei changes_detected
(rtb_wrapper.sh:85-129). Also hat ein neuer Snapshot IMMER echte Aenderungen. Der
Pool-Delta meldete trotzdem 0. -> echter Bug.

ZUERST Grundwahrheit messen (Inode-Vergleich gegen FS, schnell):
```bash
RTB=/mnt/backup/rtb_nas
A="$RTB/2026-04-27-173201"; B="$RTB/2026-05-01-103649"
echo "A=$(sudo find "$A" -type f|wc -l) B=$(sudo find "$B" -type f|wc -l)"
comm -23 <(sudo find "$B" -type f -printf '%i\n'|sort -u) <(sudo find "$A" -type f -printf '%i\n'|sort -u) | wc -l
# Ergebnis = Anzahl neu/geaenderter Dateien in B. MUSS >0 sein wenn rtb B erzeugt hat.
```

Prime-Suspects (in dieser Reihenfolge pruefen, jeweils gegen Legacy spiegeln):
1. Smart-Manifest-Reuse maskiert Aenderungen: `pcloud_json_pool_manifest.py` ReferenceCache.lookup
   (Z.112-137) gibt den ALTEN sha256 zurueck, wenn relpath+mtime+size matchen ODER Inode matcht.
   Pruefen: erzeugt der Wrapper-Aufruf (`--ref-manifest <letztes manifest>`, Wrapper Z.412-427)
   ein Manifest, das faelschlich = Basis ist? Vergleich: `jq -S '.items[]|{relpath,sha256}'`
   von erzeugtem Manifest vs basis-Manifest. Falls Smart-Mode schuld -> ggf. ref-manifest nur
   fuer Hardlink-Inode-Reuse nutzen, NICHT fuer mtime+size (oder Legacy-Verhalten spiegeln).
2. Falsche/stale Basis: Scout (remote-driven, Jaccard) waehlt Basis; delta laedt
   `{PCLOUD_ARCHIVE_DIR=/srv/pcloud-archive}/manifests/<basis>.json`. Pruefen ob die geladene
   Basis wirklich der unmittelbare Vorgaenger ist und das Manifest aktuell.
3. Wie macht LEGACY den Delta/Diff? `pcloud_push_json_manifest_to_pcloud.py` delta-copy
   (ab ~2796). Spiegeln statt neu erfinden.

## Weitere offene Punkte
- `_snapshots/`-Root-Muell auf pCloud aufraeumen (Folge des Klon-Bugs, vor Re-Run): alles auf
  Root-Ebene ausser Datums-Ordnern, `_index`, `.recycle` ist Dump und kann weg (Web-UI).
- tamper-detect `pcloud_quick_delta.py` ist pool-BLIND (liest `items`/`anchor_path` = 1to1,
  in Pool leer -> vacuous "OK"). Auf pool_refs umstellen (fileid/hash/size der _pool-Objekte).
- Legacy-Dateien loeschen NUR auf explizites "Legacy-Dateien loeschen": die 3 Referenzen oben
  + scripts/test_chunked_rtb_snapshot.sh, scripts/utilities/smart_strategy_decision_simulator.py,
  prepare_fresh_test.sh.
- DB-Cleanup backup_runs (Legacy-Eintraege) per `sudo mysql`, OHNE Passwoerter.
- Service-Cutover: backup-pipeline.service ExecStart -> rtb_pool_wrapper.sh, dann Timer wieder an.
- Test-Sequenz: dry-run -> Produktionslauf 2026-05-01-103649 -> Checks
  (scripts/utilities/pool_check_remote.py --snapshot ..., DB-SELECT, pool_check_local.py)
  -> catch-up all (`./wrapper_pcloud_pool_sync_1to1.sh` bare) -> Check-Loop.

## Arbeitsweise (User-Vorgaben, hart)
- Immer Deutsch. Kein Behaupten — CLI-Abfragen liefern, die den Zustand BEWEISEN.
- Legacy spiegeln, NICHT erfinden. Keine Familiennamen in Code/Kommentaren (generisch).
- Keine Passwoerter in DB-Befehlen (`sudo mysql`). Skripte ausfuehrbar pushen (chmod +x in git).
- Commit-Messages clean, so lang wie noetig, nicht laenger. Co-Author-Trailer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
