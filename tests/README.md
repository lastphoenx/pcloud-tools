# Tests

Manuelle / experimentelle Skripte (nicht pytest-CI).

| Datei | Zweck |
|-------|--------|
| `test_copyfile_deduplication.py` | Prüft pCloud `copyfile` Content-Dedup (Pool-Hypothese) |
| `test_legacy_deduplication.py` | Legacy-Dedup-Verhalten |

```bash
cd /opt/apps/pcloud-tools/main
/opt/apps/pcloud-tools/venv/bin/python tests/test_copyfile_deduplication.py --env-file .env
```

`PYTHONPATH` wird intern auf das Repo-Root gesetzt (`pcloud_bin_lib.py`).
