from __future__ import annotations

from ibapi import server_versions

from single_day_test.ib.gateway import IbApiGateway


def test_installed_ibapi_supports_historical_schedule() -> None:
    assert getattr(server_versions, "MAX_CLIENT_VER", 0) >= getattr(server_versions, "MIN_SERVER_VER_HISTORICAL_SCHEDULE", 165)
    IbApiGateway._ensure_schedule_client_version()
