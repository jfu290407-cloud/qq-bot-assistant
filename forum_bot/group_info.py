# -*- coding: utf-8 -*-
"""群信息与群成员查询（平台白名单能力）。

四个接口
--------
    GET /v2/groups/{group_openid}/info                      30 QPM
    GET /v2/groups/{group_openid}/members?cursor=           60 QPM，每页 30 条
    GET /v2/groups/{group_openid}/members/{member_openid}   30 QPM
    GET /v2/groups/{group_openid}/bot_state                 30 QPM

都是**仅白名单机器人可用**，未开通会返回 11253（应用无接口访问权限）。
所以这个模块的第一原则是**优雅降级**：一旦确认没权限就记住，之后不再调用。
否则定时对账每 10 分钟撞一次墙，几行真正的日志会被 400 报错淹掉。

bot_state 值得单说
------------------
它返回的两项直接决定功能**能不能用**，而不是"好不好用"：

    recv_msg_setting     群里的消息接收设置。只有 `all` 才收得到群里的**每一条**消息
                         —— **群消息互转（relay）完全依赖它**。配成 only_mention 时
                         机器人只看得到 @它 的消息，互转等于半瘫，而且现象很隐蔽
                         （会以为是自己规则写错了，去反复改规则）。
    allow_proactive_msg  本群是否接受主动推送。互转和帖子转发发出去的都是主动消息，
                         它是 false 时发一条失败一条。

所以配置页把这两个值显示出来，本身就很值 —— 它们不对的话，
别的地方怎么调都没用。

为什么值得接进来
----------------
在拿到成员列表之前，「谁退群了」只能靠 GROUP_MEMBER_REMOVE 事件，而事件是会漏的：

  · 机器人掉线 / 重启期间有人退群，事件推给了空气，永远补不回来；
  · 忘了在开放平台申请 GROUP_MEMBER_EVENT 的话，等于完全没有回收能力。

有了成员列表就能定期**对账**（见 access.reconcile），把这两块补上。

安全红线
--------
**只有全部分页都拉成功，才允许拿名单去对账。**

中途失败得到一个"少了几个人"的残缺名单，拿它对账会把**还在群里**的用户
权限误收回 —— 那比漏收严重得多（用户会直接来投诉）。所以 fetch_members()
要么返回完整名单，要么返回 None；调用方看到 None 就直接跳过这一轮。
"""
import asyncio
import json
import os
import threading
import time

from botpy import logging

from . import config, text
from .admins import role_label

__all__ = [
    "GroupDirectory",
    "read_groups",
    "read_group_detail",
    "read_notes",
    "write_note",
    "avatar_url",
    "digits_of",
    "recv_msg_label",
    "empty_bot_state",
    "role_label",
    "PAGE_SIZE",
    "NO_PERMISSION_CODE",
]

_log = logging.get_logger()

# 群备注只在配置页写、机器人只读，所以单独一个文件。
# 不塞进 forum_groups.json 是因为那个文件机器人侧也在写（群信息/成员快照），
# 两个线程同时写同一个 json 会互相覆盖。
_notes_lock = threading.Lock()

# 平台固定每页 30 条（接口不支持 limit 参数）
PAGE_SIZE = 30

# 兜底：最多翻这么多页（30 * 40 = 1200 人），防止游标异常导致死循环
MAX_PAGES = 40

# 翻页之间的间隔，别把 60 QPM 打满
PAGE_INTERVAL = 0.2

# 群基本信息默认缓存多久（秒）
INFO_CACHE_SECONDS = 3600

# 没有接口权限的业务错误码
NO_PERMISSION_CODE = "11253"

# 配置页发来的请求（保存备注 / 同步成员）多久检查一次
REQUEST_CHECK_SECONDS = 5

# 注意：role_label 是从 admins 再导出的（见文件顶部的 import）。
# 角色文案只有一份（text.yaml 的 labels.group_role）—— 配置页、启动日志、
# 群成员列表读的都是它，改一处全都跟着变。


def digits_of(text):
    """抠出一串里的数字，用来判断备注里有没有填 QQ 群号。"""
    return "".join(char for char in str(text or "") if char.isdigit())


def recv_msg_label(value):
    """群内「接收消息设置」的中文名（bot_state 的 recv_msg_setting）。

    认不出来就**原样返回**：平台以后加新取值时，页面上至少还能看到原始值，
    而不是变成一片空白让人摸不着头脑。
    """
    key = str(value or "").strip()
    if not key:
        return ""
    label = text.get(f"labels.recv_msg_setting.{key}")
    if isinstance(label, str) and label.strip():
        return label
    return key


