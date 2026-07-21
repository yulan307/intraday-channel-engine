# Draft: Algorithm Improvement and Hyperparameter Reduction

> Status: discussion draft only. This document describes review findings and
> proposed experiments; it does not change the implemented strategy contract.

## 1. Current Algorithm Summary

The current strategy pipeline is:

1. `TrendEngine` calculates HLC3 and fits a rolling ordinary least-squares
   regression.
2. A raw trend is classified as UP, DOWN, or SIDEWAY from the regression R2,
   slope, and the rolling standard deviation of fitted slopes.
3. `ChannelEngine` maintains a channel segment for the effective trend. It
   freezes a prior-segment model on a trend change, calculates a delayed-current
   model, and blends the two prediction channels.
4. `DecisionEngine` requires a prediction-channel break, threshold condition,
   slope gate, and rearm-state gates before a BUY or SELL signal is emitted.
5. Auto Threshold updates the threshold after a triggered signal for the next
   Bar.

The delayed-current and frozen-prior channel design is worth retaining. It
prevents a new channel segment from immediately replacing the prior structure.

## 2. Current Hyperparameters

The explicit controls are:

| Control | Current role |
| --- | --- |
| `trend_window` | Trend regression history and rolling slope history |
| `channel_window` | Maximum channel-history length |
| `r2_threshold` | Minimum trend-fit quality |
| `channel_high_percentile` | Upper channel residual percentile |
| `channel_low_percentile` | Lower channel residual percentile |
| `continuous_break_count` | Consecutive qualifying Bars required for a signal |
| `curr_mix_ratio` | Maximum current-channel blend weight |
| `threshold_update_rate` | Auto Threshold update percentage |
| fixed or initial threshold | Entry-price gate or Auto Threshold starting point |

There is also a hidden fixed control: the normalized mix sigmoid uses a
steepness of `4.0`.

## 3. Review Findings

### 3.1 Parameter experiment hygiene must come first

The local `configs/parameter_set.csv` contains naming inconsistencies: rows
labelled `p85` currently use percentile value `90`, and
`trend10_high_r2_breakp85` occurs more than once. Parameter selection and
result comparison should not be trusted until the parameter registry has a
unique identifier and an accurate, immutable parameter snapshot.

### 3.2 Trend classification is sensitive to window and regime

The current trend gate combines a fixed R2 threshold with a comparison between
the fitted slope and the standard deviation of overlapping rolling slopes.
Both measures change with the chosen window, sampling interval, and volatility
regime. Values such as R2 `0.6` or `0.8` therefore do not transfer reliably
between symbols or market regimes.

### 3.3 Fixed residual percentiles do not stabilize break frequency

The channel uses fixed residual percentiles, currently commonly 90 or 95.
Their observed break frequency can vary substantially with volatility and
microstructure conditions. A percentile is understandable, but it should be
expressed as an explicit target coverage or target false-break rate rather than
as an independently optimized number for each parameter set.

### 3.4 Segment changes are coupled to a noisy categorical label

An UP, DOWN, or SIDEWAY label change freezes the current Channel model and
starts a new segment. This makes the Channel boundary sensitive to the Trend
gate's windows and thresholds. Short-lived label changes can create excessive
segment switches.

### 3.5 The current evaluation metric is diagnostic, not executable PnL

Backtest now records three diagnostics. `first_reward` measures the first signal
price, `second_reward` measures only the second signal price, and `reward` is
their average when both exist (or equals `first_reward` when only one exists).
Later signals do not affect these metrics. They still use completed-day data and
are not tradable return metrics. They must not be the sole selection objective
for live parameters.

## 4. Reference Algorithm Families

