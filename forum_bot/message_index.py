# -*- coding: utf-8 -*-
"""msg_idx <-> message_id 的映射表。

为什么要这张表：
  · QQ群消息的「引用」下发的是被引用消息的 **msg_idx**（message_scene.ext 里的
    ref_msg_idx=REFIDX_xxx），而撤回接口要的是 **message_id**（ROBOT1.0_xxx）；
  · 两者是两套 ID，只能靠收消息时把对应关系记下来，撤回时再换算。

记录来源：
  · 收到的每一条群消息（需要「接收所有消息」全量模式，否则只收得到 @机器人的）；
  · 机器人自己发出的消息（发送响应里的 ext_info.ref_idx 就是它的 msg_idx）。

记录会落盘，重启不丢；过期时间见 INDEX_KEEP_SECONDS，其中「撤回上一条」只认
RECALL_WINDOW_SECONDS 内的记录（官方限制：消息发送超过 2 分钟不可撤回）。
"""
import collections
import json
import time

from botpy import logging

from . import config
from .group_message import message_index_of

__all__ = ["MessageIndex", "RECALL_WINDOW_SECONDS", "INDEX_KEEP_SECONDS"]

_log = logging.get_logger()

# 官方限制：消息发送超过 2 分钟不可撤回
RECALL_WINDOW_SECONDS = 120

# 记录保留 24 小时。撤回的硬时限只有 2 分钟，留这么久不是为了撤旧消息 ——
# 而是「重启之后还能不能找到刚才那条」要有确定答案：留 1 小时的话，
# 部署重启后引用稍早一点的消息就会被报成"机器人重启过"，误导排查。
INDEX_KEEP_SECONDS = 86400
# 24 小时的记录可能不少，落盘超过这个条数就丢最旧的，文件不能无限长
_INDEX_MAX_ENTRIES = 4000


class MessageIndex:
    def __init__(self, path=None):
        self._path = path or config.INDEX_FILE
        self._by_index = {}
        self._sent = collections.deque(maxlen=200)
        self._last_save = 0.0
        self._load()

    # ---------------- 持久化 ----------------
    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as file:
                items = json.load(file)
        except Exception:
            return

        now = time.time()
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or not item.get("message_id"):
                continue
            if now - float(item.get("ts") or 0) > INDEX_KEEP_SECONDS:
                continue
            if item.get("msg_idx"):
                self._by_index[item["msg_idx"]] = item
            if item.get("from_bot"):
                self._sent.append(item)

    def _save(self, force=False):
        now = time.time()
        # 全量模式下群消息可能很密集，写文件做个粗粒度节流。
        # 窗口越短，"重启丢最近几秒"的窗口就越小；退出时另有 flush() 兜底
        if not force and now - self._last_save < 2:
            return
        self._last_save = now
        try:
            items = sorted(
                self._by_index.values(), key=lambda item: float(item.get("ts") or 0)
            )
            # 超上限丢最旧的：都是超过撤回时限很远的记录，留着只是占地方
            items = items[-_INDEX_MAX_ENTRIES:]
            with open(self._path, "w", encoding="utf-8") as file:
                json.dump(items, file, ensure_ascii=False, indent=2)
        except Exception as error:
            _log.warning(f"[botpy] 消息索引写入失败: {error}")

    def flush(self):
        """把索引立刻写盘。**进程退出前必须调**。

        平时是 2 秒一写的节流写 —— 不 flush 的话，重启（部署/手动 restart）
        前两秒内收到的消息就没落盘，重启完引用它们「撤回/拉黑」全部报
        "没找到这条消息的记录"，看起来就像索引坏了。
        """
        self._save(force=True)

    # ---------------- 写入 ----------------
    def remember(self, message):
        """记录一条收到的群消息。"""
        msg_idx = message_index_of(message)
        if not msg_idx or not message.id:
            return
        self._by_index[msg_idx] = {
            "msg_idx": msg_idx,
            "message_id": message.id,
            "group_openid": getattr(message, "group_openid", None),
            "from_bot": bool((getattr(message, "raw", None) or {}).get("author", {}).get("bot")),
            "ts": time.time(),
        }
        self._save()

    def remember_sent(self, group_openid, message_id, ref_idx=None):
        """记录一条机器人自己发出的消息。"""
        entry = {
            "msg_idx": ref_idx,
            "message_id": message_id,
            "group_openid": group_openid,
            "from_bot": True,
            "ts": time.time(),
        }
        if ref_idx:
            self._by_index[ref_idx] = entry
        self._sent.append(entry)
        self._save(force=True)

    # ---------------- 查询 ----------------
    def resolve_message_id(self, msg_idx):
        """把被引用消息的 msg_idx 换成撤回接口要的 message_id。"""
        entry = self._by_index.get(msg_idx)
        if not entry:
            return None, "没找到这条消息的记录：机器人重启过，或者当时没收到那条消息（需要「接收所有消息」全量模式）"
        return entry["message_id"], ""

    def last_sent(self, group_openid):
        """取本群最近一条机器人发过的、还能撤回的消息，返回 (记录, 提示)。"""
        now = time.time()
        expired_age = None
        for item in reversed(self._sent):
            if item.get("group_openid") != group_openid:
                continue
            age = now - float(item.get("ts") or 0)
            if age <= RECALL_WINDOW_SECONDS:
                return item, ""
            if expired_age is None:
                expired_age = age

        if expired_age is None:
            return None, "机器人启动后还没有在本群发过消息（转发出的帖子才会被记录）"
        return None, f"最近一条是 {int(expired_age)} 秒前发的，已超过 2 分钟撤回时限"
