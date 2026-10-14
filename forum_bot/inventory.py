# -*- coding: utf-8 -*-
"""机器人所在位置的数据缓存：频道 / 子频道。

为什么要有这层缓存
----------------
频道和子频道能真查（`/users/@me/guilds` + `/guilds/{id}/channels`），
但这些是**异步接口，只能在机器人的事件循环里调**；而配置页跑在另一个线程
（http.server），没法直接 await。

所以做成"机器人侧拉、配置页侧读"：

    on_ready / 定时 / 手动请求  ->  Inventory.sync(api)   -> 内存 + 落盘
    配置页 GET /api/guilds      ->  Inventory.snapshot()  -> 直接读缓存

配置页点「同步频道」只是置一个请求标志（`request_sync`），真正的拉取发生在
机器人的循环里（`sync_loop` 每 SYNC_CHECK_SECONDS 检查一次）。这样两边线程
边界干净，也不会出现"网页线程跑去 await 别人 loop"这种坑。

结果落盘在 forum_inventory.json，机器人重启、或单独起配置页时也能先显示
上次的数据。

顺带把**频道号**也解析出来存着。多频道时这一步很关键：每个频道的频道号不同，
帖子链接用错了就跳到别的频道。`me_guilds` 的返回里带 `icon`，而频道号就藏在
icon 的 URL 里，所以同步一次就能拿到全部，不用一个个手填。

QQ 群**不在**这个模块 —— 平台没有「机器人在哪些群」的接口，只能靠事件记录，
见 groups.py。
"""
import asyncio
import json
import re
import time

from botpy import logging
from botpy.types.channel import ChannelType, PrivateType

from . import config, text

__all__ = [
    "Inventory",
    "channel_type_label",
    "private_type_label",
    "is_specified_user",
    "ordered_channels",
    "parse_channel_number",
    "format_ts",
]

_log = logging.get_logger()

# 多久检查一次"有没有人请求同步"
SYNC_CHECK_SECONDS = 15

# me_guilds 一次最多 100 个频道，多了要拿 after 往后翻；留个页数上限防死循环
GUILD_PAGE_SIZE = 100
MAX_GUILD_PAGES = 5

# 频道头像 URL 里的那串数字就是「频道号」（pd.qq.com/g/ 后面要跟的那个）
_CHANNEL_NUMBER_RE = re.compile(r"groupprohead\.gtimg\.cn/(\d+)")


def parse_channel_number(icon):
    """从频道头像 URL 里抠出频道号。

    实测 `guild_id` 直接放进 pd.qq.com/g/{guild_id} 会退回站点首页，
    能打开的是头像 URL 里的数字号，例如
    `groupprohead.gtimg.cn/1234567890123456789/100`。

    多频道下这一步很关键：**每个频道的频道号不同**，用错了链接就跳到别的频道
    或者打不开。me_guilds 的返回里带 icon，所以同步一次就能全部拿到，不用手填。
    """
    matched = _CHANNEL_NUMBER_RE.search(str(icon or ""))
    return matched.group(1) if matched else ""

# 平台枚举值 -> text.yaml 里的文案 key。
# 这里只做「数值对到哪个名字」，显示成什么字由资源文件决定。
_CHANNEL_TYPE_KEYS = {
    ChannelType.TEXT_CHANNEL.value: "text",
    ChannelType.VOICE_CHANNEL.value: "voice",
    ChannelType.GROUP_CHANNEL.value: "group",
    ChannelType.LIVE_CHANNEL.value: "live",
    ChannelType.APP_CHANNEL.value: "app",
    ChannelType.DISCUSSION_CHANNEL.value: "forum",
}

_PRIVATE_TYPE_KEYS = {
    PrivateType.PUBLIC.value: "public",
    PrivateType.ADMIN.value: "admin",
    PrivateType.SPECIFIED_USER.value: "specified_user",
}


def format_ts(value):
    if not value:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))
    except (TypeError, ValueError, OSError):
        return ""


def as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _type_label(table, group, value):
    """按「平台枚举 -> key -> 文案」两级查表，查不到就退回「未知」。"""
    key = table.get(as_int(value, -1))
    if key:
        found = text.get("labels.%s.%s" % (group, key))
        if isinstance(found, str) and found.strip():
            return found.strip()
    return text.text("labels.unknown")


def channel_type_label(value):
    return _type_label(_CHANNEL_TYPE_KEYS, "channel_type", value)


