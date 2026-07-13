# Phase 5: Single-Day Live Paper Closed Loop

## 1. Purpose

Phase 5 connects the completed-bar output of the Phase 4 `LivePaperFeed` to
the existing stateful application pipeline and produces one auditable
single-day Live Paper run.

The intended flow is:

```text
TWS
-> TradingSessionService / LivePaperFeed
-> SingleDayRunner
-> process_bar
-> TrendEngine
-> ChannelEngine
-> DecisionEngine
-> processed_1m_bar
-> signal_event
-> run_summary
```

Phase 5 remains paper-only. It must not submit, simulate, or manage orders.

## 1.1 Current Implementation

Phase 5 is implemented. `live_cli.py` creates one `LIVE_PAPER` run after
resolving its session and before any pre-market wait, then passes the Phase 4
`LivePaperFeed` to `SingleDayRunner`. The runner processes `HIST`, `LIVE`, and
`END` bars through the existing engines, persists each committed result, and
writes an atomic terminal summary plus run status.

The CLI accepts the existing fixed-threshold inputs and adds `--log-dir`,
which defaults to `data/logs`. Each run writes
`<log_dir>/<run_id>.jsonl`. The log records run creation, committed bars,
signals, completion, and failure. Phase 5 uses one injected `Clock`; production
passes `SystemClock` and tests pass a fake clock.

`configs/live_config.yaml` is the default Live launch configuration. It holds
the symbol, direction, fixed threshold, parameter selection, IB environment,
and optional `trade_date`. All matching CLI options are optional and override
only the corresponding YAML value when explicitly supplied. `trade_date` is
the YAML/CLI name for the prior Live start-date selection.

Input validation is handled at the CLI boundary as a normal exit: one concise
`ERROR:` console line, one `input_validation_error` JSONL event, exit code 2,
and no traceback. Before a run ID exists the event is written to
`<log_dir>/startup.jsonl`; after the run logger exists it uses that run's JSONL
file. Session resolution emits the requested date, current ET time, selected
date, and one of `explicit_trade_date`, `current_session`, or
`next_tradable_session`. Pre-market waiting emits matched console and
`session_waiting` JSONL status immediately and then on the one-hour,
fifteen-minute, one-minute, or one-second reporting cadence determined by the
remaining duration. This does not change `LivePaperFeed` session-end/final-Bar
waiting.

## 2. Phase 4 Contract to Preserve

Phase 4 is complete at the `CompletedBar` event boundary. Its current
contract is:

- the Live CLI accepts one symbol, direction, fixed threshold, parameter-set
  path and parameter-set ID, with an optional start date;
- the session resolver selects one tradable ET session and waits for its
  start when necessary;
- one `keepUpToDate=True` historical request produces ordered completed bars;
- each emitted raw bar is upserted to `raw_1m_bar` before it is exposed;
- bar sources are `HIST`, `LIVE`, and the final `END` bar;
- `BAR_END` is emitted only after the final `END` bar has been consumed;
- request cancellation and feed cleanup are required on normal and error
  paths;
- unexpected data, ordering, timeout, and persistence errors terminate the
  run; there is no retry, reconnect, checkpoint, or fallback behavior.

Phase 5 must consume these events without moving strategy logic into the feed
or changing the Phase 4 request and ordering rules.

## 3. Current Implementation Inventory

The repository already contains most reusable inner-loop pieces:

| Area | Current evidence | Phase 5 status |
| --- | --- | --- |
| Live bar acquisition | `bar_feed/live_paper_feed.py`, `application/live_cli.py` | Implemented as Phase 4 verification flow |
| Domain run context | `domain/models.py:RunContext` | Reusable, but no Live CLI construction path |
| Runtime state | `domain/states.py:RuntimeState` | Reusable |
| Algorithm pipeline | `application/bar_processor.py` plus the three engines | Reusable and already used by `SingleDayRunner` |
| Atomic bar persistence | `SingleDayRunner` transaction around processed bar and signal | Reusable |
| Run lifecycle persistence | `create`, `mark_completed`, `mark_failed`, `save_summary` | Exists, but requires integration-level verification |
| Live runner | `application/single_day_runner.py` | Wired to the Live CLI and covered by deterministic Live tests |
| Signal persistence | `persistence/signal_repository.py` and database insert path | Written atomically with the processed Bar when triggered |
| Summary persistence | `persistence/summary_repository.py` and database terminal methods | Completion and failure terminal records are atomic |
| Live CLI output | `application/live_cli.py` | Emits one final run summary and writes a per-run JSONL log |

