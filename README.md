# Intraday Channel Engine — Phase 3 Expand

Phase 3 Expand uses IBAPI as the only raw-market-data contract. Historical
`BarData` is persisted in SQLite with native UTC epoch `date`, OHLC, `volume`,
`wap`, and `barCount`; ET timestamps are derived for RTH validation and
strategy processing.

`processed_1m_bar` preserves the Phase 3 v5 column shape except for the
removed `initial_threshold` column. It keeps all RawBar, request-provenance,
parameter, Trend, Channel, and Decision fields as queryable columns.
`active_threshold` is nullable and records the threshold actually used by the
current Bar; Auto Threshold warm-up rows use `NULL`. No JSON payload columns
or persisted `_et` columns are used.

The backtest CLI scans selected parameter sets over one or more inclusive
calendar dates. Each selected parameter set gets one generated run ID:

```text
<YYYYMMDD-HHMMSS>_<symbol>_<parameter_set_id>_<3 alphanumeric random characters>
```

The timestamp uses the local machine timezone and one-second precision. The
same `run_id` covers all dates for that parameter set. Daily run and summary
records use `(run_id, trade_date)` as their primary key. Non-trading dates are
recorded as `SKIPPED`; failed dates are recorded as `FAILED` and later dates
continue. One multi-day CSV is exported at `data/<run_id>.csv` after all dates
have been attempted; partial rows written before a failed date remain included.

Request JSON files belong in `configs/`. A request requires `symbol` and
`direction`, accepts one or both of `trade_date_start` / `trade_date_end`, and
contains one `threshold`. A numeric threshold selects Fixed mode; omitted or
`null` selects Auto mode. Requests cannot provide `run_id`. The parameter CSV
has `is_active`: an empty `parameter_set_id` selects all rows with `1`, while
an explicit ID selects exactly that row regardless of activity.

Auto Threshold resets each date, remains null until the Nth Bar where
`N = trend_window`, initializes from that Bar's strategy price, and updates
after a triggered BUY or SELL for the next Bar. Fixed Threshold never changes.

The database schema is `phase3_expand_v1`. During initialization, any
nonconforming Phase 3 table shape causes the complete Phase 3 database to be
cleared and recreated; no old data is migrated or retained.

Install the official IBKR TWS API Python client before running this project.
The PyPI `ibapi==9.81.1.post1` package is too old for `SCHEDULE` and must not
be used. Configure `configs/ib.yaml` with paper/live TWS profiles. Live
subscriptions, orders, recovery, and checkpointing are outside this scope.
