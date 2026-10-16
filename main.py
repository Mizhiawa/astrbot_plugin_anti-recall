"""防撤回查询插件。

缓存机器人收到的消息；某条消息被撤回后，在会话内发送「防撤回」指令，
即可看到最近一段时间内被撤回消息的发送者与内容，图片与表情一并还原。
AstrBot 管理员还可以在任意会话用「防撤回 群号」查询指定群的记录。

仅依赖 AstrBot 自带能力，无需第三方依赖。
"""

import asyncio
import re
import shutil
import time
import traceback
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, AtAll, Face, Image, Plain
from astrbot.api.star import Context, Star, StarTools

# 消息缓存条数上限，防止长期运行占用过多内存
MAX_CACHE_SIZE = 2000
# 撤回记录保留条数上限
MAX_RECALL_RECORDS = 200
# 撤回通知的 OneBot 类型
RECALL_NOTICE_TYPES = ("group_recall", "friend_recall")
# 兜底匹配窗口：撤回时间与原消息时间相差不超过该值（秒）
FUZZY_WINDOW_SECONDS = 60
# 缓存监听器的优先级。AstrBot 中该值越大越先执行（默认 0），且前一个处理器调用
# stop_event() 后，后面的处理器会被直接跳过。静默监听类插件往往会拦掉非唤醒消息，
# 这里取一个足够大的值，保证消息总能在被拦截之前进入缓存。
CACHE_LISTENER_PRIORITY = 100000
# 单张图片的下载超时（秒）
IMAGE_TIMEOUT = 15
# 同时在跑的图片预下载上限，避免在消息密集的群里抢带宽/磁盘
MAX_CONCURRENT_DOWNLOADS = 8
# 图片定期清理的最小间隔（秒）
FILE_SWEEP_INTERVAL = 60
# 少数消息段的重发占位文案（按组件类名匹配）
PLACEHOLDERS = {
    "Record": "[语音]",
    "Video": "[视频]",
    "File": "[文件]",
    "Reply": "[引用消息]",
    "Forward": "[转发消息]",
    "Node": "[合并转发]",
    "Nodes": "[合并转发]",
    "Poke": "[戳一戳]",
    "Music": "[音乐]",
}
# 从指令参数里提取群号
GROUP_ID_PATTERN = re.compile(r"\d{5,12}")


def _extract_group_id(text) -> str:
    """取出参数里的群号，兼容「1104325443」「#1104325443」「群1104325443」等写法。"""
    match = GROUP_ID_PATTERN.search(str(text or ""))
    return match.group() if match else ""


def _raw_get(raw, key: str, default=None):
    """从 OneBot 原始事件中取值，兼容 aiocqhttp 的 Event(dict) 与普通 dict。"""
    if raw is None:
        return default
    if isinstance(raw, dict):
        return raw.get(key, default)
    value = getattr(raw, key, None)
    return default if value is None else value


@dataclass(slots=True)
class MediaItem:
    """消息段快照。只保存重发所需的最小信息，避免持有组件对象。"""

    kind: str  # text / image / face / other
    value: str  # 文本内容 / 图片引用 / 表情 id / 占位文案
    local_path: str | None = None  # 仅 image：落盘后的本地路径


@dataclass(slots=True)
class CachedMessage:
    """已缓存的消息，用于撤回后还原内容。"""

    message_id: str
    sender_id: str
    sender_name: str
    content: str
    items: list[MediaItem] = field(default_factory=list)
    timestamp: float = 0.0


@dataclass(slots=True)
class RecallRecord:
    """一条撤回记录。"""

    chat_id: str
    sender_id: str
    sender_name: str
    content: str
    timestamp: float
    items: list[MediaItem] = field(default_factory=list)


