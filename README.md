# Intraday Channel Engine — Phase 5 Live Paper Closed Loop

## Phase 8 concurrent Live Paper runs

Phase 8 permits independently started Live Paper processes for different symbols.
Each valid run gets a JSONL log and private SQLite database in the required
Live YAML `temporary_directory`; duplicate environment/host/port/symbol starts
are rejected by a Windows mutex. Market and order connections use distinct
random positive client IDs. IBAPI 326 regenerates only the colliding ID (up to
32 attempts), and reconnect/recovery reuse the selected IDs.

Only a post-close `BAR_END` run with `COMPLETED` atomically merges its private
`trade_date`, `raw_1m_bar`, `single_day_run`, `signal_event`, and `run_summary`
into `--database`, then exports `processed_1m_bar` to `data/<run_id>.csv` and
deletes the private file. Other terminal paths retain the private database and
do not merge. SQLite initialization is non-destructive for every caller: it
creates missing objects only and never clears existing data for schema drift.

Phase 3 Expand uses IBAPI as the only raw-market-data contract. Historical
`BarData` is persisted in SQLite with native UTC epoch `date`, OHLC, `volume`,
`wap`, and `barCount`; ET timestamps are derived for RTH validation and
strategy processing.

Phase 4 provides the Live Paper completed-Bar feed. It uses
one `reqHistoricalData(..., durationStr=max(60 seconds, elapsed session seconds + 10 seconds),
useRTH=1, keepUpToDate=True)` request, persists
each emitted raw Bar to `raw_1m_bar`, and stops before strategy execution or
orders. `useRTH=1` filters non-RTH data but does not constrain the response to
the target date: an overlong duration skips non-RTH time and returns prior
trading-date intraday RTH Bars. The live request uses a `+10 seconds` margin
with a 60-second minimum window for 1-minute bars. IBKR can prepend the previous session's final RTH Bar as the first
initial historical callback; only that structurally valid pre-session boundary
Bar is ignored. Other session-external Bars terminate the fetch.

Phase 5 connects that feed to the existing Trend, Channel, and Decision
pipeline. The Live CLI creates one `LIVE_PAPER` run before any pre-market
wait, processes `HIST`, `LIVE`, and `END` bars through `SingleDayRunner`, and
writes `processed_1m_bar`, optional `signal_event`, and one atomic terminal
`run_summary`/`single_day_run` status. Phase 4 continues to upsert
`raw_1m_bar` before the Runner consumes each completed Bar. Phase 7 adds a
separate Live-only order gateway. Each IB profile has `market_client_id` for
Backtest/market data and `order_client_id` for submission. Live requires a
non-empty `shares` list of positive integers; `--shares` replaces it and accepts
comma- or whitespace-separated values. Only consumer-classified `LIVE` signal
Bars submit `MKT` / `DAY` stock orders through `SMART` in `USD`, using the one
account returned by `managedAccounts`. A normally returning `placeOrder`
consumes one quantity. No acknowledgement, fill, funds, holdings, position,
or order-status tracking is performed. The configured IB endpoint remains the
operator's responsibility.

LivePaperFeed emits raw completed Bars without a final source. The Runner
classifies `HIST` / `LIVE` / `END` at consumption time, persists that result,
then processes, submits eligible orders, persists the Bar and signal, and
advances strategy state.

Live recovers the same `run_id` after a five-minute completed-Bar timeout. For
the first historical callback of an in-session Feed start, that deadline is
`max(session_start, subscription_start) + 5 minutes`; later deadlines use the
next expected completed Bar. IBAPI
system connection messages are logged but do not directly cancel a Live
subscription. Recovery closes both gateways, restores remaining shares from the
latest `signal_event` (or configuration when no event exists), reconnects, and
replays the session from its start. Replay upserts raw and processed Bars but
does not recreate `signal_event` rows or submit orders for replayed Bars.
Retries wait 20 seconds, 1 minute, 15 minutes, then 1 hour until session close;
an unrecovered session is FAILED. Pre-market session/account reads close both
IBAPI gateways before waiting, and market-open processing opens fresh ones.
SQLite persistence failures are terminal.

