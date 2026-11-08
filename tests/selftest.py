"""插件自检：用桩模块模拟 AstrBot 环境，覆盖缓存/撤回/图片/权限等核心逻辑。

运行：python tests/selftest.py
不依赖 AstrBot、pytest 或第三方库。
"""

import asyncio
import importlib.util
import sys
import tempfile
import time
import types
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="anti_recall_test_"))


# --------------------------------------------------------------------- AstrBot 桩
class _Logger:
    def __init__(self):
        self.lines = []

    def _log(self, level, msg):
        self.lines.append(f"{level} {msg}")

    def info(self, msg="", *a, **k):
        self._log("INFO", msg)

    def warning(self, msg="", *a, **k):
        self._log("WARN", msg)

    def error(self, msg="", *a, **k):
        self._log("ERR", msg)

    def exception(self, msg="", *a, **k):
        self._log("ERR", msg)


LOGGER = _Logger()


class _Flag:
    def __init__(self, name):
        self.name = name

    def __and__(self, other):
        return self.name == other.name


class _EMT:
    ALL = _Flag("ALL")


class _PAT:
    AIOCQHTTP = _Flag("AIOCQHTTP")


class _Filter:
    EventMessageType = _EMT
    PlatformAdapterType = _PAT

    @staticmethod
    def event_message_type(t, **kw):
        def deco(f):
            f._emt, f._extras = t, kw
            return f

        return deco

    @staticmethod
    def platform_adapter_type(t, **kw):
        def deco(f):
            f._pat = t
            return f

        return deco

    @staticmethod
    def command(name, **kw):
        def deco(f):
            f._cmd = name
            return f

        return deco


class AstrMessageEvent:
    pass


class Plain:
    type = "plain"

    def __init__(self, text=""):
        self.text = text

    def __repr__(self):
        return f"Plain({self.text!r})"


class At:
    type = "at"

    def __init__(self, qq="1", name=""):
        self.qq, self.name = qq, name


class AtAll:
    type = "atall"


class Face:
    type = "face"

    def __init__(self, **kw):
        self.id = kw.get("id")


class Image:
    type = "image"
    fail_download = False
    delay = 0.0

    def __init__(self, file="", **kw):
        self.file = file
        self.url = kw.get("url", "")
        self.path = kw.get("path", "")

    @staticmethod
    def fromURL(url, **kw):
        if not url.startswith(("http://", "https://")):
            raise ValueError("not a valid url")
        return Image(file=url, url=url, **kw)

    @staticmethod
    def fromFileSystem(path, **kw):
        p = Path(path).resolve(strict=False)
        return Image(file=p.as_uri(), path=str(p), **kw)

    async def convert_to_file_path(self):
        if Image.fail_download:
            raise RuntimeError("download failed (stub)")
        if Image.delay:
            await asyncio.sleep(Image.delay)
        target = TMP / f"stub_{abs(hash(self.url)) % 10**8}.img"
        target.write_bytes(b"img-bytes")
        return str(target)

    def __repr__(self):
        return f"Image(file={self.file!r})"


class Star:
    def __init__(self, context, config=None):
        self.context = context


class _Context:
    def get_config(self, umo=None):
        return {"platform_settings": {"rate_limit": {"count": 30, "time": 60, "strategy": "stall"}}}


class Context(_Context):
    pass


class StarTools:
    @classmethod
    def get_data_dir(cls, plugin_name=None):
        d = TMP / "plugin_data"
        d.mkdir(parents=True, exist_ok=True)
        return d


def _install_stubs():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = LOGGER

    ev = types.ModuleType("astrbot.api.event")
    ev.filter = _Filter()
    ev.AstrMessageEvent = AstrMessageEvent

    st = types.ModuleType("astrbot.api.star")
    st.Star = Star
    st.Context = Context
    st.StarTools = StarTools

    comp = types.ModuleType("astrbot.api.message_components")
    for cls in (At, AtAll, Face, Image, Plain):
        setattr(comp, cls.__name__, cls)

    api.event, api.star, api.message_components = ev, st, comp
    astrbot.api = api
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": ev,
            "astrbot.api.star": st,
            "astrbot.api.message_components": comp,
        },
    )


_install_stubs()
_spec = importlib.util.spec_from_file_location("anti_recall_main", PLUGIN_ROOT / "main.py")
main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(main)


