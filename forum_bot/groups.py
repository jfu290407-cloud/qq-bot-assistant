# -*- coding: utf-8 -*-
"""已知的群：把见过的群 openid 落盘记下来。

为什么需要它
----------
QQ 开放平台**没有**「查询机器人在哪些群」的接口 —— `/users/@me/guilds`
只覆盖频道，不覆盖群；群 openid 只在事件里出现时才能拿到。所以想知道
"机器人现在在哪些群"，只能平时把见到的记下来，启动时列出来。

记录来源（source）
----------------
    被拉进群    on_group_add_robot            最可靠
    收到消息    群消息事件（@机器人的、以及全量模式的）
    config.yaml 在配置里写死的转发目标群

机器人被移出群（on_group_del_robot）时会把对应记录删掉。

写盘做了节流：同一条记录 10 分钟内不重复写，避免全量模式下每条消息都落盘。
"""
import json
import time

from botpy import logging

from . import config

__all__ = ["KnownGroups"]

_log = logging.get_logger()

# 同一条记录多久才重新写一次盘（秒）
SAVE_THROTTLE_SECONDS = 600


def _format_ts(value):
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(value)))
    except (TypeError, ValueError, OSError):
        return ""


class KnownGroups:
    def __init__(self, path=None):
        self._path = path or config.KNOWN_GROUP_FILE
        self._groups = self._load()

    # ---------------- 持久化 ----------------
    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as file:
                items = json.load(file)
        except Exception:
            return {}
        result = {}
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("group_openid"):
                result[str(item["group_openid"])] = item
        return result

    def _save(self):
        try:
            with open(self._path, "w", encoding="utf-8") as file:
                json.dump(self._sorted(), file, ensure_ascii=False, indent=2)
        except Exception as error:
            _log.warning(f"[botpy] 已知群列表写入失败: {error}")

    def _sorted(self):
        return sorted(self._groups.values(), key=lambda item: float(item.get("ts") or 0), reverse=True)

    # ---------------- 写入 ----------------
    def remember(self, group_openid, source=""):
        """记下一个群。返回是否真的写盘了。"""
        group_openid = str(group_openid or "")
        if not group_openid:
            return False

        now = time.time()
        entry = self._groups.get(group_openid)
        if entry is None:
            self._groups[group_openid] = {
                "group_openid": group_openid,
                "source": source,
                "first_seen": _format_ts(now),
                "last_seen": _format_ts(now),
                "ts": now,
            }
            _log.info(f"[botpy] 记录到新群 {group_openid}（{source or '未知来源'}）")
            self._save()
            return True

        changed = False
        # 来源可以从空/泛化升级到更明确的
        if source and source != entry.get("source"):
            entry["source"] = source
            changed = True
        # last_seen 做节流，避免刷盘
        if now - float(entry.get("ts") or 0) > SAVE_THROTTLE_SECONDS:
            entry["last_seen"] = _format_ts(now)
            entry["ts"] = now
            changed = True
        if changed:
            self._save()
        return changed

    def forget(self, group_openid):
        removed = self._groups.pop(str(group_openid or ""), None)
        if removed:
            _log.info(f"[botpy] 已从已知群列表移除 {group_openid}（机器人被移出该群）")
            self._save()
        return removed

    # ---------------- 查询 ----------------
    def all(self):
        return self._sorted()

    def __len__(self):
        return len(self._groups)
