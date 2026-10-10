"""日志路由（FR-WEB-06）：运行日志读取 / 下载 / 清空。

事实源是 ``logs/`` 下的滚动文件（app.log + 旋转备份）；SSE 的 ``log.error``
只做「连接期间的实时提示」，历史以文件为准。文件读取放线程池，别堵事件循环。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.web.routes import schemas
from app.web.routes.context import RouteContext

logger = logging.getLogger(__name__)

# setup_logging 的行格式："%(asctime)s %(levelname)s %(name)s %(message)s"；
# 不匹配的行（traceback 续行、第三方多行输出）并入上一条的 message。
_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) (CRITICAL|ERROR|WARNING|INFO|DEBUG) (\S+) (.*)"
)
_WINDOW_BYTES = 512 * 1024  # 默认只读文件末尾窗口（日志页本就是 tail 场景，轮询友好）
_MAX_WINDOW_BYTES = 4 * 1024 * 1024
_MAX_ENTRIES = 4000  # 单次响应条数上限，防超大窗口打爆前端


def parse_entries(text: str) -> list[schemas.LogEntry]:
    """日志文本 → 条目列表；续行并入上一条，时间戳为本地时间的 epoch 秒。"""
    entries: list[schemas.LogEntry] = []
    for line in text.splitlines():
        m = _LINE.match(line)
        if m is None:
            if entries:
                entries[-1].message += "\n" + line
            continue
        stamp, level, name, message = m.groups()
        try:
            ts = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S,%f"))
        except ValueError:
            ts = 0.0
        entries.append(schemas.LogEntry(ts=ts, level=level, logger=name, message=message))
    return entries


def tail_text(path: Path, max_bytes: int) -> tuple[str, bool]:
    """读文件末尾 ≤max_bytes；返回 (文本, 是否截掉了文件开头)。

    文件还不存在（handler 惰性建文件）按空日志处理，不算错误。
    """
    if not path.exists():  # noqa: ASYNC240  路由级存在性检查非热路径
        return "", False
    size = path.stat().st_size
    start = max(0, size - max_bytes)
    with path.open("rb") as f:
        f.seek(start)
        text = f.read().decode("utf-8", errors="replace")
    if start > 0:
        # 丢弃不完整的半行，从下一个完整行开始
        nl = text.find("\n")
        text = text[nl + 1 :] if nl >= 0 else ""
    return text, start > 0


def list_log_files(logs_dir: Path) -> list[schemas.LogFile]:
    """logs/ 下的 app.log 与旋转备份；当前文件排最前，其余按名字（代数）序。"""
    files: list[schemas.LogFile] = []
    for p in logs_dir.glob("app.log*"):
        try:
            st = p.stat()
        except OSError:
            continue
        files.append(schemas.LogFile(name=p.name, size=st.st_size, mtime=st.st_mtime))
    files.sort(key=lambda f: (f.name != "app.log", f.name))
    return files


def resolve_log_file(logs_dir: Path, name: str) -> Path:
    """白名单校验：只认当前日志文件名或 app.log 本身（防 ?file= 路径穿越，NFR-02）。

    app.log 即使还没落盘也放行（首次启动尚未写日志时，读取返回空、清空负责建文件）。
    """
    allowed = {f.name for f in list_log_files(logs_dir)} | {"app.log"}
    if name not in allowed:
        raise HTTPException(status_code=404, detail="log file not found")
    return logs_dir / name


def register(app: FastAPI, ctx: RouteContext) -> None:
    """注册日志路由。"""
    logs_dir = ctx.base_dir / "data" / "logs"

    @app.get("/api/logs")
    async def logs_ep(
        file: str = "app.log",
        window: int = _WINDOW_BYTES,
        _: None = Depends(ctx.check_session),
    ) -> schemas.LogsResponse:
        """日志尾部：解析成条目返回，前端再做级别/关键字筛选（避免轮询重取）。"""
        path = resolve_log_file(logs_dir, file)
        window = max(1, min(window, _MAX_WINDOW_BYTES))
        text, truncated = await asyncio.to_thread(tail_text, path, window)
        entries = parse_entries(text)
        if len(entries) > _MAX_ENTRIES:
            entries = entries[-_MAX_ENTRIES:]
            truncated = True
        return schemas.LogsResponse(
            entries=entries,
            files=await asyncio.to_thread(list_log_files, logs_dir),
            active_file=path.name,
            file_size=path.stat().st_size if path.exists() else 0,  # noqa: ASYNC240  单次 stat
            truncated=truncated,
        )

    @app.get("/api/logs/download")
    async def logs_download(
        file: str = "app.log", _: None = Depends(ctx.check_session)
    ) -> FileResponse:
        """原始日志文件下载（附件）；文件还不存在 → 404。"""
        path = resolve_log_file(logs_dir, file)
        if not path.exists():  # noqa: ASYNC240  路由级存在性检查非热路径
            raise HTTPException(status_code=404, detail="log file not found")
        return FileResponse(path, media_type="text/plain; charset=utf-8", filename=path.name)

    @app.delete("/api/logs")
    async def logs_clear(_: None = Depends(ctx.check_session)) -> dict[str, bool]:
        """清空当前日志（truncate app.log）。

        ``RecentLinesFileHandler`` 以 append 模式打开，外部 truncate 后写入仍会追加到
        文件末尾，不需要重启进程；handler 的行计数会偏高一次，下一次写记录时按文件实际
        内容重数（见 ``app.utils.logfile``）。
        """

        def _truncate() -> None:
            path = resolve_log_file(logs_dir, "app.log")
            with path.open("w", encoding="utf-8"):
                pass

        await asyncio.to_thread(_truncate)
        logger.info("web log cleared")
        return {"ok": True}
