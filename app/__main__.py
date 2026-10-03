"""入口：装配、启动 FastAPI + TG 客户端（SDD §1.1，FR-OPS-01）。

单进程：uvicorn 与 Kurigram 共享同一 asyncio loop，避免双 loop（SDD §1.3）。
启动顺序：目录 → 迁移 → 恢复（NFR-05）→ 服务装配 → web/tg 启动。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import SecretConfig, app_dirs, load_secrets, web_dist_dir
from app.container import Container, Overrides, build_container
from app.errors import SessionLockedError, WebAuthConfigError
from app.events import EventBus, attach_event_log_bridge
from app.ports import IStore
from app.services.browser_download import BrowserDownloadService
from app.services.download import DownloadService
from app.services.local_library import LocalLibraryService
from app.services.preview import PreviewService
from app.services.search import SearchService
from app.services.source import SourceService
from app.telegram.manager import TelegramManager
from app.utils.memtrim import run_memory_trim_loop
from app.utils.proactor_patch import silence_proactor_connection_reset
from app.web import auth as web_auth
from app.web.routes import create_app


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> None:
    """统一 logging；logs/ 滚动 10MB × 5（编码规范 §2.5）。"""
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    handler = RotatingFileHandler(
        logs_dir / "app.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(fmt)
    root.addHandler(handler)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    root.addHandler(console)


@dataclass(slots=True)
class AppServices:
    """装配后的服务容器（供 web/tg 与测试共用）。

    只放**启动后仍被读**的句柄；``dirs``/``template`` 是装配期中间量（``dirs`` 已用在
    建 TG 客户端与 save_path，``template`` 已交给 DownloadService），不在此重复持有。
    """

    secrets: SecretConfig
    store: IStore
    events: EventBus
    sources: SourceService
    search: SearchService
    downloads: DownloadService
    preview: PreviewService
    browser_downloads: BrowserDownloadService
    library: LocalLibraryService
    tg: TelegramManager
    # 组合根持有的容器：关停时经它统一释放（Container.close 是生命周期收口点）
    container: Container


def build_services(base_dir: Path) -> AppServices:
    """装配全部服务；返回依赖容器（供 web/tg 与测试共用）。

    组合根：密钥与 TG 生命周期在此收口，服务装配走 ``build_container``（DI）。
    TG 管理器**先于**容器建出来——容器的客户端就是它的两个代理，于是来源索引与
    各服务从构造那一刻起就握着真客户端。旧顺序（先建容器、再往服务上回填客户端）
    漏掉了来源索引，那条链路一直拿 None 打上游（默认的逐源搜索因此整个不可用）。
    """
    secrets = load_secrets(base_dir)
    web_auth.check_auth_config(
        secrets.web_host, secrets.web_login_secret, secrets.web_login_enabled
    )
    dirs = app_dirs(base_dir)
    tg = TelegramManager(secrets, dirs["sessions"])
    container = build_container(
        base_dir,
        Overrides(
            user_client=tg.user_client_proxy,
            media_client=tg.download_client_proxy,
            secrets=secrets,
        ),
    )
    return AppServices(
        secrets=secrets,
        store=container.store,
        events=container.events,
        sources=container.sources,
        search=container.search,
        downloads=container.downloads,
        preview=container.preview,
        browser_downloads=container.browser_downloads,
        library=container.library,
        tg=tg,
        container=container,
    )


async def run(base_dir: Path) -> None:
    """单 asyncio loop：uvicorn + worker 池（SDD §1.3）。"""
    import uvicorn  # noqa: PLC0415  延迟导入保持模块加载轻

    svc = build_services(base_dir)
    store = svc.store
    # ERROR+ 日志桥到 SSE：日志页的「实时错误」靠它（事件总线见 app/events.py）
    attach_event_log_bridge(svc.events)
    recovered = store.recover_interrupted()
    if recovered:
        logging.getLogger(__name__).info("recovered %d interrupted tasks", recovered)
    # 旧记录缺时长/大小/码率（链接入队时拿不到）：启动时按落盘文件补一次
    backfilled = await asyncio.to_thread(svc.downloads.backfill_history_media)
    if backfilled:
        logging.getLogger(__name__).info("backfilled media facts for %d history rows", backfilled)

    # 缓存上限不只在下载时生效：启动先按上限修剪一次（封面按 mtime 一并进预算，FR-PLAY-02）
    try:
        await svc.preview.enforce_limits()
    except Exception:  # noqa: BLE001  缓存修剪失败不该拦住启动
        logging.getLogger(__name__).exception("startup cache trim failed")

    # 搜索缓存同理：起进程先把过期条目与超上限的部分清掉，TTL/容量不只在写入时生效
    try:
        purged = svc.search.purge_cache()
        if purged:
            logging.getLogger(__name__).info("purged %d search cache entries", purged)
    except Exception:  # noqa: BLE001  同上：清缓存失败不是启动失败
        logging.getLogger(__name__).exception("startup search cache purge failed")

    # 浏览器下载的临时区（FR-DL-08）：TTL 与并存上限不只在下次点击时生效——
    # 上一个进程崩在半路的残留、以及超过 TTL 没人取的准备，都在启动这一批收掉
    try:
        svc.browser_downloads.sweep()
    except Exception:  # noqa: BLE001  同上：清扫失败不该拦住启动
        logging.getLogger(__name__).exception("startup browser download sweep failed")

    # 本地曲库台账随启动后台扫一次（不阻塞 web/tg 启动；扫描结果经 SSE 推给前端）
    async def _startup_library_scan() -> None:
        try:
            await svc.library.rescan()
        except Exception:  # noqa: BLE001  曲库扫描失败不影响主服务
            logging.getLogger(__name__).exception("startup library scan failed")

    library_scan_task = asyncio.create_task(_startup_library_scan())

    app = create_app(
        store=svc.store,
        events=svc.events,
        downloads=svc.downloads,
        sources=svc.sources,
        search=svc.search,
        preview=svc.preview,
        browser_downloads=svc.browser_downloads,
        tg=svc.tg,
        library=svc.library,
        registry=svc.container.registry,
        base_dir=base_dir,
        web_host=svc.secrets.web_host,
        web_login_secret=svc.secrets.web_login_secret,
        web_login_enabled=svc.secrets.web_login_enabled,
        static_dir=web_dist_dir(),
    )
    svc.tg.set_services(svc.downloads, svc.search)
    await svc.downloads.start_workers()
    await svc.tg.start()  # User（有会话时）与 Bot（有 token 时）一起装配并启动

    config = uvicorn.Config(
        app,
        host=svc.secrets.web_host,
        port=svc.secrets.web_port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    trim_task = asyncio.create_task(run_memory_trim_loop())
    try:
        await server.serve()
    finally:
        trim_task.cancel()
        await svc.tg.stop()
        await svc.downloads.stop_workers()
        library_scan_task.cancel()
        # 搜索：停掉后台补齐的取数任务并把延迟写队列落盘（缓存尽力落，不阻塞退出）
        await svc.search.aclose()
        # 生命周期收口：不再绕过容器直接 store.close()。aclose 顺带关掉在线源的
        # HTTP 连接池——它是在线的 loop 资源，close() 这个同步口关不掉。
        await svc.container.aclose()


def main() -> None:
    # Docker：TGM_BASE_DIR 指向挂载卷（/data）；默认源码根
    from app.config import (  # noqa: PLC0415  仅 main 需要
        app_dirs,
        config_file,
        migrate_legacy_config,
    )

    # Windows Proactor 关连接时对已 RST 的 socket shutdown 报 10054，属 asyncio 已知噪音
    silence_proactor_connection_reset()
    base_dir = Path(os.environ.get("TGM_BASE_DIR") or Path(__file__).resolve().parent.parent)
    dirs = app_dirs(base_dir)
    setup_logging(dirs["logs"])
    # 密钥文件从 base_dir/config.yaml 收进 data/（FR-CFG-01）：老部署第一次起来时自动搬家。
    # 搬进 data/ 就等于从此跟着数据卷持久化，容器重建不用重走向导；迁移结果记进 app.log。
    migrate_legacy_config(base_dir)
    try:
        asyncio.run(run(base_dir))
    except WebAuthConfigError as e:
        # 只报「拒绝」不够：日志（docker logs / app.log）要说清改哪个文件、哪个 env 能过闸门
        logging.getLogger(__name__).error(
            "startup refused: %s\n%s",
            e.message,
            web_auth.refused_hint(config_file(base_dir)),
        )
        sys.exit(1)
    except SessionLockedError as e:
        logging.getLogger(__name__).error("startup refused: %s", e.message)
        sys.exit(1)


if __name__ == "__main__":
    main()
