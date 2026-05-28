# Sicherheits-Patch: Mode-Aware Complete-Marker

## Problem

Aktuell prüfen beide Modi (1to1 + Pool) nur ob `.upload_complete` existiert, ABER:
- Marker hat KEINE Mode-Information im Complete-Marker
- Risiko: Pool-Mode überspringt 1to1-Snapshot (oder umgekehrt)
- Bei gelöschtem Marker: Mix aus echten Files + Stubs möglich

## Lösung: Mode-Validation im Complete-Marker

### Patch für pcloud_push_json_pool_manifest_to_pcloud.py

**Zeile ~3735-3745: Pool-Mode Complete-Check**

```python
# VORHER (nur Existenz-Check):
try:
    pc.stat_file(cfg, path=marker_complete, with_checksum=False)
    _log(f"[pool-mode] ✓ Snapshot bereits vollständig hochgeladen: {snapshot_name}")
    return {"uploaded": 0, "stubs": 0, "skipped": True}
except:
    pass

# NACHHER (Mode-Validation):
try:
    result = pc.download_file(cfg, remote_path=marker_complete)
    marker_data = json.loads(result)
    existing_mode = marker_data.get("mode", "unknown")
    
    if existing_mode == "pool":
        _log(f"[pool-mode] ✓ Snapshot bereits vollständig hochgeladen (Pool-Mode): {snapshot_name}")
        return {"uploaded": 0, "stubs": 0, "skipped": True}
    else:
        _log(f"[pool-mode] ⚠ WARNING: Snapshot existiert mit anderem Mode ({existing_mode})!")
        _log(f"[pool-mode] ⚠ Bestehender Upload-Mode: {existing_mode}")
        _log(f"[pool-mode] ⚠ Aktueller Mode: pool")
        _log(f"[pool-mode] ⚠ ABBRUCH zur Vermeidung von Datenkonflikten!")
        raise RuntimeError(f"Snapshot {snapshot_name} existiert bereits mit Mode '{existing_mode}' - kann nicht mit Pool-Mode überschrieben werden!")
except json.JSONDecodeError:
    # Alter Marker ohne JSON → vermutlich Legacy
    _log(f"[pool-mode] ⚠ WARNING: Bestehender Marker ohne Mode-Info (Legacy-Format)")
    _log(f"[pool-mode] ⚠ Snapshot wird übersprungen zur Sicherheit")
    return {"uploaded": 0, "stubs": 0, "skipped": True}
except Exception as e:
    # Marker nicht gefunden oder Download-Fehler → fortfahren
    if "not found" not in str(e).lower() and "2005" not in str(e):
        _log(f"[pool-mode] Fehler beim Lesen des Complete-Markers: {e}")
    pass
```

**Zeile ~3870-3880: Complete-Marker schreiben (bereits korrekt)**

```python
# Pool-Mode schreibt bereits Mode-Info:
marker_data = {
    "snapshot": snapshot_name,
    "completed_at": time.time(),
    "uploaded": final_stats["uploaded"],
    "stubs": final_stats["stubs"],
    "skipped": final_stats["skipped"],
    "errors": final_stats["errors"],
    "duration": time.time() - t_start,
    "mode": "pool"  # ← Bereits vorhanden! ✓
}
```

### Patch für pcloud_push_json_manifest_to_pcloud.py (1to1-Mode)

**Zeile ~1775-1785: 1to1-Mode Complete-Check**

```python
# VORHER:
try:
    pc.stat_file(cfg, path=marker_complete, with_checksum=False)
    _log(f"[info] Snapshot {snapshot_name} bereits vollständig hochgeladen")
    return {"uploaded": 0, "stubs": 0, "resumed": False}
except:
    # Marker nicht da → weitermachen
    pass

# NACHHER (Mode-Validation):
try:
    result = pc.download_file(cfg, remote_path=marker_complete)
    marker_data = json.loads(result)
    existing_mode = marker_data.get("mode", "1to1")  # Default 1to1 für Legacy
    
    if existing_mode in ["1to1", "objects"]:  # 1to1-kompatible Modi
        _log(f"[info] Snapshot {snapshot_name} bereits vollständig hochgeladen (Mode: {existing_mode})")
        return {"uploaded": 0, "stubs": 0, "resumed": False}
    else:
        _log(f"[ERROR] Snapshot existiert mit inkompatiblem Mode: {existing_mode}")
        _log(f"[ERROR] Kann nicht mit 1to1-Mode überschreiben!")
        raise RuntimeError(f"Snapshot {snapshot_name} existiert mit Mode '{existing_mode}' - inkompatibel mit 1to1-Upload!")
except json.JSONDecodeError:
    # Alter Marker ohne JSON → Legacy 1to1
    _log(f"[info] Snapshot {snapshot_name} bereits vorhanden (Legacy-Format)")
    return {"uploaded": 0, "stubs": 0, "resumed": False}
except Exception as e:
    if "not found" not in str(e).lower() and "2005" not in str(e):
        _log(f"[warn] Fehler beim Lesen des Complete-Markers: {e}")
    pass
```

