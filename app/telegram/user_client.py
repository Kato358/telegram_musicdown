"""UserClient：登录、搜索（FR-AUTH-01/03、FR-SEARCH-01、SDD §2.1）。

取消息与下载是两个会话共用的动作，连同底层异常翻译都在 ``app.telegram.media``；这里
只负责登录账号自己的生命周期与搜索。交互式只读搜索是唯一不经 ``with_flood_retry``
的调用（编码规范 §2.3）：按 FR-SEARCH-01 把 FloodWait 原样透出，不挂起用户。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from pyrogram.client import Client
from pyrogram.enums import MessagesFilter
from pyrogram.errors import (
    PasswordHashInvalid,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberBanned,
    PhoneNumberInvalid,
    SessionPasswordNeeded,
    Unauthorized,
)

# RPCError 只有从定义它的模块导入 mypy 才看得见：pyrogram.errors 是用 `import *`
# 逐层再导出的，strict 模式下那条链不被当成显式再导出（运行期是同一个类对象）。
from pyrogram.errors.rpc_error import RPCError
from pyrogram.types import Message

from app.config import SecretConfig
from app.errors import AuthError
from app.telegram.flood import with_flood_retry
from app.telegram.media import download_media as media_download
from app.telegram.media import get_messages as media_get_messages
from app.telegram.media import message_dict, translate_rpc

logger = logging.getLogger(__name__)

MAX_DIALOG_SCAN = 200  # 候选源扫描上限：一次 get_dialogs，不做逐会话额外请求（FR-SRC-05）
CONNECT_TIMEOUT_SEC = 30  # Kurigram 对连不上的代理会无限重试，故本层给硬超时（FR-AUTH-03）
SESSION_NAME = "musicdown"  # 会话文件名（data/sessions/musicdown.session）；登出按此名删除



def _proxy_text(cfg: SecretConfig) -> str:
    proxy = cfg.proxy
    if proxy is None:
        return "直连"
    return f"{proxy.scheme} {proxy.hostname}:{proxy.port}"


def _connect_error(cfg: SecretConfig, cause: BaseException) -> AuthError:
    """连不上 Telegram（超时/被拒）：给出可操作的修复方向（FR-AUTH-03）。"""
    logger.warning("telegram unreachable via %s: %s", _proxy_text(cfg), cause)
    return AuthError(
        "connect_failed",
        f"连不上 Telegram（{_proxy_text(cfg)}）：检查网络，或回第 1 步改代理设置后重试",
    )


def _proxy_dict(cfg: SecretConfig) -> dict[str, Any] | None:
    if cfg.proxy is None:
        return None
    return {
        "scheme": cfg.proxy.scheme,
        "hostname": cfg.proxy.hostname,
        "port": cfg.proxy.port,
        "username": cfg.proxy.username,
        "password": cfg.proxy.password,
    }


class UserClient:
    """User Client 包装：登录、搜索、取消息、下载。"""

    def __init__(self, secrets: SecretConfig, session_dir: Any) -> None:
        self.secrets = secrets
        self.client = Client(
            SESSION_NAME,
            api_id=secrets.api_id,
            api_hash=secrets.api_hash,
            workdir=str(session_dir),
            # Kurigram 的 ProxyDict 是按协议区分的 TypedDict 联合，scheme 来自配置字符串，
            # 静态收窄不了（运行期由 normalize_proxy 校验）
            proxy=_proxy_dict(secrets),  # type: ignore[arg-type]
            in_memory=False,
        )
        #: update 排空协程（见 ``_start_update_drain``）：连接期一直活着，断连时 cancel
        self._update_drain: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        """连接（FR-AUTH-01/03）：Kurigram 的会话启动对网络失败是无限重试，故设硬超时。"""
        try:
            await asyncio.wait_for(self.client.connect(), timeout=CONNECT_TIMEOUT_SEC)
        except TimeoutError as e:
            await self.disconnect()
            raise _connect_error(self.secrets, e) from e
        except (OSError, ConnectionError) as e:
            raise _connect_error(self.secrets, e) from e
        self._start_update_drain()

    def _start_update_drain(self) -> None:
        """排空并丢弃 Kurigram 的 update 队列（kurigram#467，内存只增不减的根因）。

        本客户端只调 ``connect()``：登录流程要自己控验证码/两步密码，走不了 ``start()``
        的交互式授权。而 ``Dispatcher.start()``——唯一会 ``get()``
        ``dispatcher.updates_queue`` 的地方——只在 ``Client.initialize()`` 里被调，后者
        只由 ``Client.start()`` 触发。于是 Telegram 推过来的每一条 update 都堆进那个
        **无界** ``asyncio.Queue``，进程不重启就一直涨（实测约 1.3KB/条，闲置在线的
        音乐账号几天就能堆出几百 MB）。

        本客户端不注册任何 handler：update 对业务没有任何用处，取走丢掉即可——队列随取
        随空，占用回到常数级。
        """
        if self.client.dispatcher.handler_worker_tasks:
            return  # 有人启动过 Dispatcher：队列有真消费者，别把 update 抢走
        if self._update_drain is not None and not self._update_drain.done():
            return
        self._update_drain = asyncio.create_task(self._drain_updates())

    async def _drain_updates(self) -> None:
        """取走即丢：唯一的退出方式是 ``disconnect()`` 里的 cancel。"""
        queue = self.client.dispatcher.updates_queue
        while True:
            await queue.get()

    async def disconnect(self) -> None:
        await self._stop_update_drain()
        try:
            await self.client.disconnect()
        except Exception:  # noqa: BLE001  断连失败不该盖住真正的错误
            logger.warning("user client disconnect failed", exc_info=True)

    async def _stop_update_drain(self) -> None:
        task, self._update_drain = self._update_drain, None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def is_authorized(self) -> bool:
        """会话是否已登录（FR-AUTH-02）：未授权时 get_me 报 Unauthorized。"""
        try:
            await with_flood_retry(self.client.get_me, label="get_me")
        except Unauthorized:
            return False
        except (OSError, ConnectionError) as e:
            raise _connect_error(self.secrets, e) from e
        return True

    async def send_code(self, phone: str) -> str:
        """发验证码（FR-AUTH-01）。返回 phone_code_hash。"""
        try:
            sent = await with_flood_retry(
                lambda: self.client.send_phone_number_code(phone), label="send_code"
            )
        except PhoneNumberInvalid as e:
            raise AuthError("phone_invalid", "手机号无效：要带国家码，例如 +8613800000000") from e
        except PhoneNumberBanned as e:
            raise AuthError("phone_banned", "该手机号已被 Telegram 封禁，无法登录") from e
        except (OSError, ConnectionError) as e:
            raise _connect_error(self.secrets, e) from e
        return sent.phone_code_hash or ""

    async def sign_in(
        self, phone: str, code: str, phone_code_hash: str, password: str | None = None
    ) -> None:
        """登录（FR-AUTH-01）；两步验证缺密码时抛 password_required，由前端展开密码框。"""
        try:
            await with_flood_retry(
                lambda: self.client.sign_in(phone, phone_code_hash, code), label="sign_in"
            )
            return
        except SessionPasswordNeeded as e:
            if not password:
                raise AuthError("password_required", "该账号开启了两步验证：请填写登录密码") from e
        except PhoneCodeInvalid as e:
            raise AuthError("code_invalid", "验证码不正确：Telegram 给的是 5 位数字") from e
        except PhoneCodeExpired as e:
            raise AuthError("code_expired", "验证码已过期：请重新发送验证码") from e
        except (OSError, ConnectionError) as e:
            raise _connect_error(self.secrets, e) from e
        except RPCError as e:  # 其余 RPC 失败如实上报，不假装成功
            raise AuthError("sign_in_failed", f"登录失败：{type(e).__name__}") from e
        try:
            await with_flood_retry(
                lambda: self.client.check_password(password or ""), label="check_password"
            )
        except PasswordHashInvalid as e:
            raise AuthError("password_invalid", "两步验证密码不正确") from e

    async def get_me(self) -> dict[str, Any]:
        try:
            me = await with_flood_retry(self.client.get_me, label="get_me")
        except (OSError, ConnectionError) as e:
            raise _connect_error(self.secrets, e) from e
        return {
            "id": me.id,
            "display_name": f"{me.first_name or ''} {me.last_name or ''}".strip(),
            "username": me.username,
            "premium": bool(me.is_premium),
            "connected": True,
        }

    async def list_dialogs(self, limit: int = MAX_DIALOG_SCAN) -> list[dict[str, Any]]:
        """账号已加入的对话（FR-SRC-05）：一次扫描取回，供候选源筛选。

        只取对话对象自带字段（含成员数，若 Telegram 给了），不做逐会话 get_chat——
        每个候选再发一次请求会成倍放大 FloodWait 风险。
        """

        async def _collect() -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            async for dialog in self.client.get_dialogs(limit=limit):
                chat = dialog.chat
                # Kurigram 的 Chat.id 可空（min 对话）：没有 id 就做不了音乐源
                if chat is None or chat.id is None:
                    continue
                chat_type = getattr(getattr(chat, "type", None), "value", None) or "user"
                if chat_type == "bot":  # 机器人不是音乐源
                    continue
                title = getattr(chat, "title", None)
                if not title:
                    first = getattr(chat, "first_name", None) or ""
                    last = getattr(chat, "last_name", None) or ""
                    title = f"{first} {last}".strip() or str(chat.id)
                out.append(
                    {
                        "chat_id": chat.id,
                        "title": title,
                        "username": getattr(chat, "username", None),
                        "type": chat_type,
                        "members": getattr(chat, "members_count", None),
                    }
                )
                if len(out) >= limit:
                    break
            return out

        return await with_flood_retry(_collect, label="get_dialogs")

    async def get_chat(self, entity: str | int) -> dict[str, Any]:
        """添加源：get_chat（FR-SRC-01）；失败翻译为领域异常。"""
        try:
            chat = await with_flood_retry(lambda: self.client.get_chat(entity), label="get_chat")
        except RPCError as e:
            raise translate_rpc(e) from e
        chat_id = getattr(chat, "id", 0)
        title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or str(chat_id)
        chat_type = getattr(chat, "type", None)
        type_val = getattr(chat_type, "value", None) or "user"
        return {
            "chat_id": chat_id,
            "title": title,
            "type": type_val,
            "username": getattr(chat, "username", None),
            "can_view_history": True,
        }

    async def search_messages(
        self, chat_id: int, query: str, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        """对话内搜索（FR-SEARCH-01）：服务端 messages.search + Audio filter。

        故意**不**经 ``with_flood_retry``（编码规范 §2.3 的例外）：这是人在等结果的交互式
        只读请求，服务器要我们等 30s 就睡 30s（甚至重试 5 次）比直接说「限流了，稍后再搜」
        更糟——FR-SEARCH-01 要的就是限流可见，故 FloodWait 按 ``reason="flood_wait"`` 透出。
        """
        try:
            gen = self.client.search_messages(
                chat_id,
                limit=limit,
                offset=offset,
                query=query,
                filter=MessagesFilter.AUDIO,
            )
            msgs: list[Message] = []
            async for m in gen:
                msgs.append(m)
                if len(msgs) >= limit:
                    break
        except RPCError as e:
            raise translate_rpc(e) from e
        return [message_dict(m) for m in msgs]

    async def search_global(self, query: str, limit: int) -> list[dict[str, Any]]:
        """全账号搜索（FR-SEARCH-01 global 模式）：服务端 messages.searchGlobal + Audio filter。

        一次请求覆盖账号加入的**全部**对话（含未添加为音乐源的频道），免去逐源扇出；
        代价是结果按全局时间序、无法限定源集合，故服务层在本地按 ``chat_id`` 归源/筛选。

        与 ``search_messages`` 同一取舍：不经 ``with_flood_retry``——交互式只读请求，
        限流按 ``reason="flood_wait"`` 透出，不挂起用户。
        """
        try:
            msgs: list[Message] = []
            async for m in self.client.search_global(
                query=query, filter=MessagesFilter.AUDIO, limit=limit
            ):
                msgs.append(m)
                if len(msgs) >= limit:
                    break
        except RPCError as e:
            raise translate_rpc(e) from e
        return [message_dict(m) for m in msgs]

    async def get_messages(self, chat_id: int, message_ids: list[int]) -> list[dict[str, Any]]:
        """按 id 取消息 dict（DownloadService meta 补全用）。"""
        return await media_get_messages(self.client, chat_id, message_ids)

    async def download_media(
        self,
        message_ref: dict[str, Any],
        file_name: str,
        progress: Callable[[int, int], None] | None = None,
    ) -> str | None:
        """按 (chat_id, message_id) 下载到临时路径（FR-DL-01）。

        只服务「登录账号看得见」的消息（频道链接、搜索结果）。转发进 bot 私聊的消息
        由 bot 会话取——见 ``app.telegram.media`` 的模块说明。
        """
        return await media_download(self.client, message_ref, file_name, progress)



