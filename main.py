"""防撤回查询插件。

缓存机器人收到的消息；某条消息被撤回后，在会话内发送查询指令，
即可看到最近一段时间内被撤回消息的发送者与内容。

仅依赖 AstrBot 自带能力，无需第三方依赖。
"""

import time
from collections import OrderedDict, deque
from dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

# 消息缓存条数上限，防止长期运行占用过多内存
MAX_CACHE_SIZE = 2000
# 撤回记录保留条数上限
MAX_RECALL_RECORDS = 200
# 撤回通知的 OneBot 类型
RECALL_NOTICE_TYPES = ("group_recall", "friend_recall")


def _raw_get(raw, key: str, default=None):
    """从 OneBot 原始事件中取值，兼容 aiocqhttp 的 Event(dict) 与普通 dict。"""
    if raw is None:
        return default
    if isinstance(raw, dict):
        return raw.get(key, default)
    value = getattr(raw, key, None)
    return default if value is None else value


@dataclass(slots=True)
class CachedMessage:
    """已缓存的消息，用于撤回后还原内容。"""

    sender_id: str
    sender_name: str
    content: str
    timestamp: float


@dataclass(slots=True)
class RecallRecord:
    """一条撤回记录。"""

    chat_id: str
    sender_id: str
    sender_name: str
    content: str
    timestamp: float


class AntiRecallPlugin(Star):
    """防撤回查询

    记录收到的消息，消息被撤回后发送「防撤回」指令，即可查看最近一段时间内
    被撤回的消息的发送者与内容。
    """

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        # key: (会话标识, 消息 ID)
        self._msg_cache: OrderedDict[tuple[str, str], CachedMessage] = OrderedDict()
        self._recalls: deque[RecallRecord] = deque(maxlen=MAX_RECALL_RECORDS)

    async def initialize(self) -> None:
        logger.info("[防撤回] 插件已加载，开始缓存消息以便查询撤回记录。")

    # ---------------------------------------------------------------- 配置读取

    def _cfg(self, key: str, default):
        try:
            value = self.config.get(key, default)
        except AttributeError:
            return default
        return default if value is None else value

    def _cfg_int(self, key: str, default: int, minimum: int = 1) -> int:
        try:
            return max(minimum, int(self._cfg(key, default)))
        except (TypeError, ValueError):
            logger.warning(f"[防撤回] 配置项 {key} 不是有效整数，已回退为默认值 {default}")
            return default

    # ---------------------------------------------------------------- 事件入口

    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_onebot_event(self, event: AstrMessageEvent):
        """监听全部事件：消息入缓存，撤回通知生成记录。"""
        raw = getattr(event.message_obj, "raw_message", None)
        post_type = _raw_get(raw, "post_type")

        if post_type == "message":
            self._cache_message(event, raw)
        elif post_type == "notice" and _raw_get(raw, "notice_type") in RECALL_NOTICE_TYPES:
            self._record_recall(event, raw)

    # ---------------------------------------------------------------- 缓存逻辑

    @staticmethod
    def _chat_id(event: AstrMessageEvent, raw) -> str:
        """会话标识：群聊用 group_<群号>，私聊用 private_<会话 ID>。"""
        group_id = _raw_get(raw, "group_id")
        if group_id:
            return f"group_{group_id}"
        return f"private_{event.get_session_id()}"

    def _cache_message(self, event: AstrMessageEvent, raw) -> None:
        message_id = _raw_get(raw, "message_id")
        if message_id is None:
            return

        content = event.get_message_outline().strip() or event.get_message_str().strip()
        self._msg_cache[(self._chat_id(event, raw), str(message_id))] = CachedMessage(
            sender_id=event.get_sender_id(),
            sender_name=event.get_sender_name() or event.get_sender_id(),
            content=content or "[非文本消息]",
            timestamp=time.time(),
        )
        self._trim_cache()

    def _trim_cache(self) -> None:
        """丢弃超时与超量的缓存。"""
        deadline = time.time() - self._cfg_int("cache_minutes", 30) * 60
        while self._msg_cache:
            key = next(iter(self._msg_cache))
            if (
                self._msg_cache[key].timestamp < deadline
                or len(self._msg_cache) > MAX_CACHE_SIZE
            ):
                del self._msg_cache[key]
            else:
                break

    def _record_recall(self, event: AstrMessageEvent, raw) -> None:
        chat_id = self._chat_id(event, raw)
        author_id = str(_raw_get(raw, "user_id") or "")
        cached = self._msg_cache.pop(
            (chat_id, str(_raw_get(raw, "message_id"))),
            None,
        )

        record = RecallRecord(
            chat_id=chat_id,
            sender_id=cached.sender_id if cached else author_id,
            sender_name=(cached.sender_name if cached else "") or author_id,
            content=cached.content if cached else "[消息未在缓存中，内容不可见]",
            timestamp=time.time(),
        )
        self._recalls.append(record)
        logger.info(
            f"[防撤回] {record.sender_name}({record.sender_id}) 在 {chat_id} "
            f"撤回了一条消息：{record.content}",
        )

    # ---------------------------------------------------------------- 查询指令

    @filter.command("防撤回")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def query_recalls(self, event: AstrMessageEvent):
        """查看当前会话最近被撤回的消息"""
        if self._cfg("only_admin", False) and not event.is_admin():
            yield event.plain_result("该指令仅限管理员使用。")
            return

        window = self._cfg_int("window_minutes", 10)
        max_items = self._cfg_int("max_items", 20)
        chat_id = self._chat_id(event, getattr(event.message_obj, "raw_message", None))
        deadline = time.time() - window * 60

        recent = [
            record
            for record in self._recalls
            if record.chat_id == chat_id and record.timestamp >= deadline
        ][-max_items:]

        if not recent:
            yield event.plain_result(f"最近 {window} 分钟内没有被撤回的消息。")
            return

        lines = [f"最近 {window} 分钟内共有 {len(recent)} 条消息被撤回："]
        for index, record in enumerate(recent, 1):
            recalled_at = time.strftime("%H:%M:%S", time.localtime(record.timestamp))
            lines.append(
                f"{index}. [{recalled_at}] {self._display_sender(record)}"
                f"：{record.content}",
            )
        yield event.plain_result("\n".join(lines))

    @staticmethod
    def _display_sender(record: RecallRecord) -> str:
        """消息未被缓存时拿不到昵称，只显示 ID。"""
        if record.sender_name and record.sender_name != record.sender_id:
            return f"{record.sender_name}({record.sender_id})"
        return record.sender_id or record.sender_name or "未知用户"
