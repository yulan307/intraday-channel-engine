# Phase 8 Draft: Concurrent Live Runs for Different Symbols

## Status

Implemented on 2026-07-16. This document remains the locked Phase 8 behavior;
automated coverage verifies non-destructive initialization, client-ID retry,
and private-database merge behavior. Manual TWS Paper validation remains
required.

## Change-Minimization Rule

Unless this document explicitly changes a behavior, Phase 8 preserves the
existing Phase 7 behavior. A behavior changes only where retaining it conflicts
with a confirmed Phase 8 decision.

## Confirmed Goal

Run multiple Live instances concurrently against one IBKR endpoint and account.
Each instance owns one different symbol. This phase does not introduce multiple
strategies for the same symbol.

Each symbol runs in its own operating-system process. Phase 8 does not add a
central Live supervisor, shared strategy runtime, or shared market-data service.

## Phase 8 Startup and Live Flow

```text
Read and validate Live YAML
→ Load parameter set
→ Create run_id and run_id.jsonl
→ Generate process-lifetime market and order client IDs
→ Acquire the per-symbol Windows mutex
→ Create market and order gateways
→ Connect market gateway
→ Connect order gateway and confirm exactly one managed account
→ Create and initialize temporary_directory/run_id.sqlite3
→ Resolve the trading date and session
→ Create single_day_run
→ Disconnect both gateways and wait for the session start
→ Reconnect both gateways and enter the existing Live loop
```

## Existing Baseline

Phase 7 provides one Live process with separate market-data and order IBAPI
connections, a configured client ID for each connection, completed one-minute
bar processing, and Live Paper market-order submission.

## Explicitly Out of Scope

Phase 8 does not add any of the following capabilities:

- Account management or account-level coordination.
- Order tracking, order-status observation, execution observation, or order
  reconciliation.
- Available-funds, margin, or buying-power checks.
- Position, holdings, or exposure checks.

Each concurrent Live instance retains the existing Phase 7 submission boundary:
a normally returning local `placeOrder(...)` call is treated as a submitted
order for that instance. Phase 8 does not add broker-side confirmation.

## Decisions to Fix

The following decisions must be confirmed before designing Phase 8:

1. Process topology and launch model.
2. Client-ID lifetime across reconnect and recovery.
3. Master-database merge serialization and failure policy.
4. Market-data subscription limits and pacing policy.
5. Operator visibility beyond per-run JSONL logs.
6. Acceptance criteria and paper/live validation scope.

## Decision Record

### D1: Explicit exclusions

Confirmed: no account management, order tracking, funds checking, or holdings
checking.

### D2: Process topology

Confirmed: one independent operating-system process per symbol.

### D3: Client-ID allocation

Confirmed: each process owns dedicated, independently generated IBAPI client
IDs for its market-data and order connections. The program generates IDs at
random within the IBAPI-supported client-ID range. If an attempted connection
reports that a generated client ID is already in use, the program generates a
new random ID and retries that connection. No other connection error triggers
client-ID regeneration.

The exact numeric range is an implementation detail and must be taken from the
IBAPI requirements in force when Phase 8 is implemented.

### D4: Client-ID lifetime

Confirmed: generate the market-data and order client IDs once when the process
starts. Reuse the same pair for every connection retry, disconnect/reconnect,
and run-recovery action in that process. Generate a replacement ID only when a
connection reports that its current ID is already in use.

### D5: Per-run databases and terminal merge

Confirmed: each run uses its own SQLite database, identified by its `run_id`.
Merge that run database into the master database only after the trading day has
closed and the run has completed normally with complete data. A permitted
disconnect, reconnect, gateway rebuild, or same-run recovery does not prevent
terminal merging if the final data is complete.

If the data is terminated partway through the session or the process is forced
to stop, do not merge the private database. Retain it as the terminal artifact.

Master-database initialization and merge schema alignment are defined in D25.

### D6: Concurrent master-database merges

Confirmed: only one process may merge into the master database at a time. A
process that cannot acquire the merge right waits and retries until it can
perform its merge.

### D7: Merge failure

Confirmed: if a process has acquired the merge right but its merge fails, it
must release the merge right and exit. It must retain its private run database
for a later retry or manual handling. It must not retry the failed merge in the
same process indefinitely.

### D8: Successful merge cleanup

Confirmed: after a successful master-database merge, export the run's
`processed_1m_bar` records to CSV using the same behavior and format as the
existing Backtest CSV export. After that CSV export succeeds, delete the
private run SQLite database.

`process_1m_bar` in the decision discussion is interpreted as the existing
`processed_1m_bar` table name.

### D9: Master-database merge scope

Confirmed: do not merge `schema_meta` or `processed_1m_bar` from the private
run database into the master database. Its `processed_1m_bar` records have the
sole terminal artifact of the Backtest-compatible CSV export created from the
private run database before that database is deleted.

Merge every remaining table with an upsert after the D25 column-alignment
rules have been applied.

### D10: Per-symbol duplicate-process prevention

