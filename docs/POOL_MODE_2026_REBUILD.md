# Pool-Mode 2026 — Architektur & Umbau (`pcloud_push_json_pool_manifest_to_pcloud.py`)

> Status: **DRAFT zur Review** (2026-05-30). Beschreibt den aktuellen Stand nach den
> Bugfixes/Umbauten der letzten Runde. Operatives (Services/Wrapper) siehe
> `POOL_PIPELINE_PLAN.md`.

## 1. Grundidee

Jeder Snapshot ist **eigenständig** und besteht nur aus **Stubs**, die in einen
**zentralen, deduplizierten Pool** zeigen. Es gibt **keine** Anchor-/Hardlink-Ketten
zwischen Snapshots (anders als im alten 1to1-Modus).

```
<dest_root>/
  _pool/<xx>/<sha256>                  ← echte Dateien, dedupliziert (1× pro SHA256)
  _snapshots/<snap>/<relpath>.meta.json← Stubs (lesbare Ordnerstruktur, zeigen auf Pool)
  _snapshots/<snap>/.upload_started    ← Marker: Upload läuft
  _snapshots/<snap>/.upload_complete   ← Marker: Upload fertig (Snapshot „gültig")
  _snapshots/_index/content_index.json ← zentraler Index (pool_refs)
  _snapshots/_index/archive/<snap>_index.json
  .gc_lock                             ← schützt vor GC während Upload
```

**Konsequenzen** (wichtig fürs Gesamtdesign):
- Snapshot löschen = nur `deletefolderrecursive(_snapshots/<snap>)` + Index-Refs pflegen.
- Kein „broken chain"-Risiko: ein fehlender Nachbar-Snapshot beschädigt andere nicht.
- → Die **Gap-Logik** und der **1to1-Retention-Sync** sind im Pool-Modell überflüssig.

## 2. Pfad-Modell (der zentrale Fix)

`_get_pool_path(sha)` liefert **relativ** (kein führender Slash):
```
_pool/<xx>/<sha>
```
**Alle** Stellen bauen den absoluten Pfad einheitlich:
```python
pool_path_abs = f"{dest_root.rstrip('/')}/{_get_pool_path(sha)}"   # = <dest_root>/_pool/<xx>/<sha>
```
Betroffen & jetzt konsistent: **Upload** (Full + Delta), **Stub-Schreiben**, **Reused-Fallback**,
**Preflight-Repair**, **Restore**.

> **Historischer Bug (behoben):** Früher lieferte `_get_pool_path` `/_pool/...` (absolut). Wo das
> direkt verwendet wurde, landeten die echten Files im **pCloud-Root** `/_pool/` statt unter
> `<dest_root>/_pool/`. Ein Zwischenfix mit `dest_root + pool_path_rel` (ohne Slash) erzeugte
> `…/rtb_pool_pool/…`. Beide Varianten sind beseitigt — Upload-Ziel == Stub-Pfad == Scan-Pfad == Restore.

## 3. Stub-Format (`<relpath>.meta.json`)

```json
{
  "format_version": 1,
  "kind": "stub",
  "type": "pool_stub",
  "holder_type": "pool",
  "sha256": "…",
  "pcloud_hash": "…",
  "size": 12345,
  "mtime": 1717000000.0,
  "relpath": "Gemeinsam/Rest/datei.pdf",
  "pool_path": "<dest_root>/_pool/<xx>/<sha>",   // absolut
  "pool_fileid": 87654321,                        // pfad-unabhängig → Restore primär hierüber
  "snapshot": "<snap>"
}
```
Restore sollte primär über `pool_fileid` auflösen (überlebt Moves); `pool_path` ist menschenlesbarer Zusatz.

## 4. Enriched Index — „Single Source of Truth"

`content_index.json`:
```json
{
  "version": 1,
  "items": {},
  "pool_refs": {
    "<sha256>": { "fileid": 123, "hash": "…", "size": 999, "snapshots": ["<snap1>", "<snap2>"] }
  }
}
```
- **Vorher:** `pool_refs[sha] = ["<snap>", …]` (nur Referenzliste, kein fileid) → Restore/Recovery
  zwang zum Lesen von 100k Einzel-Stubs.
- **Jetzt:** pro SHA `fileid/hash/size/snapshots` zentral → GC, Restore und Recovery brauchen nur
  diese eine Datei.
- **Auto-Migration:** alte Listen-Einträge werden beim ersten Zugriff zu Objekten konvertiert;
  fehlende `fileid` werden lazy per `stat_file` nachgezogen und persistiert.
- **Resume:** der Index wird periodisch lokal gecheckpointet (`PCLOUD_INDEX_SAVE_INTERVAL`,
  Default 100 Files / 300s) → Wiederanlauf überspringt bereits erledigte Files.

## 5. Ablauf eines Laufs (`push_pool_mode`)

