# Backlog: Pool-Pfad-Helfer in pcloud_bin_lib (Prio 1)

> **Status:** vorbereitet, **nicht umgesetzt** (Juni 2026)  
> **Trigger:** Ruhephase nach stabilem Betrieb; nicht während laufendem Upload / direkt nach GC-Fixes.  
> **Risiko:** niedrig (reine Pfad-Strings, kein API-/Timing-Verhalten).

## Kontext

GC-Fixes (Juni 2026) haben gezeigt: pCloud-Logik in Einzelskripten dupliziert → mehrere Iterationen bis stabil.  
`pool_file_remote_path()` lebt bereits in `pcloud_bin_lib.py` (GC nutzt es).  
Drei weitere Skripte bauen denselben Pfad lokal nach.

## Ziel (ein PR, mechanischer Refactor)

### 1. Lib ergänzen (`pcloud_bin_lib.py`)

Neben bestehendem `pool_file_remote_path(pool_root, sha256)` (`pool_root` = `…/_pool`):

```python
def pool_object_path_from_dest(dest_root: str, sha256: str) -> str:
    """Vollpfad: dest_root/_pool/XX/sha (dest_root = z.B. /Backup/rtb_pool)."""

def pool_object_relpath(sha256: str) -> str:
    """Relativ zu dest_root: _pool/XX/sha (für Push)."""
```

Implementierung = bestehende f-Strings aus den Call-Sites 1:1 übernehmen (inkl. `.lower()` auf SHA).

### 2. Call-Sites umstellen (lokale Helfer löschen)

| Datei | Entfernen | Ersetzen durch |
|-------|-----------|----------------|
| `pcloud_quick_delta.py` | `_pool_obj_path()` | `pc.pool_file_remote_path(pool_root, sha)` |
| `scripts/utilities/pool_restore.py` | `_pool_obj_path()` | `pc.pool_object_path_from_dest(dest_root, sha)` |
| `pcloud_push_json_pool_manifest_to_pcloud.py` | `_get_pool_path()` | `pc.pool_object_relpath(sha256)` |

**Nicht** in diesem PR: Push-Monolith sonst anfassen, Scout, Validation, listfolder-BFS.

### 3. Optional (Prio 2, separates PR)

- `pcloud_push_json_manifest_to_pcloud.py`: `pc.deletefile(path=marker)` → `pc.delete_file(path=marker)`
- `enrich_listfolder_file_path()` wenn wieder `path`-leere Metadaten auftauchen

## Verifikation auf pi-nas (nach `git pull`, ~5 min)

```bash
cd /opt/apps/pcloud-tools/main
set -a; source .env; set +a

python3 pcloud_quick_delta.py --dest-root /Backup/rtb_pool --env-file .env
python3 pcloud_pool_gc.py --pool-root /Backup/rtb_pool --env-file .env --dry-run
python3 scripts/utilities/pool_restore.py \
  --env-file .env --pool-root /Backup/rtb_pool --list-snapshots
```

Erwartung: wie vor dem PR (tamper grün, GC `0 to delete`, Restore-Liste unverändert).

## Regel (dauerhaft)

Neue pCloud-Interaktion (Pfad, Delete, Timeout, `modified`, listfolder) → **zuerst** `pcloud_bin_lib.py`.  
Skript-Duplikat nur mit Kommentar, warum Lib nicht reicht.

Siehe auch `docs/DEVELOPER_GUIDE.md` § `pcloud_bin_lib — Pool-relevante Helfer`.
