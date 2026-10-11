"""仓储基础设施：连接、迁移与时间戳工具。

- 连接按线程独立（见 ``_Base``）：loop 线程与 ``to_thread`` 工作线程各持一条
  sqlite3 连接，多语句事务不再跨线程交错；WAL 下多连接读写互不阻塞，写锁由
  ``busy_timeout`` 排队等待。
- 迁移自立一条 autocommit 连接（见 ``_Base._migrate``）：每个文件与它的
  schema_version 记档包在同一个事务里，崩在中间不会留下「文件应用了、版本没记」
  的中间态。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("app.db.store")

#: 写锁等待上限（毫秒）：第二个连接抢不到写锁时等这么久，而不是立刻 SQLITE_BUSY。
BUSY_TIMEOUT_MS = 5000


def utcnow() -> str:
    """UTC ISO-8601 文本（store 约定）。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


class _Base:
    """连接、迁移与关闭：各聚合 mixin 的共同基类（不单独实例化）。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self._migrate()

    @property
    def _conn(self) -> sqlite3.Connection:
        """当前线程的连接；首用即建。

        按线程独立而不是全实例共享一条：store 的写路径用 ``with self._conn:``
        开多语句事务，loop 线程（async 路由直调）与 ``to_thread`` 工作线程
        （回填/淘汰/曲库扫描）若共享一条连接，两个线程的语句会落进同一个事务，
        一边回滚另一边跟着遭殃。WAL 模式本就是为多连接设计的：读读不互斥、
        写写由 busy_timeout 排队。
        """
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            # check_same_thread=False：连接不会易手，保留只为历史上的宽松语义
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
            with self._conns_lock:
                self._conns.append(conn)
        return conn

    def _migrate(self) -> None:
        """顺序执行 app/db/migrations/NNN_*.sql 并记 schema_version。

        自立一条 ``isolation_level=None`` 连接：``executescript`` 会先提交挂起的
        事务，所以事务边界不能靠外层包裹，只能写进脚本文本——每个文件和它的版本
        记档在同一个 ``BEGIN IMMEDIATE … COMMIT`` 里，要么都生效要么都不生效。
        重跑安全性由版本号保证；并发启动（多进程共享同一 data/ 卷）由写锁排队。
        """
        # 本文件在 app/db/store/ 下，迁移目录仍归 app/db/ 所有
        mig_dir = Path(__file__).resolve().parents[1] / "migrations"
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            current = int(row[0]) if row else 0
            for sql_file in sorted(mig_dir.glob("*.sql")):
                version = int(sql_file.name.split("_", 1)[0])
                if version <= current:
                    continue
                logger.info("applying migration %s", sql_file.name)
                body = sql_file.read_text(encoding="utf-8").rstrip()
                if not body.endswith(";"):
                    body += ";"
                conn.executescript(
                    "BEGIN IMMEDIATE;\n"
                    f"{body}\n"
                    "DELETE FROM schema_version;\n"
                    f"INSERT INTO schema_version (version) VALUES ({version});\n"
                    "COMMIT;"
                )
        finally:
            conn.close()

    def close(self) -> None:
        """关闭本实例创建的全部按线程连接（含各 to_thread 工作线程持的那些）。"""
        with self._conns_lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                logger.exception("closing store connection failed")
