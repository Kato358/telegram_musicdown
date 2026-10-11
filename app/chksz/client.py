"""ChKSz HTTP 客户端（app.chksz 适配器层）。

只管发请求、收 JSON、翻错误——不碰卡片、不碰缓存、不碰队列。四家接口的响应形状
差异很大（网易用 ``data``/``artists``/``br``，QQ 与酷狗用 ``list``/``singer``/
``bitrate``），归一化在 ``app/chksz/source.py`` 做。

错误一律按接口文档 §9 归类：原样转述服务端的 ``msg``，不自动无限重试；
429 按 ``Retry-After`` 最多重试一次。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from app.errors import AppError

logger = logging.getLogger(__name__)

# 上游的免费额度是每分钟 20 次（每 Key/用户/IP），搜索一次要打三个平台；
# 留足余量，别把自己打进 429。
DEFAULT_TIMEOUT_SEC = 20.0
# 429 之后唯一一次重试的等待上限：服务端给多了也不能把 worker 挂死在那儿。
MAX_RETRY_AFTER_SEC = 30.0
# 429/502/504 最多重试一次（接口文档：按 Retry-After 等待后最多重试一次）。
_RETRY_STATUSES = frozenset({429, 502, 504})


_OK = 200
_BAD_REQUEST = 400
# 音频 CDN 常按 UA 拦请求；给一个常规 UA，避免拿到 403 的空响应。
_AUDIO_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TelegramMusicDown/1.0)"}

#: 流式下载分块：64KB 对 >100MB 的母带会产生上万次迭代（每次还要过一遍进度回调），
#: 256KB 把迭代次数砍到 1/4，内存占用与流式特性都不受影响。
_CHUNK_BYTES = 256 * 1024


class ChkszError(AppError):
    """ChKSz 调用失败；``status`` 是 HTTP 状态，``message`` 是服务端原话。"""

    def __init__(self, status: int, message: str, reason: str = "chksz_error") -> None:
        super().__init__(reason, message)
        self.status = status


class ChkszClient:
    """ChKSz 的窄接口面：搜索、解析、下载。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ---- 请求内核 ----

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET 一个业务接口并返回 JSON 字典。

        ``code != 200`` 也算失败：接口文档明说「以 HTTP 状态和 msg 为准，
        不要只检查 JSON 中是否存在 data」——空 data 同样是业务失败。
        """
        query = {**params, "apikey": self.api_key}
        for attempt in (1, 2):
            try:
                resp = await self._client.get(f"{self.base_url}{path}", params=query)
            except httpx.HTTPError as e:
                raise ChkszError(0, f"ChKSz 请求失败：{e}", "chksz_unreachable") from e
            if resp.status_code in _RETRY_STATUSES and attempt == 1:
                await asyncio.sleep(self._retry_delay(resp))
                continue
            return self._unwrap(resp)
        raise ChkszError(429, "ChKSz 限流，请稍后重试", "chksz_rate_limited")  # pragma: no cover

    @staticmethod
    def _retry_delay(resp: httpx.Response) -> float:
        """``Retry-After`` 是秒数；缺失或离谱时给个短等待，不把 worker 挂死。"""
        try:
            return min(float(resp.headers.get("Retry-After", "2")), MAX_RETRY_AFTER_SEC)
        except ValueError:
            return 2.0

    @staticmethod
    def _unwrap(resp: httpx.Response) -> dict[str, Any]:
        """HTTP 状态与响应体 ``code`` 双判，失败时把服务端 ``msg`` 原样带出去。"""
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        message = str(body.get("msg") or resp.reason_phrase or f"HTTP {resp.status_code}")
        code = body.get("code")
        if resp.status_code != _OK or (isinstance(code, int) and code != _OK):
            raise ChkszError(resp.status_code, f"ChKSz 接口错误（{resp.status_code}）：{message}")
        return body

    # ---- 搜索 ----

    async def search_163(self, keyword: str, limit: int, offset: int) -> list[dict[str, Any]]:
        # 网易把列表嵌在 ``data.songs`` 里（``data.total`` 才是总数），不是 ``data`` 本身。
        body = await self._get("/api/163_search", _q(keyword, limit, offset))
        return _rows(_row(body, "data"), "songs")

    async def search_qq(self, keyword: str, limit: int) -> list[dict[str, Any]]:
        return _rows(await self._get("/api/qq_music", {"msg": keyword, "num": limit}), "list")

    async def search_kugo(self, keyword: str, limit: int) -> list[dict[str, Any]]:
        return _rows(await self._get("/api/kugou_music", {"msg": keyword}), "list")

    # ---- 解析（换播放地址）----

    async def resolve_163(self, track_id: str, quality: str) -> dict[str, Any]:
        body = await self._get("/api/163_music", {"id": track_id, "level": quality})
        return _row(body, "data")

    async def resolve_qq(self, mid: str, quality: str) -> dict[str, Any]:
        # QQ 支持按 mid 直解：不重搜，省一次额度，也避开「同名歌搜到别的版本」。
        return await self._get("/api/qq_music", {"mid": mid, "size": quality, "type": "json"})

    async def resolve_kugo(self, track_id: str, quality: str) -> dict[str, Any]:
        params = {"id": track_id, "size": quality, "type": "json"}
        return await self._get("/api/kugou_music", params)

    # ---- 下载 ----

    async def download(
        self,
        url: str,
        dest: Path,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> int:
        """流式下载到 ``dest``，返回落盘字节数。

        三家解析接口都不给文件大小（网易的 ``size`` 只在它自己那家有），所以完整性
        判据只能取自这里的响应头——NFR-01「损坏文件不进 save_path」不能因为换了
        来源就失效。返回的字节数由调用方与 ``Content-Length`` 对账。
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            async with self._client.stream(
                "GET", url, headers=_AUDIO_HEADERS, timeout=None
            ) as resp:
                if resp.status_code >= _BAD_REQUEST:
                    status = resp.status_code
                    raise ChkszError(status, f"音频下载失败（HTTP {status}）", "chksz_download")
                total = _content_length(resp)
                with dest.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(_CHUNK_BYTES):
                        fh.write(chunk)
                        written += len(chunk)
                        if progress is not None:
                            progress(written, total)
                if total is not None and written != total:
                    # 收少了就是断流：这份文件不是完整的歌，删掉别让它冒充成功。
                    await asyncio.to_thread(dest.unlink, True)
                    raise ChkszError(
                        resp.status_code,
                        f"音频下载不完整：收到 {written} 字节，声明 {total} 字节",
                        "chksz_incomplete",
                    )
        except httpx.HTTPError as e:
            raise ChkszError(0, f"音频下载失败：{e}", "chksz_unreachable") from e
        return written


def _q(keyword: str, limit: int, offset: int) -> dict[str, Any]:
    return {"keyword": keyword, "limit": limit, "offset": offset}


def _rows(body: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """取列表字段；结构不认识就当空结果（宁可少结果，也不把 dict 当 list 迭代）。"""
    value = body.get(key)
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _row(body: dict[str, Any], key: str) -> dict[str, Any]:
    value = body.get(key)
    return value if isinstance(value, dict) else body


def _content_length(resp: httpx.Response) -> int | None:
    raw = resp.headers.get("Content-Length")
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None
