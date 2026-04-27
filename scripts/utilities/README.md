# pCloud-Tools Utilities

Developer- und Wartungs-Tools für pCloud-Backups.

## 📚 Übersicht

Alle Utilities haben eine dedizierte Markdown-Dokumentation im gleichen Verzeichnis.

| Tool | Zweck | Dokumentation |
|------|-------|---------------|
| `cleanup_aborted_upload.sh` | Cleanup abgebrochener Uploads | [→ Docs](cleanup_aborted_upload.md) |
| `prepare_fresh_test.sh` | Clean-State für Workflow-Tests | [→ Docs](prepare_fresh_test.md) |
| `pcloud_restore.py` | Snapshot-Wiederherstellung von pCloud | [→ Docs](pcloud_restore.md) |
| `fix_stubs_missing_fileid.py` | FileID-Reparatur (Stubs + Index) | [→ Docs](fix_stubs_missing_fileid.md) |
| `pcloud_integrity_check.py` | 7-Ebenen Integritäts-Check | [→ Docs](pcloud_integrity_check.md) |
| `pcloud_repair_index.py` | Index-Reparatur (Phantom-Einträge) | [→ Docs](pcloud_repair_index.md) |
| `pcloud_verify_index_vs_manifests.py` | Index ↔ Manifest-Validierung | [→ Docs](pcloud_verify_index_vs_manifests.md) |
| `rewrite_stubs_from_index.py` | Stub-Regenerierung aus Index | [→ Docs](rewrite_stubs_from_index.md) |
| `analyze_manifest_duplicates.py` | Duplikat-Analyse (Excel-Report) | [→ Docs](analyze_manifest_duplicates.md) |
| `setup_venv.sh` / `.ps1` | Virtual Environment Setup | [→ Docs](setup_venv.md) |

## 🔧 Verwendung

### Voraussetzungen

```bash
# PYTHONPATH setzen (wenn außerhalb von wrapper_pcloud_sync_1to1.sh)
export PYTHONPATH="/opt/apps/pcloud-tools/main:$PYTHONPATH"

# pCloud Credentials (.env oder ENV)
export PCLOUD_USERNAME="user@example.com"
export PCLOUD_PASSWORD="..."
```

### Beispiel: Integrity-Check

```bash
python scripts/utilities/pcloud_integrity_check.py \
  --dest-root /Backup/rtb_1to1 \
  --checks anchors,checksums \
  --sample-size 100
```

Details siehe jeweilige `.md`-Dokumentation.

## 📖 Kategorien

**Testing/Maintenance:**
- prepare_fresh_test.sh → Clean-State für Tests (nach Refactoring)

**Recovery-Tools:**
- cleanup_aborted_upload.sh → Upload-Cleanup & Restart
- pcloud_restore.py → Notfall-Wiederherstellung
- pcloud_repair_index.py → Index-Reparatur
- fix_stubs_missing_fileid.py → FileID-Recovery

**Validierung:**
- pcloud_integrity_check.py → Umfassender Check
- pcloud_verify_index_vs_manifests.py → Index-Konsistenz

**Maintenance:**
- rewrite_stubs_from_index.py → Stub-Regenerierung
- analyze_manifest_duplicates.py → Duplikat-Analyse

**Development:**
- setup_venv.sh / .ps1 → Entwicklungsumgebung

## ⚠️ Hinweise

- Alle Python-Scripts benötigen `pcloud_bin_lib.py` im PYTHONPATH
- Dry-run Modus (`--dry-run`) für Testing empfohlen
- Für Produktions-Einsatz: Verbose-Modus (`--verbose`) aktivieren
- Bei großen Backups: JSON-Reports (`--out-json`) für Monitoring nutzen
