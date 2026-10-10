"""全局歌词路由：播放器歌词面板的唯一数据源（GET /api/lyrics）。

歌词文本按「歌名 + 歌手」从公共歌词接口（api.lrc.cx）取。服务端代理的原因：
接口不带 CORS 头，浏览器直连 APlayer 的 XHR 会被跨域拦死。

对 APlayer 的约定是**恒 200**：它对非 2xx 会弹一条写死的英文 notice，
而 200 空体解析出空列表，面板静默留白。歌词是纯装饰——查不到、超时、
响应里没有时间轴标签（纯文本歌词 APlayer 渲染不了）一律按空处理。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from fastapi import Depends, FastAPI, Response

from app.web.routes.context import RouteContext

logger = logging.getLogger(__name__)

# ---- api.lrc.cx 歌词文本接口（title/artist 查 LRC 文本，无 CORS 头）----
LYRICS_API = "https://api.lrc.cx/lyrics"
LYRICS_TIMEOUT = 15  # 秒；上游冷查询可达 10s+（查不到时挂满超时才回），太紧会误伤慢查询
LYRICS_FAIL_TTL = 600  # 失败负缓存秒数：查不到的查询期间内直接空响应，不反复外呼
LYRICS_TTL = 24 * 3600  # 歌词几乎不变：命中缓存一天内不再外呼
LYRICS_MAX_ENTRIES = 512  # 内存缓存条数上限（每条几 KB，封顶 MB 量级）
LYRICS_FAIL_MAX_ENTRIES = 512  # 失败负缓存条数上限（每条只是 key + 时刻，也必须有上限）
LYRICS_MAX_BYTES = 256 * 1024  # 单条响应大小上限，超过按失败处理
LYRICS_CONCURRENCY = 4  # 与封面的外呼限流一致：切歌即要歌词，挡住连点

# APlayer 只渲染带时间轴的行：响应里连一个 [mm:ss 都没有就当没有歌词
_LRC_TIME_RE = re.compile(r"\[\d{2}:\d{2}")


def _fetch_lyrics(title: str, artist: str) -> str | None:
    """同步抓歌词文本（to_thread 里跑）；查不到 / 响应不含时间轴 → None。"""
    params = urlencode({"title": title, "artist": artist})
    req = Request(f"{LYRICS_API}?{params}", headers={"User-Agent": "Mozilla/5.0"})  # noqa: S310
    with build_opener().open(req, timeout=LYRICS_TIMEOUT) as resp:  # noqa: S310
        data = resp.read(LYRICS_MAX_BYTES)
    text = data.decode("utf-8", errors="replace")
    return text if _LRC_TIME_RE.search(text) else None


class LyricsCache:
    """进程内歌词缓存：命中免外呼，失败负缓存，LRU 限量。

    播放器每次切歌都会要一次歌词（APlayer 侧按曲目缓存，跨切回不再发），
    进程内缓存把同一首歌的重复外呼与多人使用的外呼都挡掉。
    """

    def __init__(self) -> None:
        self._hits: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._fails: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(LYRICS_CONCURRENCY)

    async def get(self, title: str, artist: str) -> str:
        """取歌词文本；没有就返回空串（路由直接把空串回给 APlayer）。"""
        key = f"{title}|{artist}"
        async with self._lock:
            hit = self._hits.get(key)
            if hit is not None:
                text, expires_at = hit
                if time.monotonic() < expires_at:
                    self._hits.move_to_end(key)
                    return text
                del self._hits[key]
            failed_at = self._fails.get(key)
            if failed_at is not None:
                if time.monotonic() - failed_at < LYRICS_FAIL_TTL:
                    return ""
                del self._fails[key]  # 过期的标记顺手清掉，否则它永远占着一条
        async with self._sem:
            fetched: str | None = None
            try:
                fetched = await asyncio.to_thread(_fetch_lyrics, title, artist)
            except Exception as e:  # noqa: BLE001  歌词是装饰，外呼失败按查不到处理
                logger.info("lyrics fetch skipped title=%r artist=%r: %s", title, artist, e)
        async with self._lock:
            if fetched:
                self._hits[key] = (fetched, time.monotonic() + LYRICS_TTL)
                self._hits.move_to_end(key)
                while len(self._hits) > LYRICS_MAX_ENTRIES:
                    self._hits.popitem(last=False)
                self._fails.pop(key, None)
                return fetched
            # 上游 404 / 超时 / 返回了没有时间轴的纯文本歌词（APlayer 渲染不了）都会走到这
            logger.info("lyrics not found title=%r artist=%r", title, artist)
            self._fails[key] = time.monotonic()
            while len(self._fails) > LYRICS_FAIL_MAX_ENTRIES:
                self._fails.pop(next(iter(self._fails)))  # 插入序即淘汰序，先来的先走
            return ""


def register(app: FastAPI, ctx: RouteContext) -> None:
    """注册全局歌词路由。"""
    cache = LyricsCache()

    @app.get("/api/lyrics")
    async def lyrics_ep(
        title: str | None = None,
        artist: str | None = None,
        _: None = Depends(ctx.check_session),
    ) -> Response:
        """全局歌词文本：有则 LRC 纯文本，没有则 200 空体（见模块 docstring）。"""
        q_title = (title or "").strip()
        q_artist = (artist or "").strip()
        text = ""
        if q_title or q_artist:
            text = await cache.get(q_title, q_artist)
        return Response(text, media_type="text/plain; charset=utf-8")
