# Phase 4 Draft: Live Paper Bar Fetch

## Locked Scope, Premises, Background, and Boundaries

Phase 4 provides a dedicated Live CLI and a runnable Live Paper bar-fetch loop.
It resolves the target trading session, waits for the session start when needed,
fetches and continuously updates 1-minute RTH bars, classifies completed bars,
persists emitted raw bars, and exposes output events.

The Live CLI inputs are `symbol`, `direction` (`BUY` or `SELL`), a required
numeric Fixed or null/omitted Auto `threshold`, `parameter_set_path`, `parameter_set_id`, and an
optional ISO `start_date` (`YYYY-MM-DD`). One run accepts one symbol and one
parameter set. Live Paper allows the same Auto threshold mode as backtest.

Phase 4 ends at the output-buffer interface. It does not invoke `process 1m
bar`, strategy engines, processed-bar persistence, summaries, or order logic;
those remain Phase 5 or later work.

## Logic Flow

1. The Live CLI obtains the current ET time through `Clock.now_et()`.
2. A supplied `start_date` earlier than the current ET date is an error. When
   omitted, the current ET date is the lookup start date.
3. Starting with that date and including it, the CLI resolves four calendar
   dates of trading-session information. It reads `trade_date` first; a missing
   required date is queried through IBAPI and upserted locally. A record with
   required session data missing is not tradable. A supplied `start_date` that
   is not a trading day is an error.
4. The program selects the runnable trading date and its `session_start_et` /
   `session_end_et`. Before the session, a separate timer waits until
   `session_start_et` and then triggers the request. This timer is not the
   output consumer's waiting mechanism. A supplied current-date start after the
   session has ended is an error; an omitted start date after today's session
   selects the next trading day.
5. The fetch module sends one `reqHistoricalData` request:

   ```text
   endDateTime = ""
   durationStr = max(60 seconds, ceil(now_et - session_start_et) + 10 seconds)
   barSize = "1 min"
   whatToShow = "TRADES"
   useRTH = 1
   formatDate = 2
   keepUpToDate = True
   ```

   The ten-second duration margin reaches before the opening boundary; `useRTH=1` limits
   the request to RTH, but it does not limit data to the target trading date.
   When `durationStr` exceeds the target date's available RTH data, IBAPI skips
   non-RTH time and continues the lookback with prior-trading-date intraday RTH
   bars. The `+10 seconds` margin is deliberately small to constrain that
   behavior. IBKR may prepend one prior-session final RTH bar as the
   first initial historical callback. The 60-second minimum is required when
   the request starts exactly at session open. That first pre-session bar is an ignored
   callback boundary, after structural OHLCV validation; it is not target
   session data. Every other timestamp is validated against the resolved
   session and a session-external bar is an error rather than silently filtered.
6. Historical callbacks populate `hist_buffer`. The module internally manages
   partial real-time updates and puts only completed bars into `live_buffer`.
   A normal bar becomes complete when a bar with a new timestamp arrives.
7. On the historical end marker, the module merges `hist_buffer` with completed
   bars in `live_buffer`, de-duplicates by timestamp, and processes the ordered
   result as one batch. For duplicate timestamps, keep the bar with larger
   volume; if volume is equal but fields differ, keep the live bar and log both
   bars. A fixed `now_et` snapshot is used for the whole batch: the bar for
   `now_et` minute minus one is `LIVE`; earlier bars are `HIST`.
8. After initialization, every newly completed live bar is processed immediately
   as a one-bar batch. A bar timestamp equal to `session_end_et - 1 minute` is
   `END`; `END` replaces `HIST` or `LIVE` and is not trade-eligible.
9. Before a processed bar is exposed, it is upserted into `raw_1m_bar` using
   the Phase 3 raw-bar fields. Upsert failure raises an error and prevents
   output. IBAPI invokes callbacks on its event-loop thread; the shared SQLite
   connection permits that callback-thread write and the feed condition lock
   serializes it with output consumption. `raw_1m_bar` verifies raw fields, timestamps, and output order only;
   it does not persist `HIST`, `LIVE`, or `END`.
10. The output buffer emits `AVAILABLE` with one bar when a bar can be extracted.
    The final `END` bar is emitted as `AVAILABLE`; after it has been extracted,
    the following call emits `END`. While the session remains open and the
    output buffer is empty, the result is `WAITING`.
11. `next_event()` is non-blocking. A consumer receiving `WAITING` calls
    `wait_for_change()`, which waits for a new output bar, end, error, or close.
    This applies only after the fetch module has started.
12. At `session_end_et`, the final expected bar is complete when present. If it
    is absent, wait until `session_end_et + 60 seconds`; then raise an error.
    A late completed bar with an earlier timestamp than an already emitted bar,
    or a duplicate timestamp after emission, is an error.
13. The Phase 4 verification consumer extracts output bars and logs them without
    invoking Phase 5 processing. In all normal and error
    paths, the fetch module cancels the request in `finally`.

## Required Logic Gap Handling

All unexpected errors are raised and terminate the program. There is no retry,
recovery, fallback data source, checkpoint, or automatic reconnect in Phase 4.

## Module Classification and Boundaries

| Module | Owns | Must not own |
| --- | --- | --- |
| Live CLI and session resolver | CLI input validation, four-day schedule lookup, session selection, startup timer | Bar processing and strategy execution |
| Live fetch module | IBAPI request lifecycle, callback routing, partial-update maintenance, buffers, ordering, classification, output events, cancellation | Strategy and order decisions |
| Output verification consumer | Extraction and log output | `process 1m bar`, summaries, orders |
| `process 1m bar` consumer | Future Phase 5 consumer of output events | Phase 4 startup timer and fetch lifecycle |

## Required Database, UI, and Supporting Design

- `trade_date` is the local session cache. Missing required dates are obtained
  from IBAPI and upserted. Empty required session data means not tradable.
- `raw_1m_bar` uses the existing Phase 3 fields and identity. It is written only
  after a completed bar is ready for output and before that bar is exposed.
- `Clock.now_et()` is the single time source. Unit tests use a fake clock;
  connected TWS tests use real time.
- Acceptance requires deterministic fake-clock/fake-callback tests and a real
  TWS intraday run that verifies `raw_1m_bar` and logs.

## Minimal Implementation Closure

The loop begins with a valid Live CLI request, resolves a tradable session,
waits for the timer when necessary, starts one continuous historical request,
turns completed bars into ordered output events, persists raw verification data,
and reaches `END` after the final bar is extracted. Missing final data, invalid
data, ordering violations, persistence failures, and other unexpected failures
raise and terminate after request cancellation.

## Explicit Exclusions

- `process 1m bar` invocation, strategy calculation, processed-bar persistence,
  run summaries, and orders;
- recovery, reconnect, retry, checkpoint, multi-day scanning, ranking, and
  strategy-math changes.
