# Pool-Integrität (Dashboard Spalten Post-Upload & Audit)

Stand: Juni 2026 · pi-nas Pool-Mode (`/Backup/rtb_pool`)

## Dashboard „Pool-Integrität“

| Spalte | DB-Quelle | Wann befüllt |
|--------|-----------|--------------|
| **Post-Upload** | `backup_runs.integrity_*` | Automatisch nach jedem **erfolgreichen** Upload (`check_type=post_upload`) |
| **Audit** | `snapshot_integrity_checks` (`monthly_audit`) | 3×/Tag **05:45, 13:45, 21:45** — je **10 Snapshots** (`INTEGRITY_AUDIT_MAX`) |
| **Frische** | berechnet aus `monthly_audit_at` | `OK` / `STALE` (>35 Tage) / `FAILED` / `UNKNOWN` |

View: `v_snapshot_integrity_status` → `generate_reports.sh` → `reports.json` → Dashboard.

---

## Spalte 1 — Post-Upload (nachträglich füllen)

Alte Snapshots vor Einführung des Checks haben `—` in Post-Upload.

**Einmal-Backfill** (schreibt `check_type=manual` auf `backup_runs`, gleiche Spalte im Dashboard):

```bash
cd /opt/apps/pcloud-tools/main
source /opt/apps/pcloud-tools/venv/bin/activate
set -a; source .env; set +a

# Planung (~2 min/Snapshot):
python scripts/integrity-backfill.py --env-file .env --dry-run

# Empfohlen: batches (Pi 8GB, nachts):
python scripts/integrity-backfill.py --env-file .env --post-upload --max 10

# Oder alle (~87 × 2 min ≈ 3h, nur bei wenig Last):
python scripts/integrity-backfill.py --env-file .env --post-upload --oldest-first

sudo scripts/generate_reports.sh
sudo systemctl restart monitoring-dashboard.service
```

Einzel-Snapshot:

```bash
python scripts/utilities/pool_integrity_run.py \
  --env-file .env --pool-root /Backup/rtb_pool \
  --snapshot 2026-06-14-120015 --check-type manual
```

---

## Spalte 2 — Monthly Audit (laufender Betrieb)

### Service & Timer

| Unit | Rolle |
|------|--------|
| `integrity-audit.service` | oneshot: **INTEGRITY_AUDIT_MAX** Snapshots/Lauf (default 10), Pool-Index-Cache |
| `integrity-audit.timer` | 3× täglich **05:45, 13:45, 21:45** (+5 min Random) — 105 min nach Backup |

```bash
sudo cp systemd/integrity-audit.{service,timer}.example /etc/systemd/system/
# Pruefen: drei OnCalendar-Zeilen (05:45, 13:45, 21:45) — nicht 05:15!
grep OnCalendar /etc/systemd/system/integrity-audit.timer
sudo systemctl daemon-reload
sudo systemctl enable --now integrity-audit.timer
systemctl list-timers integrity-audit.timer
```

### Auswahl-Logik (`integrity-audit-next.py`)

Priorität für den **nächsten** Snapshot:

1. Nie `monthly_audit` → zuerst
2. Letzter Audit `FAILED` → erneut
3. Letzter Audit **≥ 35 Tage** alt → STALE, bevorzugt
4. Sonst ältester Audit-Zeitstempel

**Häufigkeit:** 3×10 = **30 Audits/Tag** → ~87 Snapshots in **~3 Tage** rotiert.

**Performance (gefiltert):** ~8s Stub-API + ~0.2s Checks. Cache = einmaliges `_pool`-listfolder pro Batch; Hauptgewinn = `manifest_scoped` (Check B war ~22s).

> Dashboard-Hinweis sagt „STALE >35d“ (nicht 30). STALE-Alarm im Summary erst nach 35 Tagen ohne Re-Audit.

### Snapshot-Größen (ohne `du`)

`du` auf `/mnt/backup/rtb_nas` hängt Stunden (Hardlinks + mergerfs). Stattdessen:

```bash
python scripts/utilities/snapshot_sizing.py --env-file .env --last 15
```

```bash
python scripts/integrity-backfill.py --env-file .env --dry-run
python scripts/integrity-backfill.py --env-file .env --audit --max 5   # Batch
# oder alle:
python scripts/integrity-backfill.py --env-file .env --audit --oldest-first
```

**Warnung:** ~1–2 min/Snapshot, API + RAM — nicht parallel zum Backup.

### Manuell ein Audit (wie Timer)

```bash
sudo systemctl start integrity-audit.service
journalctl -u integrity-audit.service -n 30 --no-pager
```

---

## JSON-Reports

Vollständige Reports (nicht DB-Historie):

`/srv/pcloud-archive/integrity/<snapshot>_<check_type>_<timestamp>.json`

---

## SQL-Migrationen (Reihenfolge)

```bash
mysql -u pcloud_backup -p pcloud_backup < sql/migrate_integrity_checks.sql
mysql -u pcloud_backup -p pcloud_backup < sql/migrate_integrity_v2.sql
mysql -u pcloud_backup -p pcloud_backup < sql/migrate_integrity_v3_view.sql
```

Siehe `sql/README.md`.

---

## Siehe auch

- `scripts/utilities/pool_verify_backup.py` — eigentliche Prüflogik
- `scripts/utilities/pool_audit_status.py` — RTB/Man/Pcl/Cmp-Matrix
- `doku/Raspi/raspinas/ops/integrity-checks.md` — pi-nas Ops-Befehle
