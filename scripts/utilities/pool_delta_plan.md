# pool_delta_plan.py — Delta vs. Full planen

Offline-Tool zur **Catch-up-Planung**: pro Snapshot wird geschätzt, ob Turbo-Delta (Scout + `copyfolder` + Phase 3) oder Full-Pool sinnvoller ist — **ohne Upload**.

Nutzt dieselbe Scout-Logik wie Production (`scout_pool_basis`: chronologischer Vorgänger bevorzugt).

---

## Voraussetzungen

```bash
cd /opt/apps/pcloud-tools/main
export PYTHONPATH=/opt/apps/pcloud-tools/main
# Credentials via --env-file .env
```

Lokale Manifeste unter `$PCLOUD_ARCHIVE_DIR/manifests/<snap>.json` müssen existieren.

---

## Typische Aufrufe

### Alle fehlenden Remote-Snapshots (statisch)

```bash
/opt/apps/pcloud-tools/venv/bin/python scripts/utilities/pool_delta_plan.py \
  --env-file .env --missing-only
```

### Catch-up-Simulation (empfohlen für Reihenfolge-Planung)

Simuliert: nach jedem geplanten Upload wird der Snapshot als „remote vorhanden“ behandelt → spätere Snaps finden oft bessere Delta-Basen.

```bash
/opt/apps/pcloud-tools/venv/bin/python scripts/utilities/pool_delta_plan.py \
  --env-file .env --missing-only --simulate-catchup
```

### Statisch vs. Simulation vergleichen

```bash
/opt/apps/pcloud-tools/venv/bin/python scripts/utilities/pool_delta_plan.py \
  --env-file .env --missing-only --simulate-catchup --compare-static
```

### Einzelner Snapshot

```bash
/opt/apps/pcloud-tools/venv/bin/python scripts/utilities/pool_delta_plan.py \
  --env-file .env --snapshot 2026-07-25-040041
```

---

## Spalten in der Ausgabe

| Spalte | Bedeutung |
|--------|-----------|
| `Snap` | Snapshot-Name |
| `#` | Catch-up-Schritt (nur `--simulate-catchup`) |
| `Basis` | Gewählter Scout-Basis-Snap |
| `Strat` | `chrono`, `jaccard`, `chrono_fallback`, `none` |
| `Sim%` | Jaccard-Ähnlichkeit (relpath + sha256) |
| `P3−` | Phase-3-Löschungen (tote Ordner + Einzel-Stubs) |
| `P4+` | Phase-4-Tasks (neu/geändert im Pool) |
| `Empf.` | `DELTA` oder `FULL` |

**Empfehlungs-Logik (vereinfacht):**

- Scout-Similarity unter `PCLOUD_SCOUT_THRESHOLD` (Default 70 %) → `FULL`
- Phase-3-Löschungen > `PCLOUD_DELTA_PLAN_DELETE_FULL` (Default 5000) → `FULL`
- sonst → `DELTA`

---

## ENV (optional)

| Variable | Default | Wirkung |
|----------|---------|---------|
| `PCLOUD_SCOUT_THRESHOLD` | `0.70` | Mindest-Similarity für Delta |
| `PCLOUD_DELTA_PLAN_DELETE_FULL` | `5000` | P3-Schwelle für FULL-Empfehlung |
| `PCLOUD_ARCHIVE_DIR` | — | Lokale Manifeste |
| `PCLOUD_DEST` | — | Remote Pool-Root |

---

## Interpretation

- **Statisch `2× FULL, 18× DELTA`** bei 20 fehlenden Snaps: wenn jeder isoliert betrachtet wird.
- **Catch-up `1× FULL, 19× DELTA`**: nach chronologischem Nachholen wird die Basis für spätere Snaps besser — weniger Phase-3-Aufwand.

Das Tool **ersetzt keinen Upload**; es hilft bei der Entscheidung, ob ein problematischer Snap bewusst als Full laufen soll oder ob chronologisches Catch-up reicht.

---

## Siehe auch

- [CHANGELOG_2026-08.md](../../docs/CHANGELOG_2026-08.md)
- [ENV_VARIABLES.md](../../docs/ENV_VARIABLES.md) — Scout, Delta-Cleanup, Circuit Breaker
