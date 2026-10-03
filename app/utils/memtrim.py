"""闲置内存回收：周期 gc + glibc malloc_trim，把空闲块还给 OS（Docker RSS 优化）。

背景：单进程 asyncio 应用过完下载/搜索峰值后，Python 的空闲 arena 与 glibc 的
线程堆空闲块（大量 ``asyncio.to_thread`` 会开多 arena）仍常驻 RSS——``docker stats``
里的闲置占用因此只涨不跌。``malloc_trim(0)`` 让 glibc 把顶层空闲内存 ``madvise``
回内核；pymalloc 释放后的空 arena 在 3.11 上随 ``gc.collect()`` 后可被归还。

非 glibc 平台（Windows / musl）没有 ``malloc_trim``：静默跳过，gc 仍生效。
"""

from __future__ import annotations

import ctypes
import gc
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

# 周期取 15 分钟：trim 本身是亚毫秒级操作，不值得更勤；闲置占用是慢变量
TRIM_INTERVAL_SEC = 15 * 60


_TrimFn = Callable[[int], int]


def _load_malloc_trim() -> _TrimFn | None:
    try:
        libc = ctypes.CDLL("libc.so.6")
        return libc.malloc_trim
    except (OSError, AttributeError):  # Windows / musl：没有该符号
        return None



async def run_memory_trim_loop(interval: float = TRIM_INTERVAL_SEC) -> None:
    """周期回收循环：gc.collect() + malloc_trim。由 ``app.__main__.run`` 作后台任务。"""
    import asyncio  # noqa: PLC0415  延迟导入保持模块加载轻

    trim = _load_malloc_trim()
    while True:
        await asyncio.sleep(interval)
        try:
            collected = gc.collect()
            freed = trim(0) if trim is not None else 0
            logger.debug(
                "memory trim: gc collected %d objects, malloc_trim freed %d",
                collected,
                freed,
            )
        except Exception:  # noqa: BLE001  回收失败绝不影响主服务
            logger.warning("memory trim failed", exc_info=True)
