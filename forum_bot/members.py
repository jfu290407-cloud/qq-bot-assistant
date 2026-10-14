# -*- coding: utf-8 -*-
"""「见过的群成员」名册：openid / 昵称 / 群内身份 / 最后活跃时间。

解决一个很实际的麻烦：管理员只能按 member_openid 授权，而它长这样 ——

    476D2D3DF90A6E51614842420EDF775B

让人去日志里翻再手抄，很容易抄错一位。这里把收到消息的成员顺手记下来并
落盘（forum_seen_members.json），配置页就能直接列出候选名单，点一下加进
管理员名单即可。

只记昵称/身份这类不敏感信息，且只记「给机器人发过消息、或全量模式下
在群里发过消息」的人。名单有上限（默认 300 人），按最后活跃时间淘汰。
"""
import json
import time

from botpy import logging

from . import config
from .admins import member_name_of, member_openid_of, member_role_of, role_label

__all__ = ["SeenMembers"]

_log = logging.get_logger()

# 名册最多记多少人
MAX_MEMBERS = 300


class SeenMembers:
    def __init__(self, path=None):
        self._path = path or config.MEMBER_FILE
        self._members = self._load()

    # ---------------- 持久化 ----------------
    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as file:
                items = json.load(file)
        except Exception:
            return {}
        result = {}
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("openid"):
                result[str(item["openid"])] = item
        return result

    def _save(self):
        try:
            with open(self._path, "w", encoding="utf-8") as file:
                json.dump(self._sorted(), file, ensure_ascii=False, indent=2)
        except Exception as error:
            _log.warning(f"[botpy] 群成员名册写入失败: {error}")

    # ---------------- 写入 ----------------
    def remember(self, message):
        """记下这条消息的发送者。机器人自己不记。"""
        openid = member_openid_of(message)
        if not openid:
            return
        raw = getattr(message, "raw", None) or {}
        if (raw.get("author") or {}).get("bot"):
            return

        role = member_role_of(message)
        group_openid = str(getattr(message, "group_openid", None) or "")
        entry = self._members.get(openid) or {"openid": openid, "groups": []}
        entry["username"] = member_name_of(message) or entry.get("username") or ""
        entry["role"] = role or entry.get("role") or ""
        entry["role_label"] = role_label(entry["role"])
        entry["last_seen"] = time.strftime("%Y-%m-%d %H:%M:%S")
        entry["_ts"] = time.time()

        groups = [item for item in (entry.get("groups") or []) if item]
        if group_openid and group_openid not in groups:
            groups.append(group_openid)
        entry["groups"] = groups[-5:]

        self._members[openid] = entry
        self._evict()
        self._save()

    def _evict(self):
        if len(self._members) <= MAX_MEMBERS:
            return
        for item in self._sorted()[MAX_MEMBERS:]:
            self._members.pop(item["openid"], None)

    # ---------------- 查询 ----------------
    def _sorted(self):
        return sorted(self._members.values(), key=lambda item: float(item.get("_ts") or 0), reverse=True)

    def all(self):
        """按最后活跃时间倒序返回名册（给配置页用，去掉内部字段）。"""
        result = []
        for item in self._sorted():
            result.append({key: value for key, value in item.items() if not key.startswith("_")})
        return result

    def find(self, openid):
        return self._members.get(str(openid))

    def __len__(self):
        return len(self._members)
