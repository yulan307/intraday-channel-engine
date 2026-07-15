# Live Connection Recovery

> Status: implemented current state.

## 1. Trigger and observed failure

The Live Paper process can receive IBAPI system code `1100` while its API
socket to TWS remains open. TWS/IB Gateway may later report `1101` or `1102`
after its connection to IBKR is restored.

The previous gateway treated `1100` as a sticky error and failed every active Live
subscription. The subscription is cancelled and removed. The Runner clears the
Feed error and waits, but no subscription remains to produce later Bars. A
subsequent `1102` therefore does not resume the run.

The concrete observed sequence was:

```text
1100: TWS/IBKR connectivity lost
Feed reports a nonfatal error
Gateway cancels the Live historical subscription
1102: TWS/IBKR connectivity restored, data maintained
No Live subscription is recreated
Runner waits without receiving another Bar
```

## 2. Recovery trigger: Bar timeout

All in-session connection disruption is intentionally converged into one
recovery trigger: a Bar timeout.

```text
expected Bar time + 5 minutes
-> no new completed Bar
-> RecoverableBarTimeout
-> enter the recovery loop
```

The opening expectation is `session_start`; the closing expectation is
`session_end`. Each receives the same five-minute tolerance. Between them, the
expectation advances minute by minute with the completed-Bar sequence.

IBAPI system connection messages, including `1100`, `1101`, `1102`, `2103`,
and `2105`, are recorded as connection-status logs only. They do not directly
fail the Feed, cancel the Live subscription, or enter the recovery loop. If
the subscription resumes and produces a completed Bar before the deadline, the
Run continues normally. If it does not, the Bar timeout performs recovery.

This applies equally to a silent IBAPI subscription failure that has no
connection-status callback. Request-specific data and validation errors retain
their existing error behavior.

## 3. Recovery loop

The recovery design deliberately avoids a fine-grained in-place restoration of
the interrupted Feed and IBAPI callback state. It is an outer loop around a
single Live Runner attempt.

```text
create or resume one RunContext
while the Live run is not terminal:
    try:
        connect gateways
        construct Feed and order submitter
        execute Runner from session start
    except RecoverableBarTimeout or gateway connection failure:
        close both gateways and the Feed
        read the latest single event for this run
        restore the shares queue from that event, or from configuration
        increment and record the recovery count
        wait for the retry interval, unless the session has ended
        continue
    except SQLite persistence failure:
        terminate the process
```

Each retry uses the same `run_id` and starts calculation again from the market
open:

```text
same run_id
-> close the interrupted Feed/subscription
-> establish usable gateway connections
-> obtain Bars from the session start through the recovery point
-> rebuild RuntimeState from its initial state
-> replay Bars in timestamp order
-> resume normal Live processing
```

`raw_1m_bar` and `processed_1m_bar` are replayable data. They may be upserted
for the same run and Bar timestamp. RuntimeState is not reused: Trend, Channel,
Auto Threshold, and summary accumulators are reconstructed by replaying the
same ordered Bars from the session start.

The recovery counter is persisted with the Run and incremented once for each
re-entry into the recovery loop.

Recovery continues until the selected session closes. The retry delay is based
on the recovery count:

```text
first recovery retry:   20 seconds
second recovery retry:  1 minute
third recovery retry:   15 minutes
fourth and later:       1 hour each
```

Before each delayed retry, the process checks the session end. If the session
has ended, it stops retrying and terminates the Run through its normal
session-end path.

## 4. RunContext and Run lifecycle

The existing `RunContext` is reused. It fixes the run identity and calculation
configuration:

```text
run_id, symbol, trade date, parameter set, direction, threshold mode,
initial threshold, run mode, start time, and threshold update rate
```

A recoverable failure does not produce a terminal summary. The same
`single_day_run` remains, or returns to, `RUNNING` before the next Runner
attempt. A summary is produced only when the Run reaches its normal terminal
completion or a non-recoverable failure.

