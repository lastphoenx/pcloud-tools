# Production-Grade Test: Chunked Upload Integration

## Übersicht
Vollständiger End-to-End-Test für Chunked Upload Integration mit echtem RTB-Snapshot.

## 🛡️ Sicherheit & Isolation

**KRITISCH:** Dieser Test ist vollständig von der Produktion isoliert!

| Komponente | Produktions-Pfad | Test-Pfad | Isoliert? |
|------------|------------------|-----------|-----------|
| **pCloud Daten** | `/My Cloud/Backup/rtb_1to1/` | `/My Cloud/TEST_CHUNKED_RTB/` | ✅ |
| **Lokale Daten** | `/mnt/rtb/snapshots/` | `/tmp/test_chunked_rtb_*/` | ✅ |
| **Index-DB** | `content_index.json` (prod) | `$TEST_ROOT/index_dir/test_index.json` | ✅ |
| **Manifeste** | `/srv/manifests/` | `/tmp/test_chunked_rtb_*/manifests/` | ✅ |
| **State-Files** | `/srv/pcloud-archive/resume/` | `/srv/pcloud-archive/resume/` | ⚠️ Shared* |

*State-Files werden durch SHA256-basierte Unique Keys isoliert (keine Kollision möglich).

**➡️ Produktions-Datenbank wird NICHT verändert!**  
Der Test nutzt `--index-path "$TEST_ROOT/index_dir/test_index.json"` um die zentrale Index-DB zu schützen.

## Was wird getestet?

### ✅ Realistische Szenarien:
1. **Ordnerstruktur** (documents/, media/, system/)
2. **Gemischte Dateigrößen**:
   - Kleine Dateien (< 1 MB) → Standard-Upload
   - Mittlere Dateien (2-500 MB) → Standard-Upload  
   - Große Datei (6 GB) → Chunked-Upload
3. **RTB-Snapshot-Integration** (mit rsync_tmbackup.sh)
4. **JSON-Manifest-Generierung**
5. **Mixed-Mode-Upload** (Standard + Chunked im selben Job)
6. **Resume-Funktionalität** (Interrupt + Restart)
7. **SHA256-Verifikation** (Server vs. Lokal)

## Ausführung

```bash
cd /opt/apps/pcloud-tools/main
git pull

# Test ausführen (dauert ~5 Min)
bash scripts/test_chunked_rtb_snapshot.sh
```

## Test-Ablauf

### Schritt 1: Test-Datenstruktur
```
/tmp/test_chunked_rtb_XXXXX/
├── source/
│   ├── documents/
│   │   ├── doc1.txt (< 1 KB)
│   │   └── doc2.txt (< 1 KB)
│   ├── media/
│   │   ├── photo.jpg (2 MB)
│   │   └── videos/
│   │       └── large_video.mkv (6 GB) ← Chunked!
│   └── system/
│       └── config.db (500 KB)
├── snapshots/
│   └── test_snapshot_2026-04-26-HHMMSS/
├── manifests/
│   └── test_snapshot_2026-04-26-HHMMSS.json
└── index_dir/                          ← Isolierte Test-Index-DB!
    └── test_index.json
```

### Schritt 2: RTB-Snapshot
Nutzt `rsync_tmbackup.sh` aus dem rtb-Projekt.

### Schritt 3: Manifest-Generierung
Berechnet SHA256 für alle Dateien (wie in Produktion).

### Schritt 4: Upload zu pCloud
```
Erwartete Logs:
[upload] Starte Upload: doc1.txt (0.00 MB)
[upload] Starte Upload: doc2.txt (0.00 MB)
[upload] Starte Upload: photo.jpg (2.00 MB)
[chunked] Große Datei (6.00 GB): large_video.mkv
[chunked] Berechne SHA256 für large_video.mkv (6.00 GB)...
[chunked] Erstelle Upload-Session...
[chunked] uploadid: 1234567890
[chunked] Starte Upload: 6.00 GB @ 128 MB Chunks
[chunked] Progress: 1,342,177,280/6,442,450,944 Bytes (20.8%)
[chunked] Progress: 2,684,354,560/6,442,450,944 Bytes (41.7%)
...
[chunked] Finalisiere Upload...
[chunked] Upload abgeschlossen: FileID=999999999
[chunked] ✓ SHA256 verifiziert
[upload] Starte Upload: config.db (0.49 MB)
```

### Schritt 5: State-Files
```bash
ls -lh /srv/pcloud-archive/resume/
# large_video.mkv_abc123def456.state.json

cat /srv/pcloud-archive/resume/large_video.mkv_*.state.json
{
  "uploadid": 1234567890,
  "offset": 6442450944,
  "chunks_uploaded": 48,
  "file_hash": "abc123...",
  "file_size": 6442450944,
  "remote_path": "/My Cloud/TEST_CHUNKED_RTB/_snapshots/.../large_video.mkv",
  "status": "in_progress",
  "updated_at": 1745668800.123
}
```

### Schritt 6: Resume-Test (optional)
```
1. Upload starten
2. Nach 60s killen (ca. 10% Upload)
3. Neu starten → Sollte bei 10% resumen
4. Upload bis zum Ende
```

