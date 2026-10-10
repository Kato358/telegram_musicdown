"""试听（FR-PLAY-01~03，SDD §2.4）与封面缓存。

- 预览槽独立于下载队列（并发 1，FR-PLAY-03）。
- 试听不入队、不写 save_path、不写 success 历史（FR-PLAY-02）。
- LRU 淘汰：试听按访问时间（总量 > preview_cache_max_bytes，默认 512MB 或条数 > 50）；
  封面与试听同目录、不进 preview_cache 表，按 mtime 一起进同一个预算（FR-PLAY-02）。
- 封面按「歌名 + 歌手」从公共封面接口（api.lrc.cx）取，落盘缓存。
- 占用与清理见 CacheStats：设置页的「缓存占用 / 清理缓存」（路由在 web/routes/settings.py）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from app.db.models import PreviewCache
from app.db.store import utcnow
from app.domain import AUDIO_EXTS, PROVIDER_TELEGRAM, TrackMeta
from app.errors import AppError
from app.events import Event, EventBus
from app.ports import IStore
from app.ports.music import FetchRef, MusicSourceIndexProto
from app.services.tags import image_media_type

logger = logging.getLogger(__name__)

MAX_PREVIEW_FILES = 50

# ---- 缓存目录的文件名约定（扫盘统计与淘汰都按前缀分类）----
PREVIEW_PREFIX = "preview_"  # 试听音频：进 preview_cache 表，按访问时间 LRU
COVER_PREFIX = "cover_"  # 封面图：不进表，按 mtime LRU
TMP_SUFFIXES = (".part", ".tmp")  # 中转文件：不算占用；超过 STALE_TMP_SEC 视为崩溃残留
STALE_TMP_SEC = 3600  # 秒；比任何一次试听下载都长，不留活着的下载被误删

# ---- 封面（api.lrc.cx：按 title/artist 查 Apple Music 曲库，301 跳到 mzstatic 图）----
COVER_API = "https://api.lrc.cx/cover"
COVER_TIMEOUT = 8  # 秒；接口查不到歌时会挂住不回，必须设超时降级
COVER_FAIL_TTL = 600  # 失败负缓存秒数：查不到的查询期间内直接 404，不反复外呼空等
COVER_FAIL_MAX_ENTRIES = 512  # 失败负缓存条数上限：按「查过的歌」无限长是缓慢的内存泄漏
COVER_CONCURRENCY = 4  # 与试听槽（并发 1）分开限流：列表页几十行同时要封面
COVER_MAX_BYTES = 5 * 1024 * 1024

# 去扩展名用的音频后缀正则：由 domain.AUDIO_EXTS 生成，不手写第二份列表
_AUDIO_EXT_RE = re.compile(
    r"\.(" + "|".join(sorted(e.lstrip(".") for e in AUDIO_EXTS)) + r")$", re.IGNORECASE
)


def _clean_title(title: str | None) -> str:
    """封面查询用的歌名：document 音频的 title 是文件名，去掉扩展名与首尾空白。"""
    return _AUDIO_EXT_RE.sub("", (title or "").strip())


def _is_image(data: bytes) -> bool:
    """魔数嗅探（JPEG/PNG/GIF/WEBP），挡住接口偶发返回的错误页文本。

    判据与标签封面读取共用 tags.image_media_type（同一套魔数，避免两处漂移）。
    """
    return image_media_type(data) is not None


class _SmallCoverRedirect(HTTPRedirectHandler):
    """mzstatic 图床的 URL 以 ``/{宽}x{高}bb.jpg`` 结尾且支持任意尺寸改写。

    lrc.cx 默认跳 3000×3000 原图（约 2MB/张），行首/播放器小图 300px 足够，
    体积差 50 倍；URL 不匹配该模式时按原样跟随（接口换源也不受影响）。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return super().redirect_request(
            req, fp, code, msg, headers, _SMALL_COVER_RE.sub("/300x300bb.jpg", newurl)
        )


_SMALL_COVER_RE = re.compile(r"/\d+x\d+(?:bb)?\.jpg(?:\?.*)?$")

_OPENER = build_opener(_SmallCoverRedirect())