Each run writes `data/logs/<run_id>.jsonl` by default; Live accepts `--log-dir`
to choose another directory. `log_level` is required in both startup YAML files
and is either `INFO` or `ERROR`. `INFO` records and mirrors the startup-to-first-
confirmed-Bar sequence, including IBAPI requests/callbacks and first-Bar strategy
results; after that confirmation normal INFO records stop while processing
continues. Both levels retain IBAPI errors and final summaries. Every IBAPI
`error(...)` callback records its request ID, code, message, callback time, and
advanced rejection payload when supplied. Live begins a terminal-only five-minute
heartbeat after the first confirmed Bar. The final real-TWS full-day validation
is performed manually; the automated suite uses fake clocks and feeds.

Live startup defaults are stored in the local, ignored `configs/live_config.yaml`.
Use `configs/live_config_sample.yaml` as the tracked setup template when setting
up a checkout. Start without
strategy arguments to use that YAML file; an explicitly supplied CLI option
overrides only its matching YAML field. `trade_date` is an optional ET date and
maps to `--trade-date`; null selects today or the next tradable session after
today's close. `ib_environment` selects the paper/live connection profile in
the local `configs/ib.yaml` (copy `configs/ib_config_sample.yaml` first).
`shares` is required; each list value is the
next submission quantity.

Both CLI entrypoints print their validated, merged launch configuration and
wait for Enter before creating runtime directories, opening SQLite, connecting
to TWS, or requesting data.

On Windows, `run_live.ps1` and `run_backtest.ps1` start the respective CLI from
the project `.venv` and project root. Their optional first positional argument
is a YAML filename under `configs/`: `./run_live.ps1 MU.yaml` passes
`--config configs/MU.yaml` to Python. Without it, Live uses
`configs/live_config.yaml` and Backtest uses `configs/backtest.yaml`. Remaining
arguments remain ordinary CLI overrides, for example
`./run_backtest.ps1 --parameter-set-id another-set`.

An `InputValidationError` is an expected CLI exit: the console prints one
`ERROR: ...` line, the error is appended to `<log_dir>/startup.jsonl` before a
run ID exists (or to the run JSONL log afterward), and the process exits with
code `2` without a traceback. After session resolution, the CLI prints and
logs the requested and selected date plus its selection reason. Before the
session begins, it prints and logs `session_waiting` immediately and then at
one-hour, fifteen-minute, one-minute, or one-second intervals as the remaining
time crosses the one-hour, ten-minute, and ten-second boundaries.

Live `processed_1m_bar` preserves the Phase 3 v5 column shape except for the
removed `initial_threshold` column. It keeps all RawBar, request-provenance,
parameter, Trend, Channel, and Decision fields as queryable columns.
`active_threshold` is nullable and records the threshold actually used by the
current Bar. The persisted `decision`
is `NULL` when no signal triggers and is `BUY` or `SELL` only for triggered
signals. No JSON payload columns or persisted `_et` columns are used.

The backtest CLI scans selected parameter sets over one or more inclusive
calendar dates. Each selected parameter set gets one generated run ID:

```text
<YYYYMMDD-HHMMSS>_<symbol>_<parameter_set_id>_<3 alphanumeric random characters>
```

The timestamp uses the local machine timezone and one-second precision. The
same `run_id` covers all dates for that parameter set. Daily `single_day_run`
records use `(run_id, trade_date)`, while `run_summary` has one scan-level row
keyed by `run_id`. Non-trading dates are recorded as `SKIPPED`; failed dates
are recorded as `FAILED` and later dates continue. One multi-day CSV is
exported at `data/<run_id>.csv` after all dates have been attempted. Backtest
retains processed Bars in memory and writes no `processed_1m_bar` SQLite rows;
partial rows from a failed date remain included. Live retains SQLite processed-
Bar auditing.

Each raw Bar also stores an ET, zone-aware, minute-rounded `timestamp` beside
its canonical IBAPI epoch `date`. At each terminal daily run, `single_day_run`
stores the actual first threshold, triggered signal count, and direction-aware
best `trend_price`. Backtest also stores `first_trigger_reward` and
`full_position_reward`. Each signal price is scored against the first threshold
and same-day best price using BUY/SELL-aware distance. The first metric is the
first signal score; the full-position metric is
`sum(signal_reward_i / 2**i)` for one-based trigger order, so unallocated shares
contribute zero. A Backtest no-signal day stores zero for both metrics; missing
inputs or a non-positive directional denominator on a signaled day produce
null metrics. Live leaves both fields null.
For each `run_id`, `run_summary` stores total processed Bars and signals plus
the average signal count and both Backtest rewards over completed days that
processed Bars. Reward averages include no-signal days as zero and exclude null
metrics. It also stores each metric's maximum and comma-separated, date-ordered
ties in `max_first_trigger_reward_days` and
`max_full_position_reward_days`. A failed day makes the scan summary `FAILED`;
skipped days do not.
When an existing database upgrades to `dual_backtest_reward_v1`, initialization
only appends missing fields. It does not reset, recalculate, or rewrite
historical rows. Legacy best-reward/efficiency columns and values remain
unchanged, while the new fields are null for old rows.

