"""SSE 事件流：空闲心跳保活、断开即摘订阅（总线订阅者不泄漏）。"""

from __future__ import annotations

import asyncio

from app.events import Event, EventBus, sse_event_stream


async def test_idle_stream_emits_ping_frames() -> None:
    # 反代会切断没有任何字节的空闲响应：等待期间必须持续产出注释帧
    bus = EventBus()
    stream = sse_event_stream(bus, ping_sec=0.01)
    assert await asyncio.wait_for(anext(stream), timeout=1) == ": ping\n\n"
    await stream.aclose()


async def test_event_frame_is_sse_data_frame() -> None:
    bus = EventBus()
    stream = sse_event_stream(bus, ping_sec=0.01)
    assert await asyncio.wait_for(anext(stream), timeout=1) == ": ping\n\n"  # 订阅已完成
    await bus.publish(Event("task.status", {"task_id": 7, "status": "success", "error": None}))
    frame = await asyncio.wait_for(anext(stream), timeout=1)
    assert frame.startswith("data: ")
    assert "task.status" in frame
    assert '"task_id": 7' in frame
    assert frame.endswith("\n\n")
    await stream.aclose()


async def test_disconnect_unsubscribes() -> None:
    # StreamingResponse 在客户端断开时关闭生成器（aclose 注入 GeneratorExit）：
    # finally 必须摘订阅，否则断连多的部署里订阅者集合只增不减
    bus = EventBus()
    stream = sse_event_stream(bus, ping_sec=0.01)
    assert await asyncio.wait_for(anext(stream), timeout=1) == ": ping\n\n"
    assert len(bus._subscribers) == 1
    await stream.aclose()
    assert len(bus._subscribers) == 0