## 4. Implemented Phase 5 Behavior

### 4.1 Live entrypoint integration

The Live CLI loads exactly one selected parameter set, generates a run ID,
creates the `RunContext` and `single_day_run` row, performs any pre-market
wait, and then invokes `SingleDayRunner` with `create_run=False`. Phase 4
feed tests remain independent because the feed interface is unchanged.

### 4.2 Run identity and configuration wiring

The existing ID format, one selected parameter set, Fixed Threshold CLI value,
and resolved session trade date populate `RunContext`. `live_phase` remains
`NULL` by decision. The selected parameter snapshot is persisted through the
existing run and processed-bar records.

### 4.3 Failure lifecycle

Failures before Runner ownership write a failed terminal record from the Live
CLI. Once started, Runner owns failures, preserves the original exception,
attempts one atomic `FAILED` terminal record, logs the failure, and closes the
feed. The outer CLI always disconnects the gateway and closes the database.

### 4.4 Live source and persistence semantics

Phase 5 confirms that:

- `HIST` bars build the initial algorithm state and are persisted as `HIST`;
- subsequent completed bars are processed identically regardless of whether
  they came from the initial history or live updates, except for `bar_source`;
- the final `END` bar is processed and persisted before `BAR_END` completes the
  run;
- `raw_1m_bar` remains the Phase 4 verification store and is written by the
  feed before processing;
- `processed_1m_bar` stores the algorithm result and does not duplicate raw
  feed ownership;
- signal rows are written only for triggered `BUY` or `SELL` decisions;
- `processed_1m_bar.decision` remains nullable for no-signal rows;
- the runtime state is advanced only after the processed bar and any signal
  have committed successfully.

The runner advances `RuntimeState` only after the processed Bar and optional
signal have committed successfully. `raw_1m_bar` continues to be written by
Phase 4 before Phase 5 receives a Bar.

### 4.5 Acceptance and observability

Deterministic fake-clock/fake-feed tests provide the implementation evidence:

- one complete fake-clock/fake-callback Live run without TWS;
- rows from `single_day_run`, `processed_1m_bar`, `signal_event`, and
  `run_summary` for the same `run_id`;
- structured logs that allow the bar and lifecycle sequence to be reconstructed;
- counts and first/last timestamps;
- `HIST`/`LIVE`/`END` boundaries;
- signal timestamps, prices, and break counts;
- final channel length and final current channel values;
- final run status and any recorded error fields.

The final real-TWS full-day test remains user-performed and is not an automated
Phase 5 validation step.

## 5. Validation Coverage

Focused tests cover:

1. A fake Live feed driving `SingleDayRunner` through `HIST`, `LIVE`, and
   `END` bars to `COMPLETED`.
2. Signal persistence and nullable no-signal decisions in that Live run.
3. A feed failure after partial progress to `FAILED`, including retained
   partial rows and cleanup.
4. A processed-bar or signal persistence failure proving that runtime state is
   not advanced.
5. Summary persistence failure and preservation of the original failure.
6. The final `END` bar being processed before the run is marked completed.
7. A full fake-clock integration assertion that all four result surfaces share
   one `run_id` and JSONL log.

The existing Phase 4 tests remain the lower-level contract tests for request
duration, callback boundaries, ordering, raw persistence, and cleanup. They
must continue to pass unchanged unless a Phase 5 integration seam requires a
small, explicitly documented adjustment.

## 6. Implementation Boundary

