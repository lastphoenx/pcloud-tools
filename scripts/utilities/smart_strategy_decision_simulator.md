# smart_strategy_decision_simulator.py

Offline-Entscheidungs-Simulator fuer Smart-Strategy 2.0.

## Zweck

Das Tool berechnet aus zwei Manifesten die absoluten Delta-Metriken und simuliert die Strategiewahl:

- SAFE-MODE
- TURBO-MODE
- TEMPLATE-DELTA-SAFE

Es werden exakt die Kernmetriken der Smart-2.0-Logik ausgewertet:

- match_count (identical_count)
- saved_calls
- cleanup_calls
- upload_calls
- upload_bytes

Hinweis: stub_ratio wird als Input uebergeben, da die echte Stub-Ratio in Produktion aus dem Remote-Index kommt.

## Aufruf

```bash
python scripts/utilities/smart_strategy_decision_simulator.py \
  --current /srv/pcloud-archive/manifests/2026-05-01-120000.json \
  --basis /srv/pcloud-archive/manifests/2026-05-01-103649.json \
  --source-snapshots 2 \
  --stub-ratio 0.92 \
  --template-exists \
  --template-match 0.97
```

## Wichtige Parameter

- --current: aktuelles Manifest
- --basis: Referenz/Basis-Manifest
- --source-snapshots: Anzahl verfuegbarer Source-Snapshots
- --stub-ratio: Stub-Ratio der Basis (aus Index-Analyse)
- --template-exists: Template vorhanden
- --template-match: Uebereinstimmung zum Template

## Schwellwerte (optional)

- --stub-transform-threshold (default: 0.80)
- --saved-calls-min (default: 1000)
- --template-strong-threshold (default: 0.90)

Die Defaults koennen auch per ENV gesetzt werden:

- PCLOUD_SMART_STUB_TRANSFORM_THRESHOLD
- PCLOUD_SMART_SAVED_CALLS_MIN
- PCLOUD_SMART_TEMPLATE_STRONG_THRESHOLD

## JSON-Ausgabe

```bash
python scripts/utilities/smart_strategy_decision_simulator.py \
  --current /path/current.json \
  --basis /path/basis.json \
  --source-snapshots 3 \
  --stub-ratio 0.88 \
  --json-out /tmp/smart_decision.json
```

Nur JSON auf stdout:

```bash
python scripts/utilities/smart_strategy_decision_simulator.py \
  --current /path/current.json \
  --basis /path/basis.json \
  --source-snapshots 3 \
  --stub-ratio 0.88 \
  --json-only
```
