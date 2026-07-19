from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from single_day_test.application.live_cli import connect_with_client_id_retry, new_process_client_ids
from single_day_test.domain.errors import ClientIdInUseError, GatewayConnectionError, PersistenceError
from single_day_test.persistence.database import Database, SqliteRepositories, merge_run_database


def test_process_client_ids_are_positive_and_distinct() -> None:
    values = iter((7, 7, 8))
    assert new_process_client_ids(lambda _low, _high: next(values)) == (7, 8)


def test_client_id_collision_retries_only_the_colliding_connection() -> None:
    attempts: list[int] = []

    class Gateway:
        def __init__(self, client_id: int) -> None: self.client_id = client_id
        def connect_gateway(self, *, require_account: bool) -> None:
            attempts.append(self.client_id)
            if len(attempts) == 1: raise ClientIdInUseError("occupied")
        def disconnect_gateway(self) -> None: pass

    gateway, client_id = connect_with_client_id_retry(Gateway, 4, require_account=False, randint=lambda *_: 9)  # type: ignore[arg-type]
    assert (gateway.client_id, client_id, attempts) == (9, 9, [4, 9])


def test_client_id_collision_exhaustion() -> None:
    class Gateway:
        def __init__(self, _client_id: int) -> None: pass
        def connect_gateway(self, *, require_account: bool) -> None: raise ClientIdInUseError("occupied")
        def disconnect_gateway(self) -> None: pass

    with pytest.raises(GatewayConnectionError, match="32"):
        connect_with_client_id_retry(Gateway, 4, require_account=False, randint=lambda *_: 4)  # type: ignore[arg-type]


def _database(path: Path) -> Database:
    database = Database(path)
    database.initialize()
    return database


def test_completed_private_database_merge_source_wins_and_aligns_columns(tmp_path: Path) -> None:
    master, temporary = _database(tmp_path / "master.sqlite3"), _database(tmp_path / "temp.sqlite3")
    try:
        master.connection.execute("INSERT INTO run_summary (run_id, status, processed_bar_count, signal_count, started_at_epoch, ended_at_epoch) VALUES ('r', 'COMPLETED', 1, 1, 1, 1)")
        temporary.connection.execute("ALTER TABLE run_summary ADD COLUMN phase8_note TEXT")
        temporary.connection.execute("INSERT INTO run_summary (run_id, status, processed_bar_count, signal_count, started_at_epoch, ended_at_epoch, phase8_note) VALUES ('r', 'COMPLETED', 2, 3, 2, 2, 'source')")
        master.connection.commit(); temporary.connection.commit()
    finally:
        master.close(); temporary.close()
    merge_run_database(tmp_path / "master.sqlite3", tmp_path / "temp.sqlite3")
    rows = sqlite3.connect(tmp_path / "master.sqlite3").execute("SELECT processed_bar_count, signal_count, phase8_note FROM run_summary").fetchall()
    assert rows == [(2, 3, "source")]


def test_incompatible_primary_key_rolls_back(tmp_path: Path) -> None:
    master, temporary = _database(tmp_path / "master.sqlite3"), _database(tmp_path / "temp.sqlite3")
    master.close(); temporary.close()
    connection = sqlite3.connect(tmp_path / "temp.sqlite3")
    connection.execute("DROP TABLE run_summary")
    connection.execute("CREATE TABLE run_summary (run_id TEXT, status TEXT, PRIMARY KEY(status))")
    connection.commit(); connection.close()
    with pytest.raises(PersistenceError, match="primary key"):
        merge_run_database(tmp_path / "master.sqlite3", tmp_path / "temp.sqlite3")