1. **Source-Integrity-Check** (`os.path.exists` je File; Hash optional via `PCLOUD_SOURCE_DEEP_CHECK=1`).
2. **GC-Lock** setzen (`.gc_lock`).
3. **Scout (remote-getrieben):** listet die remote unter `_snapshots/` vorhandenen Snapshots,
   lädt nur für diese die lokalen Manifeste, vergleicht (Jaccard relpath+sha) → bester Basis.
   - Treffer ≥ Schwelle (`PCLOUD_SCOUT_THRESHOLD`, Default 0.70) → **Turbo-Delta-Mode**.
   - sonst / kein Remote-Basis / `use_scout=False` → **Full-Pool-Mode**.
4. **Full-Pool-Mode:** Preflight (Pool-Scan + Index) → Delta-SHAs → Pool-Struktur (00-FF) →
   Ordner anlegen → Upload (parallel) → Stubs → Index schreiben → Validierung → `.upload_complete`.
5. **Turbo-Delta-Mode** (`push_pool_delta_mode`): siehe §6.

## 6. Turbo-Delta-Mode

1. **Re-Run-Schutz:** existiert `_snapshots/<snap>` schon?
   - mit `.upload_complete` → fertig, nichts zu tun.
   - ohne → unvollständiger Altlauf → **verwerfen** (`deletefolderrecursive`) + lokalen
     Index-Checkpoint löschen → sauberer Neustart. (Reconcile ist bewusst NICHT implementiert:
     der Index wird erst am Ende geschrieben, und `listfolder` liefert keine SHA256.)
2. **Phase 1 — Klon:** `copyfolder(basis → <snap>)` server-seitig.
   - **Timeout 300s** (`PCLOUD_COPYFOLDER_TIMEOUT`), da copyfolder bei 100k Files Minuten dauert.
   - **Achtung Skalierung:** `copyfolder` ist bei sehr großen Snapshots NICHT „instant" — bei
     ~100k Stubs kann es das Timeout reißen → Fallback Full-Mode (`use_scout=False`). Offener Punkt.
   - Nach dem Klon: mitkopierte `.upload_started`/`.upload_complete` löschen, Started neu schreiben.
3. **Phase 2 — Diff** (lokales Basis- vs. aktuelles Manifest): added / changed (SHA≠) / deleted.
4. **Phase 3 — Bereinigung (Bulk-Delete):** veraltete Einträge entfernen.
   - Komplett tote Teilbäume → **ein** `deletefolderrecursive` (höchster toter Ancestor).
   - Stubs in noch lebenden Ordnern → einzeln `deletefile`. (pCloud hat keinen Multi-File-Delete.)
5. **Phase 4 — Upload neu/geändert** in den Pool (+ Stubs), Index pflegen.
6. **Validierung** (versteht Dict-Format) → `.upload_complete`.

## 7. Endlosschleife / Robustheit (behoben)

- `push_pool_mode(use_scout=…)`: Delta-Fallbacks rufen mit `use_scout=False` → **echter
  Full-Upload statt erneutem Scouting** (vorher wechselseitige Rekursion → Hänger,
  gc-lock-Spam, kaum abbrechbar).
- Scout wählt nur **remote vorhandene** Basen → kein „copyfolder API 2005"-Loop mehr.

## 8. GC-Beziehung (`pcloud_pool_gc.py`)

- GC nimmt `referenced = set(pool_refs.keys())` und löscht Pool-Objekte, die **nicht** referenziert sind.
- **Damit GC etwas freigeben kann**, muss beim Snapshot-Löschen der Index gepflegt werden:
  Snapshot aus `pool_refs[sha]["snapshots"]` entfernen und — wenn leer — den **sha-Key löschen**.
  Sonst gilt jedes Objekt ewig als referenziert (Pool wächst). → siehe offene Punkte.
- Statistik-Zähler für das Dict-Format korrigiert (Lösch-Logik war nie betroffen, da key-basiert).

## 9. Wichtige ENV-Variablen

| ENV | Default | Wirkung |
|---|---|---|
| `PCLOUD_SCOUT_ENABLED` | 1 | Scout/Delta an; `0` erzwingt Full-Mode |
| `PCLOUD_SCOUT_THRESHOLD` | 0.70 | Mindest-Ähnlichkeit für Turbo-Delta |
| `PCLOUD_COPYFOLDER_TIMEOUT` | 300 | Timeout der copyfolder-Meta-Op (s) |
| `PCLOUD_INDEX_SAVE_INTERVAL` | 100 | Checkpoint alle N Files (Resume) |
| `PCLOUD_INDEX_SAVE_INTERVAL_TIME` | 300 | Checkpoint alle N s |
| `PCLOUD_VALIDATE_UPLOAD` | 1 | Post-Upload-Validierung |
| `PCLOUD_SOURCE_DEEP_CHECK` | 0 | Hash-Verifikation der Quelle (langsam) |
| `PCLOUD_PARALLEL_UPLOAD_THREADS` | 4 | Upload-Parallelität |

## 10. Changelog (relevante Commits)