| Need | Candidate method | Why it fits this architecture |
| --- | --- | --- |
| Robust trend fit | Huber regression or Theil-Sen regression | Replaces OLS sensitivity to exceptional one-minute Bars while preserving a slope-based interface. |
| Regime-normalized trend confidence | Slope t-statistic, confidence interval, or volatility-normalized slope | Gives a significance-like trend strength instead of a fixed R2 threshold. |
| Adaptive channel width | EWMA/MAD residual scale, rolling residual quantile, or conformal prediction interval | Keeps prediction-channel output while targeting an interpretable coverage level. |
| Segment boundary | CUSUM or change-point detection with hysteresis | Avoids restarting a Channel segment for every one-Bar trend-label change. |
| Continuous trend state | Kalman local-linear-trend model | Estimates slope and uncertainty online without a hard fixed lookback. |
| Reduced window dependence | Online expert ensemble across a small set of half-lives | Uses recent prediction performance to weight scales instead of selecting one permanent window. |
| Consecutive-break replacement | Sequential probability ratio test or accumulated standardized exceedance | Controls evidence accumulation through an interpretable error-rate budget. |

## 5. Recommended Direction

The recommended near-term approach is not a wholesale machine-learning rewrite.
Use a causal, robust statistical pipeline that preserves the current outer
runtime contract:

```text
CompletedBar
  -> robust or normalized trend estimate
  -> adaptive prediction interval
  -> stable change-point / rearm state
  -> signal decision
  -> existing persistence and live/backtest flows
```

### Stage A: Make selection evidence reliable

1. Correct parameter IDs and values, and reject duplicate IDs.
2. Record immutable parameter snapshots with every run.
3. Evaluate through chronological walk-forward folds, never random splits.
4. Use executable assumptions: next available price, fees, slippage, position
   lifecycle, drawdown, turnover, and no-trade rate.
5. Prefer configurations that remain acceptable across folds and symbols over
   the single highest in-sample result.

### Stage B: Replace fixed trend gates

Replace `r2_threshold` plus raw slope-standard-deviation comparison with one
of the following, tested behind a versioned strategy seam:

- robust slope divided by EWMA volatility or robust MAD scale; or
- regression slope t-statistic / lower confidence bound.

This reduces the need to retune R2 per symbol and makes the trigger comparable
across volatility regimes.

### Stage C: Turn channel width into a coverage objective

Use the residual history available strictly before the decision Bar to build a
rolling or conformal prediction interval. Retain separate upper and lower
residual distributions when asymmetry is material. The principal setting then
becomes a target coverage or false-break budget, rather than a grid of static
percentiles.

### Stage D: Stabilize segment transitions

Introduce CUSUM or a simple hysteresis rule before freezing and resetting a
Channel model. For example, a state transition requires accumulated
volatility-normalized evidence rather than one categorical trend-label change.

### Stage E: Only then test adaptive multi-scale estimation

Run a small fixed bank of causal estimators, such as 5-, 15-, and 30-minute
half-lives. Weight them by recent out-of-sample forecast loss, or require
agreement before issuing a signal. This keeps the number of live controls small
and avoids permanently committing to `trend_window = 10` or `30`.

## 6. Guardrails for an Experiment

- Keep `BarFeed -> Runner -> persistence` and live/backtest entry points
  unchanged.
- Add a versioned strategy seam so existing and candidate logic can replay the
  same completed Bars.
- Compare the first divergence and intermediate trend, channel, and decision
  states; do not compare final reward only.
- Run deterministic historical replay first, then no-order shadow live.
- Promote only a candidate that is stable across chronological folds, symbols,
  and volatility regimes after realistic execution assumptions.

## 7. Initial Recommendation

The smallest high-value experiment is:

1. Correct the parameter registry and replace the metric with walk-forward,
   execution-aware evaluation.
2. Keep the existing Channel blending logic.
3. Replace the Trend gate with a volatility-normalized robust slope.
4. Replace the fixed channel percentile with a causal adaptive residual
   interval.
5. Add change-point hysteresis only if segment-switch analysis confirms that it
   is the main source of instability.

This sequence removes the most fragile fixed controls while keeping comparison,
rollback, and live safety practical.
