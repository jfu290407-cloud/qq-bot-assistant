# -*- coding: utf-8 -*-
"""拉黑名单：内容 + 本地来源记录。

内容本身只有一份真相 —— settings 里的 blacklist_keywords（配置页可直接
批量编辑，改动立刻写回 forum_forward_settings.json，重启不丢）。这里不再
另存一份内容，避免两处不一致。

forum_blacklist.json 记的是**每条拉黑是怎么来的**：

    {
      "content": "123456",
      "by_openid": "476D2D3DF90A6E51614842420EDF775B",
      "by_name": "张三",
      "group_openid": "39FD6F7D...",
      "at": "2026-09-14 16:13:41"
    }

有什么用：
  · 群里「拉黑列表」会带上是谁在哪个群加的，方便回溯；
  · 配置页的拉黑名单会显示来源，手工在页面上加的标成「手动添加」；
  · 取消拉黑时同步清掉对应记录。

手工在配置页删掉某条内容、但记录还在时，下次打开会以内容列表为准，
残留记录会被自动清理。
"""
import json
import os
import threading
import time

from botpy import logging

from . import config
from .admins import member_name_of, member_openid_of
from .settings import split_lines

__all__ = ["Blacklist", "MemberBlacklist"]

_log = logging.get_logger()


class Blacklist:
    def __init__(self, settings, path=None):
        self._settings = settings
        self._path = path or config.BLACKLIST_FILE
        self._sources = self._load()

    # ---------------- 持久化 ----------------
    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as file:
                items = json.load(file)
        except Exception:
            return {}
        result = {}
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("content"):
                result[str(item["content"])] = item
        return result

    def _save(self):
        try:
            with open(self._path, "w", encoding="utf-8") as file:
                json.dump(list(self._sources.values()), file, ensure_ascii=False, indent=2)
        except Exception as error:
            _log.warning(f"[botpy] 拉黑记录写入失败: {error}")

    # ---------------- 内容（settings 是唯一真相） ----------------
    def contents(self):
        return split_lines(self._settings.snapshot().get("blacklist_keywords"))

    def _write_contents(self, phrases):
        saved = self._settings.update({"blacklist_keywords": "\n".join(phrases)})
        return split_lines(saved.get("blacklist_keywords"))

    def entries(self):
        """内容 + 来源，给配置页和「拉黑列表」指令用。"""
        result = []
        for index, content in enumerate(self.contents(), start=1):
            source = self._sources.get(content) or {}
            result.append(
                {
                    "index": index,
                    "content": content,
                    "by_name": source.get("by_name") or "",
                    "by_openid": source.get("by_openid") or "",
                    "group_openid": source.get("group_openid") or "",
                    "at": source.get("at") or "",
                    "manual": not source,
                }
            )
        return result

    def _source_text(self, entry):
        if entry.get("manual"):
            return "手动添加"
        who = entry.get("by_name") or (entry.get("by_openid") or "")[:8]
        when = entry.get("at") or ""
        group = (entry.get("group_openid") or "")[:8]
        parts = [item for item in (who, when) if item]
        text = " / ".join(parts)
        return f"{text}（群 {group}…）" if group else text

    def describe_lines(self, limit=50):
        """「拉黑列表」指令的输出行。"""
        lines = []
        for entry in self.entries()[:limit]:
            lines.append(f"{entry['index']}. {entry['content']}　—— {self._source_text(entry)}")
        more = len(self.entries()) - len(lines)
        if more > 0:
            lines.append(f"…… 其余 {more} 条未显示")
        return lines

    # ---------------- 增删 ----------------
    def add(self, text, message=None, by_name="", by_openid="", group_openid=""):
        """加一条拉黑，返回 (是否新增, 加入后的总数)。

        来源默认从 message 里取；**配置页从 AI 候选里加**时没有 message，
        用 by_name 等参数直接给（比如 "AI 拆分"）—— 不然会标成"手动添加"，
        管理员回溯时不知道是 AI 建议的。
        """
        phrases = self.contents()
        added = text not in phrases
        if added:
            phrases = self._write_contents(phrases + [text])

        source = {
            "content": text,
            "by_openid": by_openid
            or (member_openid_of(message) if message is not None else ""),
            "by_name": by_name
            or (member_name_of(message) if message is not None else ""),
            "group_openid": group_openid
            or (
                str(getattr(message, "group_openid", None) or "")
                if message is not None
                else ""
            ),
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if added or text not in self._sources:
            self._sources[text] = {key: value for key, value in source.items() if value}
            self._save()

        who = source["by_name"] or source["by_openid"] or "未知"
        if added:
            _log.info(f"[botpy] 已拉黑 {text!r}（{who}），当前共 {len(phrases)} 条")
        else:
            _log.info(f"[botpy] {text!r} 已在拉黑名单里（由 {who} 再次提交），仍为 {len(phrases)} 条")
        return added, len(phrases)

    def remove(self, text):
        """按内容解除拉黑，返回被移除的内容列表。"""
        phrases = self.contents()
        # 引用预览可能被截断，这里做双向包含的宽容匹配
        matched = [item for item in phrases if item == text or text in item or item in text]
        if not matched:
            return []
        phrases = self._write_contents([item for item in phrases if item not in matched])
        for item in matched:
            self._sources.pop(item, None)
        self._save()
        return matched

    def prune(self):
        """清掉「记录里有、内容列表里已经没有」的残留（配置页手工删过的）。"""
        valid = set(self.contents())
        stale = [key for key in self._sources if key not in valid]
        if stale:
            for key in stale:
                self._sources.pop(key, None)
            self._save()
        return len(stale)


# ----------------------------------------------------------------------
# 成员黑名单（按 member_openid）
# ----------------------------------------------------------------------
_MEMBER_LOCK = threading.RLock()
# 每次写盘 +1。实例缓存靠它判断"要不要重读" —— 光看 mtime 不够：
# Windows 的 mtime 精度可能到 10 毫秒，连着写两次会撞在同一个刻度上，
# 缓存就永远不会失效（配置页删了人、机器人还在撤，最难查）。
_MEMBER_GENERATION = [0]


class MemberBlacklist:
    """**成员**黑名单（按 member_openid）。

    和上面那个内容黑名单是两件事，别混：

      · 内容黑名单 —— 这条**内容**不许再说（别人说同样的也撤）
      · 成员黑名单 —— 这个**人**说的话都撤（不看内容）

    分开的理由：管理员有时候只想屏蔽一个词（广告词），有时候想屏蔽一个人
    （刷屏的）。混在一起必然互相误伤。

    为什么用它代替「踢人」
    ----------------------
    平台的 QQ群接口里**没有移除成员的能力**（唯一的那个
    `DELETE /guilds/{id}/members/{uid}` 是 QQ**频道**的，不是群）。
    所以"把人赶走"只能用这个办法：他还在群里，但一说话就被撤。

    「在所有群生效」的两个前提
    --------------------------
    名单按 member_openid 匹配，撤回是**逐个群各调一次**接口。所以：

      1. **member_openid 要跨群稳定** —— 同一个人在不同群里得是同一个 openid。
         官方文档没明说，先用 ForumSeen 的数据验一下：
         forum_seen_members.json 里给每个人都记了"出现在哪些群"
         （见配置页「群成员」列表里的群数）。
      2. **机器人在那些群得是管理员** —— 否则撤普通成员的消息会报 40062003。
         这种情况 recall 那边会记下来不再白试（见 RecallGuard）。
    """

    def __init__(self, path=None):
        self._path = path or config.MEMBER_BLACKLIST_FILE
        self._cache = {}
        self._stamp = None

    # ---------------- 持久化 ----------------
    def _entries(self):
        """带缓存的读取。

        撤回是**每条消息**都要问一次"这个人被拉黑了吗"，每次都读盘太浪费；
        但纯内存缓存又会让配置页删掉的人继续被撤。折中：只在文件真变过时重读。
        """
        try:
            stat = os.stat(self._path)
            file_stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            file_stamp = None
        with _MEMBER_LOCK:
            stamp = (_MEMBER_GENERATION[0], file_stamp)
            if stamp == self._stamp:
                return self._cache
            self._cache = self._load()
            self._stamp = stamp
            return self._cache

    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as file:
                data = json.load(file)
        except Exception:
            return {}
        members = data.get("members") if isinstance(data, dict) else None
        if not isinstance(members, dict):
            return {}
        result = {}
        for openid, entry in members.items():
            key = str(openid or "")
            if key and isinstance(entry, dict):
                result[key] = dict(entry)
        return result

    def _save(self, members):
        try:
            temp = self._path + ".tmp"
            with open(temp, "w", encoding="utf-8") as file:
                json.dump({"members": members}, file, ensure_ascii=False, indent=2)
            os.replace(temp, self._path)
        except Exception as error:
            _log.warning(f"[botpy] 成员黑名单写入失败: {error}")
            return
        with _MEMBER_LOCK:
            _MEMBER_GENERATION[0] += 1

    # ---------------- 读 ----------------
    def is_banned(self, openid):
        """这个 member_openid 在黑名单里吗（转发/撤回的热路径会频繁调）。"""
        return str(openid or "") in self._entries()

    def find(self, openid):
        return self._entries().get(str(openid or ""))

    def entries(self):
        """按加入时间倒序，给配置页和「成员黑名单」指令用。"""
        return sorted(
            self._entries().values(),
            key=lambda item: str(item.get("at") or ""),
            reverse=True,
        )

    def __len__(self):
        return len(self._entries())

    # ---------------- 写 ----------------
    def add(self, openid, target_message=None, by_openid="", by_name=""):
        """把一个人加进成员黑名单，返回 (是否新增, 加入后的总数, 记录)。

        参数分得细一点是有原因的 —— **"被拉黑的人"和"下指令的人"是两个人**：

          target_message   被拉黑那个人的一条消息（取昵称、所在群），可为 None
          by_openid/by_name  是谁下的手（指令发出者）

        之前把这两者混成一个 message，结果 `by_name` 记成了**被拉黑那个人的昵称**
        （看着像"是他自己拉黑的自己"）。分开就没有这个问题。
        """
        key = str(openid or "")
        if not key:
            return False, len(self._entries()), {}
        with _MEMBER_LOCK:
            members = dict(self._load())
            existed = key in members
            record = members.get(key) or {"openid": key}
            # 昵称取新的；这次取不到就留着旧的（消息里偶尔没有 username）
            if target_message is not None:
                record["username"] = (
                    member_name_of(target_message) or record.get("username") or ""
                )
                record["group_openid"] = (
                    str(getattr(target_message, "group_openid", None) or "")
                    or record.get("group_openid") or ""
                )
            if by_openid:
                record["by_openid"] = str(by_openid)
            if by_name:
                record["by_name"] = str(by_name)
            record["at"] = record.get("at") or time.strftime("%Y-%m-%d %H:%M:%S")
            members[key] = record
            self._save(members)

        who_text = record.get("username") or key[:8]
        if existed:
            _log.info(f"[botpy] 成员黑名单：{who_text} 已经在名单里（{len(members)} 人）")
        else:
            # 用 WARNING：这一步的后果比"加一条拉黑词"重得多 —— 他在**任何群**
            # 说话都会被撤，而且撤不掉的时候是静默的（只在日志里说）
            _log.warning(
                f"[botpy] 成员黑名单：已加入 {who_text}（{key[:8]}…），"
                f"他在任何群发消息都会被撤回（共 {len(members)} 人）"
            )
        return not existed, len(members), dict(record)

    def remove(self, openid):
        """按 openid 移出成员黑名单，返回 (是否移除了, 记录)。"""
        key = str(openid or "")
        if not key:
            return False, {}
        with _MEMBER_LOCK:
            members = dict(self._load())
            old = members.pop(key, None)
            if old is None:
                return False, {}
            self._save(members)
        _log.info(f"[botpy] 成员黑名单：已移出 {old.get('username') or key[:8]}")
        return True, old

    def clear(self):
        """清空成员黑名单，返回清掉的条数。"""
        with _MEMBER_LOCK:
            members = self._load()
            if not members:
                return 0
            self._save({})
        _log.warning(f"[botpy] 成员黑名单：已清空 {len(members)} 人")
        return len(members)
