# -*- coding: utf-8 -*-
"""配置页「概况 / 群聊 / 频道」三个页面的数据组装。

配置页跑在独立线程，这里**只读**各个存储的快照，不做任何写操作
（唯一的例外是「同步频道」，那也只是给 inventory 置个请求标志）。

数据来源一览
-----------
    概况  机器人元信息（inventory.meta）+ 各存储的条数 + 设置里的开关
    群聊  groups（事件记录）+ subscribers（订阅）+ members（见过的成员）+ access（授权）
          + group_info（白名单接口的群名/人数/成员快照，可能为空）
    频道  inventory（API 真查）+ 设置里的「资料板块 ID」
"""
import time

from . import text
from .access import read_state
from .group_info import (
    avatar_url,
    digits_of,
    empty_bot_state,
    read_group_detail,
    read_groups,
    read_notes,
)
from .inventory import as_int, channel_type_label, is_specified_user, private_type_label
from .settings import split_lines

__all__ = ["Dashboard"]

# GET /guilds/{guild_id}/api_permission 返回里的 auth_status
AUTH_STATUS_LABELS = {
    1: "已授权",
    0: "未授权",
}

PERMISSION_NOTE = (
    "这份列表来自 GET /guilds/{guild_id}/api_permission。官方文档只给了示例、"
    "没写 auth_status 的取值定义，这里按通行约定把 1 当「已授权」、0 当「未授权」。"
    "可以用已知能调通的接口反过来核对：比如「修改子频道用户权限」（PUT channels 权限）"
    "既然实际能成功，它就应该显示为已授权；如果显示成未授权，说明 0/1 是反的。"
)


