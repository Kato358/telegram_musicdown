"""行数封顶的文件日志（编码规范 §2.5、FR-WEB-06）：只保留最近 500 行。"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.utils.logfile import DEFAULT_MAX_LINES, RecentLinesFileHandler, drop_legacy_rotations


@pytest.fixture(autouse=True)
def _close_test_handlers() -> Iterator[None]:
    """句柄常开是实现的常态：用例结束必须 close，否则 GC 时 ResourceWarning 一片。"""
    yield
    for logger in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(logger, logging.Logger) and logger.name.startswith("test.logfile."):
            for handler in logger.handlers:
                handler.close()
            logger.handlers.clear()


def _logger(path: Path, max_lines: int) -> logging.Logger:
    """挂一个行数封顶 handler 的独立 logger（不向外传播，别污染别的用例）。"""
    logger = logging.getLogger(f"test.logfile.{path.name}.{max_lines}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handler = RecentLinesFileHandler(path, max_lines=max_lines)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_file_never_grows_beyond_the_line_cap(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    logger = _logger(path, DEFAULT_MAX_LINES)

    for i in range(1200):
        logger.info("line %d", i)

    lines = _lines(path)
    # 上限是硬的：文件行数任何时候都不超过 500
    assert len(lines) <= DEFAULT_MAX_LINES, f"文件有 {len(lines)} 行，超过上限"
    # 最新一条必须留着，最早的一条必须已经不在
    assert lines[-1] == "line 1199"
    assert "line 0" not in lines
    # 留下的必须是**连续**的一段尾巴（不是东一条西一条）
    assert lines[0] == f"line {1200 - len(lines)}"


def test_existing_file_is_counted_so_the_cap_applies_immediately(tmp_path: Path) -> None:
    # 上一个进程留下的文件（或旧版旋转日志）也要计入：不数它，新进程就不会触发裁剪
    path = tmp_path / "app.log"
    path.write_text("".join(f"old {i}\n" for i in range(1500)), encoding="utf-8")

    logger = _logger(path, DEFAULT_MAX_LINES)
    logger.info("fresh")

    lines = _lines(path)
    assert len(lines) <= DEFAULT_MAX_LINES
    assert lines[-1] == "fresh"
    assert "old 0" not in lines
    assert lines[0] == f"old {1500 - (len(lines) - 1)}"


def test_traceback_record_counts_every_line_it_writes(tmp_path: Path) -> None:
    # 异常记录是多行一条：按行封顶必须把 traceback 的每一行都算进去
    path = tmp_path / "app.log"
    logger = _logger(path, max_lines=10)

    for i in range(6):
        try:
            raise ValueError(f"boom {i}")
        except ValueError:
            logger.exception("failed %d", i)

    text = path.read_text(encoding="utf-8")
    assert text.count("\n") <= 10
    assert "boom 5" in text  # 最新那条完整保留
    assert "boom 0" not in text  # 最早的已被裁掉


def test_legacy_rotations_are_dropped_but_current_file_survives(tmp_path: Path) -> None:
    (tmp_path / "app.log").write_text("current\n", encoding="utf-8")
    (tmp_path / "app.log.1").write_text("older\n", encoding="utf-8")
    (tmp_path / "app.log.2").write_text("oldest\n", encoding="utf-8")
    (tmp_path / "app.log.keep").write_text("not a rotation\n", encoding="utf-8")

    assert drop_legacy_rotations(tmp_path) == 2

    assert (tmp_path / "app.log").read_text(encoding="utf-8") == "current\n"
    assert not (tmp_path / "app.log.1").exists()
    assert not (tmp_path / "app.log.2").exists()
    assert (tmp_path / "app.log.keep").exists()
