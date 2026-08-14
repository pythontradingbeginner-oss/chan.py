# P7 Holdout Fixture

`holdout_r10_baseline.json` is a small golden fixture for
`tests/test_trade_review_plotter.py`.

It is generated from the current strategy/data contract with:

```bash
python scripts/run_p7_holdout.py --output-dir reports/p7_holdout --oos-start 2026-06-01
```

The runtime report is written to `reports/p7_holdout/latest.json`.  This fixture
is kept in `tests/fixtures` so tests do not depend on `%TEMP%` files that may be
removed by cleanup jobs.