Confirmed: before opening SQLite or connecting to IBKR, each Live process must
acquire a Windows named mutex. Its name is derived from the IB environment,
host, port, and canonical symbol. If the mutex is already held, the new process
must refuse to start and report that the same symbol is already running against
that endpoint. The operating system releases the mutex when its owning process
ends or crashes.

### D11: Launch model

Confirmed: Phase 8 adds no multi-symbol launcher, process supervisor, or child
process creation. The operator manually starts each single-symbol Live process.

### D12: Concurrent-process limit

Confirmed: Phase 8 imposes no application-level cap on the number of concurrent
Live processes. IBKR/TWS connection, subscription, and pacing limits remain the
effective limits.

### D13: Failure isolation

Confirmed: a failure in one symbol's Live process affects only that process. It
performs its own terminal handling and exit. It does not stop, restart, or
otherwise affect other symbol processes. A failed or forced-stopped process
does not merge its private database.

### D14: Run ID and log isolation

Confirmed: after successful launch validation, create the normal `run_id` and
write all startup diagnostics and later run diagnostics to that run's own JSONL
log. Phase 8 does not use a shared `startup.jsonl` file.

The formal run-ID structure remains timestamp, symbol, parameter-set ID, and
random suffix.

### D15: Validation failure persistence

Confirmed: if launch configuration validation fails, create only the run JSONL
log, using `UNKNOWN` for any unavailable symbol or parameter-set-ID component,
and do not create a private run SQLite database. Create the private database
only after validation has completed successfully.

### D16: Pre-run startup failures

Confirmed: retain the Phase 7 timing for creation of `single_day_run`: after
the trading session is resolved and both pre-market gateways are disconnected,
but before the session-start wait. A market connection failure,
account-connection failure, trading-session query failure, or other startup
failure before that record exists is written only to the run's own JSONL log.
Phase 8 does not create a failed `single_day_run` solely for such a startup
failure.

### D17: Existing recovery behavior

Confirmed: retain the existing Phase 7 single-process disconnect, reconnect,
same-`run_id` recovery, historical-Bar replay, and retry-until-session-close
behavior unchanged. Phase 8 modifies it only as required to preserve the
process-lifetime client IDs confirmed above.

### D18: Per-run launch configuration

Confirmed: the operator creates one YAML file for each run. Its format and
fields remain the same as the existing Live configuration. Each manually
started single-symbol process reads its own YAML through the existing
single-config launch path. Phase 8 does not add a multi-symbol configuration
format.

Unless explicitly changed later, existing CLI field-override behavior remains
available.

### D19: YAML selection

Confirmed: retain the existing YAML selection behavior. Without `--config`,
the Live CLI reads its default Live YAML. When `--config <path>` is supplied,
that explicitly named YAML replaces the default YAML for the launch.

### D20: Database path layout

Confirmed: retain the existing master-database path behavior. Add one
temporary-file-directory setting to the existing Live YAML format. The process
creates its private `run_id` SQLite database under that configured temporary
directory.

### D21: Temporary-directory YAML key

Confirmed: the new Live YAML key is `temporary_directory`.

### D22: Atomic master-database merge

Confirmed: each master-database merge is one atomic transaction. Commit only
if every required table merge succeeds. If any write fails, roll back the
entire merge and leave the private run database unchanged for later handling.

### D23: Concurrent-run acceptance environment

Confirmed: after implementation and automated validation, the operator manually
connects the program to TWS Paper and runs concurrent symbol processes for the
real integration test. Phase 8 does not require a real Live-account test.

### D24: Retained-database recovery tooling

Confirmed: Phase 8 does not add a merge-only command or other recovery tool for
a private run database retained after a failed master merge. The operator handles
such artifacts manually.

### D25: Non-destructive schema initialization and merge alignment

Confirmed: call `initialize()` only when the target SQLite file or a required
table does not exist. Do not rebuild, drop, or replace an existing database or
table because its schema differs from the current application schema.

Before merging a private database into the master database, compare the source
and target table columns. When the target table lacks a source column, add a
compatible nullable column to the target table. When the target table has a
column that is absent from the source data, insert `NULL` for that column.

### D26: Upsert conflict resolution

Confirmed: when an upsert encounters a matching primary key, values from the
private run database overwrite the corresponding values in the master database.

### D27: Non-column schema incompatibility

Confirmed: if a source and target table differ in a way that cannot be resolved
by adding columns, such as a primary-key or incompatible table-structure
difference, treat the merge as failed. Retain the private run database, release
the master-database merge right, and exit the process.

### D28: Data-complete terminal merge gate

Confirmed: the merge gate is successful completion after the trading day has
closed with complete run data. Complete data means the Live feed successfully
emits `BAR_END` and `SingleDayRunner` returns `COMPLETED`. Reconnection,
gateway rebuild, and same-run recovery are permitted during the session and do
not disqualify the run when it reaches that result. Any partial-data
termination or forced process stop skips merging and retains the private run
database.
