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
from pathlib import Path

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
        # 上一个进程留下的文件也要计数：不数就永远到不了触发裁剪的阈值
        self._lines = self._count_lines()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
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

        读不到文件（并发清空 / 权限）时只把计数归零，下一次 emit 重新数——写日志失败
        不该影响业务（``emit`` 的调用方本就按 logging 约定不处理异常）。
        """
        try:
            lines = self.path.read_text(encoding=self.encoding, errors="replace").splitlines(
                keepends=True
            )
        except OSError:
            self._lines = 0
            return
        keep = max(1, int(self.max_lines * TRIM_TO_RATIO))
        if len(lines) <= keep:
            self._lines = len(lines)
            return
        kept = lines[-keep:]
        tmp = self.path.with_name(f".{self.path.name}.trim")
        try:
            tmp.write_text("".join(kept), encoding=self.encoding, newline="")
            tmp.replace(self.path)
        except OSError:
            tmp.unlink(missing_ok=True)
            self._lines = self._count_lines()
            return
        self._lines = len(kept)


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
