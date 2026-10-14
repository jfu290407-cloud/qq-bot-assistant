# -*- coding: utf-8 -*-
"""群成员**自助绑定** QQ 号。

为什么要有这个模块
==================
QQ 机器人开放平台**不提供 QQ 号**。官方文档「用户对象(User)」页开头写着：

    用户对象中所涉及的 ID 类数据，都仅在机器人场景流通，**与真实的 ID 无关**。
    请不要理解为真实的 ID

群消息里能拿到的只有 `member_openid`（而且**按 appid 隔离** —— 换个机器人，
同一个人的 openid 就变了）。所以想让转发里出现 QQ 号，只能靠"人自己说"。
这个文件就是那份"人自己说"的记录：

    {"members": {"<member_openid>": {"qq": "123456789", "nick": "张三", ...}}}

**这个绑定无法验证真伪**（这一节请务必读完）
=============================================
机器人没有任何手段证明 member_openid 背后是谁 —— 这正是平台不给 QQ 号的原因。
张三完全可以填李四的 QQ 号。

所以：

  · **它不是安全机制，只是显示用的标签。** 权限判定必须继续走 admin_openids
    （按 openid），**绝不能**改成"看 QQ 号对不对得上"—— 那等于把授权交给了
    一个谁都能填的输入框。
  · 能做的约束只有三条，就这些：**只能绑自己那个 openid**（指令压根不接受
    openid 参数）、限频、管理员能在配置页删掉。
  · 界面文案上要说清楚，别让人以为它验证过身份。

优先级
======
设置项 relay_notes（管理员在配置页手工填 `openid=备注`）仍然有效，而且
**优先级更高** —— 管理员得能纠正用户填错的、也得能给不肯自己绑的人填上：

    1. relay_notes（管理员手工）
    2. 本模块（OneBot 自动匹配的真实号 > 用户自绑的号，见 source 字段）
    3. member_openid 前 8 位

读写的并发
==========
和 group_info 一样：写盘走「临时文件 + os.replace」原子替换，每次读都从盘上读。
配置页的网页线程会直接删这里的记录，机器人必须**立刻**看到 —— 管理员删了却
还在按旧数据转发，是最难查的一类问题。

转发路径（每条消息都要问一次"这个人的 QQ 号")额外加了一层按 mtime 的缓存，
见 QQBindings._entries。
"""
import json
import os
import threading
import time

from botpy import logging

from . import config

__all__ = [
    "QQBindings",
    "normalize_qq",
    "read_bindings",
    "bind_qq",
    "unbind_qq",
    "clear_bindings",
]

_log = logging.get_logger()

# QQ 号长度。5 位是历史最短（QQ 号从 10000 起），12 位留足余量。
# 卡这两头是为了挡住明显不是 QQ 号的东西："123"、"13800138000000000"。
QQ_MIN_LEN = 5
QQ_MAX_LEN = 12

_LOCK = threading.RLock()

# 每次写盘 +1。QQBindings 的缓存拿它判断"要不要重读"——
# 光靠 mtime 不够：Windows 的 mtime 精度可能到 10 毫秒，两次连着的写
# （删一条、再删一条）会撞在同一个刻度上，缓存就永远不会失效。
_GENERATION = [0]


def _path(path=None):
    return path or getattr(config, "MEMBER_QQ_FILE", "") or ""


def normalize_qq(value):
    """把用户填的东西整理成 QQ 号；认不出返回空串。

    **只留数字**：用户经常把说明一起打进来（"我的QQ是123456789"），
    或者带空格、带加号（"+ 123456789"），这些都不该让他重打一遍。
    长度限制则挡住明显不是 QQ 号的东西。

    超出长度**不截断**，直接判为无效 —— 截断会把一个错的号码变成一个
    看起来对的号码，那比拒绝更糟。
    """
    raw = "".join(char for char in str(value or "") if char.isdigit())
    if not QQ_MIN_LEN <= len(raw) <= QQ_MAX_LEN:
        return ""
    return raw


def read_bindings(path=None):
    """读绑定表，返回 {member_openid: {"qq", "nick", "group_openid", "at"}}。

    读失败、结构不对、单条记录坏了 —— 一律当"没有"处理。
    调用方是转发主流程，不能因为一个坏文件就把消息卡住；
    坏掉的记录留着只会让页面显示一条空的，不如扔掉。
    """
    target = _path(path)
    if not target:
        return {}
    try:
        with open(target, "r", encoding="utf-8") as file:
            data = json.load(file)
    except Exception:
        return {}

    members = data.get("members") if isinstance(data, dict) else None
    if not isinstance(members, dict):
        return {}

    result = {}
    for openid, entry in members.items():
        if not isinstance(entry, dict):
            continue
        qq = normalize_qq(entry.get("qq"))
        key = str(openid or "")
        if not key or not qq:
            continue
        result[key] = {
            "qq": qq,
            "nick": str(entry.get("nick") or ""),
            "group_openid": str(entry.get("group_openid") or ""),
            "at": float(entry.get("at") or 0),
            # 来源："self" 用户自己绑 / "onebot" OneBot 自动匹配 / 空 = 旧数据
            "source": str(entry.get("source") or ""),
        }
    return result