def _uptime_text(start_ts):
    if not start_ts:
        return ""
    seconds = max(0, int(time.time() - float(start_ts)))
    days, rem = divmod(seconds, 86400)
    hours, minutes = divmod(rem // 60, 60)
    parts = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    parts.append(f"{minutes} 分钟")
    return " ".join(parts)


class Dashboard:
    def __init__(self, settings, inventory, members, groups, subscribers, admins, group_info=None):
        self._settings = settings
        self._inventory = inventory
        self._members = members
        self._groups = groups
        self._subscribers = subscribers
        self._admins = admins
        # GroupDirectory：只为读「接口可不可用」「哪些群正在同步」这类运行期状态。
        # 群信息本身还是从文件读（配置页在独立线程，不共享内存）
        self._group_info = group_info

    def _bot_name(self):
        return str((self._inventory.meta() or {}).get("bot_name") or "")

    # ------------------------------------------------------------------
    # 概况
    # ------------------------------------------------------------------
    def overview(self):
        settings = self._settings.snapshot()
        meta = self._inventory.meta()
        sync = self._inventory.snapshot()
        access = read_state()
        guilds = sync["guilds"]
        channel_total = sum(len(item.get("channels") or []) for item in guilds)
        admin_ids = split_lines(settings.get("admin_openids"))
        target = (settings.get("access_channel_id") or "").strip()

        start_ts = meta.get("started_ts")
        return {
            "bot": {
                "name": meta.get("bot_name") or "（未就绪）",
                "appid": meta.get("appid") or "",
                "ready": bool(meta.get("ready")),
                "online_since": meta.get("started_at") or "",
                "uptime": _uptime_text(start_ts),
            },
            "stats": [
                {
                    "key": "groups",
                    "label": "群聊",
                    "value": len(self._groups),
                    "unit": "个群",
                    "note": f"已订阅 {len(self._subscribers)} 个",
                    "panel": "groups",
                },
                {
                    "key": "guilds",
                    "label": "频道与板块",
                    "value": len(guilds),
                    "unit": f"频道 / {channel_total} 板块",
                    "note": f"同步于 {sync['synced_text']}" if sync["synced_text"] else "还没同步过",
                    "panel": "guilds",
                },
                {
                    "key": "access",
                    "label": "资料板块授权",
                    "value": len(access["bindings"]),
                    "unit": "人",
                    "note": f"待兑换验证码 {access['pending_codes']} 个",
                    "panel": "access",
                },
                {
                    "key": "members",
                    "label": "记录到的群成员",
                    "value": len(self._members),
                    "unit": "人",
                    "note": f"管理员 {len(admin_ids)} 人",
                    "panel": "admin",
                },
            ],
            "modules": self._modules(settings, target, guilds),
            "sync": {
                "text": sync["synced_text"],
                "error": sync["last_error"],
                "has_data": bool(guilds),
                "syncing": sync["syncing"],
            },
        }

    def _modules(self, settings, target, guilds):
        """功能开关一览（只反映设置状态，不猜测运行期行为）。"""
        cached_groups = read_groups()["groups"]
        target_channel = None
        for guild in guilds:
            for channel in guild.get("channels") or []:
                if channel.get("id") == target:
                    target_channel = channel
        if target and target_channel and not is_specified_user(target_channel.get("private_type")):
            access_note = "板块不是「指定成员可见」，授权不会生效"
        elif target:
            access_note = "已就绪"
        else:
            access_note = "还没设置资料板块 ID"

        return [
            {
                "label": "帖子转发",
                "enabled": True,
                "note": settings.get("mode") or "instant",
                "panel": "strategy",
            },
            {
                "label": "Markdown 卡片",
                "enabled": (settings.get("style") or "text") != "text",
                "note": settings.get("style") or "text",
                "panel": "card",
            },
            {
                "label": "免打扰时段",
                "enabled": bool(settings.get("quiet_hours_enabled")),
                "note": f"{settings.get('quiet_start')} - {settings.get('quiet_end')}",
                "panel": "strategy",
            },
            {
                "label": "加群解锁板块",
                "enabled": bool(settings.get("access_enabled")),
                "note": access_note,
                "panel": "access",
            },
            {
                "label": "拉黑自动撤回",
                "enabled": bool(settings.get("blacklist_enabled")),
                "note": f"{len(split_lines(settings.get('blacklist_keywords')))} 条内容",
                "panel": "block",
            },
            {
                "label": "关键词自动撤回",
                "enabled": bool(settings.get("recall_enabled")),
                "note": f"{len(split_lines(settings.get('recall_keywords')))} 个词"
                if settings.get("recall_enabled")
                else "未开启",
                "panel": "recall",
            },
            {
                "label": "成员对账",
                "enabled": bool(settings.get("access_enabled"))
                and bool(settings.get("access_reconcile", True)),
                "note": (
                    f"已缓存 {len(cached_groups)} 个群的成员快照"
                    if cached_groups
                    else "还没有群信息数据（该接口可能需要白名单权限）"
                ),
                "panel": "access",
            },
            {
                "label": "群主自动放行",
                "enabled": bool(settings.get("admin_allow_group_owner")),
                "note": "群主 / 群管理员免名单用管理指令",
                "panel": "admin",
            },
            {
                "label": "打印群消息",
                "enabled": bool(settings.get("log_group_messages")),
                "note": "排查字段用，稳定后可关掉",
                "panel": "debug",
            },
        ]

    @staticmethod
    def _relay_recv_warning(settings, rows):
        """互转开着、但有群没把消息推给机器人时，返回一句警告（否则返回空串）。

        这是「规则配了却不生效」最常见、也最隐蔽的原因：`recv_msg_setting` 不是 `all`
        时机器人根本收不到群里的普通聊天，用户看不到任何报错，只会去反复改规则。
        所以直接摆到群聊页顶上，把真正的原因说清楚。
        """
        if not settings.get("relay_enabled"):
            return ""
        blocked = [
            str(row.get("group_no") or "") or str(row.get("group_openid") or "")[:10]
            for row in rows
            if str((row.get("bot_state") or {}).get("recv_msg_setting") or "")
            not in ("", "all")
        ]
        if not blocked:
            return ""
        return text.text("messages.relay.recv_warning", groups="、".join(blocked))

    # ------------------------------------------------------------------
    # 群聊
    # ------------------------------------------------------------------
    def groups(self):
        settings = self._settings.snapshot()
        subscribed = self._subscribers.all()
        access_group = (settings.get("access_group_openid") or "").strip()
        target = (settings.get("access_channel_id") or "").strip()
        # 白名单接口查到的群信息（群名 / 人数 / 成员快照），拿不到权限时是空的
        official = {
            row["group_openid"]: row
            for row in read_groups(bot_name=self._bot_name())["groups"]
        }
        refreshing = set(self._group_info.refreshing) if self._group_info else set()
        # 备注里填的群号；没填的话，「加群解锁」那个群可以用 access_group_label 兜一下
        # （那里通常填的就是群号，省得再填一遍）
        fallback_no = digits_of(settings.get("access_group_label"))
        # **备注直接读文件**，不绕 official —— official 只包含「调通 /info 的群」，
        # 某个群没查到基本信息时它的备注就会显示成空，看起来像"填了没保存上"。
        notes = read_notes()

        member_counts = {}
        for item in self._members.all():
            for group_openid in item.get("groups") or []:
                member_counts[group_openid] = member_counts.get(group_openid, 0) + 1

        binding_counts = {}
        for binding in read_state()["bindings"]:
            group_openid = binding.get("group_openid") or ""
            if group_openid:
                binding_counts[group_openid] = binding_counts.get(group_openid, 0) + 1

        rows = []
        for item in self._groups.all():
            group_openid = str(item.get("group_openid") or "")
            info = official.get(group_openid) or {}
            is_access = bool(access_group) and group_openid == access_group
            # 备注（本地数据）驱动群号与头像；官方接口根本不给群号
            note = notes.get(group_openid, "")
            # 没填备注时，「加群解锁」那个群退回用 access_group_label 兜
            number = digits_of(note) or (fallback_no if is_access else "")
            rows.append(
                {
                    "group_openid": group_openid,
                    "source": item.get("source") or "",
                    "first_seen": item.get("first_seen") or "",
                    "last_seen": item.get("last_seen") or "",
                    "subscribed": group_openid in subscribed,
                    "member_count": member_counts.get(group_openid, 0),
                    "binding_count": binding_counts.get(group_openid, 0),
                    "is_access_group": is_access,
                    "ts": float(item.get("ts") or 0),
                    # 白名单接口的结果（没有就是空）
                    "name": info.get("name") or "",
                    "memo": info.get("memo") or "",
                    "class_text": info.get("class_text") or "",
                    "tags": info.get("tags") or [],
                    "group_member_num": info.get("member_num"),
                    "member_snapshot": info.get("member_snapshot") or 0,
                    "info_at": info.get("info_at") or "",
                    "member_at": info.get("member_at") or "",
                    # 本地备注（群号）与头像；平台接口不返回群号，只能本地填
                    "note": note,
                    "group_no": number,
                    "avatar": avatar_url(number),
                    "bot_role": info.get("bot_role") or "",
                    "bot_role_label": info.get("bot_role_label") or "",
                    # 机器人在群内的状态（bot_state 接口）。
                    # 没查过的群给一份**空壳**，页面就按同一形状读，
                    # 省得到处判空、也不会因为少一个键就报错。
                    "bot_state": info.get("bot_state") or empty_bot_state(),
                    # 运行期状态
                    "refreshing": group_openid in refreshing,
                    "has_snapshot": bool(info.get("member_at")),
                }
            )
        # 订阅的排在前面，其余按最后活跃倒序
        rows.sort(key=lambda row: (not row["subscribed"], -row["ts"]))

        return {
            "rows": rows,
            "subscribed_total": len(subscribed),
            "access_channel_id": target,
            "official_available": bool(official),
            "api_available": self._group_info.available() if self._group_info else bool(official),
            "api_hint": (self._group_info.denied_hint() if self._group_info else ""),
            # 互转开着、但某些群没把消息推给机器人 —— 直接点出来，别让人去猜规则
            "relay_warning": self._relay_recv_warning(settings, rows),
            "note": (
                "平台没有「查询机器人在哪些群」的接口，这份列表来自事件记录："
                "被拉进群、收到群消息、以及 config.yaml 里写死的转发目标群。"
                "机器人被移出群会自动移除。"
                "群名 / 群人数 / 群成员来自「获取群基本信息」等接口，属于平台白名单能力"
                "（未开通会返回 11253，拿不到就是空白）；"
                "群号平台接口不返回，填在「备注」里才能显示出来并加载群头像。"
            ),
        }

    # ------------------------------------------------------------------
    # 群详情（面板点「详情」后展开的那一页）
    # ------------------------------------------------------------------
    def group_detail(self, group_openid):
        group_openid = str(group_openid or "")
        settings = self._settings.snapshot()
        row = read_group_detail(group_openid, bot_name=self._bot_name())

        known = {}
        for item in self._groups.all():
            known[str(item.get("group_openid") or "")] = item
        item = known.get(group_openid) or {}

        access_group = (settings.get("access_group_openid") or "").strip()
        is_access = bool(access_group) and group_openid == access_group
        number = row.get("group_no") or ""
        if not number and is_access:
            number = digits_of(settings.get("access_group_label"))
        row["group_no"] = number
        row["avatar"] = avatar_url(number)

        bindings = [
            binding for binding in read_state()["bindings"]
            if binding.get("group_openid") == group_openid
        ]
        subscribed = group_openid in self._subscribers.all()
        row.update(
            {
                "source": item.get("source") or "",
                "first_seen": item.get("first_seen") or "",
                "last_seen": item.get("last_seen") or "",
                "subscribed": subscribed,
                "is_access_group": is_access,
                "binding_count": len(bindings),
                "bindings": bindings,
                "refreshing": group_openid in (self._group_info.refreshing if self._group_info else []),
                "api_available": self._group_info.available() if self._group_info else False,
                "api_hint": (self._group_info.denied_hint() if self._group_info else ""),
            }
        )
        row["features"] = self._group_features(settings, row, subscribed, is_access, len(bindings))
        return row

    @staticmethod
    def _group_features(settings, row, subscribed, is_access, binding_count):
        """「功能管理」页签：这个群上现在生效的功能。"""
        bot_role = row.get("bot_role_label") or "未知（还没同步过成员）"
        return [
            {
                "label": "转发频道新帖到本群",
                "enabled": subscribed,
                "note": "群里发「订阅」/「取消订阅」切换" if subscribed else "本群没有订阅",
                "panel": "strategy",
            },
            {
                "label": "加群解锁资料板块",
                "enabled": bool(settings.get("access_enabled")) and is_access,
                "note": (
                    f"本群是解锁群，已授权 {binding_count} 人"
                    if is_access
                    else "本群不是「指定解锁群」"
                ),
                "panel": "access",
            },
            {
                "label": "成员对账（退群回收权限）",
                "enabled": bool(settings.get("access_reconcile", True)) and bool(settings.get("access_enabled")),
                "note": (
                    f"上次同步 {row.get('member_at') or '还没同步过'}，共 {row.get('member_snapshot') or 0} 人"
                    if row.get("member_snapshot")
                    else "还没有成员快照，点右上角「同步成员」"
                ),
                "panel": "access",
            },
            {
                "label": "群主 / 群管理员免名单用管理指令",
                "enabled": bool(settings.get("admin_allow_group_owner")),
                "note": f"机器人身份：{bot_role}",
                "panel": "admin",
            },
            {
                "label": "拉黑内容自动撤回",
                "enabled": bool(settings.get("blacklist_enabled")),
                "note": f"{len(split_lines(settings.get('blacklist_keywords')))} 条内容",
                "panel": "block",
            },
        ]

    # ------------------------------------------------------------------
    # 频道
    # ------------------------------------------------------------------
    def guilds(self):
        sync = self._inventory.snapshot()
        # 每个频道的资料板块 / 推送策略都可能是独立配置的，所以逐频道取生效值
        known_groups = self._groups.all()

        result = []
        for guild in sync["guilds"]:
            guild_id = str(guild.get("id") or "")
            target = (self._settings.snapshot(guild_id).get("access_channel_id") or "").strip()
            profile = self._settings.profile(guild_id)

            channels = []
            for channel in guild.get("channels") or []:
                channel_id = str(channel.get("id") or "")
                channels.append(
                    {
                        "id": channel_id,
                        "name": channel.get("name") or "(无名)",
                        "type": channel_type_label(channel.get("type")),
                        "private": private_type_label(channel.get("private_type")),
                        "nested": bool(channel.get("nested")),
                        "is_target": bool(target) and channel_id == target,
                        "target_ok": is_specified_user(channel.get("private_type")),
                    }
                )
            permissions = []
            for item in guild.get("permissions") or []:
                status = as_int(item.get("auth_status"), -1)
                permissions.append(
                    {
                        "path": item.get("path") or "",
                        "method": (item.get("method") or "").upper(),
                        "desc": item.get("desc") or "",
                        "auth_status": status,
                        "granted": status == 1,
                        "status_text": AUTH_STATUS_LABELS.get(status, f"未知({status})"),
                    }
                )
            # 未授权的排前面，方便一眼看到缺什么
            permissions.sort(key=lambda it: (it["granted"], it["desc"] or it["path"]))
            granted = sum(1 for item in permissions if item["granted"])

            subscribers = profile.get("subscribers") or []
            routes = profile.get("channel_routes") or {}
            result.append(
                {
                    "id": guild_id,
                    "name": guild.get("name") or "(无名)",
                    "icon": guild.get("icon") or "",
                    "channel_number": guild.get("channel_number") or "",
                    "description": guild.get("description") or "",
                    "member_count": guild.get("member_count"),
                    "owner": bool(guild.get("owner")),
                    "joined_at": guild.get("joined_at") or "",
                    "channel_count": len(channels),
                    "channel_error": guild.get("channel_error") or "",
                    "channels": channels,
                    "permissions": permissions,
                    "permission_granted": granted,
                    "permission_total": len(permissions),
                    "permission_error": guild.get("permission_error") or "",
                    # --- 多频道：这个频道的独立配置 ---
                    "has_profile": guild_id in self._settings.guild_ids(),
                    "label": profile.get("label") or "",
                    "override_count": len(self._settings.overridden_keys(guild_id)),
                    "subscribers": [str(item) for item in subscribers],
                    "channel_routes": routes,
                    "access_channel_id": target,
                    "access_group_openid": (
                        self._settings.snapshot(guild_id).get("access_group_openid") or ""
                    ).strip(),
                    "access_enabled": bool(self._settings.snapshot(guild_id).get("access_enabled")),
                    "mode": self._settings.snapshot(guild_id).get("mode"),
                }
            )

        # 已知群（「转发目标群」勾选框的候选池）。
        # **必须带上群号 / 群名** —— 以前页面只显示 openid 前 10 位（39FD6F7D3A…），
        # 等于让人从一串乱码里盲选，根本没法确认哪个群是哪个。
        notes = read_notes()
        known = []
        for item in known_groups:
            openid = str(item.get("group_openid") or "")
            note = notes.get(openid, "")
            number = digits_of(note)
            name = ""
            if self._group_info is not None:
                try:
                    name = self._group_info.name_of(openid)
                except Exception:
                    name = ""
            known.append(
                {
                    "group_openid": openid,
                    "source": item.get("source") or "",
                    "last_seen": item.get("last_seen") or "",
                    "note": note,
                    "number": number,
                    "group_name": name,
                    # 展示标签：**群号优先** —— 那是用户自己填的备注，一定认得出来；
                    # 其次群名；最后才退回 openid（至少能对上「群聊」页那一列）
                    "label": number or name or (
                        openid[:10] + ("…" if len(openid) > 10 else "")
                    ),
                }
            )
        # 有群号的排前面并按号码排，认不出来的沉底 —— 要勾的群几乎一定有群号
        known.sort(key=lambda row: (0 if row["number"] else 1, row["label"]))

        return {
            "guilds": result,
            "known_groups": known,
            "global_subscribers": sorted(self._subscribers.all()),
            "synced_text": sync["synced_text"],
            "last_error": sync["last_error"],
            "has_data": bool(sync["guilds"]),
            "syncing": sync["syncing"],
            "permission_note": PERMISSION_NOTE,
            "multi_note": (
                "机器人可以同时加多个频道，每个频道各配一套：**转发到哪些群**、"
                "推送策略与文案、资料板块与解锁群、频道号。"
                "没做独立配置的频道会跟随「全局」那一档。"
            ),
        }
