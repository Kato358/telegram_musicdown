"""按行数封顶的文件日志（日志页的数据源，FR-WEB-06）。

为什么不用 ``RotatingFileHandler``：日志页只做 tail（读末尾窗口），而 10MB × 5 的轮转在
长期运行的容器里会攒下最多 50MB 磁盘与五个再也不会更新的 ``app.log.N``。需求是「只保留
最近 500 行」，故这里按**行数**封顶。代价是**没有历史**——要留证请先在日志页下载文件。

如何封顶：追加写；一旦行数超过 ``max_lines`` 就重写文件、只留最后 ``TRIM_TO_RATIO`` 那一
截（经同目录临时文件原子替换）。回落而不是刚好留满，是为了把写放大从「写满一条就重写一
次全文件」摊成每 1/5 上限行一次；文件行数因此**恒不超过 max_lines**（在 400–500 行之间
波动）。``logging.Handler.handle`` 已用 ``self.lock`` 串行化 ``emit``，计数与重写之间不会
被别的线程插入。
"""

from __future__ import annotations

import logging
from contextlib import suppress
from pathlib import Path
from typing import TextIO

#: 默认保留行数（编码规范 §2.5）。
DEFAULT_MAX_LINES = 500

#: 裁剪时回落到上限的多少比例（0.8 = 每次只裁掉 20%，少做全文件重写）。
TRIM_TO_RATIO = 0.8


class RecentLinesFileHandler(logging.Handler):
    """追加写日志，并保证文件不超过 ``max_lines`` 行。"""

    def __init__(
        self,
        path: Path,
        max_lines: int = DEFAULT_MAX_LINES,
        encoding: str = "utf-8",
    ) -> None:
        super().__init__()
        self.path = Path(path)
        self.max_lines = max(1, max_lines)
        self.encoding = encoding
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 追加句柄常开：每条记录都 open/close 一次，在高频 INFO（连接类事件）下是
        # 纯 SysCall 浪费。打不开（只读目录等）降级成 emit 内逐次 open，行为与旧版一致。
        self._fh: TextIO | None = self._open()
        # 上一个进程留下的文件也要计数：不数就永远到不了触发裁剪的阈值
        self._lines = self._count_lines()

    def _open(self) -> TextIO | None:
        try:
            return self.path.open("a", encoding=self.encoding, newline="")
        except OSError:
            return None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
            if self._fh is not None:
                self._fh.write(text + "\n")
                self._fh.flush()
            else:
                with self.path.open("a", encoding=self.encoding, newline="") as fh:
                    fh.write(text + "\n")
            self._lines += text.count("\n") + 1  # 异常记录是多行一条，按行计
            if self._lines > self.max_lines:
                self._trim()
        except Exception:  # noqa: BLE001  logging 约定：emit 内异常交 handleError，不外抛
            self.handleError(record)

    def _count_lines(self) -> int:
        """当前文件的行数；文件不在（首次启动 / 刚被清空）按 0。"""
        try:
            with self.path.open("rb") as fh:
                return fh.read().count(b"\n")
        except OSError:
            return 0

    def _trim(self) -> None:
        """把文件裁到 ``TRIM_TO_RATIO × max_lines`` 行（只留最新的那些行）。

        Windows 上「还开着的追加句柄」会让同目录的 ``replace`` 失败（文件被占用），
        所以裁剪全程先收句柄、重写完再打开；读不到文件（并发清空 / 权限）时只把计数
        归零，下一次 emit 重新数——写日志失败不该影响业务（``emit`` 的调用方本就按
        logging 约定不处理异常）。
        """
        # 先收句柄：否则 tmp.replace(self.path) 在 Windows 上必然占用失败
        self._close_fh()
        try:
            lines = self.path.read_text(encoding=self.encoding, errors="replace").splitlines(
                keepends=True
            )
        except OSError:
            self._lines = 0
            self._fh = self._open()
            return
        keep = max(1, int(self.max_lines * TRIM_TO_RATIO))
        if len(lines) <= keep:
            self._lines = len(lines)
            self._fh = self._open()
            return
        kept = lines[-keep:]
        tmp = self.path.with_name(f".{self.path.name}.trim")
        try:
            tmp.write_text("".join(kept), encoding=self.encoding, newline="")
            tmp.replace(self.path)
        except OSError:
            tmp.unlink(missing_ok=True)
            self._lines = self._count_lines()
            self._fh = self._open()
            return
        self._lines = len(kept)
        self._fh = self._open()

    def _close_fh(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            with suppress(OSError):
                fh.close()

    def close(self) -> None:
        self._close_fh()
        super().close()


def drop_legacy_rotations(logs_dir: Path) -> int:
    """删掉旧版旋转日志（``app.log.1`` … ``app.log.N``），返回删除个数。

    改成按行封顶后这些文件再没有写入者：留着只是磁盘占用，也让人以为「还有更早的历史」。
    只认 ``app.log.<数字>`` 这个 ``RotatingFileHandler`` 的命名，别的一律不碰；删不掉的
    （别处打开着）不该拦住启动。
    """
    removed = 0
    for path in sorted(Path(logs_dir).glob("app.log.*")):
        if not path.name.rsplit(".", 1)[-1].isdigit():
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed += 1
    return removed