def _write(members, path=None):
    """原子替换写盘（临时文件 + os.replace）。"""
    target = _path(path)
    if not target:
        _log.warning("[botpy] 没有配置 MEMBER_QQ_FILE，自助绑定无法保存")
        return
    try:
        directory = os.path.dirname(target)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temp = target + ".tmp"
        with open(temp, "w", encoding="utf-8") as file:
            json.dump({"members": members}, file, ensure_ascii=False, indent=2)
        os.replace(temp, target)
    except Exception as error:
        _log.warning(f"[botpy] QQ 绑定表写入失败: {error}")
        return
    with _LOCK:
        _GENERATION[0] += 1


def bind_qq(member_openid, qq, nick="", group_openid="", path=None, source=""):
    """记下一条自助绑定，返回 (状态, 记录)。

    状态：
        "ok"      新绑定
        "update"  覆盖了原来的绑定（用户换了个号）
        "same"    号码没变（只刷新了昵称 / 时间）
        "bad_qq"  那串东西看着不是 QQ 号
        "no_user" 认不出是谁（消息里没有 member_openid）

    source 标记这条绑定是谁提供的（"self" 用户自绑 / "onebot" 自动匹配），
    配置页据此显示来源。OneBot 的自动匹配按用户决定**覆盖用户自绑的错号**
    （source="onebot" 传进来时，即使号码没变也会把来源刷新成 onebot）。
    """
    openid = str(member_openid or "").strip()
    if not openid:
        return "no_user", {}
    number = normalize_qq(qq)
    if not number:
        return "bad_qq", {}

    with _LOCK:
        members = read_bindings(path)
        old = members.get(openid) or {}
        record = {
            "qq": number,
            # 昵称取新的；这次没带就留着旧的（消息里偶尔取不到 username）
            "nick": str(nick or "").strip() or str(old.get("nick") or ""),
            "group_openid": str(group_openid or "").strip() or str(old.get("group_openid") or ""),
            # 同号重绑**不动 at**：那个时间戳的含义是"他什么时候绑的这个号"，
            # 每发一次都刷新只会让页面上的"绑定时间"变成他最后说话的时间，没意义
            "at": float(old.get("at") or time.time()) if old.get("qq") == number else time.time(),
            "source": str(source or "").strip() or str(old.get("source") or ""),
        }
        if old == record:
            # 一个字段都没变就**不写盘**。有人在群里反复发同一条绑定指令时，
            # 每发一次写一次文件没有任何好处，白白磨损磁盘、还让 mtime 老是变
            return "same", dict(record)
        members[openid] = record
        _write(members, path)

    if not old:
        state = "ok"
    elif old.get("qq") == number:
        state = "same"
    else:
        state = "update"
    return state, dict(record)


def unbind_qq(member_openid, path=None):
    """删掉一条绑定，返回 (是否删掉了, 原来的记录)。"""
    openid = str(member_openid or "").strip()
    if not openid:
        return False, {}
    with _LOCK:
        members = read_bindings(path)
        old = members.pop(openid, None)
        if old is None:
            return False, {}
        _write(members, path)
    return True, old


def clear_bindings(path=None):
    """清空全部绑定（配置页的「清空」按钮），返回删掉的条数。"""
    with _LOCK:
        members = read_bindings(path)
        if not members:
            return 0
        _write({}, path)
    return len(members)


class QQBindings:
    """机器人侧的绑定表：读 + 自助写。

    读接口全部走 `_entries()` 那层缓存 —— 转发是**每条消息**都要问一次
    "这个人的 QQ 号是多少"，每次都读盘太浪费。
    """

    def __init__(self, path=None):
        self._path = path
        self._cache = {}
        self._stamp = None

    def _entries(self):
        """按 (代数, mtime, 大小) 判断要不要重读。

        纯内存缓存会让**配置页删掉的绑定继续生效**（管理员删了却还在转，
        最难查）；纯读盘又太浪费。折中：只在文件真的变过时重读。
        """
        target = _path(self._path)
        if not target:
            return {}
        try:
            stat = os.stat(target)
            file_stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            # 文件还不存在：也算一种状态，并进 stamp 里，
            # 免得刚创建的文件被"上次不存在"的缓存挡住
            file_stamp = None
        with _LOCK:
            stamp = (_GENERATION[0], file_stamp)
            if stamp == self._stamp:
                return self._cache
            self._cache = read_bindings(target)
            self._stamp = stamp
            return self._cache

    # ---------------- 读 ----------------
    def all(self):
        return dict(self._entries())

    def qq_of(self, member_openid):
        """这个人的 QQ 号；没绑定返回空串。"""
        entry = self._entries().get(str(member_openid or "")) or {}
        return str(entry.get("qq") or "")

    def has(self, member_openid):
        return bool(self.qq_of(member_openid))

    def __len__(self):
        return len(self._entries())

    # ---------------- 写 ----------------
    def bind(self, member_openid, qq, nick="", group_openid="", source=""):
        return bind_qq(member_openid, qq, nick, group_openid, self._path, source)

    def unbind(self, member_openid):
        return unbind_qq(member_openid, self._path)

    def describe(self):
        count = len(self._entries())
        if not count:
            return "QQ 自助绑定：还没有人绑定"
        return f"QQ 自助绑定：{count} 人已绑定"
