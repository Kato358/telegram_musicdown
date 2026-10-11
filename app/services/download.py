"""下载队列引擎（FR-DL-01~06，SDD §2.3）。

状态机：
``queued → downloading → 校验大小 → 写标签 → success``；
``downloading → paused → queued``；``failed → 自动重试(≤3, 指数退避) → queued``；
``queued → cancelled``；入队去重命中 → ``skipped``。

关键点：
- 并发：asyncio.Semaphore（max_download_task，默认 3）。
- 入队同时建 history 行（status=queued）并把 id 挂到 tasks.history_id：历史页、恢复流程
  与去重都以它为准（SDD §2.3/§3.2）。
- 元数据走 TrackMeta 单一契约（SDD §2.2）：渲染路径、写 history、写标签共用一份。
- 完整性：`getsize(temp) == file_size` 必须一致才落盘（NFR-01）；temp 与 save_path
  可能分处两个挂载点（compose 把 `./data` 与 `./downloads` 各挂一次），落盘走
  `utils.fs.move_into_place`：同盘原子 rename，跨盘复制后替换，不报 EXDEV。
- 去重在入队时做（FR-DL-05）；「强制重新下载」绕过检查。
- 重试计数存 tasks 表；指数退避 min(2^n * 30s, 1h)。
- 依赖注入 TelegramClient 协议（FakeUserClient 可替换，NFR-07）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from app.db.models import Task
from app.domain import (
    PROVIDER_TELEGRAM,
    DownloadRequest,
    TemplateConfig,
    TrackMeta,
    card_to_meta,
    is_audio_message,
    message_to_card,
    meta_from_dict,
)
from app.errors import AppError, TaskNotFoundError
from app.events import Event, EventBus
from app.ports import IStore
from app.ports.music import FetchRef, FetchResult, MusicSourceIndexProto
from app.ports.telegram import MediaClientProto
from app.services.history_writer import HistoryWriter, history_row
from app.services.path_builder import render_path, resolve_conflict
from app.services.progress import ProgressReporter, TaskRunState
from app.services.tags import TagService
from app.utils.fs import move_into_place

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BASE_SEC = 30
RETRY_CAP_SEC = 3600
#: worker 空转兜底轮询间隔（秒）。正常流转由 _wake 事件即时唤醒；这个 timeout 只服务
#: 「退避时间到了」这类由时间而不是事件驱动的就绪，故不必亚秒级。
IDLE_POLL_SEC = 2.0


def backoff_sec(retry_count: int) -> int:
    """指数退避 min(2^n * 30s, 1h)。"""
    return int(min(2**retry_count * RETRY_BASE_SEC, RETRY_CAP_SEC))


def _utc_after(seconds: int) -> str:
    """now + seconds 的 UTC ISO-8601 文本（与 store 的时间戳约定一致）。"""
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


class DownloadQueueServiceProto(Protocol):
    """下载队列对外协议面（bot 层依赖它，不依赖 DownloadService 具体类）。"""

    store: IStore

    async def enqueue(self, req: DownloadRequest) -> int | None: ...
    async def cancel_task(self, task_id: int) -> None: ...


class DownloadService:
    """下载队列：入队去重、worker 池、状态机、重试（FR-DL-01~06）。"""

    def __init__(
        self,
        store: IStore,
        client: MediaClientProto,
        events: EventBus,
        temp_dir: Path,
        cfg: TemplateConfig,
        max_concurrent: int = 3,
        *,
        registry: MusicSourceIndexProto,
        default_quality: str | None = None,
    ) -> None:
        self.store = store
        self.client = client
        self.events = events
        self.temp_dir = temp_dir
        self.cfg = cfg
        self.max_concurrent = max_concurrent
        self._sem = asyncio.Semaphore(max_concurrent)
        self._worker_tasks: list[asyncio.Task[None]] = []
        # 「有新任务入队/可重试了」的唤醒信号：worker 空转时等它，不必纯轮询
        self._wake = asyncio.Event()
        # stop_workers 进行中：抑制 worker 死亡后的自动补位，避免关机时长出孤儿任务
        self._stopping_workers = False
        # 运行态（暂停/取消标记 + 在跑协程）：与进度回调共用同一份事实源
        self._run_state = TaskRunState()
        self.tags = TagService()
        # 历史行结算（展示字段/读数/终态）独立成协作对象：与队列调度无关
        self._history = HistoryWriter(store, self.tags, cfg)
        # 来源索引（SDD §2.7）由组合根注入：取音频只认 MusicSourceProto，本服务因此
        # 不必知道「这条是频道里的消息」还是「这条是在线源的曲子」。不自己拼一份——
        # 拼出来的是「只有 Telegram」的索引，接不上在线源，且会拿错客户端去打上游。
        self.registry = registry
        # 入队时没指定音质就听这个（设置页的「下载默认音质」）。
        self.default_quality = default_quality

    def apply_template(self, cfg: TemplateConfig) -> None:
        """运行中替换落盘模板（FR-CFG-03：设置保存即时生效，不必重启）。

        只覆盖模板与落盘根；下载并发数绑定 worker 池规模，改动仍在下次启动生效。
        """
        self.cfg = cfg
        self._history.apply_template(cfg)

    async def enqueue(self, req: DownloadRequest) -> int | None:
        """入队：去重检查（FR-DL-05）→ 建 history + task（FR-DL-01）；命中返回 None。"""
        meta = req.meta
        hit = self.store.find_history_success(meta.chat_id, meta.message_id, meta.unique_id)
        if hit and not req.force:
            logger.info("dedupe hit chat=%s msg=%s", meta.chat_id, meta.message_id)
            return None
        history_id = self.store.upsert_history(history_row(meta, req.source_id))
        payload = {
            "meta": asdict(meta),
            "source_id": req.source_id,
            "force": req.force,
            "quality": req.quality,
        }
        task_id = self.store.create_task(
            Task(
                id=None,
                type="link",
                payload_json=json.dumps(payload, ensure_ascii=False),
                history_id=history_id,
            )
        )
        await self._set_status(task_id, "queued", history_id)
        self._wake.set()  # 立即唤醒空转 worker，不必等兜底轮询
        return task_id

    async def start_workers(self, count: int | None = None) -> None:
        """启动 worker 池。

        worker 非取消原因退出（如失败记账自身抛错）由 done-callback 补位：没有它，
        并发池会悄悄少几个人，队列看起来「不动了」却没有任何报错。
        """
        self._stopping_workers = False
        n = count or self.max_concurrent
        for i in range(n):
            self._spawn_worker(i)

    def _spawn_worker(self, index: int) -> None:
        name = f"worker-{index}"
        t = asyncio.create_task(self._worker(name))
        self._worker_tasks.append(t)

        def _done(task: asyncio.Task[None], name: str = name, index: int = index) -> None:
            self._on_worker_done(task, name, index)

        t.add_done_callback(_done)

    def _on_worker_done(self, task: asyncio.Task[None], name: str, index: int) -> None:
        """worker 退出回调：正常取消不补位；意外死亡记录并重启同编号 worker。"""
        with contextlib.suppress(ValueError):
            self._worker_tasks.remove(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        if self._stopping_workers:
            logger.error("worker %s exited during shutdown: %r", name, exc)
            return
        logger.error("worker %s died unexpectedly, restarting: %r", name, exc)
        self._spawn_worker(index)

    async def stop_workers(self) -> None:
        self._stopping_workers = True
        for t in list(self._worker_tasks):
            t.cancel()
        await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        # 死亡回调可能在 gather 返回后才补位；再扫一次取消，别留孤儿 pending task
        for t in list(self._worker_tasks):
            t.cancel()
        self._worker_tasks.clear()

    async def pause_task(self, task_id: int) -> None:
        """用户暂停：取消当前协程，保留 temp 分片（SDD §2.3）。"""
        task = self._require_task(task_id)
        self._run_state.mark_paused(task_id)
        running = self._run_state.running_task(task_id)
        if running is not None and running is not asyncio.current_task():
            running.cancel()
        await self._set_status(task_id, "paused", task.history_id)

    async def resume_task(self, task_id: int) -> None:
        """恢复：重新入队 queued。"""
        task = self._require_task(task_id)
        self._run_state.clear(task_id)
        await self._set_status(task_id, "queued", task.history_id)
        self._wake.set()

    async def cancel_task(self, task_id: int) -> None:
        task = self._require_task(task_id)
        self._run_state.mark_cancelled(task_id)
        running = self._run_state.running_task(task_id)
        if running is not None and running is not asyncio.current_task():
            running.cancel()
        await self._set_status(task_id, "cancelled", task.history_id)

    def _require_task(self, task_id: int) -> Task:
        task = self.store.get_task(task_id)
        if task is None:
            raise TaskNotFoundError("任务不存在")
        return task

    def _mirror_history_status(self, history_id: int | None, status: str) -> None:
        """把状态写回这条下载的历史行（下载页与状态筛选只认它，任务台账只补读数）。

        用户视角里「这条下载在干什么」只有一个答案，两处数据不能各写各的。
        """
        if history_id is not None:
            self.store.mark_history_status(history_id, status)

    async def _set_status(
        self,
        task_id: int,
        status: str,
        history_id: int | None = None,
        *,
        error: str | None = None,
        finished: bool = False,
    ) -> None:
        """任务状态变更的唯一出口：写台账 + 镜像历史行 + 发布 task.status 事件。

        这三步原先在 10 处各写一遍（且容易漏掉其中一步——历史行漏更新会让下载页
        永远显示旧状态），收口后「状态只有一个答案」这条不变量只需要维护一处。
        """
        self.store.update_task(task_id, status=status, error=error)
        if history_id is not None:
            self.store.mark_history_status(history_id, status, error=error, finished=finished)
        await self._publish_status(task_id, status, error)

    async def _publish_status(self, task_id: int, status: str, error: str | None = None) -> None:
        """仅发布 task.status 事件（状态已由别处写库时用，避免重复写）。"""
        await self.events.publish(
            Event("task.status", {"task_id": task_id, "status": status, "error": error})
        )

    async def retry_task(self, task_id: int) -> None:
        """把一条失败、取消或跳过的任务重新放回队列。"""
        task = self._require_task(task_id)
        if task.status not in {"failed", "cancelled", "skipped"}:
            raise AppError("invalid_task_state", "任务当前不可重试")
        self._run_state.clear(task_id)
        temp_path = self._temp_path_for_task(task)
        if temp_path is not None:
            await asyncio.to_thread(self._discard_temp, temp_path)
        self.store.reset_task_for_retry(task_id)
        if task.history_id is not None:
            self.store.reset_history_for_retry(task.history_id)
        self._wake.set()
        await self._publish_status(task_id, "queued")

    async def delete_task(self, task_id: int) -> None:
        """删除任务台账记录和临时分片，保留 history 及已落盘文件。

        台账没了，这条下载就没有执行者：还把历史行留在非终态，列表里就会挂着一个
        永远不会动的「等待」，所以顺手把它结算成 `cancelled`。
        """
        task = self._require_task(task_id)
        if task.status == "downloading":
            await self.cancel_task(task_id)
        temp_path = self._temp_path_for_task(task)
        if temp_path is not None:
            await asyncio.to_thread(self._discard_temp, temp_path)
        if not self.store.delete_task(task_id):
            raise TaskNotFoundError("任务不存在")
        if task.status in {"queued", "downloading", "paused"}:
            self._mirror_history_status(task.history_id, "cancelled")

    async def retry_failed(self) -> int:
        """手动重试失败项（FR-DL-04）：以失败的历史行逐条重排。

        下载页的「失败项」就是这些行（统计卡、状态筛选与这个动作说的是同一件事），
        所以按行遍历：台账还在且可重试就复位它，台账没了就按消息重新入队。
        """
        rows = self.store.list_history(status="failed", limit=500)
        ids = [int(row.id) for row in rows if row.id is not None]
        task_ids = self.store.latest_task_ids(ids)
        count = 0
        for row in rows:
            task_id = task_ids.get(int(row.id or 0))
            task = self.store.get_task(task_id) if task_id is not None else None
            if task is not None and task.status in {"failed", "cancelled", "skipped"}:
                await self.retry_task(int(task.id or 0))
            else:
                await self.enqueue(
                    DownloadRequest(
                        meta=TrackMeta(chat_id=row.chat_id, message_id=row.message_id),
                        source_id=row.source_id,
                    )
                )
            count += 1
        return count

    # ---- worker ----

    async def _worker(self, name: str) -> None:
        logger.info("%s started (concurrency=%d)", name, self.max_concurrent)
        if self._stopping_workers:
            return  # 停机过程中被 done-callback 补位出来的：直接退，不接活
        while True:
            # 先清再查：清在查前，入队发生的 set() 不会被这次查询吞掉又漏掉唤醒
            self._wake.clear()
            task = self._next_queued()
            if task is None:
                # 事件驱动等待：入队/恢复/重试都会 set()。兜底 timeout 只服务
                # 「退避时间到了」这种由时间驱动、没有事件伴随的就绪。
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=IDLE_POLL_SEC)
                continue
            task_id = int(task["id"])
            current = asyncio.current_task()
            if current is not None:
                self._run_state.register_running(task_id, current)
            try:
                await self._run_task(task)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # NFR-08：失败任务写人类可读原因到 tasks/history 再返回
                reason = f"task crashed: {e}"
                try:
                    await self._fail_or_retry(task, reason)
                except Exception:
                    # 记账自身失败（如 DB 故障）不能把 worker 拖死在这个任务上：
                    # 让异常逃出 _worker，由 done-callback 记录并以新 worker 补位。
                    logger.exception("worker %s bookkeeping failed on task %s", name, task_id)
                    self._release_claimed(task_id)
                    raise
                logger.exception("worker %s crashed on task %s", name, task_id)
            finally:
                if current is not None:
                    self._run_state.unregister_running(task_id, current)
                self._run_state.forget_cancelled(task_id)

    def _release_claimed(self, task_id: int) -> None:
        """worker 死亡时把它名下那条任务放回 ``queued``（尽力而为）。

        ``next_queued_task`` 取到任务即标 ``downloading``；记账失败会让 worker 死在这条
        任务上，若不放回，补位的 worker 只会查到一条 ``downloading``——任务僵在队列里，
        直到下次重启才被 ``recover_interrupted`` 收走。
        """
        with contextlib.suppress(Exception):
            task = self.store.get_task(task_id)
            if task is not None and task.status == "downloading":
                self.store.update_task(task_id, status="queued")

    async def _fail_or_retry(self, task: dict[str, Any], reason: str) -> None:
        """失败结算（FR-DL-04）：未超上限则排入退避重试，超限则落 failed 终态。

        ``next_queued_task`` 只在 ``next_retry_at`` 到达后才取这条任务，因此退避期间
        它不会被重复消费；指数退避见 ``backoff_sec``。
        """
        task_id = int(task["id"])
        history_id = task.get("history_id")
        retry_count = int(task.get("retry_count") or 0)
        if retry_count < MAX_RETRIES:
            next_at = _utc_after(backoff_sec(retry_count))
            self.store.update_task(
                task_id,
                status="queued",
                retry_count=retry_count + 1,
                next_retry_at=next_at,
                error=reason,
            )
            self._mirror_history_status(history_id, "queued")
            logger.warning(
                "task %s failed (attempt %d/%d), retry at %s: %s",
                task_id,
                retry_count + 1,
                MAX_RETRIES,
                next_at,
                reason,
            )
            await self._publish_status(task_id, "queued", reason)
            self._wake.set()  # 退避未到的任务不会被取走，唤醒只为即时性；到点由兜底轮询接住
            return
        await self._set_status(task_id, "failed", history_id, error=reason)

    def _next_queued(self) -> dict[str, Any] | None:
        """取下一条可执行任务（FR-DL-01）：SQL 在仓储里，本层只补历史镜像。"""
        task = self.store.next_queued_task()
        if task is None:
            return None
        self._mirror_history_status(task.get("history_id"), "downloading")
        return task

    async def _run_task(self, task: dict[str, Any]) -> None:
        """执行一条下载任务（SDD §2.3）：台账里只有下载任务一种类型。"""
        if task["type"] != "link":
            # 老库里的残留（同步子系统已移除，v0.10）：结算成失败并留下原因，
            # 不当作下载去跑——它的 payload 里没有 meta，跑了只会以怪错误反复重试
            await self._set_status(
                task["id"],
                "failed",
                task.get("history_id"),
                error=f"任务类型已移除：{task['type']}",
            )
            return
        payload = json.loads(task["payload_json"])
        task_id: int = task["id"]
        history_id: int | None = task.get("history_id")
        meta = _meta_from_payload(payload)
        # `/download <link>` 入队只有 chat_id/message_id：下载前取一次消息补全 meta
        # （ext 决定落盘扩展名与标签容器，file_size 决定完整性校验）。转发入队的 meta
        # 由 bot 会话在入队时就补全了，登录账号也查不到那条消息——不重复去查。
        if (
            meta.provider == PROVIDER_TELEGRAM
            and not meta.via_bot
            and meta.ext is None
            and meta.file_size is None
        ):
            meta = await self._hydrate_meta(meta)
        target = self.registry.by_meta(meta.provider, payload.get("source_id"))
        if target is None:
            await self._set_status(
                task_id,
                "failed",
                history_id,
                error=f"没有可用的取数来源：{meta.provider}",
            )
            return
        quality = payload.get("quality") or self.default_quality
        expected_size: int | None = meta.file_size
        reporter = ProgressReporter(
            self.store, self.events, self._run_state, task_id, expected_size
        )

        async with self._sem:
            if self._run_state.is_stopped(task_id):
                return
            temp_path = self.temp_dir / f"task_{task_id}_{meta.message_id}"
            self.temp_dir.mkdir(parents=True, exist_ok=True)
            await reporter.flush()
            try:
                # 取音频交给来源自己：本服务只管队列、进度与落盘，不认 Telegram
                # 也不认 ChKSz。progress 是同步回调，由适配器在合适的时机调用。
                result = await target.fetch(
                    FetchRef.of(meta, target.scope_id, quality), temp_path, reporter
                )
            except asyncio.CancelledError:
                if self._run_state.is_stopped(task_id):
                    return  # 暂停/取消：不标 failed，保留分片由后续动作处理
                raise
            except AppError as e:
                await self._fail_or_retry(task, e.message)
                return
            finally:
                await reporter.flush()

            # 实际拿到的容器/码率只有解析之后才知道：在线源对没有的音质会静默降级，
            # 落盘与展示都得用实测值而不是用户请求的那一档。
            meta = _apply_fetched(meta, result)
            if history_id is not None:
                self._history.sync_display(history_id, meta)

            # 校验对账的是**声明**大小（meta.file_size），不是刚落盘的字节数——
            # 拿实测值当期望值等于自己跟自己对账，损坏文件会一路进 save_path。
            # 在线源声明不出大小，那一档由适配器自己按 Content-Length 兜住。
            if not await self._validate_download_size(
                task_id, history_id, temp_path, expected_size
            ):
                return  # NFR-01：损坏文件不落 save_path
            await self._complete_download(
                task_id, history_id, temp_path, result.file_size or expected_size, payload, meta
            )

    async def _validate_download_size(
        self,
        task_id: int,
        history_id: int | None,
        temp_path: Path,
        expected_size: int | None,
    ) -> bool:
        """校验下载分片大小；失败时落库并发布失败状态。"""
        if expected_size is None:
            return True
        actual = await asyncio.to_thread(self._file_size_or_missing, temp_path)
        if actual == expected_size:
            return True
        await asyncio.to_thread(self._discard_temp, temp_path)
        reason = f"size mismatch: got {actual}, expected {expected_size}"
        await self._set_status(task_id, "failed", history_id, error=reason)
        return False

    async def _complete_download(
        self,
        task_id: int,
        history_id: int | None,
        temp_path: Path,
        expected_size: int | None,
        payload: dict[str, Any],
        meta: TrackMeta,
    ) -> None:
        """将已校验分片落盘，处理去重、写标签并发布完成状态。"""
        rendered = render_path(meta, self.cfg)
        target, dedupe_hit = resolve_conflict(Path(rendered), expected_size=expected_size)
        if dedupe_hit:
            await asyncio.to_thread(self._discard_temp, temp_path)
            await self._set_status(task_id, "skipped", history_id, finished=True)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        # 跨挂载点（`/data/data/temp` → `/data/downloads`）时 rename 会 EXDEV；
        # helper 自己退化成复制，任务不必因此重下一遍。
        await asyncio.to_thread(move_into_place, temp_path, target)

        # 写标签（FR-META-01，失败仅日志不影响状态）；finish 同时把历史行
        # 结算成 success（带 save_path），故此处传 history_id=None 避免重复写。
        if history_id:
            await asyncio.to_thread(self._history.finish, history_id, str(target), payload, meta)
        await self._set_status(task_id, "success")

    @staticmethod
    def _file_size_or_missing(path: Path) -> int:
        return path.stat().st_size if path.exists() else -1

    async def _hydrate_meta(self, meta: TrackMeta) -> TrackMeta:
        """meta 只有 chat_id/message_id 时（bot 链接/转发入队），取消息补全元数据。

        取不到消息或非音频 → 原样返回，行为与之前一致（message_{id} 回退）。
        """
        try:
            msgs = await self.client.get_messages(meta.chat_id, [meta.message_id])
        except AppError:
            return meta
        msg = (msgs if isinstance(msgs, list) else [msgs])[0]
        if msg is None or not is_audio_message(msg):
            # 非音频（含「这个 id 在该对话里不存在」的空消息）没有可补的字段：拿它
            # 生成的 card 会把 chat_id 抹成 0，于是取数改去对话 0 找——原样返回，
            # 让 fetch 按原来的定位去报它自己拿得到的原因。
            return meta
        card = message_to_card(msg, channel_title=meta.channel_title)
        hydrated = card_to_meta(card)
        if hydrated.ext is None and hydrated.mime:
            hydrated.ext = {"audio/flac": "flac", "audio/mpeg": "mp3"}.get(hydrated.mime)
        hydrated.track = meta.track
        hydrated.album = meta.album or hydrated.album
        return hydrated

    def _discard_temp(self, temp_path: Path) -> None:
        if temp_path.exists():
            temp_path.unlink()

    def _temp_path_for_task(self, task: Task) -> Path | None:
        try:
            payload: object = json.loads(task.payload_json)
            if not isinstance(payload, dict):
                return None
            meta: object = payload.get("meta")
            if not isinstance(meta, dict):
                return None
            raw_message_id: object = meta.get("message_id")
            if not isinstance(raw_message_id, (int, str)):
                return None
            message_id = int(raw_message_id)
        except (TypeError, ValueError, AttributeError):
            return None
        if task.id is None or message_id <= 0:
            return None
        return self.temp_dir / f"task_{task.id}_{message_id}"

    def backfill_history_media(self, limit: int = 500) -> int:
        """启动回填（兼容入口）：委托 HistoryWriter。"""
        return self._history.backfill_media(limit)


def _apply_fetched(meta: TrackMeta, result: FetchResult) -> TrackMeta:
    """把取音频的实测事实并回 meta。

    搜索卡片上的容器/码率是「这个平台大概是什么样」，解析之后才知道确切值：
    在线源对没有的音质会静默降级（母带歌多半只有无损），拿请求的档位去渲染文件名
    与标签就是把没下到的东西说成下到了。缺项保留原值，不覆盖成 None。
    """
    updates: dict[str, Any] = {}
    if result.ext:
        updates["ext"] = result.ext
    if result.mime:
        updates["mime"] = result.mime
    if result.bitrate:
        updates["bitrate"] = result.bitrate
    if result.file_size:
        updates["file_size"] = result.file_size
    return replace(meta, **updates) if updates else meta


def _meta_from_payload(payload: dict[str, Any]) -> TrackMeta:
    """payload['meta'] → TrackMeta；缺失字段走回退链（FR-NAME-02）。"""
    return meta_from_dict(payload.get("meta") or {})
