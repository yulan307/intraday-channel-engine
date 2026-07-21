from __future__ import annotations

from collections.abc import Sequence

from .enums import Direction


def calculate_position_rewards(
    *,
    direction: Direction,
    first_threshold: float | None,
    best_price: float | None,
    order_prices: Sequence[float],
) -> tuple[float | None, float | None, float | None]:
    """Return first, second, and daily rewards for the first two Backtest signals."""
    if not order_prices:
        return 0.0, 0.0, 0.0
    if first_threshold is None or best_price is None:
        return None, None, None

    selected_order_prices = order_prices[:2]
    if direction is Direction.BUY:
        denominator = first_threshold - best_price
        numerators = (first_threshold - price for price in selected_order_prices)
    else:
        denominator = best_price - first_threshold
        numerators = (price - first_threshold for price in selected_order_prices)
    if denominator <= 0:
        return None, None, None

    signal_rewards = [
        max(0.0, min(1.0, numerator / denominator))
        for numerator in numerators
    ]
    first_reward = signal_rewards[0]
    if len(signal_rewards) == 1:
        return first_reward, None, first_reward
    second_reward = signal_rewards[1]
    return first_reward, second_reward, (first_reward + second_reward) / 2
