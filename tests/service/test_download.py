"""服务层测试：下载状态机、暂停、FloodWait、去重、媒体组（NFR-07，SDD §7）。

FakeUserClient 注入依赖，不 mock 被测对象（编码规范 §6 反模式表）。
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mutagen.id3 import ID3, TALB, TIT2, TPE1
from pyrogram.types import Message

from app.db.models import History
from app.db.models import Task as TaskModel
from app.db.store import Store
from app.domain import TemplateConfig, TrackMeta
from app.events import EventBus
from app.services.download import (
    IDLE_POLL_SEC,
    MAX_RETRIES,
    DownloadRequest,
    DownloadService,
    backoff_sec,
)
from app.services.progress import ProgressReporter, TaskRunState
from app.services.search import SearchService
from app.telegram import flood as flood_mod
from app.telegram.flood import with_flood_retry
from app.telegram.user_client import message_dict
from tests.fakes import FakeFloodWait, FakeUserClient, fake_registry, make_audio_message

CHAT_ID = -100123


def req(message_id: int, **meta: Any) -> DownloadRequest:
    """单条下载请求：元数据走 TrackMeta（SDD §2.2）。"""
    return DownloadRequest(meta=TrackMeta(chat_id=CHAT_ID, message_id=message_id, **meta))


def worker_row(store: Store, task_id: int) -> dict[str, Any]:
    """Worker 消费用的任务行（与 _next_queued 同形：含 type/history_id）。"""
    task = store.get_task(task_id)
    assert task is not None
    return {
        "id": task.id,
        "type": task.type,
        "payload_json": task.payload_json,
        "history_id": task.history_id,
    }


@pytest.fixture()
def svc(tmp_path: Path) -> tuple[DownloadService, Store, FakeUserClient]:
    store = Store(tmp_path / "app.db")
    events = EventBus()
    client = FakeUserClient([make_audio_message(1), make_audio_message(2)])
    service = DownloadService(
        store,
        client,
        events,
        tmp_path / "temp",
        TemplateConfig(save_path=tmp_path / "library"),
        registry=fake_registry(store, client),
    )
    return service, store, client


async def test_download_success_saves_to_save_path(
    svc: tuple[DownloadService, Store, FakeUserClient], tmp_path: Path
) -> None:
    service, store, _ = svc
    task_id = await service.enqueue(req(1, file_size=100))
    assert task_id is not None
    await service._run_task(worker_row(store, task_id))
    assert store.get_task(task_id).status == "success"  # type: ignore[union-attr]
    # save_path 有文件
    assert any((tmp_path / "library").glob("**/*"))
    # 入队即建 history 行，完成时写回落盘路径（历史页与恢复流程的事实源）
    history_id = store.get_task(task_id).history_id  # type: ignore[union-attr]
    assert history_id is not None
    row = store.get_history(history_id)
    assert row is not None
    assert row.status == "success"
    assert row.save_path



async def test_download_lands_when_temp_and_library_are_different_devices(
    svc: tuple[DownloadService, Store, FakeUserClient],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 部署现场（FR-CFG-02）：compose 把 ./data 与 ./downloads 分别挂成两个挂载点，
    # Linux 的 rename(2) 不许跨 mount —— temp 分片搬进 save_path 一律 EXDEV。
    # 老代码在这里炸成「任务崩溃 → 退避重试 → 重下一遍」，永远落不了盘。
    service, store, _ = svc
    temp_dir = service.temp_dir
    real_replace = os.replace

    def cross_device(*args: object, **kwargs: object) -> None:
        src, dst = Path(str(args[0])), Path(str(args[1]))
        if src.is_relative_to(temp_dir) and not dst.is_relative_to(temp_dir):
            raise OSError(errno.EXDEV, "Invalid cross-device link", str(src), str(dst))
        real_replace(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", cross_device)
    task_id = await service.enqueue(req(1, file_size=100))
    assert task_id is not None

    await service._run_task(worker_row(store, task_id))

    assert store.get_task(task_id).status == "success"  # type: ignore[union-attr]
    saved = [p for p in (tmp_path / "library").rglob("*") if p.is_file()]
    assert len(saved) == 1
    assert saved[0].stat().st_size == 100
    # 源分片跟着搬走了；正式路径旁也没留 .xdev 中转残骸
    assert not list((tmp_path / "temp").glob("*"))
    assert not [p for p in saved[0].parent.iterdir() if p.name.startswith(".")]

async def test_bare_link_task_hydrates_meta(
    svc: tuple[DownloadService, Store, FakeUserClient], tmp_path: Path
) -> None:
    # bot 链接/转发入队只有 chat_id/message_id：下载前取消息补全 ext/file_size，
    # 落盘带扩展名（标签容器依赖它）且大小校验生效。
    service, store, client = svc
    task_id = await service.enqueue(req(1))  # meta 全空
    assert task_id is not None
    await service._run_task(worker_row(store, task_id))
    assert store.get_task(task_id).status == "success"  # type: ignore[union-attr]
    # ext 来自 Fake 消息的 audio.file_name（晴天.mp3），文件名不再是无后缀 message_1
    saved = [p for p in (tmp_path / "library").rglob("*") if p.is_file()]
    assert saved and saved[0].suffix == ".mp3"
    # 大小校验现在生效：Fake 内容 100 字节，注入不符 → failed 且不落盘
    client.size_override = 99
    task_id2 = await service.enqueue(
        DownloadRequest(meta=TrackMeta(chat_id=CHAT_ID, message_id=2), force=True)
    )
    assert task_id2 is not None
    await service._run_task(worker_row(store, task_id2))
    assert store.get_task(task_id2).status == "failed"  # type: ignore[union-attr]


async def test_unresolvable_message_keeps_its_locator(
    svc: tuple[DownloadService, Store, FakeUserClient]
) -> None:
    # 入队的 (chat_id, message_id) 是这条音频唯一的定位信息。Kurigram 对「这个 id
    # 在该对话里不存在」给回的是 chat_id=0 的空消息——拿它补全会把取数改派到对话 0，
    # 界面于是报一个与原因无关的错。补不上就原样带着原定位去取，让 fetch 自己报错。
    service, store, client = svc
    client.messages = [message_dict(Message(id=99, empty=True))]
    task_id = await service.enqueue(
        DownloadRequest(meta=TrackMeta(chat_id=CHAT_ID, message_id=99), force=True)
    )
    assert task_id is not None
    await service._run_task(worker_row(store, task_id))
    assert client.last_ref is not None
    assert (client.last_ref["chat_id"], client.last_ref["message_id"]) == (CHAT_ID, 99)


async def test_size_mismatch_not_saved(
    svc: tuple[DownloadService, Store, FakeUserClient], tmp_path: Path
) -> None:
    # NFR-01：损坏文件（大小不符）不落 save_path
    service, store, client = svc
    client.size_override = 50  # 注入大小不符（期望 100）
    task_id = await service.enqueue(req(1, file_size=100))
    assert task_id is not None
    await service._run_task(worker_row(store, task_id))
    assert store.get_task(task_id).status == "failed"  # type: ignore[union-attr]
    assert "size mismatch" in (store.get_task(task_id).error or "")  # type: ignore[union-attr]
    # save_path 无文件（NFR-01）
    assert not list((tmp_path / "library").glob("**/*"))


async def test_dedupe_enqueue_skips(svc: tuple[DownloadService, Store, FakeUserClient]) -> None:
    # FR-DL-05：同 (chat_id, message_id) 已成功 → 去重命中
    service, store, _ = svc
    # 预置成功历史
    store.upsert_history(History(id=None, chat_id=CHAT_ID, message_id=1, status="success"))
    task_id = await service.enqueue(req(1))
    assert task_id is None  # 命中去重，不入队


async def test_dedupe_force_bypasses(svc: tuple[DownloadService, Store, FakeUserClient]) -> None:
    service, store, _ = svc
    store.upsert_history(History(id=None, chat_id=CHAT_ID, message_id=1, status="success"))
    task_id = await service.enqueue(
        DownloadRequest(meta=TrackMeta(chat_id=CHAT_ID, message_id=1), force=True)
    )
    assert task_id is not None


async def test_failed_history_row_reused_on_retry(
    svc: tuple[DownloadService, Store, FakeUserClient],
) -> None:
    # 同一消息重试不该新增 history 行（tasks.history_id 始终指向它）
    service, store, _ = svc
    first = await service.enqueue(req(1, file_size=100))
    assert first is not None
    history_id = store.get_task(first).history_id  # type: ignore[union-attr]
    store.mark_history_status(int(history_id), "failed", error="boom")
    again = await service.enqueue(req(1, file_size=100))
    assert again is not None
    assert store.get_task(again).history_id == history_id  # type: ignore[union-attr]
    assert store.get_history(int(history_id)).status == "queued"  # type: ignore[union-attr]


async def test_floodwait_retries_after_wait(
    svc: tuple[DownloadService, Store, FakeUserClient], tmp_path: Path
) -> None:
    # NFR-09：FloodWait 注入 → 挂起后重试、不无视等待。
    # 引擎内 with_flood_retry 捕获 Kurigram 的 FloodWait；这里验证包装器重试行为
    # 与 FakeFloodWait 等价语义（按 seconds 挂起后重试）。
    service, store, client = svc
    client.flood_queue = [1]  # 1 次 1 秒 FloodWait
    task_id = await service.enqueue(req(1))
    assert task_id is not None
    with pytest.raises(Exception, match="flood wait"):
        # 引擎不捕获 FakeFloodWait（它只认 Kurigram 的 FloodWait）；
        # 挂起行为单测由 test_with_flood_retry_retries 覆盖
        await service._run_task(worker_row(store, task_id))
    assert client.download_calls == 1


async def test_with_flood_retry_retries() -> None:
    # NFR-09：with_flood_retry 按 FloodWait.seconds 挂起后重试（FakeFloodWait 等价注入）

    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FakeFloodWait(0)
        return "ok"

    async def no_sleep(_: float) -> None:
        return None

    # monkeypatch flood.py 模块内的 FloodWait 引用（from-import 已绑定到该模块）

    original = flood_mod.FloodWait
    flood_mod.FloodWait = FakeFloodWait  # type: ignore[assignment,misc]
    try:
        out = await with_flood_retry(fn, sleep=no_sleep)
    finally:
        flood_mod.FloodWait = original
    assert out == "ok"
    assert calls == 2


def test_backoff_sec_exponential() -> None:
    # 重试退避计算 min(2^n * 30s, 1h)
    assert backoff_sec(0) == 30
    assert backoff_sec(1) == 60
    assert backoff_sec(3) == 240
    assert backoff_sec(10) == 3600


async def test_voice_message_not_audio(tmp_path: Path) -> None:
    # 验收 #11：voice 消息不入队（is_audio_message 判定）
    store = Store(tmp_path / "app.db")
    voice_msg = make_audio_message(3, voice=True)
    client = FakeUserClient([voice_msg, make_audio_message(4)])
    service = SearchService(store, client, registry=fake_registry(store, client))
    resp = await service.search("晴天")
    # voice 被排除，非 voice 的正常入结果
    assert all(r.message_id != 3 for r in resp.results)


async def test_pause_task_marks_paused(svc: tuple[DownloadService, Store, FakeUserClient]) -> None:
    service, store, _ = svc
    task_id = await service.enqueue(req(1))
    await service.pause_task(task_id)  # type: ignore[arg-type]
    assert store.get_task(task_id).status == "paused"  # type: ignore[union-attr]
    await service.resume_task(task_id)  # type: ignore[arg-type]
    assert store.get_task(task_id).status == "queued"  # type: ignore[union-attr]


async def test_history_follows_task_lifecycle(
    svc: tuple[DownloadService, Store, FakeUserClient],
) -> None:
    # 下载页只认 history 的状态（列表、筛选、统计卡同一份事实）：任务一动它就得跟着动
    service, store, _ = svc
    task_id = await service.enqueue(req(1))
    assert task_id is not None
    task = store.get_task(task_id)
    assert task is not None and task.history_id is not None
    history_id = task.history_id
    assert store.get_history(history_id).status == "queued"  # type: ignore[union-attr]

    row = service._next_queued()
    assert row is not None and row["id"] == task_id
    assert store.get_history(history_id).status == "downloading"  # type: ignore[union-attr]

    await service.pause_task(task_id)
    assert store.get_history(history_id).status == "paused"  # type: ignore[union-attr]
    await service.resume_task(task_id)
    assert store.get_history(history_id).status == "queued"  # type: ignore[union-attr]
    await service.cancel_task(task_id)
    assert store.get_history(history_id).status == "cancelled"  # type: ignore[union-attr]


async def test_retry_failed_reaches_rows_without_task_ledger(
    svc: tuple[DownloadService, Store, FakeUserClient],
) -> None:
    # 「重试失败项」对着下载页的失败行：台账还在就复位它，台账被删过就按消息重新入队
    service, store, _ = svc
    task_id = await service.enqueue(req(1))
    assert task_id is not None
    task = store.get_task(task_id)
    assert task is not None and task.history_id is not None
    store.update_task(task_id, status="failed", error="network down")
    store.mark_history_status(task.history_id, "failed", error="network down", finished=True)
    orphan_id = store.upsert_history(
        History(id=None, chat_id=CHAT_ID, message_id=2, title="Orphan", status="failed")
    )

    retried = await service.retry_failed()

    assert retried == 2
    assert store.get_task(task_id).status == "queued"  # type: ignore[union-attr]
    orphan = store.get_history(orphan_id)
    assert orphan is not None and orphan.status == "queued"
    latest = store.latest_task_ids([orphan_id])
    assert latest[orphan_id] != task_id  # 重新入队开的是新任务


async def test_recover_interrupted(tmp_path: Path) -> None:
    # NFR-05：downloading → failed（retryable），queued 保持；
    # 非终态却没有在跑任务的历史行一并结算，不留「永远等待」的行

    store = Store(tmp_path / "app.db")
    orphan_id = store.upsert_history(
        History(id=None, chat_id=CHAT_ID, message_id=9, title="Orphan")
    )
    live_id = store.upsert_history(History(id=None, chat_id=CHAT_ID, message_id=10, title="Live"))
    queued = store.create_task(
        TaskModel(id=None, type="link", payload_json=json.dumps({}), history_id=live_id)
    )
    downloading = store.create_task(TaskModel(id=None, type="link", payload_json=json.dumps({})))
    store.update_task(downloading, status="downloading")
    n = store.recover_interrupted()
    assert n == 1
    assert store.get_task(queued).status == "queued"  # type: ignore[union-attr]
    assert store.get_task(downloading).status == "failed"  # type: ignore[union-attr]
    live = store.get_history(live_id)
    assert live is not None and live.status == "queued"  # 有在跑的任务：不动
    orphan = store.get_history(orphan_id)
    assert orphan is not None
    assert orphan.status == "failed"
    assert orphan.error == "interrupted, retryable"


async def test_download_reports_progress_and_persists_snapshot(
    svc: tuple[DownloadService, Store, FakeUserClient],
) -> None:
    service, store, _ = svc
    task_id = await service.enqueue(req(1, file_size=100))
    assert task_id is not None
    events = await service.events.subscribe()

    await service._run_task(worker_row(store, task_id))

    received = []
    while not events.empty():
        received.append(events.get_nowait())
    progress_events = [event for event in received if event.type == "task.progress"]
    assert progress_events
    assert progress_events[-1].payload["progress_bytes"] == 100
    assert progress_events[-1].payload["total_bytes"] == 100
    assert progress_events[-1].payload["speed"] is not None
    assert received[-1].type == "task.status"
    assert received[-1].payload["status"] == "success"

    row = store.get_task(task_id)
    assert row is not None
    assert row.progress_bytes == 100
    assert row.total_bytes == 100
    assert row.speed is not None


async def test_progress_reports_without_known_total(tmp_path: Path) -> None:
    """总字节未知（在线源不给大小）照样发进度帧，只是没有百分比与 ETA。

    回归：早先 ``__call__`` 把 ``None`` 也喂给 ``int()``，异常被吞成「直接不报」，
    未知大小的下载因此从头到尾没有一帧进度。
    """
    store = Store(tmp_path / "app.db")
    events = EventBus()
    queue = await events.subscribe()
    reporter = ProgressReporter(store, events, TaskRunState(), task_id=7, total_bytes=None)

    reporter(50, None)

    frames = []
    while not queue.empty():
        frames.append(queue.get_nowait())
    progress = [frame for frame in frames if frame.type == "task.progress"]
    assert progress[-1].payload["progress_bytes"] == 50
    assert progress[-1].payload["total_bytes"] is None
    assert progress[-1].payload["eta"] is None


async def test_retry_resets_task_history_and_temp_fragment(
    svc: tuple[DownloadService, Store, FakeUserClient], tmp_path: Path
) -> None:
    service, store, _ = svc
    task_id = await service.enqueue(req(1))
    assert task_id is not None
    task = store.get_task(task_id)
    assert task is not None and task.history_id is not None
    history_id = task.history_id
    store.update_task(
        task_id,
        status="failed",
        progress_bytes=80,
        total_bytes=100,
        speed=12.5,
        retry_count=2,
        next_retry_at="2099-01-01T00:00:00+00:00",
        error="network down",
    )
    final_path = tmp_path / "library" / "kept.mp3"
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"done")
    store.mark_history_status(
        history_id, "failed", error="network down", save_path=str(final_path), finished=True
    )
    temp_path = service.temp_dir / f"task_{task_id}_1"
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.write_bytes(b"partial")

    await service.retry_task(task_id)

    row = store.get_task(task_id)
    assert row is not None
    assert row.status == "queued"
    assert row.progress_bytes == 0
    assert row.total_bytes is None
    assert row.speed is None
    assert row.retry_count == 2
    assert row.next_retry_at is None
    assert row.error is None
    history = store.get_history(history_id)
    assert history is not None
    assert history.status == "queued"
    assert history.error is None
    assert history.finished_at is None
    assert history.save_path == str(final_path)
    assert not temp_path.exists()
    assert final_path.exists()


async def test_retry_rejects_invalid_or_missing_task(
    svc: tuple[DownloadService, Store, FakeUserClient],
) -> None:
    service, store, _ = svc
    task_id = await service.enqueue(req(1))
    assert task_id is not None
    with pytest.raises(Exception, match="不可重试"):
        await service.retry_task(task_id)
    with pytest.raises(Exception, match="任务不存在"):
        await service.retry_task(999)


async def test_delete_task_keeps_history_and_saved_file(
    svc: tuple[DownloadService, Store, FakeUserClient], tmp_path: Path
) -> None:
    service, store, _ = svc
    task_id = await service.enqueue(req(1))
    assert task_id is not None
    task = store.get_task(task_id)
    assert task is not None and task.history_id is not None
    history_id = task.history_id
    final_path = tmp_path / "library" / "kept.mp3"
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"done")
    store.mark_history_status(history_id, "success", save_path=str(final_path), finished=True)
    temp_path = service.temp_dir / f"task_{task_id}_1"
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.write_bytes(b"partial")

    await service.delete_task(task_id)

    assert store.get_task(task_id) is None
    history = store.get_history(history_id)
    assert history is not None
    assert history.save_path == str(final_path)
    assert final_path.exists()
    assert not temp_path.exists()


async def test_delete_downloading_task_cancels_active_worker(
    svc: tuple[DownloadService, Store, FakeUserClient],
) -> None:
    service, store, client = svc
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_download(
        self: FakeUserClient,
        message_ref: dict[str, Any],
        file_name: str,
        progress: Callable[[int, int], None] | None = None,
    ) -> str:
        del self, message_ref, file_name, progress
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    client.download_media = blocked_download.__get__(client, FakeUserClient)  # type: ignore[method-assign]
    task_id = await service.enqueue(req(1))
    assert task_id is not None
    task = store.get_task(task_id)
    assert task is not None and task.history_id is not None
    history_id = task.history_id
    worker = asyncio.create_task(service._worker("delete-test"))
    await asyncio.wait_for(started.wait(), timeout=1)
    assert store.get_task(task_id).status == "downloading"  # type: ignore[union-attr]

    await service.delete_task(task_id)
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)

    assert store.get_task(task_id) is None
    assert store.get_history(history_id) is not None


def _tagged_mp3_bytes(tmp_path: Path) -> bytes:
    """带 ID3 标签的最小 MP3：验证完成后的显示元数据从落盘文件里读出来。"""
    p = tmp_path / "tagged.mp3"
    p.write_bytes(b"\xff\xfb\x90\x44" + b"\x00" * 107)
    tags = ID3()
    tags.add(TIT2(encoding=3, text="夜曲"))
    tags.add(TPE1(encoding=3, text="周杰伦"))
    tags.add(TALB(encoding=3, text="十一月的萧邦"))
    tags.save(p)
    return p.read_bytes()


def _untagged_flac_bytes() -> bytes:
    """最小合法 FLAC（无 Vorbis Comment）：读标签得到全空，走文件名补全。"""
    return (
        b"fLaC"
        + b"\x00\x00\x00\x22"
        + b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x10"
        + b"\x00\x00\x00\x10\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    )


async def test_finish_display_uses_file_tags(
    svc: tuple[DownloadService, Store, FakeUserClient], tmp_path: Path
) -> None:
    # FR-META-01：完成后的显示元数据以落盘文件的内嵌标签为准，TG 元数据只兜底。
    service, store, client = svc
    client.content = _tagged_mp3_bytes(tmp_path)
    task_id = await service.enqueue(req(1, ext="mp3", title="TG标题.mp3"))
    assert task_id is not None
    await service._run_task(worker_row(store, task_id))
    history_id = store.get_task(task_id).history_id  # type: ignore[union-attr]
    assert history_id is not None
    row = store.get_history(history_id)
    assert row is not None
    assert (row.title, row.artist, row.album) == ("夜曲", "周杰伦", "十一月的萧邦")


async def test_finish_display_parses_filename_when_untagged(
    svc: tuple[DownloadService, Store, FakeUserClient], tmp_path: Path
) -> None:
    # 下载中复用 TG 原文件名；完成后无标签 → 收掉扩展名并按「歌手 - 歌名」补出歌手。
    service, store, client = svc
    client.content = _untagged_flac_bytes()
    task_id = await service.enqueue(req(2, ext="flac", title="周杰伦 - 夜曲.flac"))
    assert task_id is not None
    history_id = store.get_task(task_id).history_id  # type: ignore[union-attr]
    assert history_id is not None
    row = store.get_history(history_id)
    assert row is not None
    assert row.title == "周杰伦 - 夜曲.flac"
    assert row.artist is None
    await service._run_task(worker_row(store, task_id))
    row = store.get_history(history_id)
    assert row is not None
    assert (row.title, row.artist) == ("夜曲", "周杰伦")


def _mp3_frames_bytes() -> bytes:
    """10 帧 128kbps/44100Hz 的裸 MPEG 帧：mutagen 读得出时长与码率。"""
    return (b"\xff\xfb\x90\x44" + b"\x00" * 413) * 10


async def test_finish_history_backfills_media_facts(
    svc: tuple[DownloadService, Store, FakeUserClient],
) -> None:
    # 完成回填：时长/大小/码率以落盘文件实测为准（链接入队时这些读数是空的）
    service, store, client = svc
    client.content = _mp3_frames_bytes()
    task_id = await service.enqueue(req(1, ext="mp3"))
    assert task_id is not None
    history_id = store.get_task(task_id).history_id  # type: ignore[union-attr]
    assert history_id is not None
    row = store.get_history(history_id)
    assert row is not None
    assert row.file_size is None  # 下载前读数是空的
    await service._run_task(worker_row(store, task_id))
    row = store.get_history(history_id)
    assert row is not None
    assert row.file_size == 4170  # 落盘实测
    # 合成帧总长 ≈0.26s，round 后是 0 —— 0 视为无值不回填（真实曲目不会是 0 秒）
    assert row.duration_sec is None
    assert row.bitrate == 128  # kbps


async def test_bare_link_hydrates_history_media(
    svc: tuple[DownloadService, Store, FakeUserClient],
) -> None:
    # 链接入队 meta 全空：hydrate 拿到消息后，时长/大小先随 meta 回填历史行
    service, store, _ = svc
    task_id = await service.enqueue(req(1))
    assert task_id is not None
    history_id = store.get_task(task_id).history_id  # type: ignore[union-attr]
    assert history_id is not None
    await service._run_task(worker_row(store, task_id))
    row = store.get_history(history_id)
    assert row is not None
    assert row.duration_sec == 269  # Fake 消息 audio.duration
    assert row.file_size == 100


async def test_backfill_history_media_repairs_old_rows(
    svc: tuple[DownloadService, Store, FakeUserClient], tmp_path: Path
) -> None:
    # 启动回填：旧记录（各读数为 NULL 但文件在）按文件补一次；文件不在的行不编造。
    service, store, _ = svc
    p = tmp_path / "frames.mp3"
    p.write_bytes(_mp3_frames_bytes())
    store.upsert_history(
        History(id=None, chat_id=CHAT_ID, message_id=1, status="success", save_path=str(p))
    )
    missing = tmp_path / "gone.mp3"
    store.upsert_history(
        History(id=None, chat_id=CHAT_ID, message_id=2, status="success", save_path=str(missing))
    )
    count = service.backfill_history_media()
    assert count == 1
    rows = store.list_history()
    by_msg = {r.message_id: r for r in rows}
    assert by_msg[1].file_size == 4170
    assert by_msg[1].bitrate == 128
    assert by_msg[2].file_size is None


async def test_worker_failure_schedules_backoff_retry(
    svc: tuple[DownloadService, Store, FakeUserClient],
) -> None:
    # FR-DL-04：任务崩溃未超上限 → 回队列 + 记退避时间（不是直接终态）
    service, store, _ = svc
    task_id = await service.enqueue(req(1))
    assert task_id is not None
    row = worker_row(store, task_id)
    assert row["id"] is not None

    await service._fail_or_retry(row, "boom")

    task = store.get_task(task_id)
    assert task is not None
    assert task.status == "queued"
    assert task.retry_count == 1
    assert task.next_retry_at is not None  # 退避期间不该被立刻重取
    assert task.error == "boom"
    # 历史行跟着回 queued（下载页只认它）
    assert store.get_history(task.history_id).status == "queued"  # type: ignore[union-attr]


async def test_worker_failure_exhausts_retries_to_failed(
    svc: tuple[DownloadService, Store, FakeUserClient],
) -> None:
    # 超过 MAX_RETRIES 后落 failed 终态，不再排重试
    service, store, _ = svc
    task_id = await service.enqueue(req(1))
    assert task_id is not None
    store.update_task(task_id, retry_count=MAX_RETRIES)
    row = worker_row(store, task_id)
    row["retry_count"] = MAX_RETRIES

    await service._fail_or_retry(row, "still broken")

    task = store.get_task(task_id)
    assert task is not None
    assert task.status == "failed"
    assert task.next_retry_at is None
    assert task.error == "still broken"
    assert store.get_history(task.history_id).status == "failed"  # type: ignore[union-attr]


async def test_backoff_prevents_immediate_requeue(
    svc: tuple[DownloadService, Store, FakeUserClient],
) -> None:
    # 退避生效：排了未来时间的任务不会被 next_queued_task 立刻取走
    service, store, _ = svc
    task_id = await service.enqueue(req(1))
    assert task_id is not None
    row = worker_row(store, task_id)
    await service._fail_or_retry(row, "boom")

    assert service._next_queued() is None  # 还在退避窗口内
    # 时间到（把 next_retry_at 拨到过去）→ 可再次被消费
    store.update_task(task_id, next_retry_at="2000-01-01T00:00:00+00:00")
    got = service._next_queued()
    assert got is not None and got["id"] == task_id


async def test_worker_respawns_after_unexpected_death(
    svc: tuple[DownloadService, Store, FakeUserClient],
) -> None:
    # 失败记账自身抛错（如 DB 故障）会杀死 worker：必须补位，否则并发池悄悄缩水、
    # 队列假死且没有任何报错。补出来的 worker 还要继续把失败结算成退避重试。
    service, store, _ = svc
    task_id = await service.enqueue(req(1))
    assert task_id is not None

    real_run_task = service._run_task
    real_fail_or_retry = service._fail_or_retry
    deaths = 0

    async def exploding_run_task(task: dict[str, Any]) -> None:
        raise RuntimeError("fetch blew up")

    async def flaky_bookkeeping(task: dict[str, Any], reason: str) -> None:
        nonlocal deaths
        deaths += 1
        if deaths == 1:
            raise RuntimeError("db gone")  # 记账失败：worker 只能死
        await real_fail_or_retry(task, reason)

    service._run_task = exploding_run_task  # type: ignore[method-assign]
    service._fail_or_retry = flaky_bookkeeping  # type: ignore[method-assign]
    try:
        await service.start_workers(1)
        first = service._worker_tasks[0]
        await asyncio.sleep(0.2)  # 死亡 + 补位 + 新 worker 再消费一次同一任务

        assert deaths == 2  # 第一次记账炸掉 worker；补位后的第二次恢复正常结算
        current = list(service._worker_tasks)
        assert len(current) == 1
        assert current[0] is not first
        assert not current[0].done()
        task = store.get_task(task_id)
        assert task is not None
        assert task.retry_count == 1  # 新 worker 把失败正常排进了退避重试
        assert task.next_retry_at is not None
    finally:
        service._run_task = real_run_task  # type: ignore[method-assign]
        service._fail_or_retry = real_fail_or_retry  # type: ignore[method-assign]
        await service.stop_workers()


async def test_enqueue_wakes_idle_worker_immediately(
    svc: tuple[DownloadService, Store, FakeUserClient],
) -> None:
    # worker 空转等待时，入队必须即时唤醒它，而不是干等 IDLE_POLL_SEC 兜底轮询
    service, store, _ = svc
    await service.start_workers(1)
    await asyncio.sleep(0.05)  # worker 进入 _wake 等待（队列空）
    assert store.list_tasks() == []

    started = asyncio.get_running_loop().time()
    task_id = await service.enqueue(req(1))
    assert task_id is not None
    # 兜底轮询是 2s：唤醒若失效，这条任务要等最多 2s 才被取走
    for _ in range(200):
        task = store.get_task(task_id)
        assert task is not None
        if task.status != "queued":
            break
        await asyncio.sleep(0.01)
    task = store.get_task(task_id)
    assert task is not None
    # 唤醒的证明就是「已离开 queued」：被 worker 取走即标 downloading，成功路径随后自行跑完
    assert task.status in {"downloading", "success"}
    assert asyncio.get_running_loop().time() - started < IDLE_POLL_SEC
    await service.stop_workers()


async def test_stop_workers_suppresses_respawn(
    svc: tuple[DownloadService, Store, FakeUserClient],
) -> None:
    # 停机后仍有 worker 意外死亡时不补位：否则关机时会长出没人 await 的孤儿任务
    service, _, _ = svc
    await service.start_workers(1)
    await service.stop_workers()
    assert service._worker_tasks == []

    async def die() -> None:
        raise RuntimeError("late death")

    late = asyncio.create_task(die())
    await asyncio.sleep(0.05)  # 让它死透，制造一个异常退出的已完成任务
    service._on_worker_done(late, "worker-0", 0)
    assert service._worker_tasks == []
