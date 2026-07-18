from __future__ import annotations


def calculate_reward_efficiency(
    *,
    first_threshold: float | None,
    signal_count: int,
    best_price: float | None,
    best_order_price: float | None,
) -> tuple[float | None, float | None]:
    """Return the capped threshold-distance reward and signal-count penalty."""
    if signal_count == 0:
        return 0.0, 0.0
    if (
        first_threshold is None
        or best_price is None
        or best_order_price is None
    ):
        return None, None
    denominator = abs(best_price - first_threshold)
    if denominator == 0:
        return None, None
    reward = min(1.0, abs(best_order_price - first_threshold) / denominator)
    return reward, reward ** signal_count