Backtest startup defaults are stored in the local, ignored `configs/backtest.yaml`.
Use `configs/backtest_config_sample.yaml` as the tracked setup template when
setting up a checkout. Run
`python -m single_day_test.application.backtest_cli` to use it, or pass
`--config` for another YAML file. Every supplied CLI option overrides only its
matching YAML field. The YAML supplies the symbol, direction, threshold,
`threshold_update_rate`, `log_level`,
parameter CSV selection, inclusive trade-date range, IB profile, database, and
IB config path. One date field selects one date; both select the inclusive
range. A non-empty `parameter_set_id` plus one selected date runs one daily
backtest. An empty ID scans every `is_active = 1` parameter row; an explicit ID
selects exactly that row regardless of activity.

Auto Threshold resets each date, initializes from the first completed Bar's
raw `open`, and updates after a triggered BUY or SELL for the next Bar.
BUY requires both the existing price/`pred_high` breakout condition and
`trend_slope >= trend_slope_std`. SELL requires the existing price/`pred_low`
breakout condition and `trend_slope <= -trend_slope_std`. Missing slope values
produce no signal. Signal post-processing is unchanged.
Decision starts each day open. After a signal it records the current Channel
`break_trend`, blocks repeat signals until a non-null effective-trend change,
then requires a DOWN observation for BUY or an UP observation for SELL before
break counting may resume. This runtime-only rearm state resets on every actual
signal; Trend and Channel state remain continuous.
`threshold_update_rate` is a 0-100 percentage. With a numeric threshold, a
numeric rate (including `0`) enables Auto and uses that threshold as the
initial value; null or omission keeps Fixed mode. A null threshold remains
Auto and initializes from the first Bar open. Auto BUY updates to
`signal_price × (1 - rate/100)` and SELL updates to
`signal_price × (1 + rate/100)`. That signal updates the
next active threshold while Trend and Channel state continue; the signal Bar
itself retains its calculation. Fixed Threshold never changes either state.

The tracked parameter template is `configs/parameter_set_sample.csv`; the runtime
`configs/parameter_set.csv` is local and ignored. SQLite schema metadata is
`channel_mix_v1`. Initialization creates missing tables without clearing or
rebuilding existing data, and forward-adds the dedicated Channel-mix
processed-bar columns when absent.

Install the official IBKR TWS API Python client before running this project.
The PyPI `ibapi==9.81.1.post1` package is too old for `SCHEDULE` and must not
be used. Copy `configs/ib_config_sample.yaml` to the local `configs/ib.yaml`,
then configure paper/live TWS profiles and separate
market/order client IDs. Fill tracking, reconciliation, and checkpointing are
outside this scope.

## Channel blended prediction

Each parameter-set CSV row requires `curr_mix_ratio` in `[0, 1]` and
`channel_window >= trend_window`. Channel
records prior-segment `last_pred_high` / `last_pred_low`, delayed-current
`curr_pred_high` / `curr_pred_low`, and the applied `mix` for audit. Decision
continues to consume only final `pred_high` / `pred_low`. Current predictions
are null only below three Bars, use the fitted intercept from three Bars through
`delay = trend_window // 2`, use `n - delay` through
`n = 2 * delay`, and use fixed `delay` afterward. When both pairs exist, final
prediction is `last * (1 - mix) + curr * mix`; `mix` is `curr_mix_ratio` times
a normalized k=4 sigmoid spanning exactly from zero at `n = delay` to one at
`n = 2 * delay`. The first segment remains without final predictions until a
valid last model exists. On a trend switch, the old current model is frozen and
immediately supplies the switch-Bar last prediction at its previous current
coordinate plus one (`delay + 1` after the delayed prefix is stable); later
last predictions advance from that coordinate one Bar at a time. An Auto signal
does not reset or freeze Channel state; it only updates the next active
threshold.