def _fetch_cover(title: str, artist: str) -> bytes | None:
    """同步抓取封面字节（to_thread 里跑）；跟随 301（跳转时改写为 300px 小图）。"""
    params = urlencode({"title": title, "artist": artist})
    req = Request(f"{COVER_API}?{params}", headers={"User-Agent": "Mozilla/5.0"})  # noqa: S310
    with _OPENER.open(req, timeout=COVER_TIMEOUT) as resp:  # noqa: S310
        data = resp.read(COVER_MAX_BYTES)
    return data if _is_image(data) else None


@dataclass(slots=True, frozen=True)
class CacheStats:
    """缓存占用（FR-PLAY-02）：与 max_bytes 同一口径——磁盘实际字节，不是表里记的 file_size。

    设置页显示 total_bytes / max_bytes；清理后复用同一结构，值即清理后的现状。
    """

    total_bytes: int
    max_bytes: int
    preview_bytes: int
    preview_count: int
    cover_bytes: int
    cover_count: int


@dataclass(slots=True)
class _Scan:
    """一次扫盘的结果（内部结构，不出口）：统计口径 + 淘汰/清理候选。"""

    preview_bytes: int
    preview_count: int
    covers: list[tuple[float, Path, int]]  # (mtime, 路径, 字节)，最旧在前
    stale_temps: list[Path]  # 超过 STALE_TMP_SEC 的中转文件（崩在半路的残留）


