"""User Client 的 update 队列排空（kurigram#467）。

只调 ``connect()`` 的客户端不会启动 Kurigram 的 Dispatcher，而它是唯一消费
``dispatcher.updates_queue`` 的地方：不排空就等于按时间线性涨的内存（实测 ~1.3KB/条，
闲置在线的账号几天能堆几百 MB）。这里用替身验证「连接期随取随空、断连后停下」。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.config import SecretConfig
from app.telegram.user_client import UserClient


class _StubDispatcher:
    def __init__(self) -> None:
        self.updates_queue: asyncio.Queue[Any] = asyncio.Queue()
        self.handler_worker_tasks: list[Any] = []


class _StubKurigram:
    """只提供 connect/disconnect 与 dispatcher 的 Kurigram 替身（不连网）。"""

    def __init__(self) -> None:
        self.dispatcher = _StubDispatcher()
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False


def _client(tmp_path: Path) -> tuple[UserClient, _StubKurigram]:
    client = UserClient(SecretConfig(), tmp_path)
    stub = _StubKurigram()
    client.client = stub  # type: ignore[assignment]  换成只记调用的替身
    return client, stub


async def test_connect_drains_incoming_updates(tmp_path: Path) -> None:
    client, stub = _client(tmp_path)
    await client.connect()

    for i in range(1000):
        stub.dispatcher.updates_queue.put_nowait({"update": i})
    await asyncio.sleep(0)

    assert stub.connected is True
    assert stub.dispatcher.updates_queue.qsize() == 0, "队列必须随取随空，不随时间堆积"


async def test_disconnect_stops_the_drain(tmp_path: Path) -> None:
    client, stub = _client(tmp_path)
    await client.connect()
    await client.disconnect()

    stub.dispatcher.updates_queue.put_nowait({"update": 1})
    await asyncio.sleep(0)

    assert stub.connected is False
    assert stub.dispatcher.updates_queue.qsize() == 1, "断连后排空必须停掉，别再兜着队列不放"


async def test_drain_stays_away_from_a_running_dispatcher(tmp_path: Path) -> None:
    # 真有人启动过 Dispatcher（handler_worker_tasks 非空）时不能抢它的 update
    client, stub = _client(tmp_path)
    stub.dispatcher.handler_worker_tasks.append(object())
    await client.connect()

    stub.dispatcher.updates_queue.put_nowait({"update": 1})
    await asyncio.sleep(0)

    assert stub.dispatcher.updates_queue.qsize() == 1