## 5. Single event is durable and is not rebuilt

`single event` is the record of an event that has already occurred. Recovery
does not regenerate, overwrite, or replay it.

During replay from market open:

```text
existing single event
-> preserve it
-> do not submit an order
-> do not consume shares again

no single event for a past Bar
-> recalculate only the strategy/runtime state
-> do not create an event
-> do not submit an order
```

Only a newly observed eligible Live Bar after recovery may create a new single
event and submit an order.

`single event` remains insert-only. It is not an upsert target.

The existing Runner classifies each Bar at consumption time. A Bar is `LIVE`
only when its timestamp is the immediately preceding minute; otherwise it is
`HIST` (with the final session Bar classified as `END`). Recovery adds no
separate replay boundary: Bars that were Live before termination and are
provided again later naturally classify as `HIST`.

## 6. Shares recovery

The current `LiveOrderSubmitter` holds its share-consumption position only in
memory. That is insufficient after a process restart or a newly created
submitter.

Each persisted single event should add these two fields:

```text
share             the quantity consumed by this event; nullable when no share
                  was consumed
remained_shares   JSON array snapshot after this event completed
```

Example:

```json
{
  "share": 10,
  "remained_shares": [10, 10, 10]
}
```

This means the event consumed one `10`, and the remaining queue after it is
`[10, 10, 10]`.

At recovery, the latest persisted single event supplies the shares queue for
the resumed order submitter. If no single event exists for the Run, the
configured initial shares list is used.

The existing implementation creates a `SignalEvent` only for a triggered BUY
or SELL decision. It does not persist other event types. A Live single event
has a null `share` when a share was not consumed: the shares queue was already
exhausted, the order connection was unavailable, or order submission failed
without raising the Runner's terminal first-Bar error.

## 7. Replay and persistence rules

1. Keep the original `run_id`; do not create a replacement run.
2. Reset RuntimeState to the configured initial state.
3. Fetch the selected session from market open and replay Bars in timestamp
   order. Recovery does not add a historical-versus-Live boundary; the existing
   consumption-time source classifier determines each Bar's source.
4. Upsert raw-Bar and processed-Bar data for the same key.
5. Derive/rebuild aggregate statistics from the replay, rather than
   incrementing old aggregate values.
6. Preserve every existing single event unchanged; do not create an event or
   submit an order for a replayed Bar.
7. After replay reaches the recovery point, resume normal Live Feed handling.
   Only a new Live BUY or SELL appends a single event and may consume a share.

Schema upgrade adds `share`, `remained_shares`, and the recovery counter. The
existing schema policy is retained: an incompatible local schema is cleared and
recreated during the one-time upgrade. This is separate from recovery; a
recovery attempt within the upgraded schema does not clear Run data.

Any SQLite write failure terminates the process. It does not enter the recovery
loop.

## 8. Pre-session waiting policy

The process must not retain IBAPI connections while waiting for the market to
open.

```text
connect market gateway
-> read required market/session information
-> connect order gateway
-> read and validate required account information
-> close both gateways
-> wait for market open without an IBAPI connection
-> enter the outer recovery loop
-> first action: establish fresh market and order gateway connections
-> start the Live historical request
```

The connections established after market open are the only connections used by
the Live Feed and Live order submission path. This prevents a pre-session
`1100` from leaving a stale gateway error that later blocks the first Live
request.

If either fresh connection cannot be established at market open, the outer loop
catches that gateway connection failure and applies the same retry policy. It
retries until the session ends using the same `20 seconds -> 1 minute -> 15
minutes -> 1 hour` schedule. Each wait and retry emits an operator-visible
status message.

## 9. Explicit non-goals for this draft

- Reconstructing a cancelled subscription in place.
- Recreating or mutating an existing single event.
- Re-submitting orders for past Bars.
- Treating a database upsert as a way to reverse an external IBKR order.