Phase 5 may add or adjust only the Live application wiring, run lifecycle
integration, observability, and tests needed to close the loop. It must not
change:

- Trend, Channel, or Decision mathematics;
- Phase 4 session resolution, callback classification, ordering, or request
  parameters;
- the Phase 3 processed-bar schema or nullable decision contract;
- Auto Threshold semantics unless separately approved;
- order, execution, recovery, checkpoint, ranking, or multi-day scan logic.

## 7. Completion State

Phase 5 is complete: a single Live Paper run starts from the supported
CLI, create one `RUNNING` record, consume the Phase 4 feed, process every
completed bar through the existing engines, persist processed bars and signal
events transactionally, write a final `COMPLETED` summary after `END`, and
leave a `FAILED` summary plus cleanup evidence for an injected failure.

The implementation has deterministic coverage of the Live entrypoint boundary
and all terminal persistence surfaces. A real-TWS full-day run remains the
user's final environment acceptance step.

## 8. Decision Archive

The following decisions were confirmed before implementation. New questions
must be handled in a later review round rather than added to this archive.

### 8.1 Entry point

Phase 5 continues to use `src/single_day_test/application/live_cli.py` as the
official entry point. Its existing CLI surface is retained, while its internal
verification loop is replaced by the complete `SingleDayRunner` flow. On
Windows, `./run_live.ps1` launches this CLI through the project `.venv` and
forwards `--help` and every CLI override unchanged.

### 8.2 Run ID

Live Paper reuses `DefaultIdGenerator` and its existing format:

```text
YYYYMMDD-HHMMSS_symbol_parameter_set_id_3-random-characters
```

The timestamp uses local machine time. The CLI does not accept a caller-owned
`run_id`.

### 8.3 Live phase

Phase 5 does not use `live_phase`. It creates `RunContext` with
`live_phase=None`, so `single_day_run.live_phase` remains `NULL`. No
pre-market or in-session state update feature is added in this scope.

### 8.4 Signal eligibility

`HIST`, `LIVE`, and `END` bars all enter the same algorithm pipeline and may
produce `BUY` or `SELL` signal events. Future Paper Order logic may impose a
separate restriction on which source can create an actual order.

### 8.5 Bar transaction boundary

`processed_1m_bar` and an optional `signal_event` are committed in one
transaction. `RuntimeState` advances only after that transaction succeeds. A
write failure rolls back the bar's persistence and stops the run without retry.

### 8.6 Failure handling

Feed, algorithm, or persistence failures terminate the run. The original
exception is preserved; the system attempts to write a `FAILED` summary and
mark `single_day_run` as `FAILED`. A failure while writing the failure summary
must not replace the original exception. There is no retry, recovery, or
continued processing. Feed, gateway, and database cleanup remains mandatory.

### 8.7 Acceptance boundary

Phase 5 implementation must include deterministic fake-clock/fake-callback
integration tests covering a complete `HIST -> LIVE -> END -> COMPLETED` run,
failure and cleanup behavior, and shared `run_id` evidence across all result
tables. The final real-TWS full-day test is deferred to the user and is not a
blocking implementation step in this round.

### 8.8 Run creation timing

After the selected trading session has resolved successfully, Phase 5 creates
the `RunContext` and `single_day_run` record before any pre-market wait. This
preserves an auditable failed run if the wait or subsequent startup fails.

### 8.9 Time source

All Phase 5 run time uses one injected `Clock`: `RunContext.started_at_et`,
feed time decisions, and `RunSummary.ended_at_et`. Production uses
`SystemClock`; deterministic tests use a fake clock. `SingleDayRunner` must
not read system time directly.

### 8.10 Waiting ownership

On `BAR_WAITING`, `SingleDayRunner` calls `feed.wait_for_change()` without a
polling timeout. `LivePaperFeed` owns deadline-aware condition waiting: it
wakes for callbacks, errors, or closure, and otherwise wakes at the session
end and final-bar timeout deadlines. The runner does not calculate session or
final-bar wait times.
