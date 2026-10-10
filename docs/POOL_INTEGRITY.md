# Pool-Integrität (Dashboard Spalten Post-Upload & Audit)

Stand: August 2026 · pi-nas Pool-Mode (`/Backup/rtb_pool`)

## Upload-Pipeline vs. Audit

| Wann | Was | Wo |
|------|-----|-----|
| **Jeder Upload** | Hartes Integrity-Gate (Subprozess: subtree `listfolder`, Snap-Archiv-Index) | `pcloud_push_json_pool_manifest_to_pcloud.py` → `_run_listfolder_integrity_gate()` |
| **Jeder Upload** | `post_upload` in MariaDB | im Gate via `pool_integrity_run.py` (`check_type=post_upload`) |
| **3×/Tag Timer** | `monthly_audit` (3 Snapshots/Lauf, je Subprozess) | `integrity-audit.service` |
| **Wrapper (optional)** | Zweiter Lauf | nur bei `PCLOUD_POST_UPLOAD_INTEGRITY=1` (Default: `skip`) |

**Log-Marker:** `[integrity-gate] Post-Upload Integritaet (Subprozess, subtree listfolder)...`

**RAM/Zeit (Post-Upload):** Verify im **eigenen Prozess**. Stubs: `listfolder_safe` pro Top-Level-Ordner (Backup, Paperless, …) — vollständiger Abgleich, typisch **~1–3 Min**, kein BFS pro Ordner, kein 867 MB-Master-Index (stattdessen `_index/archive/<SNAP>_index.json` falls vorhanden).

**Periodischer Audit:** voller Master-`content_index.json` + gleicher Stub-Fetch (Subtree-Batches).

## Dashboard „Pool-Integrität“

| Spalte | DB-Quelle | Wann befüllt |
|--------|-----------|--------------|
| **Post-Upload** | `backup_runs.integrity_*` | Automatisch nach jedem **erfolgreichen** Upload (`check_type=post_upload`) |
| **Audit** | `snapshot_integrity_checks` (`monthly_audit`) | 3×/Tag **05:45, 13:45, 21:45** — je **3 Snapshots** (`INTEGRITY_AUDIT_MAX=3`, Subprozess je Snap; ~9/Tag) |
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

# Planung (~1.5 min/Snapshot, Subprozess je Snapshot):
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
| `integrity-audit.service` | oneshot: **INTEGRITY_AUDIT_MAX** Snapshots/Lauf (je `pool_integrity_run.py` Subprozess), `KillMode=control-group` |
| `integrity-audit.timer` | 3× täglich **05:45, 13:45, 21:45** (+5 min Random) — **nach** Backup-Fenster (04/12/20); nicht parallel zum Upload+Gate laufen lassen |

```bash
cd /opt/apps/pcloud-tools/main
# Wichtig: Ziel OHNE .example — sonst laedt systemd die Units nicht!
sudo cp systemd/integrity-audit.service.example /etc/systemd/system/integrity-audit.service
sudo cp systemd/integrity-audit.timer.example   /etc/systemd/system/integrity-audit.timer
# Alte Fehlkopien entfernen (falls vorhanden):
sudo rm -f /etc/systemd/system/integrity-audit.service.example \
           /etc/systemd/system/integrity-audit.timer.example
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

**Häufigkeit:** Default `INTEGRITY_AUDIT_MAX=3` → 3×3 = **~9 Audits/Tag** (reicht für ≤3 neue Backups/Tag + Rotation unter 35 Tage). Bei Backlog temporär erhöhen (z. B. 10). Subprozess je Snapshot — kein `PoolRemoteCache` im Parent.

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

`integrity-audit.service` ist **Type=oneshot** — `systemctl start` **ohne** `--no-block` wartet synchron, bis alle Snapshots des Laufs fertig sind (bei mehreren Snapshots pro Lauf kann das lange dauern; die Shell wirkt „hängend“, `activating` ist normal).

```bash
sudo systemctl start --no-block integrity-audit.service
journalctl -u integrity-audit.service -n 30 --no-pager
# Fortschritt live (Ctrl+C beendet nur journalctl, nicht den Audit):
journalctl -u integrity-audit.service -f
```

Synchron warten (bewusst, Terminal blockiert bis Ende): `sudo systemctl start integrity-audit.service`

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