def private_type_label(value):
    return _type_label(_PRIVATE_TYPE_KEYS, "private_type", value)


def is_specified_user(value):
    """子频道是否为「指定成员可见」。

    加群解锁靠 update_channel_user_permissions 给单个用户授权，
    只有私有类型是「指定成员可见」时才有意义。
    """
    return as_int(value, -1) == PrivateType.SPECIFIED_USER.value


def ordered_channels(channels):
    """按「分组 -> 组内子频道」排好，返回 [(子频道, 是否在分组里), ...]。

    平台返回的是扁平列表，直接按 parent_id 字符串排序会让分组里的子频道
    跑到最后，读起来很别扭。
    """
    items = list(channels or [])
    group_ids = {
        str(item.get("id") or "")
        for item in items
        if as_int(item.get("type"), -1) == ChannelType.GROUP_CHANNEL.value
    }

    top, children = [], {}
    for item in items:
        parent_id = str(item.get("parent_id") or "")
        own_id = str(item.get("id") or "")
        if parent_id in group_ids and parent_id != own_id:
            children.setdefault(parent_id, []).append(item)
        else:
            top.append(item)

    ordered = []
    for item in sorted(top, key=lambda it: as_int(it.get("position"))):
        ordered.append((item, False))
        for child in sorted(children.get(str(item.get("id") or ""), []), key=lambda it: as_int(it.get("position"))):
            ordered.append((child, True))
    return ordered


