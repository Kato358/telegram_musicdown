"""仓储连接契约：按线程独立、busy_timeout 生效、close 收口全部连接。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from app.db.store import Store
from app.db.store.base import BUSY_TIMEOUT_MS


def _worker_connection(store: Store) -> sqlite3.Connection:
    seen: list[sqlite3.Connection] = []

    def worker() -> None:
        seen.append(store._conn)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert len(seen) == 1
    return seen[0]


def test_connection_is_per_thread(tmp_path: Path) -> None:
    # loop 线程与 to_thread 工作线程各持一条连接：多语句事务不再跨线程交错
    store = Store(tmp_path / "app.db")
    main_conn = store._conn
    worker_conn = _worker_connection(store)
    assert worker_conn is not main_conn
    store.close()


def test_same_thread_reuses_one_connection(tmp_path: Path) -> None:
    store = Store(tmp_path / "app.db")
    assert store._conn is store._conn
    store.close()


def test_busy_timeout_is_enforced(tmp_path: Path) -> None:
    # 第二个连接抢不到写锁时要等，而不是立刻 SQLITE_BUSY
    store = Store(tmp_path / "app.db")
    row = store._conn.execute("PRAGMA busy_timeout").fetchone()
    assert row is not None
    assert int(row[0]) == BUSY_TIMEOUT_MS
    store.close()


def test_per_thread_connections_share_the_same_data(tmp_path: Path) -> None:
    store = Store(tmp_path / "app.db")
    worker_conn = _worker_connection(store)
    store.set_setting("probe", "from-main")
    assert store.get_setting("probe") == "from-main"
    row = worker_conn.execute("SELECT value FROM settings WHERE key='probe'").fetchone()
    assert row is not None
    assert row[0] == "from-main"
    store.close()


def test_close_releases_every_thread_connection(tmp_path: Path) -> None:
    store = Store(tmp_path / "app.db")
    main_conn = store._conn
    worker_conn = _worker_connection(store)
    store.close()
    with pytest.raises(sqlite3.ProgrammingError):
        main_conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        worker_conn.execute("SELECT 1")


def test_migration_is_idempotent_and_versioned(tmp_path: Path) -> None:
    # 重复开库（同一个路径再来一个 Store）不重放迁移、版本保持最新
    first = Store(tmp_path / "app.db")
    first.set_setting("kept", "yes")
    first.close()

    second = Store(tmp_path / "app.db")
    assert second.get_setting("kept") == "yes"
    row = second._conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    assert row is not None
    assert int(row[0]) == 7  # migrations/007_chksz_providers.sql 是当前最新
    second.close()