### Schritt 7: Verifikation
Prüft für jede Datei:
- ✅ Existenz auf pCloud
- ✅ Größe (Lokal vs. Remote)
- ✅ SHA256 (bei großen Dateien)

## Erwartete Ergebnisse

### Success-Kriterien:
- [x] Alle Dateien erfolgreich hochgeladen
- [x] Ordnerstruktur korrekt repliziert
- [x] Kleine Dateien nutzen Standard-Upload
- [x] Große Datei nutzt Chunked-Upload
- [x] Progress-Logs alle 10 Chunks
- [x] State-File wird nach jedem Chunk aktualisiert
- [x] Resume funktioniert nach Interrupt
- [x] SHA256-Verifikation erfolgreich
- [x] Keine Errors/Warnings

### Performance-Erwartung:
| Datei | Größe | Methode | Zeit (Schätzung) |
|-------|-------|---------|------------------|
| doc1.txt | < 1 KB | Standard | < 1s |
| doc2.txt | < 1 KB | Standard | < 1s |
| photo.jpg | 2 MB | Standard | < 5s |
| config.db | 500 KB | Standard | < 2s |
| large_video.mkv | 6 GB | Chunked (48 Chunks @ 128 MB) | 3-5 Min |

**Gesamt:** ~5-7 Min (inkl. SHA256-Berechnung)

## Cleanup

### Automatisch (am Ende des Scripts):
```bash
# Wähle "y" bei der Cleanup-Frage
rm -rf /tmp/test_chunked_rtb_XXXXX
python3 -c 'import pcloud_bin_lib as pc; pc.deletefolder_recursive(pc.load_cfg(), path="/My Cloud/TEST_CHUNKED_RTB")'
```

### Manuell:
```bash
# Lokal
rm -rf /tmp/test_chunked_rtb_*

# pCloud
python3 << EOF
import pcloud_bin_lib as pc
cfg = pc.load_cfg()
pc.deletefolder_recursive(cfg, path="/My Cloud/TEST_CHUNKED_RTB")
EOF

# State-Files (optional)
rm -f /srv/pcloud-archive/resume/*.state.json
```

## Troubleshooting

### Problem: "RTB-Script nicht gefunden"
```bash
# Prüfe Pfad zu rsync_tmbackup.sh
ls -l ../rtb/rsync_tmbackup.sh

# Oder: Pfad im Script anpassen (Zeile 43)
```

### Problem: "Keine State-Files"
→ Normal, wenn Upload unter Threshold (< 5 GB) oder bereits abgeschlossen.
→ State-Files werden nach erfolgreichem Upload gelöscht.

### Problem: "SHA256-Mismatch"
→ Datei wurde während Upload geändert (sehr unwahrscheinlich bei dd-generierten Files).
→ Prüfe Logs auf Netzwerkfehler während Upload.

### Problem: "Resume funktioniert nicht"
→ Prüfe, ob State-Dir schreibbar ist:
```bash
ls -ld /srv/pcloud-archive/resume/
# Sollte: drwxrwxr-x
```

## Varianten

### Test mit niedrigerem Threshold (1 GB):
```bash
PCLOUD_RESUME_THRESHOLD_GB=1 bash scripts/test_chunked_rtb_snapshot.sh
```
→ Dann würde auch photo.jpg (2 MB) über Chunked-Upload laufen (für Testing).

### Test mit größerer Datei (20 GB):
```bash
# Zeile 35 im Script anpassen:
dd if=/dev/urandom of="$TEST_ROOT/source/media/videos/large_video.mkv" bs=1M count=20480 status=progress
```
→ Dauert ~15-20 Min für Upload.

### Test ohne Resume:
→ Überspringe Schritt 6 (drücke "N" bei der Frage).

## Vergleich mit PoC

| Feature | PoC (poc_chunked_resume.py) | Production-Test (dieser) |
|---------|----------------------------|--------------------------|
| Dateianzahl | 1 (isoliert) | 5+ (gemischt) |
| Ordnerstruktur | Keine | 3-Level-Hierarchie |
| RTB-Integration | Nein | Ja (echter Snapshot) |
| Manifest | Fake-JSON | Generiert (wie Produktion) |
| Mixed-Mode | Nein | Ja (Standard + Chunked) |
| pCloud-Ordner | `/tmp/test_upload` | `/_snapshots/test_snapshot_...` |
| Verifikation | Manuell | Automatisch (alle Dateien) |

## Next Steps nach erfolgreichem Test

1. ✅ Production-Deployment auf pi-nas
2. ✅ Monitoring der ersten großen Backups
3. ✅ Metrics-Check (MET_RESUMED_FILES)
4. ✅ State-Dir-Cleanup-Job (optional)

## Support

Bei Fehlern: Logs prüfen und Issues auf GitHub öffnen mit:
- Test-Output (vollständig)
- State-File-Inhalt
- `ls -lh /tmp/test_chunked_rtb_*/`
- pCloud-Screenshots (optional)