class AntiRecallPlugin(Star):
    """防撤回查询

    记录收到的消息，消息被撤回后发送「防撤回」指令，即可查看最近一段时间内
    被撤回的消息的发送者与内容（含图片与表情）。
    AstrBot 管理员可在任意会话用「防撤回 群号」查询指定群。
    """

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        # key: (会话标识, 消息 ID)
        self._msg_cache: OrderedDict[tuple[str, str], CachedMessage] = OrderedDict()
        self._recalls: deque[RecallRecord] = deque(maxlen=MAX_RECALL_RECORDS)
        self._download_tasks: set[asyncio.Task] = set()
        self._images_dir: Path | None = self._resolve_images_dir()
        self._last_file_sweep = 0.0

    async def initialize(self) -> None:
        logger.info("[防撤回] 插件已加载，开始缓存消息以便查询撤回记录。")

    async def terminate(self) -> None:
        for task in list(self._download_tasks):
            task.cancel()
        self._download_tasks.clear()
        self._msg_cache.clear()
        self._recalls.clear()
        if self._images_dir is not None:
            shutil.rmtree(self._images_dir, ignore_errors=True)
        logger.info("[防撤回] 插件已卸载，缓存与图片已清理。")

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

    def _debug_enabled(self) -> bool:
        return bool(self._cfg("debug", False))

    def _resolve_images_dir(self) -> Path | None:
        try:
            images_dir = Path(StarTools.get_data_dir()) / "images"
            images_dir.mkdir(parents=True, exist_ok=True)
            return images_dir
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[防撤回] 无法创建图片缓存目录，图片将按原始链接发送：{e}")
            return None

    # ---------------------------------------------------------------- 事件入口

    @filter.event_message_type(
        filter.EventMessageType.ALL,
        priority=CACHE_LISTENER_PRIORITY,
    )
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_onebot_event(self, event: AstrMessageEvent):
        """监听全部事件：消息入缓存，撤回通知生成记录。

        这个处理器是纯观察者：不修改事件、不回复、也绝不向外抛异常。
        它注册在最高优先级上，一旦抛异常，AstrBot 会对该消息 stop_event()
        并跳过后续所有插件的处理，等于连累其他插件。
        """
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            post_type = _raw_get(raw, "post_type")

            if post_type == "message":
                self._cache_message(event, raw)
            elif (
                post_type == "notice"
                and _raw_get(raw, "notice_type") in RECALL_NOTICE_TYPES
            ):
                self._record_recall(event, raw)
        except Exception as e:  # noqa: BLE001
            detail = traceback.format_exc() if self._debug_enabled() else e
            logger.warning(f"[防撤回] 缓存消息失败，已忽略该事件：{detail}")

    # ---------------------------------------------------------------- 缓存逻辑

    @staticmethod
    def _chat_id(event: AstrMessageEvent, raw) -> str:
        """会话标识：群聊用 group_<群号>，私聊用 private_<会话 ID>。"""
        group_id = _raw_get(raw, "group_id")
        if group_id:
            return f"group_{group_id}"
        return f"private_{event.get_session_id()}"

    def _snapshot_items(self, event: AstrMessageEvent) -> list[MediaItem]:
        """把消息段拍成快照，图片同时启动后台下载。"""
        items: list[MediaItem] = []
        for comp in event.get_messages():
            if isinstance(comp, Plain):
                if comp.text.strip():
                    items.append(MediaItem("text", comp.text))
            elif isinstance(comp, Image):
                item = MediaItem("image", comp.url or comp.file or comp.path or "")
                items.append(item)
                if self._cfg("cache_images", True):
                    self._start_download(item)
            elif isinstance(comp, Face):
                items.append(MediaItem("face", str(comp.id)))
            elif isinstance(comp, AtAll):
                items.append(MediaItem("other", "@全体成员"))
            elif isinstance(comp, At):
                items.append(MediaItem("other", f"@{comp.name or comp.qq}"))
            else:
                name = type(comp).__name__
                items.append(MediaItem("other", PLACEHOLDERS.get(name, f"[{name}]")))
        return items

    def _cache_message(self, event: AstrMessageEvent, raw) -> None:
        message_id = _raw_get(raw, "message_id")
        if message_id is None:
            return

        chat_id = self._chat_id(event, raw)
        content = event.get_message_outline().strip() or event.get_message_str().strip()
        cached = CachedMessage(
            message_id=str(message_id),
            sender_id=event.get_sender_id(),
            sender_name=event.get_sender_name() or event.get_sender_id(),
            content=content or "[非文本消息]",
            items=self._snapshot_items(event),
            timestamp=time.time(),
        )
        self._msg_cache[(chat_id, cached.message_id)] = cached
        self._trim_cache()

        if self._debug_enabled():
            logger.info(
                f"[防撤回] 缓存消息 chat={chat_id} id={cached.message_id} "
                f"from={cached.sender_name}({cached.sender_id}) "
                f"items={[item.kind for item in cached.items]}",
            )

    def _trim_cache(self) -> None:
        """丢弃超时与超量的缓存，并定期清理图片文件。"""
        cache_seconds = self._cfg_int("cache_minutes", 30) * 60
        deadline = time.time() - cache_seconds
        while self._msg_cache:
            key = next(iter(self._msg_cache))
            if (
                self._msg_cache[key].timestamp < deadline
                or len(self._msg_cache) > MAX_CACHE_SIZE
            ):
                del self._msg_cache[key]
            else:
                break

        if time.time() - self._last_file_sweep < FILE_SWEEP_INTERVAL:
            return
        self._last_file_sweep = time.time()
        self._sweep_files(deadline)

    def _sweep_files(self, deadline: float) -> None:
        """删除已超出缓存时长的图片文件。"""
        if self._images_dir is None or not self._images_dir.exists():
            return
        for path in self._images_dir.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < deadline:
                    path.unlink()
            except OSError as e:
                logger.warning(f"[防撤回] 清理图片文件失败 {path.name}: {e}")

    def _record_recall(self, event: AstrMessageEvent, raw) -> None:
        chat_id = self._chat_id(event, raw)
        message_id = str(_raw_get(raw, "message_id"))
        author_id = str(_raw_get(raw, "user_id") or "")

        cached = self._msg_cache.pop((chat_id, message_id), None)
        hit = "精确"
        if cached is not None:
            pass
        elif author_id and self._cfg("fuzzy_match", True):
            cached = self._take_fuzzy(chat_id, author_id)
            hit = "兜底" if cached is not None else "未命中"
        else:
            hit = "未命中"

        record = RecallRecord(
            chat_id=chat_id,
            sender_id=cached.sender_id if cached else author_id,
            sender_name=(cached.sender_name if cached else "") or author_id,
            content=cached.content if cached else "[消息未在缓存中，内容不可见]",
            timestamp=time.time(),
            items=cached.items if cached else [],
        )
        self._recalls.append(record)
        logger.info(
            f"[防撤回] {record.sender_name}({record.sender_id}) 在 {chat_id} "
            f"撤回了一条消息（{hit}）：{record.content}",
        )

        if self._debug_enabled() and cached is None:
            recent_ids = [key[1] for key in self._msg_cache if key[0] == chat_id][-10:]
            logger.info(
                f"[防撤回] 未命中详情 chat={chat_id} 通知id={message_id} "
                f"作者={author_id} 该会话最近缓存id={recent_ids}",
            )

    def _take_fuzzy(self, chat_id: str, author_id: str) -> CachedMessage | None:
        """兜底匹配：同会话、同作者、时间邻近的最近一条未认领缓存消息。"""
        deadline = time.time() - FUZZY_WINDOW_SECONDS
        for key in list(reversed(self._msg_cache)):
            cached = self._msg_cache[key]
            if (
                key[0] == chat_id
                and cached.sender_id == author_id
                and cached.timestamp >= deadline
            ):
                return self._msg_cache.pop(key)
        return None

    # ---------------------------------------------------------------- 图片处理

    def _start_download(self, item: MediaItem) -> None:
        """后台下载图片，避免阻塞事件处理链。"""
        if self._images_dir is None:
            return
        if len(self._download_tasks) >= MAX_CONCURRENT_DOWNLOADS:
            # 并发已满，留给查询时按需下载
            return
        try:
            task = asyncio.get_running_loop().create_task(self._materialize_image(item))
        except RuntimeError:
            return
        self._download_tasks.add(task)
        task.add_done_callback(self._download_tasks.discard)

    async def _materialize_image(self, item: MediaItem) -> str | None:
        """把图片落到本地并返回路径；失败返回 None。"""
        if item.local_path and Path(item.local_path).exists():
            return item.local_path
        ref = item.value
        if not ref:
            return None

        if not ref.startswith(("http://", "https://")):
            # 少数情况下 NapCat 会给本地路径
            if Path(ref).is_file():
                item.local_path = ref
                return ref
            return None

        if self._images_dir is None:
            return None

        try:
            image = Image.fromURL(ref)
            temp_path = await asyncio.wait_for(
                image.convert_to_file_path(),
                timeout=IMAGE_TIMEOUT,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[防撤回] 图片下载失败，将退回原始链接：{e}")
            return None

        suffix = Path(temp_path).suffix or ".jpg"
        target = self._images_dir / f"{int(time.time() * 1000)}_{uuid4().hex[:8]}{suffix}"
        try:
            shutil.move(str(temp_path), str(target))
        except Exception:  # noqa: BLE001
            try:
                shutil.copy2(str(temp_path), str(target))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[防撤回] 保存图片失败：{e}")
                return None
        item.local_path = str(target)
        return item.local_path

    # ---------------------------------------------------------------- 查询指令

    @filter.command("防撤回")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def query_recalls(self, event: AstrMessageEvent, group_id: str = ""):
        """查看最近被撤回的消息；管理员可用「防撤回 群号」查询指定群"""
        target_group = _extract_group_id(group_id)
        if str(group_id or "").strip() and not target_group:
            yield event.plain_result("用法：/防撤回（当前会话） 或 /防撤回 群号（管理员）")
            return

        if target_group:
            # 跨会话查询只对 AstrBot 管理员开放
            if not event.is_admin():
                yield event.plain_result("查询指定群的撤回记录需要 AstrBot 管理员权限。")
                return
            chat_id = f"group_{target_group}"
            title = f"群 {target_group} "
        else:
            if self._cfg("only_admin", False) and not event.is_admin():
                yield event.plain_result("该指令仅限管理员使用。")
                return
            chat_id = self._chat_id(
                event,
                getattr(event.message_obj, "raw_message", None),
            )
            title = ""

        window = self._cfg_int("window_minutes", 10)
        max_items = self._cfg_int("max_items", 20)
        deadline = time.time() - window * 60
        recent = [
            record
            for record in self._recalls
            if record.chat_id == chat_id and record.timestamp >= deadline
        ][-max_items:]

        if not recent:
            yield event.plain_result(f"{title}最近 {window} 分钟内没有被撤回的消息。")
            return

        chain: list = [Plain(f"{title}最近 {window} 分钟内共有 {len(recent)} 条消息被撤回：")]
        for index, record in enumerate(recent, 1):
            recalled_at = time.strftime("%H:%M:%S", time.localtime(record.timestamp))
            chain.append(
                Plain(f"\n{index}. [{recalled_at}] {self._display_sender(record)}："),
            )
            if record.items:
                chain.extend(await self._render_items(record.items))
            else:
                chain.append(Plain(record.content))
        yield event.chain_result(chain)

    async def _render_items(self, items: list[MediaItem]) -> list:
        """把消息段快照还原成可发送的组件。"""
        rendered: list = []
        for item in items:
            if item.kind == "text":
                rendered.append(Plain(item.value))
            elif item.kind == "face":
                try:
                    rendered.append(Face(id=int(item.value)))
                except (TypeError, ValueError):
                    rendered.append(Plain("[表情]"))
            elif item.kind == "image":
                path = await self._materialize_image(item)
                if path:
                    try:
                        rendered.append(Image.fromFileSystem(path))
                        continue
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[防撤回] 构造图片消息段失败：{e}")
                rendered.append(Plain("[图片]"))
            else:
                rendered.append(Plain(item.value))
        return rendered

    @staticmethod
    def _display_sender(record: RecallRecord) -> str:
        """消息未被缓存时拿不到昵称，只显示 ID。"""
        if record.sender_name and record.sender_name != record.sender_id:
            return f"{record.sender_name}({record.sender_id})"
        return record.sender_id or record.sender_name or "未知用户"
