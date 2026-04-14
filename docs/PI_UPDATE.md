# Pi Update nach git revert/reset Chaos

## Problem
Das sync_repos.sh Script könnte wegen der git history changes (revert commits) verwirrt sein.

## Lösung (auf dem Pi ausführen):

```bash
# 1. Als root oder mit sudo
cd /opt/apps/pcloud-tools/main

# 2. Lokale Änderungen wegwerfen (falls vorhanden)
git fetch origin

# 3. History-Check: Wo stehen wir?
git log --oneline -5

# 4. Hard reset auf remote main (sauberste Lösung)
git reset --hard origin/main

# 5. Verify: Sollte jetzt 4b0b0a3 sein
git log --oneline -1
# Erwarte: 4b0b0a3 feat: Add intelligent health check script with RTB-awareness

# 6. File permissions prüfen (wichtig nach reset!)
chmod +x pcloud_health_check.sh pcloud_status.sh wrapper_pcloud_sync_1to1.sh

# 7. Status verifizieren
git status
# Erwarte: "On branch main, Your branch is up to date with 'origin/main'."
```

## Falls sync_repos.sh dennoch Probleme macht:

```bash
# Alternative: Manuell pullen
cd /opt/apps/pcloud-tools/main
sudo -u thomas git fetch origin
sudo -u thomas git reset --hard origin/main
sudo chmod +x *.sh
```

## Nach dem Update verfügbar:
- `pcloud_health_check.sh` (neu)
- `pcloud_status.sh` (MariaDB Version)
- `wrapper_pcloud_sync_1to1.sh` (MariaDB Version)
- `sql/init_pcloud_db.sql` (MariaDB Schema)
- `.env.example` (mit PCLOUD_DB_* Variablen)
