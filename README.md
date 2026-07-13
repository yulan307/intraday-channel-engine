# Intraday Channel Engine — Phase 5 Live Paper Closed Loop

Phase 3 Expand uses IBAPI as the only raw-market-data contract. Historical
`BarData` is persisted in SQLite with native UTC epoch `date`, OHLC, `volume`,
`wap`, and `barCount`; ET timestamps are derived for RTH validation and
strategy processing.

Phase 4 provides the Live Paper completed-Bar feed. It uses
one `reqHistoricalData(..., durationStr=elapsed session seconds + 10 seconds,
useRTH=1, keepUpToDate=True)` request, persists
each emitted raw Bar to `raw_1m_bar`, and stops before strategy execution or
orders. `useRTH=1` filters non-RTH data but does not constrain the response to
the target date: an overlong duration skips non-RTH time and returns prior
trading-date intraday RTH Bars. The live request uses only a `+10 seconds`
margin. IBKR can prepend the previous session's final RTH Bar as the first
initial historical callback; only that structurally valid pre-session boundary
Bar is ignored. Other session-external Bars terminate the fetch.

Phase 5 connects that feed to the existing Trend, Channel, and Decision
pipeline. The Live CLI creates one `LIVE_PAPER` run before any pre-market
wait, processes `HIST`, `LIVE`, and `END` bars through `SingleDayRunner`, and
writes `processed_1m_bar`, optional `signal_event`, and one atomic terminal
`run_summary`/`single_day_run` status. Phase 4 continues to upsert
`raw_1m_bar` before Phase 5 consumes each completed Bar. Live runs remain
Fixed Threshold, paper-only, and have no order, retry, recovery, or checkpoint
behavior.

Each Live run writes `data/logs/<run_id>.jsonl` by default; use `--log-dir` to
choose another directory. The JSONL file records run creation, committed Bars,
signals, completion, and failure. The final real-TWS full-day validation is
performed manually; the automated suite uses fake clocks and feeds.

Live startup defaults are stored in `configs/live_config.yaml`. Start without
strategy arguments to use that YAML file; an explicitly supplied CLI option
overrides only its matching YAML field. `trade_date` is an optional ET date and
maps to `--trade-date`; null selects today or the next tradable session after
today's close. `ib_environment` selects the existing paper/live connection
profile in `configs/ib.yaml`.

On Windows, `run_live.ps1` and `run_backtest.ps1` start the respective CLI from
the project `.venv` and project root. They forward all arguments unchanged, so
`./run_live.ps1 --help`, `./run_backtest.ps1 --help`, and YAML overrides such
as `./run_backtest.ps1 --parameter-set-id another-set` are supported.

An `InputValidationError` is an expected CLI exit: the console prints one
`ERROR: ...` line, the error is appended to `<log_dir>/startup.jsonl` before a
run ID exists (or to the run JSONL log afterward), and the process exits with
code `2` without a traceback. After session resolution, the CLI prints and
logs the requested and selected date plus its selection reason. Before the
session begins, it prints and logs `session_waiting` immediately and then at
one-hour, fifteen-minute, one-minute, or one-second intervals as the remaining
time crosses the one-hour, ten-minute, and ten-second boundaries.

`processed_1m_bar` preserves the Phase 3 v5 column shape except for the
removed `initial_threshold` column. It keeps all RawBar, request-provenance,
parameter, Trend, Channel, and Decision fields as queryable columns.
`active_threshold` is nullable and records the threshold actually used by the
current Bar; Auto Threshold warm-up rows use `NULL`. The persisted `decision`
is `NULL` when no signal triggers and is `BUY` or `SELL` only for triggered
signals. No JSON payload columns or persisted `_et` columns are used.

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

Backtest startup defaults are stored in `configs/backtest.yaml`. Run
`python -m single_day_test.application.backtest_cli` to use it, or pass
`--config` for another YAML file. Every supplied CLI option overrides only its
matching YAML field. The YAML supplies the symbol, direction, threshold,
parameter CSV selection, inclusive trade-date range, IB profile, database, and
IB config path. One date field selects one date; both select the inclusive
range. A non-empty `parameter_set_id` plus one selected date runs one daily
backtest. An empty ID scans every `is_active = 1` parameter row; an explicit ID
selects exactly that row regardless of activity.

Auto Threshold resets each date, remains null until the Nth Bar where
`N = trend_window`, initializes from that Bar's strategy price, and updates
after a triggered BUY or SELL for the next Bar. That signal also resets the
Trend and Channel state for the next Bar; the signal Bar itself retains the
pre-reset calculation. Fixed Threshold never changes or resets either state.

The database schema is `phase3_expand_v2`. During initialization, any
nonconforming Phase 3 table shape causes the complete Phase 3 database to be
cleared and recreated; no old data is migrated or retained.

Install the official IBKR TWS API Python client before running this project.
The PyPI `ibapi==9.81.1.post1` package is too old for `SCHEDULE` and must not
be used. Configure `configs/ib.yaml` with paper/live TWS profiles. Orders,
recovery, and checkpointing are outside this scope.
