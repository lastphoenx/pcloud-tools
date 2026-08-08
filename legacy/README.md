# Legacy 1to1-Modus (eingestellt)

Produktion auf pi-nas läuft über **Pool-Modus**:

- Wrapper: `wrapper_pcloud_pool_sync_1to1.sh` (Repo-Root)
- RTB: `rtb_pool_wrapper.sh`

Dieser Ordner enthält den alten **1to1-/Anchor-Modus** (`/Backup/rtb_1to1`) zur Referenz und für seltene Recovery-Tests.

## Dateien

| Datei | Funktion |
|-------|----------|
| `wrapper_pcloud_sync_1to1.sh` | Legacy-Orchestrator |
| `pcloud_json_manifest.py` | Manifest Schema v3 |
| `pcloud_push_json_manifest_to_pcloud.py` | SAFE/TURBO Upload, Retention-Sync |
| `pcloud_quick_delta.py` | tamper-detect (1to1 und manuell auch für Pool nutzbar) |
| `pcloud_manifest_diff.py` | Manifest-Diff für Delta-Copy |
| `create_folder_template.py` | Einmal-Setup `_folder_template` auf pCloud (1to1 SAFE-Mode Beschleunigung) |

## Aufruf

```bash
export MAIN_DIR=/opt/apps/pcloud-tools/main
# Direkt:
/opt/apps/pcloud-tools/main/legacy/wrapper_pcloud_sync_1to1.sh --dry-run
# Oder Root-Stub (Kompatibilität):
/opt/apps/pcloud-tools/main/wrapper_pcloud_sync_1to1.sh --dry-run
```

`PYTHONPATH` setzt der Legacy-Wrapper automatisch (`legacy` + Repo-Root für `pcloud_bin_lib.py`).
