# ops-dashboard helpers

Two modules behind the daily ops dashboard:

- `stats.py` - `mean` and `tail`, the only numeric helpers.
- `report.py` - `weekly`, the rollup the dashboard renders.

The weekly panel is specified to show the last seven readings, oldest first.
