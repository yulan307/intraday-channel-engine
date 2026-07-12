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


class HistoricalDataError(SingleDayTestError):
    pass


class BarValidationError(SingleDayTestError):
    pass


class BarOrderingError(SingleDayTestError):
    pass


class AlgorithmError(SingleDayTestError):
    pass


class PersistenceError(SingleDayTestError):
    pass