- `65a090c` — Mojibake in Log-/Doc-Strings repariert.
- `4befd8d` — remote-getriebener Scout, Endlosschleife behoben, Re-Run, Bulk-Delete, copyfolder-Timeout.
- `0aadd02` — Pfad konsistent dest-relativ, Enriched-Index, Validierung Dict-Format, GC-Statistik,
  toten Code entfernt (`_upload_to_pool`-Modul, `_write_pool_stub`, `_process_pool_item`).

## 11. Offene Punkte (noch NICHT umgesetzt)

1. **Folder-Template-Save (a3):** Im Pool-Mode wird nach dem Anlegen der Ordner **kein**
   `_folder_template` gespeichert (anders als Legacy) → jeder Voll-Lauf legt alle Ordner
   einzeln neu an (bei 11.611 Ordnern ~73 min; bei 1.101 ~5 min). Fix = Template nach
   Ordneranlage speichern + bei hoher Ähnlichkeit automatisch nutzen.
2. **Auto-Mode-Detect / `--dest-root`-Default:** `--snapshot-mode` Default ist `objects`;
   ohne `--snapshot-mode pool` läuft der falsche Modus (nur Warnung). Vorschlag: bei
   Pool-Ziel/`schema>=4` automatisch Pool erzwingen; optional Default-`--dest-root`.
3. **GC „drop empty refs":** Snapshot-Löschen muss den sha-Key entfernen, wenn `snapshots`
   leer wird (sonst räumt GC nie auf). Wird mit GC zusammen umgesetzt.
4. **copyfolder-Skalierung:** bei sehr großen Snapshots ist der Voll-Klon teuer/riskant —
   ggf. Strategie überdenken (Diff-only ohne Voll-Klon).

## 12. Erkenntnisse / Lessons Learned

Hart erarbeitete Einsichten aus dem Debugging (damit sie nicht wieder passieren):

- **Pool-Pfad strikt dest-relativ, an EINER Stelle bauen.** Ein führender `/` in
  `_get_pool_path` macht aus `_pool/...` einen **Account-Root-Pfad** → Daten landen in
  `/_pool/` statt `<dest>/_pool/`. Ein Zwischenfix mit String-`+` (ohne Slash) erzeugte
  `<dest>_pool/`. Lehre: relativer Fragment-Pfad + überall identisch `f"{dest}/{rel}"`.
- **Scout muss REMOTE-getrieben sein.** Basis-Auswahl aus lokalen Manifesten wählte
  Snapshots, die remote gar nicht existierten → `copyfolder API 2005` → Fallback-Schleife.
  Lokale Manifeste ≠ Remote-Realität.
- **Fallback nie zurück in dieselbe Entscheidung.** Delta-Fehler → `push_pool_mode` ohne
  Scout (`use_scout=False`), sonst wechselseitige Rekursion (Hänger, gc-lock-Spam, kaum
  abbrechbar).
- **Index = Single Source of Truth.** Vorher lagen fileids nur in 100k Einzel-Stubs → jede
  Recovery/Restore war teuer. Jetzt `pool_refs[sha] = {fileid,hash,size,snapshots}`.
- **Validierung muss das Datenformat kennen.** Nach Umstellung Liste→Dict prüfte die
  Validierung weiter `snapshot in <dict>` (= Keys) → falsch-negativ → kein
  `.upload_complete` trotz Erfolg. Format-Checks beim Umstieg immer mitziehen.
- **`copyfolder` ist NICHT O(1) bei 100k.** Bei großen Snapshots Minuten → Timeout 300s,
  und als Strategie-Grenze im Hinterkopf behalten.
- **Manifest ≠ Index.** Manifest = Quell-Scan (Input, **kein** fileid, vor Upload erzeugt).
  `content_index.json` = angereicherter Output (fileid/hash/size). Nicht verwechseln.
  `"hash"` im Index ist der **pCloud-Hash** (int, für `checksumfile`), NICHT die sha256
  (die ist der Key).
- **Pool ist zweistufig & fix.** `_pool/<XX>/<sha>`, 256 Ordner, keine Unterordner, keine
  neuen → per-File `ensure_path` auf den Pool-Parent ist nach Phase 0 immer redundant.
- **Mojibake-Ursache war die Quelldatei** (doppelt-encodiert gespeichert), nicht das
  Terminal. Reparatur per selektivem cp1252→UTF-8 Round-Trip.
- **Gap-/Chain-Logik ist im Pool obsolet.** Snapshots sind eigenständig (Stubs→Pool);
  ein fehlender Nachbar beschädigt keine anderen. Catch-up = „lade was remote fehlt".

## 13. Verifikations-Skripte

Read-only Checks (keine Änderungen) unter `scripts/utilities/`:

- `pool_check_remote.py --env-file .env --dest-root <root> [--snapshot <name>]`
  Index lädt+enriched, `_pool`-Objektzahl, **jede Index-SHA physisch im Pool?**,
  `.upload_complete` + Stub-Anzahl.
- `pool_check_local.py --manifest <pfad> [--master-index <pfad>]`
  Manifest (Quell-Scan, fileid by design NICHT erwartet) + Master-Index enriched?