class Inventory:
    """频道/子频道缓存。机器人侧写，配置页侧读。"""

    def __init__(self, path=None):
        self._path = path or config.INVENTORY_FILE
        self._sync_requested = False
        self._syncing = False
        self._data = self._load()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as file:
                data = json.load(file)
        except Exception:
            data = None
        if not isinstance(data, dict):
            data = {}
        return {
            "guilds": data.get("guilds") if isinstance(data.get("guilds"), list) else [],
            "synced_at": float(data.get("synced_at") or 0),
            "last_error": str(data.get("last_error") or ""),
            "meta": data.get("meta") if isinstance(data.get("meta"), dict) else {},
        }

    def _save(self):
        try:
            with open(self._path, "w", encoding="utf-8") as file:
                json.dump(self._data, file, ensure_ascii=False, indent=2)
        except Exception as error:
            _log.warning(f"[botpy] 位置缓存写入失败: {error}")

    # ------------------------------------------------------------------
    # 元信息（机器人名字 / appid / 启动时间），配置页「概况」用
    # ------------------------------------------------------------------
    def set_meta(self, **fields):
        meta = self._data.setdefault("meta", {})
        meta.update(fields)
        self._save()

    def meta(self):
        return dict(self._data.get("meta") or {})

    # ------------------------------------------------------------------
    # 同步
    # ------------------------------------------------------------------
    def request_sync(self):
        """配置页点「同步频道」时调（可能在别的线程），只置标志。"""
        self._sync_requested = True

    def take_sync_request(self):
        if not self._sync_requested:
            return False
        self._sync_requested = False
        return True

    @property
    def syncing(self):
        return self._syncing

    async def _all_guilds(self, api):
        """拉全量频道列表。

        `/users/@me/guilds` 一次最多返回 100 个，多了要用 `after=上一页最后一个 id`
        往后翻。以前只拉一页，机器人加入超过 100 个频道时会**静默漏掉**后面的。
        """
        collected = []
        seen = set()
        cursor = None
        for _ in range(MAX_GUILD_PAGES):
            page = await api.me_guilds(guild_id=cursor, limit=GUILD_PAGE_SIZE) or []
            page = [item for item in page if isinstance(item, dict)]
            fresh = [item for item in page if str(item.get("id") or "") not in seen]
            for item in fresh:
                seen.add(str(item.get("id") or ""))
            collected.extend(fresh)
            if len(page) < GUILD_PAGE_SIZE or not fresh:
                break
            cursor = str(page[-1].get("id") or "")
            if not cursor:
                break
        else:
            _log.warning(f"[botpy] 频道列表翻页达到上限 {MAX_GUILD_PAGES} 页，可能还有没拉到的")
        return collected

    async def sync(self, api):
        """拉取频道与子频道。必须在机器人的事件循环里调。"""
        self._syncing = True
        try:
            try:
                raw_guilds = await self._all_guilds(api)
            except Exception as error:
                self._data["last_error"] = f"拉取频道列表失败：{error}"
                _log.warning(f"[botpy] 拉取频道列表失败: {error}")
                self._save()
                return self.snapshot()

            guilds = []
            for guild in raw_guilds:
                guild_id = str(guild.get("id") or "")
                icon = str(guild.get("icon") or "")
                entry = {
                    "id": guild_id,
                    "name": guild.get("name") or "",
                    "icon": icon,
                    # 从 icon 里解析出来的「频道号」，帖子链接要用它。
                    # 存下来就不用每发一条帖子都去调 get_guild()。
                    "channel_number": parse_channel_number(icon),
                    "description": guild.get("description") or "",
                    "owner_id": str(guild.get("owner_id") or ""),
                    "member_count": guild.get("member_count"),
                    "max_members": guild.get("max_members"),
                    "owner": bool(guild.get("owner")),
                    "joined_at": guild.get("joined_at") or "",
                    "channels": [],
                    "channel_error": "",
                    "permissions": [],
                    "permission_error": "",
                }
                try:
                    channels = await api.get_channels(guild_id=guild_id) or []
                except Exception as error:
                    entry["channel_error"] = str(error)
                    _log.warning(f"[botpy] 拉取子频道列表失败（guild={guild_id}）: {error}")
                else:
                    for channel, nested in ordered_channels(channels):
                        entry["channels"].append(
                            {
                                "id": str(channel.get("id") or ""),
                                "name": channel.get("name") or "",
                                "type": as_int(channel.get("type"), -1),
                                "private_type": as_int(channel.get("private_type"), -1),
                                "position": as_int(channel.get("position")),
                                "nested": nested,
                            }
                        )

                # 机器人在这个频道里能用的接口权限
                # （GET /guilds/{guild_id}/api_permission）
                try:
                    apis = await api.get_permissions(guild_id=guild_id) or []
                except Exception as error:
                    entry["permission_error"] = str(error)
                    _log.warning(f"[botpy] 拉取接口权限列表失败（guild={guild_id}）: {error}")
                else:
                    entry["permissions"] = [
                        {
                            "path": str(item.get("path") or ""),
                            "method": str(item.get("method") or ""),
                            "desc": str(item.get("desc") or ""),
                            "auth_status": as_int(item.get("auth_status"), -1),
                        }
                        for item in apis
                        if isinstance(item, dict)
                    ]
                guilds.append(entry)

            self._data["guilds"] = guilds
            self._data["synced_at"] = time.time()
            self._data["last_error"] = ""
            self._save()
            _log.info(f"[botpy] 已同步位置信息：{len(guilds)} 个频道")
            return self.snapshot()
        finally:
            self._syncing = False

    async def sync_loop(self, api):
        """定时检查有没有人来要同步；也顺手定期自己刷一下。"""
        last_auto = time.time()
        while True:
            await asyncio.sleep(SYNC_CHECK_SECONDS)
            want = self.take_sync_request()
            # 每 10 分钟自刷一次，保证「概况」上的数字不算太旧
            if not want and time.time() - last_auto < 600:
                continue
            last_auto = time.time()
            try:
                await self.sync(api)
            except Exception as error:
                _log.warning(f"[botpy] 定时同步位置信息异常: {error}")

    # ------------------------------------------------------------------
    # 读取（配置页线程）
    # ------------------------------------------------------------------
    def snapshot(self):
        return {
            "guilds": self._data.get("guilds") or [],
            "synced_at": self._data.get("synced_at") or 0,
            "synced_text": format_ts(self._data.get("synced_at")),
            "last_error": self._data.get("last_error") or "",
            "syncing": self._syncing,
        }

    def has_data(self):
        return bool(self._data.get("guilds"))

    def guild_of(self, guild_id):
        """某个频道的缓存记录（没有就返回 {}）。"""
        guild_id = str(guild_id or "")
        for item in self._data.get("guilds") or []:
            if str(item.get("id") or "") == guild_id:
                return item
        return {}

    def channel_number_of(self, guild_id):
        """某个频道的「频道号」（帖子链接用）。

        同步时从 icon 解析好了，所以这里是纯内存读取、不发请求。
        没同步过或 icon 里没有数字就返回空串，调用方再决定要不要兜底。
        """
        return str(self.guild_of(guild_id).get("channel_number") or "")
