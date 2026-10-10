# Ops-Index-Lücke (live Snapshots ∉ `pool_index_gc.sqlite3`)

## Sofort (vor scharfem GC)

1. **Cron prüfen** — scharfer `pcloud_pool_gc.py` ohne `--dry-run` und Retention mit `--run-gc` deaktivieren, bis Konsistenz wieder **level 0** ist.
2. **Konsistenz** (nach `git pull`):

   ```bash
   cd /opt/apps/pcloud-tools/main
   /opt/apps/pcloud-tools/venv/bin/python scripts/pool_consistency_check.py --env-file .env
   ```

   Erwartung bei bekannter Lücke: **Exit 2**, `live_missing_ops_db` listet die betroffenen Namen.

3. **Pool-Gaps im Detail** (Index-SHA ohne Pool-Listing, mit Snapshot-Zuordnung):

   ```bash
   cd /opt/apps/pcloud-tools/main
   set -a && . ./.env && set +a
   /opt/apps/pcloud-tools/venv/bin/python pool_gap_report.py --csv /tmp/pool_gaps.csv
   # optional: echte pCloud-Präsenz pro SHA (159× API)
   /opt/apps/pcloud-tools/venv/bin/python pool_gap_report.py --verify-stat --csv /tmp/pool_gaps_verify.csv
   ```

4. **Schaden prüfen** (optional, pro Snapshot):

   ```bash
   python scripts/utilities/pool_integrity_run.py --env-file .env \
     --pool-root "$PCLOUD_DEST" --snapshot <NAME> --check-type manual --no-db
   ```

## Reparatur (Quelle, nicht nur Ops-DB)

- **Nicht** manuell in `pool_index_gc.sqlite3` „raten“.
- Archiv-Indizes auf pCloud (`_snapshots/_index/archive/<snap>_index.json`) sind für die drei Lücken-Snapshots **present** — Referenzen von dort bzw. über Backup-Pipeline / `finalize-only` in **Backup-DB** und Remote-Master zurückspielen (Entwickler-Pfad, zuerst auf **Kopie** der Backup-DB testen).
- Nach Master-Sync: Ops-DB folgt per `checksumfile`/Import automatisch.

## GC wieder freigeben

1. `pool_consistency_check.py` → Exit **0**
2. `pcloud_pool_gc.py --dry-run` (RAM laut `docs/ram-acceptance-pi.md`)
3. Scharfer GC erst mit aktivem **Manifest-Guard** (`PCLOUD_GC_MANIFEST_GUARD=1`, Default) — schützt Pool-SHAs aus lokalen Manifesten, wenn der Index Snapshots vergisst.
4. Cron schrittweise reaktivieren.

## Beobachtung

- JSONL: `PCLOUD_POOL_CONSISTENCY_JSONL` (Default `/var/log/backup/pool_consistency.jsonl`)
- Sinnvolle Zeiten: nach Backup-Timer, **vor** GC/Retention