class PreviewService:
    """试听缓存（LRU）、封面缓存与预览槽（FR-PLAY-02/03）。"""

    def __init__(
        self,
        store: IStore,
        registry: MusicSourceIndexProto,
        events: EventBus,
        preview_dir: Path,
        max_bytes: int = 512 * 1024 * 1024,
        preview_quality: str | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.events = events
        self.preview_dir = preview_dir
        self.max_bytes = max_bytes
        # 试听档位（设置页可改，缺省 320k）：试听只为判断「是不是这首歌」，
        # 不该替用户把额度烧在母带上。
        self.preview_quality = preview_quality
        self._sem = asyncio.Semaphore(1)
        self._cover_sem = asyncio.Semaphore(COVER_CONCURRENCY)
        self._cover_failures: dict[str, float] = {}

    async def request_preview(
        self,
        chat_id: int,
        message_id: int,
        file_size: int | None = None,
        *,
        provider: str = PROVIDER_TELEGRAM,
        ref: str | None = None,
    ) -> int:
        """请求试听：缓存命中直接返回 preview_id；未命中占预览槽下载（SDD §2.4）。

        缓存键是 ``(chat_id, message_id)``——在线源用保留负号 chat_id 加曲目 id 的
        哈希，与 Telegram 消息天然不撞。``provider``/``ref`` 只影响怎么取，不影响键。
        """
        hit = self.store.get_preview(chat_id, message_id)
        if hit is not None and await asyncio.to_thread(self._cache_valid, hit, file_size):
            preview_id = hit.id
            if preview_id is not None:
                self.store.touch_preview(preview_id)
                return preview_id
            return await self._download_preview(chat_id, message_id, file_size, provider, ref)

        async with self._sem:
            return await self._download_preview(chat_id, message_id, file_size, provider, ref)


    def _cache_valid(self, hit: PreviewCache, file_size: int | None) -> bool:
        """缓存记录命中且文件完整（SDD §2.4）。"""
        p = Path(hit.file_path)
        if not p.exists():
            return False
        return p.stat().st_size == (file_size or hit.file_size)

    async def _download_preview(
        self,
        chat_id: int,
        message_id: int,
        file_size: int | None,
        provider: str,
        ref: str | None,
    ) -> int:
        target = self.registry.by_meta(provider, None)
        if target is None:
            raise AppError("preview_failed", f"没有可用的试听来源：{provider}")
        self.preview_dir.mkdir(parents=True, exist_ok=True)
        # 中转名与最终名必须不同：取 temp_path.suffix 会得到 ".part"，两者同形就等于
        # 没有中转（崩在半路会留下一个「看起来像缓存」的半截文件）
        temp_path = self.preview_dir / f"{PREVIEW_PREFIX}{chat_id}_{message_id}.part"
        meta = TrackMeta(chat_id=chat_id, message_id=message_id, provider=provider, ref=ref)
        try:
            await target.fetch(FetchRef.of(meta, target.scope_id, self.preview_quality), temp_path)
        except AppError:
            raise
        except Exception as e:
            temp_path.unlink(missing_ok=True)
            raise AppError("preview_failed", f"preview download failed: {e}") from e
        actual = temp_path.stat().st_size
        if file_size is not None and actual != file_size:
            temp_path.unlink()
            raise AppError("preview_failed", f"preview size mismatch: {actual} != {file_size}")
        # .bin：内容按字节流出（application/octet-stream），扩展名不表达容器
        final = self.preview_dir / f"{PREVIEW_PREFIX}{chat_id}_{message_id}.bin"
        temp_path.replace(final)  # 原子替换（NFR-01 同款 temp → 目标）
        preview_id = self.store.put_preview(
            PreviewCache(
                id=None,
                chat_id=chat_id,
                message_id=message_id,
                file_path=str(final),
                file_size=actual,
                last_access_at=utcnow(),
            )
        )
        await self.events.publish(Event("preview.ready", {"preview_id": preview_id}))
        await asyncio.to_thread(self._evict_lru)
        return preview_id

    async def enforce_limits(self) -> CacheStats:
        """立即按上限修剪一次并回占用（FR-PLAY-02）：启动装配后调用，让上限不只在下载时生效。"""
        await asyncio.to_thread(self._evict_lru)
        return await self.cache_stats()

    async def cache_stats(self) -> CacheStats:
        """缓存占用（FR-PLAY-02）：设置页显示 total_bytes / max_bytes，编辑上限后即时可见。"""
        return await asyncio.to_thread(self._cache_stats)

    async def clear_cache(self) -> CacheStats:
        """清空试听与封面缓存（FR-PLAY-02）：试听能重下、封面能重抓，删了只是下次慢一点。"""
        await asyncio.to_thread(self._clear_cache)
        return await self.cache_stats()

    def _cache_stats(self) -> CacheStats:
        scan = self._scan_cache()
        cover_bytes = sum(size for _, _, size in scan.covers)
        return CacheStats(
            total_bytes=scan.preview_bytes + cover_bytes,
            max_bytes=self.max_bytes,
            preview_bytes=scan.preview_bytes,
            preview_count=scan.preview_count,
            cover_bytes=cover_bytes,
            cover_count=len(scan.covers),
        )

    def _evict_lru(self) -> None:
        """LRU 淘汰（SDD §2.4）：试听按 last_access_at、封面按 mtime，共用一个字节预算。

        先删试听：体积大、随时能从 Telegram 重下；仍超预算再删最旧的封面（小、要外呼，
        留到最后）。占用按磁盘实际值算，不信 preview_cache 里记录的 file_size。
        """
        while True:
            total, cnt = self.store.preview_totals()
            if total <= self.max_bytes and cnt <= MAX_PREVIEW_FILES:
                break
            previews = self.store.list_previews_by_access()
            if not previews:
                break
            victim = previews[0]
            self._unlink(Path(victim.file_path))
            # 文件删不掉（Windows 上正被流占用）也要删记录：否则这条永远是最旧的一条，
            # 循环退不出去；留下的孤儿文件由「清理缓存」收拾
            self.store.delete_preview(victim.id)  # type: ignore[arg-type]
            logger.info("preview evicted id=%s path=%s", victim.id, victim.file_path)

        scan = self._scan_cache()
        if scan.stale_temps:
            # 表里记着的文件就是活缓存：名字像中转文件也不当残留删（旧版本把试听落成 .tmp，
            # 留在盘上的那批仍是有效缓存；正常只有崩在半路的 .part 会中招）
            live = {Path(row.file_path).name for row in self.store.list_previews_by_access()}
            for stale in scan.stale_temps:
                if stale.name not in live:
                    self._unlink(stale)
        used = scan.preview_bytes + sum(size for _, _, size in scan.covers)
        for _, path, size in scan.covers:
            if used <= self.max_bytes:
                break
            if self._unlink(path):
                used -= size
                logger.info("cover evicted path=%s", path)

    def _clear_cache(self) -> None:
        """清空缓存目录与 preview_cache 记录（FR-PLAY-02）。

        先删文件后清记录：崩在中间留下的是指向空文件的记录，命中校验会判失效并自愈。
        """
        if self.preview_dir.is_dir():
            for p in self.preview_dir.iterdir():
                if p.is_file():
                    self._unlink(p)
        self.store.delete_all_previews()
        self._cover_failures.clear()  # 负缓存同批作废：清理后封面重新外呼一次
        logger.info("cache cleared dir=%s", self.preview_dir)

    def _scan_cache(self) -> _Scan:
        """扫一遍缓存目录（同步；调用方用 to_thread）：统计口径 = 磁盘实际字节。

        中转文件（.part/.tmp）不算占用，只把超过 STALE_TMP_SEC 的残留列出来删。
        """
        scan = _Scan(preview_bytes=0, preview_count=0, covers=[], stale_temps=[])
        if not self.preview_dir.is_dir():
            return scan
        now = time.time()
        for p in self.preview_dir.iterdir():
            try:
                if not p.is_file():
                    continue
                st = p.stat()
            except OSError:
                continue  # 并发清理窗口：文件刚被删/清空
            if p.name.endswith(TMP_SUFFIXES):
                if now - st.st_mtime > STALE_TMP_SEC:
                    scan.stale_temps.append(p)
            elif p.name.startswith(COVER_PREFIX):
                scan.covers.append((st.st_mtime, p, st.st_size))
            elif p.name.startswith(PREVIEW_PREFIX):
                scan.preview_bytes += st.st_size
                scan.preview_count += 1
        scan.covers.sort(key=lambda entry: entry[0])  # 最旧在前
        return scan

    def _unlink(self, path: Path) -> bool:
        """删缓存文件；删不掉（Windows 上正被流占用）只记日志，不打断下载或封面流程。"""
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError:
            logger.warning("cache file busy, kept path=%s", path)
            return False

    def stream_path(self, preview_id: int) -> Path:
        """返回试听文件路径；路由层只接受 id，禁止 ?path=（NFR-02）。"""
        for p in self.store.list_previews_by_access():
            if p.id == preview_id:
                path = Path(p.file_path)
                if path.exists():
                    self.store.touch_preview(preview_id)
                    return path
        raise AppError("not_found", f"preview {preview_id} not found")

    async def cover_path(self, title: str | None, artist: str | None) -> Path | None:
        """按「歌名 + 歌手」从公共封面接口取封面：缓存命中直接返回，取不到返回 None。

        缓存文件名是查询串的 md5——搜索行、播放器、飞片动画对同一首歌共用同一份
        文件；失败的查询负缓存 COVER_FAIL_TTL 秒（接口查不到时会空等超时，不值得
        反复试）。与试听共用 preview 目录与同一个字节预算，但不进 preview_cache 表：
        封面是纯装饰，丢了重新抓即可，不值得占一条记录，故按文件 mtime 参与 LRU。
        """
        q_title = _clean_title(title)
        q_artist = (artist or "").strip()
        if not q_title and not q_artist:
            return None
        key = f"{q_title}|{q_artist}"
        digest = hashlib.md5(key.encode()).hexdigest()  # noqa: S324  缓存键，非安全用途
        final = self.preview_dir / f"{COVER_PREFIX}{digest}.jpg"
        if final.exists():
            return final
        failed_at = self._cover_failures.get(key)
        if failed_at is not None:
            if time.monotonic() - failed_at < COVER_FAIL_TTL:
                return None
            del self._cover_failures[key]  # 过期的标记顺手清掉，否则它永远占着一条
        async with self._cover_sem:
            if final.exists():  # 排队期间别的请求已取回
                return final
            data: bytes | None = None
            try:
                data = await asyncio.to_thread(_fetch_cover, q_title, q_artist)
            except Exception as e:  # noqa: BLE001  封面是装饰，失败负缓存后 404
                logger.info("cover fetch skipped title=%r artist=%r: %s", q_title, q_artist, e)
            if not data:
                logger.info("cover not found title=%r artist=%r", q_title, q_artist)
                self._cover_failures[key] = time.monotonic()
                while len(self._cover_failures) > COVER_FAIL_MAX_ENTRIES:
                    self._cover_failures.pop(next(iter(self._cover_failures)))  # 插入序即淘汰序
                return None
            self.preview_dir.mkdir(parents=True, exist_ok=True)
            self._cover_failures.pop(key, None)
            temp_path = self.preview_dir / f"{COVER_PREFIX}{digest}_{uuid4().hex[:8]}.tmp"
            temp_path.write_bytes(data)
            temp_path.replace(final)
            await asyncio.to_thread(self._evict_lru)  # 封面也算占用：写完就进预算
            return final
