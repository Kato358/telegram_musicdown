"""FastAPI 应用装配（SDD §4）：错误包络、SSE、静态资源；路由按资源分模块注册。

装配本身不写任何业务端点——端点在各资源模块（auth/setup/stats/sources/
downloads/history/preview/browser_download/settings/search），本文件只负责应用级横切面（SRP）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.errors import AppError
from app.events import EventBus, sse_event_stream
from app.ports import IStore
from app.ports.music import MusicSourceIndexProto
from app.services.browser_download import BrowserDownloadService
from app.services.download import DownloadService
from app.services.local_library import LocalLibraryService
from app.services.preview import PreviewService
from app.services.search import SearchService
from app.services.source import SourceService
from app.telegram.manager import TelegramManager
from app.web.routes import schemas  # noqa: F401  兼容旧 import 路径（tests 直接 import）
from app.web.routes.context import RouteContext, register_all

logger = logging.getLogger(__name__)

# 前端产物缺失时的首屏说明（首次部署的唯一出路就是把产物建出来）
_FRONTEND_MISSING = """前端产物缺失，暂时打不开界面。

在源码根构建一次再启动：
  cd web && npm install && npm run build
或用 ./run.sh（缺产物时会自动构建）。

Docker：镜像构建前要先有 web/dist（Dockerfile 会 COPY web/dist）。
产物在别处时用 TGM_STATIC_DIR 指过去。
"""


def create_app(  # noqa: PLR0915  应用级横切面注册
    store: IStore,
    events: EventBus,
    downloads: DownloadService,
    sources: SourceService,
    search: SearchService,
    preview: PreviewService,
    tg: TelegramManager,
    *,
    library: LocalLibraryService | None = None,
    registry: MusicSourceIndexProto | None = None,
    browser_downloads: BrowserDownloadService | None = None,
    base_dir: Path,
    web_host: str = "127.0.0.1",
    web_login_secret: str = "",
    web_login_enabled: bool = True,
    static_dir: Path | None = None,
) -> FastAPI:
    """装配 FastAPI 应用：错误包络 + SSE + 静态资源 + 按资源注册的路由。

    ``library`` 未给时按默认布局兜底装配（save_path = base_dir/downloads），
    曲库路由因此始终可用；生产由容器传入。
    """
    # 来源索引：显式传入用容器的那份；未传就取下载服务手上那份（三者本来就该同一份）。
    source_registry = registry or downloads.registry
    # 音频标签服务：封面/曲库路由与曲库扫描共用同一份（下载服务自持一份，此处复用）
    tags = downloads.tags
    library_service = library
    if library_service is None:
        library_service = LocalLibraryService(store, base_dir / "downloads", tags, events)
    # 浏览器下载（FR-DL-08）：未显式传入时就地兜底装配，取下载服务手上那份来源索引、
    # 临时目录与模板——三者本来就该是同一个，另起一份就等于「搜索里点的」与
    # 「浏览器下到的」可能不是一路。
    browser_service = browser_downloads
    if browser_service is None:
        browser_service = BrowserDownloadService(
            source_registry, downloads.temp_dir, downloads.cfg
        )
    ctx = RouteContext(
        store=store,
        events=events,
        downloads=downloads,
        registry=source_registry,
        sources=sources,
        search=search,
        preview=preview,
        browser_downloads=browser_service,
        library=library_service,
        tg=tg,
        tags=tags,
        base_dir=base_dir,
        web_host=web_host,
        web_login_secret=web_login_secret,
        web_login_enabled=web_login_enabled,
        static_dir=static_dir,
    )
    app = FastAPI(title="telegram-musicdown")

    # 前端产物是原文发给浏览器的：StaticFiles 自己不压缩，部署里也没有反代，
    # 于是首屏 616 kB JS + 82 kB CSS 一个字节不少地过网（gzip 后约 189 kB/15 kB）。
    # 这里统一压：JS/CSS/字体/JSON 都吃得到，且 starlette 的 GZipMiddleware 默认把
    # text/event-stream 排除在外（DEFAULT_EXCLUDED_CONTENT_TYPES），/api/events 的
    # 实时流不会被缓存住。已自带 content-encoding 的响应也会跳过，不会压第二遍。
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        payload: dict[str, object] = {"code": exc.code, "message": exc.message}
        if hasattr(exc, "reason"):
            payload["reason"] = exc.reason
        return JSONResponse(status_code=400, content={"error": payload})

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Pydantic 校验失败也走同一错误包络（NFR-08/§4），前端只需认一种错误形状。"""
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        message = f"{field}: {first.get('msg', 'invalid request')}".lstrip(": ")
        return JSONResponse(
            status_code=400, content={"error": {"code": "validation_error", "message": message}}
        )

    # ---- events (SSE) ----

    @app.get("/api/events")
    async def events_sse(_: None = Depends(ctx.check_session)) -> StreamingResponse:
        """实时事件流（SDD §4「所有 /api/* 需 Web 会话」的最后一角）：任务进度、错误日志、
        曲库扫描都从这里广播，未登录可读等于把运行状态公网裸奔，故同样过 check_session。
        """

        return StreamingResponse(sse_event_stream(events), media_type="text/event-stream")

    # ---- 按资源注册路由（auth/setup/stats/sources/downloads/history/preview/settings/search）----
    register_all(app, ctx)

    # ---- static ----
    static = ctx.static_dir
    if static and static.exists():
        # /assets 等静态文件；其余非 /api 路径全部回退 index.html（SPA 路由，SDD §4.3）
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

        @app.exception_handler(404)
        async def spa_fallback(request: Request, exc: object) -> object:
            if request.url.path.startswith("/api"):
                return JSONResponse(status_code=404, content={"detail": "Not Found"})
            return FileResponse(static / "index.html")
    else:
        # 前端产物缺失：首次部署最常见的原因（本地还没构建、Docker 镜像里没 COPY 进去）。
        # 这里给可操作说明而不是 404 裸 JSON——否则首屏只有一行 {"detail":"Not Found"}，
        # 用户没有任何入口去进初始化向导。
        logger.error("web frontend missing at %s: run a build (npm run build) first", static)

        @app.exception_handler(404)
        async def spa_missing(request: Request, exc: object) -> object:
            if request.url.path.startswith("/api"):
                return JSONResponse(status_code=404, content={"detail": "Not Found"})
            return PlainTextResponse(_FRONTEND_MISSING, status_code=503)

    return app