def avatar_url(group_no):
    """QQ 群头像地址。

    **群号是拼头像的唯一办法**：平台没有给「群头像」的字段，
    `p.qlogo.cn/gh/{群号}/{群号}/640` 这个地址里的两段都是群号。
    而三个群信息接口返回的字段里**没有群号**（只有群 openid / 群名 / 人数），
    所以群号只能由管理员在面板上填「群备注」—— 填了才有头像。
    """
    number = digits_of(group_no)
    if len(number) < 5:
        return ""
    return f"https://p.qlogo.cn/gh/{number}/{number}/640"


def _format_ts(value):
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(value)))
    except (TypeError, ValueError, OSError):
        return ""


def _read(path):
    """读盘并做结构兜底，GroupDirectory 和配置页共用。"""
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except Exception:
        data = None
    if not isinstance(data, dict):
        return {"groups": {}, "members": {}}
    return {
        "groups": data.get("groups") if isinstance(data.get("groups"), dict) else {},
        "members": data.get("members") if isinstance(data.get("members"), dict) else {},
    }


def read_notes(path=None):
    """读群备注表 {group_openid: 备注}。

    备注主要是拿来放 **QQ 群号** 的：平台那三个群信息接口**都不返回群号**，
    而群头像地址（p.qlogo.cn/gh/{群号}/...）又必须要群号，所以只能本地记一份。
    """
    try:
        with open(path or config.GROUP_NOTE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(key): str(value).strip()
        for key, value in data.items()
        if str(key) and str(value).strip()
    }


def write_note(group_openid, note, path=None):
    """写入 / 删除一条群备注（空备注 = 删除）。

    配置页在独立线程里直接调这个，**不经过机器人** —— 备注文件只有网页线程写，
    机器人只读，所以没有竞态。落盘用「临时文件 + os.replace」保证原子性。
    """
    group_openid = str(group_openid or "")
    if not group_openid:
        return {}
    note = str(note or "").strip()[:64]
    target = path or config.GROUP_NOTE_FILE

    with _notes_lock:
        notes = read_notes(target)
        if note:
            notes[group_openid] = note
        else:
            notes.pop(group_openid, None)
        temp = target + ".tmp"
        try:
            with open(temp, "w", encoding="utf-8") as file:
                json.dump(notes, file, ensure_ascii=False, indent=2)
            os.replace(temp, target)
        except Exception as error:
            _log.warning(f"[botpy] 群备注写入失败: {error}")
    return notes


def _bot_member(members, bot_name=""):
    """从成员快照里认出「机器人自己」那一条。

    接口没有「我是谁」的返回，只能这样认：
      1. 优先按昵称匹配（成员的 username == 机器人昵称）；
      2. 认不出就退回第一个 bot=true 的成员。
    群里有别的机器人时第 2 步可能认错，所以第 1 步优先。
    """
    bots = [
        item for item in members
        if isinstance(item, dict) and item.get("bot")
    ]
    if not bots:
        return {}
    if bot_name:
        for item in bots:
            if str(item.get("username") or "") == str(bot_name):
                return item
    return bots[0]


def _member_row(item):
    return {
        "openid": str(item.get("member_openid") or ""),
        "username": str(item.get("username") or ""),
        "role": str(item.get("member_role") or ""),
        "role_label": role_label(item.get("member_role")),
        "bot": bool(item.get("bot")),
        "joined_at": str(item.get("joined_at") or ""),
        "union_openid": str(item.get("union_openid") or ""),
    }


def _bot_state_row(entry):
    """把 bot_state 的缓存整理成面板要用的形状。

    **available 和"值是空"要分开**：available=False 是"这个群还没查过"，
    查过但平台没给某个字段是另一回事 —— 页面上两者的说法完全不同。
    """
    state = entry.get("bot_state") if isinstance(entry, dict) else None
    state = state if isinstance(state, dict) else {}
    role = str(state.get("member_role") or "")
    setting = str(state.get("recv_msg_setting") or "")
    proactive = state.get("allow_proactive_msg")
    return {
        "available": bool(state),
        "at": _format_ts(entry.get("bot_state_at") if isinstance(entry, dict) else None),
        "member_role": role,
        "member_role_label": role_label(role) if role else "",
        "joined_at": str(state.get("joined_at") or ""),
        # 平台没给这个字段时是 None，**不能当成 False** —— "没数据"和"明确不接受"
        # 是两回事，页面上的颜色和文案都不一样
        "allow_proactive_msg": proactive if isinstance(proactive, bool) else None,
        "recv_msg_setting": setting,
        "recv_msg_label": recv_msg_label(setting),
    }


