from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.enums import FeedStatus
from ..domain.models import CompletedBar
from ..domain.errors import InputValidationError


@dataclass(frozen=True)
class FeedEvent:
    status: FeedStatus
    bar: CompletedBar | None

    def __post_init__(self) -> None:
        if self.status is FeedStatus.BAR_AVAILABLE and self.bar is None:
            raise InputValidationError(
                "BAR_AVAILABLE requires a non-None bar"
            )
        if self.status in (FeedStatus.BAR_WAITING, FeedStatus.BAR_END) and self.bar is not None:
            raise InputValidationError(
                f"{self.status.value} requires bar=None, got {self.bar}"
            )


class BarFeed(Protocol):
    def start(self) -> None:
        ...

    def next_event(self) -> FeedEvent:
        ...

    def close(self) -> None:
        ...
