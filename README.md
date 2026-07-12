# Intraday Channel Engine – Phase 3

Phase 3 uses IBAPI as the only raw-market-data contract. Historical `BarData`
is persisted in SQLite with its native fields: UTC epoch `date`, OHLC,
`volume`, `wap`, and `barCount`, plus request provenance (`bar_size`,
`what_to_show`, and `use_rth`). ET timestamps are derived only for RTH checks
and strategy processing.

`processed_1m_bar` is fully columnar: it preserves all RawBar fields and raw
request metadata (`bar_size`, `what_to_show`, `use_rth`, and `source`), and
expands parameter, Trend, Channel, and Decision payload fields into queryable
columns. Original JSON payload columns are not stored. The incompatible
Phase 3 v1/v2/v3/v4 schemas are cleared once during initialization and are not
migrated. `processed_1m_bar` contains no columns with an `_et` suffix and
stores an America/New_York `timestamp` at 1-minute bar precision.

After each run, its persisted `processed_1m_bar` rows are also exported to
`data/<run_id>.csv`. The CSV uses the exact SQLite table field names and order;
the SQLite records remain the primary persisted data.

Run parameters are supplied in a JSON request containing `symbol`,
`trade_date`, `direction`, `initial_threshold`, and a `parameter_set` object
with only `path` and `parameter_set_id`. The selected row is loaded from the
central [configs/parameter_set.csv](D:\Codes\Intrady_Channel_Engine\configs\parameter_set.csv). The run
uses validated local IBAPI data when present; otherwise it connects to TWS,
resolves the RTH schedule through `SCHEDULE`, fetches RTH 1-minute `TRADES`
bars, validates them, and stores them before running the strategy.

Install the official IBKR TWS API Python client before running this project.
The PyPI `ibapi==9.81.1.post1` package is too old for `SCHEDULE` and must not
be used. This checkout is validated with official API 10.48.1; API 10.12 or
newer is required. Download the current official Windows API installer, then
install its extracted `source/pythonclient` directory into `.venv` with pip.

Configure `configs/ib.yaml` with independent `paper` and `live` TWS profiles.
Use `--ib-environment paper` (default) or `--ib-environment live` when a fetch
is required. Initialize a new database with
`python -m single_day_test.application.backtest_cli init-db`. A legacy
Phase 2 database is rejected; the one-time destructive reset is
`init-db --rebuild-legacy`.

No live subscription, orders, recovery, or checkpointing is included.
