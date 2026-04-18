# pCloud-Tools

Automatisierte, deduplizierte Cloud-Backups für die pCloud-API. Lokale Rsync-Snapshots werden effizient in die Cloud synchronisiert: jede Datei wird genau einmal physisch gespeichert, alle weiteren Snapshots referenzieren sie als Metadaten-Stubs. Ein typischer Nacht-Lauf mit 50 MB Änderungen an einem 90-GB-Bestand dauert wenige Minuten.

Läuft vollautomatisch als systemd-Timer auf Linux/Debian (Raspberry Pi). Nach dem initialen Setup ist kein manueller Eingriff nötig.

→ **Architektur & Ablaufkette:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)  
→ **Setup-Anleitung:** [docs/SETUP.md](docs/SETUP.md)

---

## 📚 Table of Contents

- [Repositories & Zusammenspiel](#repositories--zusammenspiel)
- [Installation](#installation)
- [Kern-Tools](#kern-tools)
- [Wartung & Diagnose](#wartung--diagnose-manuell)
- [Monitoring & Alerting](#monitoring--alerting)
- [Dokumentation](#dokumentation)
- [Technologie-Stack](#technologie-stack)

---

## Repositories & Zusammenspiel

pCloud-Tools ist Teil einer mehrstufigen Backup-Pipeline. Jedes Repo steht für sich, sie können aber kombiniert werden:

| Repo | Funktion |
|---|---|
| **[entropy-watcher-und-clamav-scanner](https://github.com/lastphoenx/entropy-watcher-und-clamav-scanner)** | Security Gate vor dem Backup (Entropy + ClamAV) |
| **[rtb](https://github.com/lastphoenx/rtb)** | Wrapper für Rsync Time Backup: startet Backup nur bei erkannten Änderungen |
| **[rsync-time-backup](https://github.com/laurent22/rsync-time-backup)** (extern) | Hardlink-basierte lokale Snapshots |
| **pCloud-Tools** (dieser Repo) | Deduplizierter Upload lokaler Snapshots in die pCloud |

**Ablaufkette (vollständige Pipeline):**
```
EntropyWatcher + ClamAV  →  RTB Wrapper  →  rsync-time-backup  →  pCloud-Tools
      (Safety Gate)          (Dry-Run)        (Hardlink-Snap)      (Cloud-Sync)
```

Der Einstiegspunkt ist `rtb_wrapper.sh` (rtb-Repo), der nach erfolgreichem lokalem Backup automatisch `wrapper_pcloud_sync_1to1.sh` aufruft.  
→ Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

---

## Installation

```bash
git clone https://github.com/lastphoenx/pcloud-tools
cd pcloud-tools

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# .env mit pCloud-Credentials befüllen
```

→ Vollständige Setup-Anleitung inkl. MariaDB, systemd-Timer und erstem Backup-Run: [docs/SETUP.md](docs/SETUP.md)

---

## Kern-Tools

Diese Dateien bilden den produktiven Kern — sie werden automatisch vom Wrapper angestossen und sollten **nicht verschoben** werden:

| Datei | Funktion |
|---|---|
| `wrapper_pcloud_sync_1to1.sh` | Orchestrator: ruft alle Phasen in Reihenfolge auf |
| `pcloud_json_manifest.py` | Manifest-Erstellung (Smart-Hashing via inode/mtime) |
| `pcloud_push_json_manifest_to_pcloud.py` | Upload-Engine: SAFE-MODE / TURBO-MODE, Deduplication |
| `pcloud_quick_delta.py` | Post-Upload-Verifikation (Delta-Check) |
| `pcloud_bin_lib.py` | pCloud Binary-API-Bibliothek (Connection, Retry, Chunked Upload) |
| `create_folder_template.py` | Einmaliges Setup des `_folder_template`-Cache (SAFE-MODE Beschleunigung) |
| `pcloud_health_check.sh` | Backup-Status, Quota, Alter — Nagios/Zabbix-kompatibel |
| `pcloud_status.sh` | Interaktives Status-Dashboard aus MariaDB |

---

## Wartung & Diagnose (manuell)

Diese Tools liegen unter `scripts/` und werden **nicht automatisch** angestossen:

| Datei | Funktion |
|---|---|
| `scripts/cleanup_aborted_upload.sh` | Bereinigt abgebrochene Uploads (lokal + remote) |
| `scripts/cleanup_orphaned_manifests.sh` | Entfernt Manifeste ohne zugehörigen Snapshot |
| `scripts/fix_stubs_missing_fileid.py` | Repariert Stubs ohne FileID (nach API-Fehlern) |
| `scripts/rewrite_stubs_from_index.py` | Regeneriert alle Stubs eines Snapshots aus dem Index |
| `scripts/pcloud_manifest_diff.py` | Vergleicht zwei Manifeste (Diff-Ansicht) |
| `scripts/pcloud_integrity_check.py` | Tiefenprüfung: Hashes, FileIDs, Holder-Konsistenz |
| `scripts/pcloud_repair_index.py` | Repariert den Remote-Index (Phantom-Anchors etc.) |
| `scripts/pcloud_restore.py` | ⚠️ Stellt Snapshots von pCloud wieder her (Notfall-Tool) |
| `scripts/pcloud_verify_index_vs_manifests.py` | Gleicht Remote-Index gegen lokale Manifeste ab |

---

## Monitoring & Alerting

Dashboard und Alerting sind implementiert; die systemd/nginx-Integration auf dem Pi ist noch einzurichten.

**Vorhandene Komponenten:**

| Datei | Funktion |
|---|---|
| `dashboard/index.html` | Web-Dashboard (Vanilla HTML/JS, kein Framework) — zeigt Status aller Services |
| `scripts/aggregate_status.sh` | Sammelt Status aller Services als `/opt/apps/monitoring/status.json` |
| `scripts/send_alert.sh` | pCloud-spezifische Alerts via Apprise |
| `scripts/send_aggregated_alert.sh` | Multi-Service-Alerts, erkennt Statuswechsel (kein Spam) |

**Quick Setup:**
```bash
# Dashboard deployen
sudo mkdir -p /var/www/monitoring
sudo cp dashboard/index.html /var/www/monitoring/

# Status-Aggregator (cron, alle 5 min)
*/5 * * * * /opt/apps/pcloud-tools/main/scripts/aggregate_status.sh

# Alerts aktivieren
sudo cp apprise.yml.example /opt/apps/apprise.yml
# Telegram/Discord/ntfy-Credentials eintragen
```

→ Dashboard-Setup (nginx): [dashboard/README.md](dashboard/README.md)  
→ Apprise-Konfiguration: [docs/APPRISE_SETUP.md](docs/APPRISE_SETUP.md)

---

## Dokumentation

| Dokument | Inhalt |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Architektur, Ablaufkette, SAFE/TURBO-Logik, Tool-Inventar |
| [docs/SETUP.md](docs/SETUP.md) | Vollständige Installations-Anleitung |
| [docs/APPRISE_SETUP.md](docs/APPRISE_SETUP.md) | Alerting-Konfiguration (Telegram, Discord, ntfy…) |
| [docs/RCLONE_TOKEN_REFRESH.md](docs/RCLONE_TOKEN_REFRESH.md) | pCloud OAuth-Token erneuern (headless/SSH) |
| [docs/GAP_HANDLING.md](docs/GAP_HANDLING.md) | Gap-Handling: Szenarien, Strategien, Troubleshooting + Quick Start |
| [docs/GAP_HANDLING_FAQ.md](docs/GAP_HANDLING_FAQ.md) | Gap-Handling: FAQs |
| [docs/GAP_HANDLING_WORKFLOWS.md](docs/GAP_HANDLING_WORKFLOWS.md) | Gap-Handling: Visuelle Workflow-Diagramme (Mermaid) |
| [docs/DELTA_COPY_ANALYSIS.md](docs/DELTA_COPY_ANALYSIS.md) | Technische Analyse der Delta-Copy-Implementierung |

---

## Technologie-Stack

- **OS:** Debian Bookworm (Raspberry Pi 5)
- **Storage:** 5x 2.5" SATA SSD (Radxa Penta SATA HAT)
- **Backup:** rsync, JSON-Manifests, pCloud Binary API
- **Automation:** Bash, systemd-Timer
- **Monitoring:** Vanilla HTML/JS Dashboard, Apprise (Alerts)
- **DB:** MariaDB (Backup-Historie, Status-Tracking)

**Warum Raspberry Pi statt NAS-Appliance?** Der Wechsel von QNAP (TS-453 Pro, TS-473A, TS-251+) und LaCie 5big NAS Pro auf einen Raspberry Pi 5 war nicht nur eine Entscheidung für mehr Kontrolle, sondern auch für deutlich weniger Stromverbrauch. Ein QNAP mit 4 Festplatten zieht im Dauerbetrieb ~30–50 W; das LaCie läuft ähnlich. Der Raspberry Pi 5 mit 5x 2.5" SSDs kommt auf ca. 10–15 W — bei identischer Funktionalität. Bei Dauerbetrieb sind das je nach Setup **50–80% weniger Stromkosten**, ohne Abstriche bei Verfügbarkeit oder Leistung.