def empty_bot_state():
    """没查过 bot_state 时的空壳，形状和 _bot_state_row 完全一致。

    给"只在备注里出现过、还没同步过的群"用 —— 页面按同一形状读，
    不用到处判 None（少一类"忘了判空"的崩溃）。
    """
    return _bot_state_row({})


def _group_row(group_openid, entry, member_entry, bot_name="", with_members=False, note=""):
    """把一份缓存整理成面板要用的形状。"""
    entry = entry if isinstance(entry, dict) else {}
    info = entry.get("info") if isinstance(entry.get("info"), dict) else {}
    member_entry = member_entry if isinstance(member_entry, dict) else {}
    raw_members = member_entry.get("list")
    raw_members = raw_members if isinstance(raw_members, list) else []
    tags = info.get("group_tags")

    note = str(note or "").strip()
    number = digits_of(note)
    members = [_member_row(item) for item in raw_members if isinstance(item, dict)]
    bot = _bot_member(raw_members, bot_name)

    row = {
        "group_openid": group_openid,
        "name": str(info.get("group_name") or ""),
        "memo": str(info.get("group_finger_memo") or ""),
        "class_text": str(info.get("group_class_text") or ""),
        "tags": [str(item) for item in tags] if isinstance(tags, list) else [],
        "member_num": info.get("group_member_num"),
        "info_at": _format_ts(entry.get("at")),
        # 本地备注：群号是靠它拿到的（平台接口不给群号）
        "note": note,
        "group_no": number,
        "avatar": avatar_url(number),
        # 机器人在这个群里的身份（从成员快照里的 bot 成员推出来）
        "bot_role": str(bot.get("member_role") or ""),
        "bot_role_label": role_label(bot.get("member_role")) if bot else "",
        "bot_name": str(bot.get("username") or ""),
        "bot_joined_at": str(bot.get("joined_at") or ""),
        "member_snapshot": len(members),
        "member_at": _format_ts(member_entry.get("at")),
        # 机器人在群内的状态（bot_state 接口）。这两项决定功能**能不能用**：
        #   recv_msg_setting    不是 all 时，群消息互转收不到普通聊天，等于半瘫
        #   allow_proactive_msg 为 false 时，主动消息（互转 / 帖子转发）发一条失败一条
        "bot_state": _bot_state_row(entry),
    }
    if with_members:
        # 平台返回的顺序是随机的，按「群主 -> 管理员 -> 机器人 -> 普通成员」排一下，
        # 面板上一眼就能看到关键的人
        order = {"owner": 0, "admin": 1}
        members.sort(key=lambda m: (order.get(m["role"], 3 if not m["bot"] else 2), m["username"]))
        row["members"] = members
        row["owner"] = next((m for m in members if m["role"] == "owner"), None)
        row["admins"] = [m for m in members if m["role"] == "admin"]
    return row


def read_groups(path=None, bot_name="", notes_path=None):
    """只读地把群信息缓存整理成面板要的形状（不需要 client / settings）。

    配置页跑在独立线程，所以这里读文件而不是共享内存 —— 和 access.read_state 一个套路。

    列表 = **接口缓存里的群 + 只在备注里出现过的群**。
    后者不能漏：备注（QQ 群号）完全是本地数据，不该因为某个群还没查过基本信息
    就在面板上消失 —— 用户填了群号却看不到，会以为没保存上。
    """
    data = _read(path or config.GROUP_FILE)
    notes = read_notes(notes_path)
    # 先列缓存里的（信息最全），再补上只有备注的
    ids = list(data["groups"]) + [key for key in notes if key not in data["groups"]]
    groups = [
        _group_row(
            group_openid, data["groups"].get(group_openid) or {},
            data["members"].get(group_openid), bot_name,
            note=notes.get(group_openid, ""),
        )
        for group_openid in ids
    ]
    groups.sort(key=lambda row: row["group_openid"])
    return {"groups": groups}