# --------------------------------------------------------------------------- 假事件
class FakeEvent:
    def __init__(
        self,
        raw,
        message=None,
        outline="",
        text="",
        sender_id="1",
        sender_name="甲",
        session_id="1",
        admin=True,
    ):
        self.message_obj = types.SimpleNamespace(raw_message=raw, message=message or [])
        self._outline = outline or text
        self._text = text or outline
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._session_id = session_id
        self._admin = admin
        self.replies = []

    def get_message_outline(self):
        return self._outline

    def get_message_str(self):
        return self._text

    def get_messages(self):
        return self.message_obj.message

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_session_id(self):
        return self._session_id

    def is_admin(self):
        return self._admin

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)


def msg(chat, mid, message, sender_id="20001", sender_name="张三", text=""):
    raw = {"post_type": "message", "message_type": "group", "message_id": mid, "time": int(time.time())}
    if chat:
        raw.update(group_id=chat)
    return FakeEvent(
        raw,
        message=message,
        text=text,
        sender_id=sender_id,
        sender_name=sender_name,
        session_id=str(chat or sender_id),
    )


def recall(chat, mid, author="20001", notice="group_recall"):
    raw = {"post_type": "notice", "notice_type": notice, "user_id": author, "message_id": mid}
    if chat:
        raw["group_id"] = chat
    return FakeEvent(raw, session_id=str(chat or author))


def query(chat=1104325443, session_id=None):
    return FakeEvent({"post_type": "message", "group_id": chat}, session_id=session_id or str(chat))


async def collect(agen):
    return [item async for item in agen]


def flat(replies):
    item = replies[0]
    if item[0] == "chain":
        return "".join(c.text for c in item[1] if isinstance(c, Plain))
    return item[1]


