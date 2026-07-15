from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ..domain.enums import Direction
from ..domain.errors import IbApiError
from ..support.logging import StructuredLogger


class OrderGateway(Protocol):
    def connect_gateway(self, *, require_account: bool = False) -> None: ...
    def disconnect_gateway(self) -> None: ...
    def is_connected(self) -> bool: ...
    def submit_market_order(self, symbol: str, action: str, quantity: int) -> int: ...


class LiveOrderSubmitter:
    """Owns the in-memory Phase 7 submission budget and order connection policy."""

    def __init__(self, gateway: OrderGateway, shares: tuple[int, ...], logger: StructuredLogger | None = None) -> None:
        self.gateway = gateway
        self.shares = shares
        self.logger = logger
        self._next_share_index = 0

    @property
    def has_remaining_shares(self) -> bool:
        return self._next_share_index < len(self.shares)

    @property
    def current_quantity(self) -> int | None:
        return self.shares[self._next_share_index] if self.has_remaining_shares else None

    def _info(self, event: str, **fields: object) -> None:
        if self.logger is not None:
            self.logger.info(event, **fields)

    def _error(self, event: str, **fields: object) -> None:
        if self.logger is not None:
            self.logger.error(event, **fields)

    def _connect_attempts(self, attempts: int, stage: str, *, raise_on_failure: bool) -> bool:
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                self.gateway.disconnect_gateway()
                self.gateway.connect_gateway(require_account=True)
                self._info("order_connection_ready", stage=stage, attempt=attempt)
                return True
            except Exception as exc:
                last_error = exc
                self._error(
                    "order_connection_failed",
                    stage=stage,
                    attempt=attempt,
                    attempts=attempts,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
        if raise_on_failure:
            raise IbApiError(f"Order connection failed after {attempts} attempts during {stage}") from last_error
        return False

    def start(self) -> None:
        self._connect_attempts(3, "startup", raise_on_failure=True)

    def recover_after_first_bar(self) -> bool:
        if self.gateway.is_connected():
            return True
        return self._connect_attempts(3, "post_first_bar", raise_on_failure=False)

    def submit(self, symbol: str, direction: Direction, *, raise_on_error: bool) -> bool:
        quantity = self.current_quantity
        if quantity is None:
            self._info("order_submission_skipped", symbol=symbol, reason="shares_exhausted")
            return False
        if not self.gateway.is_connected() and not self._connect_attempts(1, "pre_submission", raise_on_failure=False):
            self._error("order_submission_skipped", symbol=symbol, quantity=quantity, reason="order_connection_unavailable")
            return False
        try:
            order_id = self.gateway.submit_market_order(symbol, direction.value, quantity)
        except Exception as exc:
            self._error(
                "order_submission_failed",
                symbol=symbol,
                action=direction.value,
                quantity=quantity,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            if raise_on_error:
                raise
            return False
        self._next_share_index += 1
        self._info(
            "order_submitted",
            order_id=order_id,
            symbol=symbol,
            action=direction.value,
            quantity=quantity,
            remaining_execution_count=len(self.shares) - self._next_share_index,
        )
        return True
