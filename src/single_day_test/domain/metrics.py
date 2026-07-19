from __future__ import annotations

from collections.abc import Sequence

from .enums import Direction


def calculate_position_rewards(
    *,
    direction: Direction,
    first_threshold: float | None,
    best_price: float | None,
    order_prices: Sequence[float],
) -> tuple[float | None, float | None]:
    """Return first-trigger and planned-full-position rewards for Backtest."""
    if not order_prices:
        return 0.0, 0.0
    if first_threshold is None or best_price is None:
        return None, None

    if direction is Direction.BUY:
        denominator = first_threshold - best_price
        numerators = (first_threshold - price for price in order_prices)
    else:
        denominator = best_price - first_threshold
        numerators = (price - first_threshold for price in order_prices)
    if denominator <= 0:
        return None, None

    signal_rewards = [
        max(0.0, min(1.0, numerator / denominator))
        for numerator in numerators
    ]
    full_position_reward = sum(
        reward / (2 ** index)
        for index, reward in enumerate(signal_rewards, start=1)
    )
    return signal_rewards[0], full_position_reward