async def scenario():
    assert (PLUGIN_ROOT / "metadata.yaml").exists()
    plugin = main.AntiRecallPlugin(Context(), {"window_minutes": 10, "cache_minutes": 30, "max_items": 20})
    G, G2 = 1104325443, 99887766

    # 1. 纯文本：缓存 → 撤回 → 查询
    await plugin.on_onebot_event(msg(G, 1001, [Plain("今晚吃什么")], text="今晚吃什么"))
    await plugin.on_onebot_event(recall(G, 1001))
    text = flat(await collect(plugin.query_recalls(query(G))))
    assert "张三(20001)：今晚吃什么" in text, text

    # 2. 图片：认领本地文件，源文件被外部删除后仍能还原
    external = TMP / "astrbot_temp" / "media_image_x.png"
    external.parent.mkdir(parents=True, exist_ok=True)
    external.write_bytes(b"\x89PNG\r\n\x1a\n" + b"z" * 512)
    await plugin.on_onebot_event(
        msg(G, 1002, [Plain("看图"), Image(url=str(external))], text="看图"),
    )
    for task in list(plugin._download_tasks):
        await task
    item = plugin._msg_cache[("group_1104325443", "1002")].items[1]
    owned = Path(item.local_path)
    assert owned.exists() and plugin._images_dir in owned.parents, item.local_path
    external.unlink()
    assert owned.exists(), "源文件删除后插件目录副本应仍在"
    await plugin.on_onebot_event(recall(G, 1002))
    chain = (await collect(plugin.query_recalls(query(G))))[0][1]
    assert any(isinstance(c, Image) for c in chain), "源文件清理后图片仍应还原"
    print("图片归插件所有（源文件被删仍可还原）✅")

    # 3. 极快撤回：撤回通知先到，消息事件后到 → 补齐
    await plugin.on_onebot_event(recall(G, 8200, author="20006"))
    rec = plugin._recalls[-1]
    assert "不可见" in rec.content, rec.content
    assert ("group_1104325443", "8200") in plugin._pending
    await plugin.on_onebot_event(
        msg(G, 8200, [Plain("秒撤回的内容"), Face(id=5)], sender_id="20006", sender_name="周八", text="秒撤回的内容"),
    )
    assert rec.content == "秒撤回的内容" and rec.sender_name == "周八", (rec.content, rec.sender_name)
    assert [i.kind for i in rec.items] == ["text", "face"], rec.items
    assert ("group_1104325443", "8200") not in plugin._msg_cache
    print("极快撤回（通知先到、消息后到）已补齐 ✅")

    # 4. 绝不猜邻居：ID 对不上时保持不可见，邻居不能被取走
    await plugin.on_onebot_event(msg(G, 1004, [Plain("邻居的内容")], sender_id="20004", sender_name="赵六", text="邻居的内容"))
    await plugin.on_onebot_event(recall(G, 999999, author="20004"))
    rec = plugin._recalls[-1]
    assert "不可见" in rec.content, rec.content
    assert ("group_1104325443", "1004") in plugin._msg_cache, "邻居消息不能被取走"
    # 模拟挂起超时
    plugin._prune_pending(time.time() + 10**6)
    assert "不可见" in rec.content
    print("ID 对不上时不猜邻居（内容不错位、邻居不被取走）✅")

    # 5. 权限与参数
    replies = await collect(plugin.query_recalls(FakeEvent({"post_type": "message", "group_id": G}, admin=False), str(G)))
    assert "管理员权限" in replies[0][1]
    replies = await collect(plugin.query_recalls(query(G), "全部"))
    assert "用法" in replies[0][1]
    replies = await collect(plugin.query_recalls(FakeEvent({"post_type": "message", "group_id": G}, session_id=str(G))))
    assert "看" in flat(replies)
    for raw, want in [("1104325443", "1104325443"), ("#99887766", "99887766"), ("群1104325443", "1104325443"), ("全部", "")]:
        assert main._extract_group_id(raw) == want, raw
    print("指令参数与权限边界 ✅")

    # 6. 跨群查询只输出该群记录
    await plugin.on_onebot_event(msg(G2, 2001, [Plain("另一个群")], sender_id="30001", sender_name="王五", text="另一个群"))
    await plugin.on_onebot_event(recall(G2, 2001, author="30001"))
    text = flat(await collect(plugin.query_recalls(query(0, session_id="40001"), G2)))
    assert "群 99887766" in text and "另一个群" in text and "今晚吃什么" not in text, text
    print("跨群查询隔离 ✅")

    # 7. 图片体积上限与重试上限
    huge = TMP / "huge.png"
    huge.write_bytes(b"x" * (main.IMAGE_MAX_BYTES + 1))
    await plugin.on_onebot_event(msg(G, 1003, [Image(url=str(huge))]))
    for task in list(plugin._download_tasks):
        await task
    it = plugin._msg_cache[("group_1104325443", "1003")].items[0]
    assert it.local_path is None, "超限图片不应被认领"
    Image.fail_download = True
    await plugin.on_onebot_event(msg(G, 1005, [Image.fromURL("https://example.com/dead.png")]))
    for task in list(plugin._download_tasks):
        await task
    it2 = plugin._msg_cache[("group_1104325443", "1005")].items[0]
    await plugin._materialize_image(it2)
    assert it2.attempts == main.MAX_IMAGE_ATTEMPTS, it2.attempts
    assert await plugin._materialize_image(it2) is None
    Image.fail_download = False
    print("图片体积上限与重试上限 ✅")

    # 8. 图片清理：过期删除 + 总量上限，且跑在后台任务里
    imgs = plugin._images_dir
    for leftover in imgs.iterdir():
        if leftover.is_file():
            leftover.unlink()
    stale = imgs / "stale.png"
    stale.write_bytes(b"x")
    old = time.time() - 7200
    import os as _os

    _os.utime(stale, (old, old))
    plugin._last_file_sweep = 0.0
    await plugin.on_onebot_event(msg(G, 1006, [Plain("触发清扫")], text="触发清扫"))
    assert plugin._sweep_task is not None, "清扫应在后台任务里执行"
    await plugin._sweep_task
    assert not stale.exists(), "过期图片应被清理"
    print("图片清扫（后台任务 + 过期删除）✅")

    # 9. 缓存过期与记录数上限
    for item in plugin._msg_cache.values():
        item.timestamp -= 7200
    await plugin.on_onebot_event(msg(G, 1007, [Plain("新消息")], text="新消息"))
    assert len(plugin._msg_cache) == 1, plugin._msg_cache
    print("消息缓存过期清理 ✅")

    # 10. 卸载清理
    imgs_dir = plugin._images_dir
    await plugin.terminate()
    assert not imgs_dir.exists(), "卸载后图片目录应删除"
    print("卸载清理 ✅")

    print("\n全部自检通过 ✅")


asyncio.run(scenario())