def read_group_detail(group_openid, path=None, bot_name="", notes_path=None):
    """单个群的详情（含完整成员列表），给面板的「群详情」用。"""
    data = _read(path or config.GROUP_FILE)
    notes = read_notes(notes_path)
    group_openid = str(group_openid or "")
    # 备注是本地数据，**不看接口脸色**，一定要读出来。
    # 群还没查到基本信息时（没权限 / 没刷新过）走下面的 found=False 分支 ——
    # 那条分支以前直接 return 一个空字典，备注就跟着丢了，正是"填了却看不到"的原因。
    note = notes.get(group_openid, "")
    entry = data["groups"].get(group_openid)
    if entry is None:
        row = _group_row(group_openid, {}, None, bot_name, note=note)
        row["found"] = False
        # 保持输出形状统一（页面会直接读 row.members）——
        # 没缓存就是空列表，而不是这个键不存在
        row["members"] = []
        return row
    row = _group_row(
        group_openid, entry, data["members"].get(group_openid), bot_name,
        with_members=True, note=note,
    )
    row["found"] = True
    return row


class GroupDirectory:
    """群信息 / 群成员的取数与缓存。"""

    def __init__(self, api, path=None):
        self._api = api
        self._path = path or config.GROUP_FILE
        self._data = _read(self._path)
        # 拿到 11253 之后就置位，之后不再调用（只在内存里，重启会再试一次）。
        #
        # **按接口分开记**：平台是逐个接口授予白名单的 —— 实测过「群基本信息」
        # 有权限而「群成员列表」报 11253 的情况。用一个总开关会让能用的那个
        # 接口也被一起停掉（群名 / 人数就再也不刷新了）。
        self._denied_info = ""
        self._denied_members = ""
        self._denied_bot_state = ""
        # 机器人昵称，用来在成员快照里认出自己（配置页显示「机器人身份」）
        self._bot_name = ""
        # 配置页线程发来的「同步成员」请求。网页线程不能 await 异步接口，
        # 所以只能置标志、由机器人的循环去消费（和 Inventory.request_sync 一个套路）。
        # 群备注不走这条路，配置页直接写文件（见 write_note）。
        self._pending_members = set()
        # 正在同步的群（配置页据此把按钮置灰）
        self._refreshing = set()

    # ------------------------------------------------------------------
    # 可用性
    # ------------------------------------------------------------------
    def info_available(self):
        """「获取群基本信息」能不能用（群名 / 群人数 / 群分类）。"""
        return not self._denied_info

    def members_available(self):
        """「群成员列表 / 群成员信息」能不能用（成员快照、对账都依赖它）。"""
        return not self._denied_members

    def bot_state_available(self):
        """「获取机器人群内状态」能不能用（群内身份 / 是否接受主动消息 / 接收设置）。"""
        return not self._denied_bot_state

    def available(self):
        """任一接口可用。

        **判断具体功能时别用这个** —— 平台逐个接口授权，
        「群基本信息有权限、群成员列表没有」是常态。用上面三个细分方法。
        """
        return (
            self.info_available()
            or self.members_available()
            or self.bot_state_available()
        )

    def denied_hint(self, kind="info"):
        """无权限时的说明。kind 取 "info" / "members" / "bot_state"。"""
        if kind == "members":
            return self._denied_members
        if kind == "bot_state":
            return self._denied_bot_state
        return self._denied_info

    def retry(self):
        """清掉「无权限」标记，让下次调用重新试一遍（配置页「重新检测」用）。"""
        self._denied_info = ""
        self._denied_members = ""
        self._denied_bot_state = ""

    def _deny(self, error, kind="info"):
        """这次失败是不是「没权限」？是就按接口记下来并返回 True。

        **按接口分开记**：平台是逐个接口授予白名单的。
        """
        # 变量名别叫 text —— 那个名字在这个模块里是文案模块
        message = str(error)
        if NO_PERMISSION_CODE not in message and "无接口访问权限" not in message:
            return False
        hint = (
            "这个机器人没有群信息接口的访问权限（11253）。"
            "「获取群基本信息 / 群成员列表 / 群成员信息 / 机器人群内状态」是白名单能力，"
            "需要在 QQ 开放平台联系运营申请开通"
        )
        which = {
            "members": "群成员接口",
            "bot_state": "机器人群内状态接口",
        }.get(kind, "群基本信息接口")
        if kind == "members":
            self._denied_members = hint
        elif kind == "bot_state":
            self._denied_bot_state = hint
        else:
            self._denied_info = hint
        _log.warning(
            f"[botpy] {hint}（本次是{which}，之后不再调用它；"
            f"其它接口不受影响。重启会重新试一次）"
        )
        return True

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def _save(self):
        try:
            with open(self._path, "w", encoding="utf-8") as file:
                json.dump(self._data, file, ensure_ascii=False, indent=2)
        except Exception as error:
            _log.warning(f"[botpy] 群信息缓存写入失败: {error}")

    # ------------------------------------------------------------------
    # 配置页发来的请求（网页线程写，机器人循环消费）
    # ------------------------------------------------------------------
    def set_bot_name(self, name):
        self._bot_name = str(name or "")

    def bot_name(self):
        return self._bot_name

    def request_members(self, group_openid=None):
        """请求同步某个群的成员；不传就同步所有已知的群。

        备注不走这里 —— 备注是配置页直接写文件的（见 write_note），
        这样点完就是即时生效，不用等机器人循环来消费。
        """
        self._pending_members.add(str(group_openid or "*"))

    def take_members_request(self):
        members, self._pending_members = self._pending_members, set()
        return members

    @property
    def refreshing(self):
        return sorted(self._refreshing)

    async def serve_once(self, known_groups=None):
        """把积压的配置页请求处理掉。返回处理过的群 openid 列表。"""
        members = self.take_members_request()

        targets = set()
        if members:
            if "*" in members:
                # known_groups 可以是 list，也可以是 KnownGroups.all 这样的函数
                # （传函数才能拿到"此刻"的列表，而不是启动时的快照）
                source = known_groups() if callable(known_groups) else (known_groups or [])
                targets.update(
                    str(item.get("group_openid") or "")
                    for item in source
                )
                targets.discard("")
            else:
                targets.update(item for item in members if item)

        done = []
        for group_openid in sorted(targets):
            self._refreshing.add(group_openid)
            try:
                await self.fetch_info(group_openid)
                await self.fetch_bot_state(group_openid)
                await self.fetch_members(group_openid)
                done.append(group_openid)
            finally:
                self._refreshing.discard(group_openid)
        return done

    async def serve_loop(self, known_groups=None):
        """定时消费配置页发来的请求。

        轮询而不是用事件，是因为配置页在另一个线程、也没有共享的 loop；
        REQUEST_CHECK_SECONDS 只有 5 秒，点「同步成员」的体感几乎即时。
        """
        while True:
            await asyncio.sleep(REQUEST_CHECK_SECONDS)
            try:
                await self.serve_once(known_groups)
            except Exception as error:
                _log.warning(f"[botpy] 群信息任务异常: {error}")

    # ------------------------------------------------------------------
    # 缓存读取
    # ------------------------------------------------------------------
    def note_of(self, group_openid):
        entry = self._data["groups"].get(str(group_openid or "")) or {}
        return str(entry.get("note") or "")

    def bot_member_of(self, group_openid):
        """机器人自己在这个群里的成员记录（用于显示「机器人身份」）。"""
        return _bot_member(self.members_of(group_openid), self._bot_name)

    def info_of(self, group_openid):
        entry = self._data["groups"].get(str(group_openid or "")) or {}
        info = entry.get("info")
        return info if isinstance(info, dict) else {}

    def bot_state_of(self, group_openid):
        """机器人在这个群里的状态（没查过就是空 dict）。"""
        entry = self._data["groups"].get(str(group_openid or "")) or {}
        state = entry.get("bot_state")
        return state if isinstance(state, dict) else {}

    def members_of(self, group_openid):
        entry = self._data["members"].get(str(group_openid or "")) or {}
        items = entry.get("list")
        return items if isinstance(items, list) else []

    def member_ids_of(self, group_openid):
        return {
            str(item.get("member_openid") or "")
            for item in self.members_of(group_openid)
            if isinstance(item, dict) and item.get("member_openid")
        }

    def members_text(self, group_openid):
        entry = self._data["members"].get(str(group_openid or "")) or {}
        if not entry:
            return ""
        return f"{len(entry.get('list') or [])} 人（{_format_ts(entry.get('at'))} 拉取）"

    def name_of(self, group_openid):
        return str(self.info_of(group_openid).get("group_name") or "")

    # ------------------------------------------------------------------
    # 取数
    # ------------------------------------------------------------------
    async def fetch_info(self, group_openid, max_age=0, save=True):
        """取群基本信息。返回 (info, 提示)。

        max_age > 0 且缓存没超过这个秒数时直接用缓存，不发请求。
        """
        group_openid = str(group_openid or "")
        if not group_openid:
            return {}, "没给群 openid"
        if not self.info_available():
            return self.info_of(group_openid), self._denied_info

        if max_age:
            entry = self._data["groups"].get(group_openid) or {}
            fresh = entry.get("info") and time.time() - float(entry.get("at") or 0) < max_age
            if fresh:
                return entry["info"], ""

        try:
            info = await self._api.get_group_info(group_openid=group_openid)
        except Exception as error:
            if self._deny(error, "info"):
                return self.info_of(group_openid), self._denied_info
            _log.warning(f"[botpy] 获取群信息失败 group={group_openid}: {error}")
            return self.info_of(group_openid), f"获取群信息失败：{error}"

        info = info if isinstance(info, dict) else {}
        if save:
            # **必须合并进已有的 entry**，不能整条替换 ——
            # 直接赋值会把这个群里刚查到的 bot_state 一起抹掉，
            # 页面上就会轮流丢数据（拉完信息丢状态、拉完状态丢信息）。
            entry = self._data["groups"].setdefault(group_openid, {})
            entry["info"] = info
            entry["at"] = time.time()
            self._save()
        _log.info(
            f"[botpy] 群信息 group={group_openid} "
            f"名称={info.get('group_name')!r} 人数={info.get('group_member_num')}"
        )
        return info, ""

    async def fetch_bot_state(self, group_openid, save=True):
        """取机器人在这个群里的状态，返回 (state, 提示)。

        返回的是两个"决定功能能不能用"的值：
            recv_msg_setting     不是 all 的话，群消息互转收不到普通聊天
            allow_proactive_msg  是 false 的话，主动消息发一条失败一条
        """
        group_openid = str(group_openid or "")
        if not group_openid:
            return {}, "没给群 openid"
        if not self.bot_state_available():
            return self.bot_state_of(group_openid), self._denied_bot_state

        try:
            state = await self._api.get_group_bot_state(group_openid=group_openid)
        except Exception as error:
            if self._deny(error, "bot_state"):
                return self.bot_state_of(group_openid), self._denied_bot_state
            _log.warning(
                f"[botpy] 获取机器人群内状态失败 group={group_openid}: {error}"
            )
            return self.bot_state_of(group_openid), f"获取群内状态失败：{error}"

        state = state if isinstance(state, dict) else {}
        if save:
            entry = self._data["groups"].setdefault(group_openid, {})
            entry["bot_state"] = state
            entry["bot_state_at"] = time.time()
            self._save()

        setting = str(state.get("recv_msg_setting") or "")
        _log.info(
            f"[botpy] 群内状态 group={group_openid} "
            f"身份={state.get('member_role')!r} "
            f"接收消息={setting!r} "
            f"接受主动消息={state.get('allow_proactive_msg')}"
        )
        if setting and setting != "all":
            # 这条值得单独说：用户会以为是互转规则写错了，去反复改规则，
            # 而真正的原因是群里根本没把消息推给机器人
            _log.warning(
                f"[botpy] 群 {group_openid} 的消息接收设置是 {setting!r}（不是 all），"
                f"机器人收不到群里的普通聊天 —— 群消息互转对这个群不会生效。"
                f"需要群管理员在群设置里把机器人的消息接收改为「接收全部消息」"
            )
        return state, ""

    async def fetch_members(self, group_openid, save=True, retries=2):
        """拉全量群成员。

        **要么返回完整名单，要么返回 None**（见模块开头的「安全红线」）。
        retries 是整轮重试次数 —— 中途某页超时是常见的，重来一整轮比
        接着残缺的游标往下翻要安全得多。
        """
        group_openid = str(group_openid or "")
        if not group_openid or not self.members_available():
            return None

        for attempt in range(1, max(1, retries) + 1):
            members = await self._walk_members(group_openid)
            if members is not None:
                if save:
                    self._data["members"][group_openid] = {
                        "list": members,
                        "at": time.time(),
                    }
                    self._save()
                _log.info(f"[botpy] 已拉取群 {group_openid} 成员 {len(members)} 人")
                return members
            if attempt < retries:
                await asyncio.sleep(1)
        return None

    async def _walk_members(self, group_openid):
        """按游标翻完全部页；任何一页失败都返回 None。"""
        members = []
        seen = set()
        cursor = ""

        for page in range(1, MAX_PAGES + 1):
            try:
                resp = await self._api.get_group_members(
                    group_openid=group_openid, cursor=cursor
                )
            except Exception as error:
                if self._deny(error, "members"):
                    return None
                _log.warning(
                    f"[botpy] 拉群成员第 {page} 页失败（group={group_openid}）: {error}"
                )
                return None

            resp = resp if isinstance(resp, dict) else {}
            for item in resp.get("members") or []:
                if not isinstance(item, dict):
                    continue
                openid = str(item.get("member_openid") or "")
                if openid and openid not in seen:
                    seen.add(openid)
                    members.append(item)

            next_cursor = str(resp.get("next_cursor") or "")
            if not next_cursor:
                return members
            if next_cursor == cursor:
                # 游标没前进 = 平台行为异常，再翻就是死循环
                _log.warning(f"[botpy] 群成员游标没有前进（{cursor!r}），停止翻页")
                return None
            cursor = next_cursor
            await asyncio.sleep(PAGE_INTERVAL)

        _log.warning(f"[botpy] 群成员翻页超过 {MAX_PAGES} 页仍未到底，放弃本轮")
        return None

    async def fetch_member(self, group_openid, member_openid):
        """取单个群成员信息，失败返回 None。"""
        group_openid = str(group_openid or "")
        member_openid = str(member_openid or "")
        if not group_openid or not member_openid or not self.members_available():
            return None
        try:
            member = await self._api.get_group_member(
                group_openid=group_openid, member_openid=member_openid
            )
        except Exception as error:
            if self._deny(error, "members"):
                return None
            # 「成员不在群里」也会以错误形式返回，这个是正常分支，别刷 warning
            _log.info(
                f"[botpy] 查群成员失败 group={group_openid} member={member_openid}: {error}"
            )
            return None
        return member if isinstance(member, dict) else None

    # ------------------------------------------------------------------
    # 展示
    # ------------------------------------------------------------------
    def describe(self, group_openid=None):
        if not self.available():
            return "群信息接口：不可用（未获白名单权限，11253）"
        group_openid = str(group_openid or "")
        info = self.info_of(group_openid) if group_openid else {}
        if not info:
            return "群信息接口：可用（还没查过）"
        return (
            f"群信息接口：可用；群「{info.get('group_name') or '(无名)'}」"
            f"{info.get('group_member_num') or '?'} 人"
        )

    def describe_lines(self, group_openid):
        """「群信息」指令的输出。"""
        group_openid = str(group_openid or "")
        if not self.available():
            return [
                "群信息接口不可用。",
                self._denied or "",
                "「获取群基本信息 / 群成员列表 / 群成员信息」是平台白名单能力，",
                "需要在 QQ 开放平台联系运营申请开通后才能用。",
            ]

        info = self.info_of(group_openid)
        lines = [f"群 openid：{group_openid}"]
        if not info:
            lines.append("（还没查到群信息，稍后再试，或发「刷新群信息」）")
        else:
            lines += [
                f"群名称：{info.get('group_name') or '(没拿到)'}",
                f"群人数：{info.get('group_member_num') or '(没拿到)'}",
            ]
            if info.get("group_class_text"):
                lines.append(f"群分类：{info['group_class_text']}")
            if info.get("group_finger_memo"):
                lines.append(f"群简介：{info['group_finger_memo']}")
            tags = info.get("group_tags")
            if isinstance(tags, list) and tags:
                lines.append("群标签：" + "、".join(str(item) for item in tags))

        snapshot = self.members_text(group_openid)
        if snapshot:
            lines.append(f"上次拉到的成员快照：{snapshot}")
        else:
            lines.append("成员快照：还没有（发「刷新群信息」拉一份）")
        return lines

    def member_lines(self, group_openid, limit=30):
        """群成员列表的文本版（「群成员」指令 / 排查用）。"""
        members = self.members_of(group_openid)
        if not members:
            return ["还没有成员快照，发「刷新群信息」拉一份"]
        lines = []
        for item in members[:limit]:
            lines.append(
                f"{item.get('username') or '(没拿到昵称)'}"
                f"　{role_label(item.get('member_role'))}"
                f"　入群 {item.get('joined_at') or '?'}"
            )
        if len(members) > limit:
            lines.append(f"…… 其余 {len(members) - limit} 人未显示")
        return lines
