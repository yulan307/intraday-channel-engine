class SingleDayTestError(Exception):
    pass


class InputValidationError(SingleDayTestError):
    pass


class NonTradingDayError(SingleDayTestError):
    pass


class InvalidTradeDateError(SingleDayTestError):
    pass


class IbApiError(SingleDayTestError):
    pass


class GatewayConnectionError(IbApiError):
    pass


class ClientIdInUseError(GatewayConnectionError):
    pass


class LiveProcessAlreadyRunningError(InputValidationError):
    pass


class HistoricalDataError(SingleDayTestError):
    pass


class RecoverableBarTimeout(HistoricalDataError):
    pass


class BarValidationError(SingleDayTestError):
    pass


class BarOrderingError(SingleDayTestError):
    pass


class AlgorithmError(SingleDayTestError):
    pass


class PersistenceError(SingleDayTestError):
    pass
