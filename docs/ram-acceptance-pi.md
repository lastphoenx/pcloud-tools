# RAM-Abnahme (pi-nas)

Ziel: automatische Jobs (Backup Delta, Integrity-Audit, Pool-GC, delete-snapshots/Retention) ohne OOM.

Messung: `/usr/bin/time -v` → **Maximum resident set size** + Exit-Code.

| Job | Befehl (Kurz) | Max RSS (KiB) | Exit | Datum |
|-----|----------------|---------------|------|-------|
| GC dry-run | `pcloud_pool_gc.py --dry-run` | 327040 | 0 | 2026-10-10 |
| Backup Delta | (Pipeline ein Snapshot) | | | |
| Integrity 1 Snap + Archiv | `pool_verify_backup --snapshots …` | | | |
| Integrity 1 Snap ohne Archiv | wie Audit / fehlendes Archiv | | | |
| delete-snapshots dry | | | | |
| delete-snapshots scharf (Test-Snap) | | | | |
| Ops-DB Re-Import erzwungen | Ops-DB sichern, löschen, GC/import | | | |

RSS-Limit: *(vom Betreiber festgelegt)*

## Bewusst nicht in dieser Runde

- `--audit-mode` (gesperrt)
- `get_textfile(maxbytes=None)` Voll-Loads
- Manifest-Loads Push/Restore
- v1-Utilities (`pcloud_integrity_check`, …)

## Commit-Regel

Vor Commit auf dem Pi: relevante Zeilen der Tabelle aktualisieren und in die Commit-Beschreibung kopieren.
