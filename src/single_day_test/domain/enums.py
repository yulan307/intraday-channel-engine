from enum import Enum


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class RunMode(str, Enum):
    BACKTEST = "BACKTEST"
    LIVE_PAPER = "LIVE_PAPER"


class LivePhase(str, Enum):
    PRE_MARKET_WAIT = "PRE_MARKET_WAIT"
    IN_SESSION = "IN_SESSION"


class BarSource(str, Enum):
    HIST = "HIST"
    LIVE = "LIVE"


class FeedStatus(str, Enum):
    BAR_AVAILABLE = "bar_available"
    BAR_WAITING = "bar_waiting"
    BAR_END = "bar_end"


class TrendLabel(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    SIDEWAY = "SIDEWAY"


class DecisionLabel(str, Enum):
    BUY = "BUY"
    NO_BUY = "NO_BUY"
    SELL = "SELL"
    NO_SELL = "NO_SELL"


class RunStatus(str, Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