**Zeile ~2100-2110: Complete-Marker schreiben**

```python
# VORHER (wahrscheinlich ohne Mode):
pc.put_textfile(cfg, path=marker_complete, text=json.dumps({
    "snapshot": snapshot_name,
    "completed_at": time.time(),
    "uploaded": uploaded,
    "stubs": stubs
}))

# NACHHER (mit Mode):
pc.put_textfile(cfg, path=marker_complete, text=json.dumps({
    "snapshot": snapshot_name,
    "completed_at": time.time(),
    "uploaded": uploaded,
    "stubs": stubs,
    "mode": "1to1"  # ← NEU!
}))
```

## Testing

```bash
# 1. Test Pool-Mode mit neuem Snapshot
SNAP=/mnt/backup/rtb_nas/2026-05-28-120000
python pcloud_push_json_pool_manifest_to_pcloud.py --snapshot-mode pool ...

# 2. Marker prüfen
python -c "
import json
from pcloud_bin_lib import *
cfg = load_config_from_env('.env')
result = download_file(cfg, '/Backup/rtb_1to1/_snapshots/2026-05-28-120000/.upload_complete')
print(json.dumps(json.loads(result), indent=2))
"
# Sollte zeigen: "mode": "pool"

# 3. Test: 1to1-Mode versucht denselben Snapshot
python pcloud_push_json_manifest_to_pcloud.py --snapshot-mode 1to1 ...
# Sollte abbrechen: "inkompatibel mit 1to1-Upload!"
```

## Migration für bestehende Marker

**Für alte Marker OHNE Mode-Info:**

```bash
# Optionales Migrations-Script
python << 'EOF'
import json, sys
sys.path.insert(0, '/opt/apps/pcloud-tools/main')
from pcloud_bin_lib import *

cfg = load_config_from_env('.env')
snapshots_root = "/Backup/rtb_1to1/_snapshots"

# Liste alle Snapshots
result = _rest_get(cfg, "listfolder", {"path": snapshots_root})
snapshots = [c for c in result["metadata"]["contents"] if c.get("isfolder")]

for snap in snapshots:
    snap_path = snap["path"]
    marker_path = f"{snap_path}/.upload_complete"
    
    try:
        # Lese Marker
        marker_content = download_file(cfg, marker_path)
        marker_data = json.loads(marker_content)
        
        # Hat bereits Mode?
        if "mode" in marker_data:
            print(f"✓ {snap['name']}: bereits Mode={marker_data['mode']}")
            continue
        
        # Heuristik: Hat Ordner .meta.json Files?
        has_stubs = False
        try:
            files = _rest_get(cfg, "listfolder", {"path": snap_path, "recursive": 1})
            for f in files["metadata"]["contents"]:
                if not f.get("isfolder") and f.get("name", "").endswith(".meta.json"):
                    has_stubs = True
                    break
        except:
            pass
        
        # Setze Mode
        guessed_mode = "pool" if has_stubs else "1to1"
        marker_data["mode"] = guessed_mode
        marker_data["migrated"] = True
        marker_data["migration_timestamp"] = time.time()
        
        # Schreibe zurück
        put_textfile(cfg, path=marker_path, text=json.dumps(marker_data))
        print(f"✓ {snap['name']}: Mode gesetzt auf {guessed_mode}")
        
    except Exception as e:
        print(f"✗ {snap['name']}: Fehler - {e}")
EOF
```

## Rollout-Plan

1. **Code-Patch anwenden** (beide Scripts)
2. **Test mit neuem Snapshot**
3. **Migration-Script für alte Marker** (optional)
4. **Deployment auf pi-nas**
5. **Monitoring**: Logs auf Mode-Warnings prüfen

## Alternativen

**Wenn Code-Patch nicht gewünscht:**

1. **Separate Dest-Roots** (EMPFOHLEN)
   ```bash
   1to1: /Backup/rtb_1to1_legacy
   Pool: /Backup/rtb_1to1_pool
   ```

2. **Snapshot-Name-Prefix**
   ```bash
   1to1: 2026-05-24-200014
   Pool: pool-2026-05-24-200014
   ```

3. **Exclusive Mode-Operation**
   ```bash
   # Stop 1to1 komplett
   sudo systemctl stop backup-pipeline.timer
   # Nutze nur Pool
   sudo systemctl start backup-pipeline-pool.timer
   ```
