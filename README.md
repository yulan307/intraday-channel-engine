# Intraday Channel Engine – Phase 1

Phase 1 implements the project skeleton, domain models, runtime states, and the
pure Regression, Trend, Channel, and Decision algorithms.

**No database, no IBAPI/TWS, no BarFeed implementation, no application runner,
no orders, and no recovery.**

The Core Engine is IO-free: it receives explicit domain inputs and returns
result objects plus next state. `DecisionTransition` keeps a signal bar's
recorded break count separate from the state to apply only after that bar has
been persisted.

See `docs/` for design documents and `CONTEXT` for current state.
