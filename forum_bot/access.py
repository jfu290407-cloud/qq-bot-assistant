# -*- coding: utf-8 -*-
"""「加入 QQ 群才给频道板块权限」：设备码流程 + 用户级子频道权限下发。

为什么方向必须是「频道侧发码、群侧兑换」
--------------------------------------
机器人拿不到 QQ 号；频道 user_id 和群 member_openid 是两套完全独立、
且按 appid 隔离的 ID 空间，平台也没有任何接口能把它们对上。
所以只能让用户在两个平台各走一步，由机器人把两边身份"缝"起来 ——
而这两步里各自能拿到的信息是不对称的：

  · 频道里 @机器人：事件的 author.id 就是「要拿权限的那个人」，身份明确；
  · 群里   @机器人：事件的 author.member_openid 是「这个人此刻真的在群里」
    的唯一可信证明（平台没有查询群成员的接口）。

于是码在**频道侧**生成并绑定 user_id，在**群侧**兑换：

    频道 @机器人「领资料」  ->  码 ABCD23（绑定 user_id）
    群   @机器人 ABCD23     ->  拿到 member_openid，双向校验通过
    机器人 -> update_channel_user_permissions(add=view_permission)

反过来的流程（群侧发码、频道侧兑换）有个洞：群里任何人都能把码转给
不在群的人，机器人在频道侧无法分辨兑换者是谁，权限就白送了。

码公开也不怕
-----------
偷到码的人必须「自己在群里发消息」才能兑换，而这一步只是替**码原本绑定的
那个频道用户**完成验证，偷码者本人什么也拿不到；而他真在群里的话，
他本来就有资格自己领一个码。所以码就算被截图公开也无所谓。

一进一出的闭环
-------------
· 进：群成员加入事件（GROUP_MEMBER_ADD）只记日志；
· 出：群成员退出/被移出事件（GROUP_MEMBER_REMOVE）-> 立即收回该用户权限。
  这个事件是回收权限的**主要**手段；`access_days` 有效期只是兜底
  （应对事件漏推、或者用户只是"换个号"的情况）。

三重回收，谁都不会单独可靠
------------------------
    1. 退群事件   实时，但会漏（机器人掉线期间退群的永远补不回来）
    2. 成员对账   主动拉群成员列表比对，补上事件漏掉的那部分（见 reconcile）
    3. 有效期     完全静态，专治"换个号"和上面两条都失效的情况

第 2 条依赖平台的白名单接口（11253），拿不到权限时会自动降级成"不做对账"，
不影响前两条。
"""
import asyncio
import json
import random
import re
import time

from botpy import logging
from botpy.flags import Permission

from . import config, text

__all__ = [
    "AccessGuard",
    "normalize_code",
    "looks_like_code",
    "read_state",
    "CODE_LENGTH",
    "CODE_CHARS",
]

_log = logging.get_logger()

# 验证码：6 位纯数字。
# 特意不含 0 —— 以 0 开头的码用户很容易漏打那个 0（"012345" -> "12345"），
# 而验证码是按字符串比的，漏一位就直接失败。
# 剩下 9 个数字 6 位 = 531441 种组合，配上 5 分钟时效和一次性使用足够了。
CODE_CHARS = "123456789"
CODE_LENGTH = 6

# 只保留 ASCII 字母数字（中文、空格、中划线等会被清掉，避免"这是个码吗"的误判）
_CODE_INPUT_RE = re.compile(r"[^0-9A-Za-z]")

# 定期回收（过期 / 退群未收回成功）的间隔
SWEEP_INTERVAL_SECONDS = 600


def normalize_code(text):
    """把用户输入规整成大写、去掉空格和中划线等杂字符。"""
    return _CODE_INPUT_RE.sub("", str(text or "")).upper()


def looks_like_code(text):
    """这句群消息是不是一个验证码。

    长度和字符集都严格匹配，避免和正常聊天混淆：
    中文、带字母、长度不是 6、含 0 的，都会被判为"不是码"。
    """
    code = normalize_code(text)
    if len(code) != CODE_LENGTH:
        return False
    return all(char in CODE_CHARS for char in code)


def _format_ts(value):
    if not value:
        return ""
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(value)))
    except (TypeError, ValueError, OSError):
        return ""


def _read_file(path):
    """读盘并做结构兜底，AccessGuard 和配置页共用。"""
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except Exception:
        data = None
    if not isinstance(data, dict):
        return {"codes": {}, "bindings": {}}
    return {
        "codes": data.get("codes") if isinstance(data.get("codes"), dict) else {},
        "bindings": data.get("bindings") if isinstance(data.get("bindings"), dict) else {},
    }


def read_state(path=None):
    """只读地读授权表，给配置页展示用（不需要 client / settings）。"""
    data = _read_file(path or config.ACCESS_FILE)
    codes = data["codes"]
    now = time.time()
    bindings = []
    for item in sorted(data["bindings"].values(), key=lambda it: float(it.get("granted") or 0), reverse=True):
        expire = item.get("expire")
        bindings.append(
            {
                "user_id": str(item.get("user_id") or ""),
                "guild_id": str(item.get("guild_id") or ""),
                "group_openid": str(item.get("group_openid") or ""),
                "granted": _format_ts(item.get("granted")),
                "expire": _format_ts(expire) if expire else text.text("labels.long_term"),
                "left": bool(item.get("left_at")),
                "revoke_failed": str(item.get("revoke_failed") or ""),
            }
        )
    # 顺带把过期/用过的码清出来（只返回数量，页面不展示码本身）
    alive = sum(
        1
        for item in codes.values()
        if not item.get("used") and float(item.get("expire") or 0) > now
    )
    return {"bindings": bindings, "pending_codes": alive}


class AccessGuard:
    """验证码、绑定关系、以及子频道权限的下发与回收。"""

    def __init__(self, client, settings, registry=None, path=None):
        self._client = client
        self._settings = settings
        # 群信息目录（白名单接口）。没有它也能跑，只是少了「对账」这一路回收。
        self._registry = registry
        self._path = path or config.ACCESS_FILE
        self._data = self._load()

    # ------------------------------------------------------------------
    # 配置读取（按频道）
    # ------------------------------------------------------------------
    def _cfg(self, guild_id=None):
        return self._settings.snapshot(guild_id)

    def enabled(self, guild_id=None):
        """这个频道的「加群解锁」开着吗。

        guild_id 为空时表示"有没有任何一个频道开着"，给启动日志和定时任务用。
        """
        if guild_id is None:
            if self._settings.snapshot().get("access_enabled"):
                return True
            return any(
                self._cfg(item).get("access_enabled")
                for item in self._settings.guild_ids()
            )
        return bool(self._cfg(guild_id).get("access_enabled"))

    def target_guilds(self, known_guild_ids=()):
        """需要在频道里执行「加群解锁」的频道列表（对账、诊断用）。

        = 配置里出现过的频道 ∪ 调用方给的已知频道，再按各自是否开启过滤。
        """
        ids = list(dict.fromkeys(
            list(self._settings.guild_ids())
            + [str(item) for item in known_guild_ids if item]
        ))
        return [item for item in ids if self._cfg(item).get("access_enabled")]

    def channel_id(self, guild_id=None):
        """要解锁的「资料」子频道 ID。"""
        return str(self._cfg(guild_id).get("access_channel_id") or "").strip()

    def group_openids(self, guild_id=None):
        """要求加入的群列表（**可配多个**，任一群里都能兑换）。

        配置里一行一个 openid，兼容逗号分隔；非法/空行忽略。
        返回空列表表示"不限制群"（旧行为）。
        """
        raw = str(self._cfg(guild_id).get("access_group_openid") or "")
        groups = []
        for item in raw.replace("，", ",").replace("\n", ",").split(","):
            group = item.strip()
            if group and group not in groups:
                groups.append(group)
        return groups

    def group_openid(self, guild_id=None):
        """第一个配置的群（旧接口，文案/日志用；校验走 group_openids）。"""
        groups = self.group_openids(guild_id)
        return groups[0] if groups else ""

    def group_label(self, guild_id=None):
        return str(self._cfg(guild_id).get("access_group_label") or "").strip() or text.text(
            "labels.fallback_group"
        )

    def ttl_seconds(self, guild_id=None):
        return max(1, int(self._cfg(guild_id).get("access_code_ttl_minutes") or 5)) * 60

    def valid_seconds(self, guild_id=None):
        """授权有效期；0 表示永久（此时完全依赖退群事件回收）。"""
        days = int(self._cfg(guild_id).get("access_days") or 0)
        return days * 86400 if days > 0 else 0

    def missing_config(self, guild_id=None):
        """返回还没配置好的项，用于给管理员提示。"""
        missing = []
        if not self.channel_id(guild_id):
            missing.append(text.text("messages.access.missing_channel"))
        if not self.group_openids(guild_id):
            missing.append(text.text("messages.access.missing_group"))
        return missing

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    @staticmethod
    def _key(guild_id, user_id):
        """绑定表的主键。

        同一个 QQ 用户在**两个频道里的 user_id 是同一个**，所以光按 user_id 存
        会导致一个人在 A 频道解锁后，在 B 频道就绑不上了。因此主键带上频道维度：

            "3500634359415041451::USER_ABC"

        旧数据（键就是 user_id）在 _load 里自动迁移。
        """
        return f"{guild_id}::{user_id}"

    def _load(self):
        data = _read_file(self._path)
        # 迁移旧格式：键只有 user_id 的补上频道前缀。
        # 这里不删原文件内容，_save() 时自然写回新格式。
        migrated = False
        for key, binding in list(data["bindings"].items()):
            if "::" in key or not isinstance(binding, dict):
                continue
            guild_id = str(binding.get("guild_id") or "")
            user_id = str(binding.get("user_id") or key)
            new_key = self._key(guild_id, user_id)
            if new_key != key:
                data["bindings"].pop(key, None)
                data["bindings"][new_key] = binding
                migrated = True
        if migrated:
            _log.info(f"[botpy] 绑定表已迁移为「频道::用户」格式，共 {len(data['bindings'])} 条")
            self._data = data
            self._save()
        return data

    def _save(self):
        try:
            with open(self._path, "w", encoding="utf-8") as file:
                json.dump(self._data, file, ensure_ascii=False, indent=2)
        except Exception as error:
            _log.warning(f"[botpy] 授权绑定表写入失败: {error}")

    # ------------------------------------------------------------------
    # 验证码
    # ------------------------------------------------------------------
    def _new_code(self):
        while True:
            code = "".join(random.choice(CODE_CHARS) for _ in range(CODE_LENGTH))
            if code not in self._data["codes"]:
                return code

    def issue(self, user_id, guild_id=None, channel_name=""):
        """给频道用户发一个验证码。

        验证码**绑定发码时所在的频道**，因为要解锁的板块和要加的群都是按频道配的。
        channel_name 是板块的真实名字（调用方实时查好传进来），给
        「你已经有权限了」那条回复用。返回 (结果 dict 或 None, 给用户看的说明)。
        """
        guild_id = str(guild_id or "")
        if not self.enabled(guild_id):
            return None, text.text("messages.access.feature_off")
        missing = self.missing_config(guild_id)
        if missing:
            return None, text.text(
                "messages.access.not_configured", items="、".join(missing)
            )

        user_id = str(user_id or "")
        if not user_id:
            return None, text.text("messages.access.no_user_id")

        existing = self.binding_of(user_id, guild_id)
        if existing:
            return None, self.status_text(user_id, guild_id, channel_name)

        now = time.time()
        # 同一个人在同一个频道里的旧码还没过期就直接复用：省得刷屏，也避免用户重复点
        for code, item in self._data["codes"].items():
            if item.get("user_id") != user_id or item.get("used"):
                continue
            if str(item.get("guild_id") or "") != guild_id:
                continue
            remain = float(item.get("expire") or 0) - now
            if remain > 0:
                return {
                    "code": code,
                    "expire_minutes": max(1, int(remain // 60) + 1),
                    "reused": True,
                }, ""

        code = self._new_code()
        self._data["codes"][code] = {
            "user_id": user_id,
            "guild_id": guild_id,
            "created": now,
            "expire": now + self.ttl_seconds(guild_id),
            "used": False,
        }
        self._save()
        _log.info(f"[botpy] 已下发验证码 {code} 给频道用户 {user_id}（频道 {guild_id or '未指定'}）")
        return {
            "code": code,
            "expire_minutes": self.ttl_seconds(guild_id) // 60,
            "reused": False,
        }, ""

    def redeem(self, code, group_openid, member_openid):
        """群内兑换验证码。

        返回 (绑定记录 或 None, 给群里看的说明)。
        """
        if not self.enabled():
            return None, text.text("messages.access.feature_off")

        code = normalize_code(code)
        group_openid = str(group_openid or "")
        member_openid = str(member_openid or "")

        now = time.time()
        item = self._data["codes"].get(code)
        if not item:
            return None, text.text("messages.access.redeem_bad_code")
        if item.get("used"):
            return None, text.text("messages.access.redeem_used")
        if float(item.get("expire") or 0) < now:
            self._data["codes"].pop(code, None)
            self._save()
            return None, text.text("messages.access.redeem_expired")

        user_id = str(item.get("user_id") or "")
        # 用**发码时记下的频道**来校验，而不是"当前唯一配置的那个" ——
        # 多频道下这样才对得上：发给 A 频道的码只能用 A 频道配的群来兑换。
        guild_id = str(item.get("guild_id") or "")
        expected_list = self.group_openids(guild_id)
        if expected_list and group_openid not in expected_list:
            return None, text.text("messages.access.redeem_wrong_group")

        # 一对一约束（**按频道各算各的**）：同一个频道里，一个群账号只解锁一个
        # 频道账号，反过来也一样。挡住「拿自己的码给一堆频道小号刷权限」。
        # 跨频道不冲突：同一个人在两个频道里各解锁一次是正常需求。
        other = self.find_by_member(group_openid, member_openid, guild_id)
        if other and str(other.get("user_id")) != user_id:
            return None, text.text("messages.access.redeem_member_taken")
        bound = self.binding_of(user_id, guild_id)
        if bound and str(bound.get("member_openid")) != member_openid:
            return None, text.text("messages.access.redeem_user_taken")

        # 校验都过了，先把码消费掉（防止一码多用），再把绑定交给调用方去落地。
        # 注意这里**不写绑定表** —— 落地走 commit()，只有授权成功才写。
        # 否则会出现"绑上了但没权限"的死状态：用户既没有权限，
        # 又因为已有绑定而不能重新领码（issue 会回「你已经有权限了」）。
        self._data["codes"].pop(code, None)
        self._save()

        valid = self.valid_seconds(guild_id)
        binding = {
            "user_id": user_id,
            "guild_id": guild_id,
            "channel_id": self.channel_id(guild_id),
            "group_openid": group_openid,
            "member_openid": member_openid,
            "granted": now,
            "expire": (now + valid) if valid else 0,
        }
        _log.info(
            f"[botpy] 验证通过：群成员 {member_openid} -> 频道用户 {user_id}"
            f"（频道 {guild_id or '未指定'}，群 {group_openid}），待授权"
        )
        return binding, ""

    def commit(self, binding):
        """授权成功后把绑定写进绑定表。"""
        user_id = str((binding or {}).get("user_id") or "")
        if not user_id:
            return None
        guild_id = str((binding or {}).get("guild_id") or "")
        self._data["bindings"][self._key(guild_id, user_id)] = binding
        self._save()
        _log.info(f"[botpy] 已记录绑定：频道 {guild_id or '未指定'} 的用户 {user_id}")
        return binding

    # ------------------------------------------------------------------
    # 权限下发 / 回收
    # ------------------------------------------------------------------
    async def grant(self, binding):
        """给频道用户开「资料」子频道的可查看权限，返回 (是否成功, 提示)。"""
        channel_id = str(binding.get("channel_id") or self.channel_id())
        user_id = str(binding.get("user_id") or "")
        try:
            await self._client.api.update_channel_user_permissions(
                channel_id=channel_id,
                user_id=user_id,
                add=Permission(view_permission=True),
            )
        except Exception as error:
            # 局部变量不能叫 text —— 会盖住文案模块
            detail = str(error)
            _log.warning(f"[botpy] 授权失败 channel={channel_id} user={user_id}: {detail}")
            return False, self._api_hint(detail)
        _log.info(f"[botpy] 已授权频道用户 {user_id} 查看板块 {channel_id}")
        return True, ""

    async def revoke(self, user_id, channel_id=None):
        """收回频道用户的「资料」板块可查看权限，返回 (是否成功, 提示)。"""
        user_id = str(user_id or "")
        binding = self.binding_of(user_id) or {}
        channel_id = str(channel_id or binding.get("channel_id") or self.channel_id())
        if not channel_id or not user_id:
            return False, "缺少板块 ID 或用户 ID"
        try:
            await self._client.api.update_channel_user_permissions(
                channel_id=channel_id,
                user_id=user_id,
                remove=Permission(view_permission=True),
            )
        except Exception as error:
            detail = str(error)
            _log.warning(
                f"[botpy] 收回权限失败 channel={channel_id} user={user_id}: {detail}"
            )
            return False, self._api_hint(detail)
        _log.info(f"[botpy] 已收回频道用户 {user_id} 对板块 {channel_id} 的查看权限")
        return True, ""

    @staticmethod
    def _api_hint(detail):
        """把接口报错翻译成能照着做的提示。"""
        # 错误码是主判据；message 兜底（老版本 botpy 不会把 code 带进异常文案）
        if "301003" in detail or "put channel permission error" in detail:
            # 官方文档：「修改子频道用户权限」要求操作人有「管理子频道」权限，
            # 机器人还必须被设为管理员；而且该接口只对「指定成员可见」的
            # 子频道有意义（types/channel.py 里 PrivateType.SPECIFIED_USER 的
            # 注释原文就是"可使用 修改子频道权限接口 指定成员"）。
            return text.text("messages.access.api_301003")
        if "403" in detail or "Forbidden" in detail:
            return text.text("messages.access.api_403")
        if "404" in detail or "Not Found" in detail:
            return text.text("messages.access.api_404")
        if "429" in detail or "频率" in detail or "太频繁" in detail:
            return text.text("messages.access.api_429")
        return text.text("messages.access.api_other", error=detail)

    # ------------------------------------------------------------------
    # 事件：成员进 / 出
    # ------------------------------------------------------------------
    async def on_member_add(self, data):
        """新成员加入群。只记录，不发消息（避免打扰）。"""
        data = data or {}
        _log.info(
            f"[botpy] 群成员加入 group={data.get('group_openid')} "
            f"member={data.get('member_openid')}"
        )

    async def on_member_leave(self, data):
        """成员退群或被移出 -> 立刻收回他在频道里的板块权限。

        这是回收权限**最快**的一路（实时），但会漏：机器人掉线期间有人退群，
        事件就永远补不回来了。漏掉的那部分由 reconcile() 定期对账兜住。

        收回失败时保留绑定并标记，交给 sweep() 定时重试，不会静默丢掉。
        """
        if not self.enabled():
            return None
        data = data or {}
        member_openid = str(data.get("member_openid") or "")
        group_openid = str(data.get("group_openid") or "")
        if not member_openid:
            return None

        binding = self.find_by_member(group_openid, member_openid)
        if not binding:
            return None

        user_id = str(binding.get("user_id"))
        ok, hint = await self.revoke(user_id, binding.get("channel_id"))
        if ok:
            self._drop(binding)
            _log.info(f"[botpy] 群成员 {member_openid} 退群，已收回频道用户 {user_id} 的板块权限")
        else:
            binding["left_at"] = time.time()
            binding["revoke_failed"] = hint
            _log.warning(
                f"[botpy] 群成员 {member_openid} 退群，但收回 {user_id} 的权限失败：{hint}"
                f"（已保留记录，稍后自动重试）"
            )
        self._save()
        return binding

    async def sweep(self):
        """定时回收：对账 + 已退群未收回成功的 + 过了有效期的。"""
        if not self.enabled():
            return []

        removed = []
        # 对账失败（没权限 / 没拉全 / 某频道关了）只是少一路回收，
        # 不能影响后面的有效期回收，所以这里不提前 return
        found, hint = await self.reconcile()
        if hint:
            _log.info(f"[botpy] 成员对账：{hint}")
        removed += found

        now = time.time()
        for key, binding in list(self._data["bindings"].items()):
            expired = bool(binding.get("expire")) and float(binding["expire"]) < now
            left = bool(binding.get("left_at"))
            if not (expired or left):
                continue
            user_id = str(binding.get("user_id") or "")
            ok, hint = await self.revoke(user_id, binding.get("channel_id"))
            if ok:
                self._data["bindings"].pop(key, None)
                removed.append((user_id, "已退群" if left else "已过期"))
            else:
                binding["revoke_failed"] = hint
        if removed:
            self._save()
            _log.info(f"[botpy] 已回收 {len(removed)} 个板块授权：{removed}")
        return removed

    async def reconcile(self, guild_id=None, member_ids=None, known_guild_ids=()):
        """拿群成员名单对账，把「人已经不在群里」的授权收回。

        这是补「退群事件漏推」的那一路：机器人掉线期间有人退群，事件就永远
        补不回来了，只能靠主动拉名单比对。

        guild_id 为空时，遍历所有开着加群解锁的频道各对一遍
        （每个频道有自己的解锁群，成员名单不能混着用）。

        返回 ([(user_id, 原因), ...], 说明)。

        **安全红线**：只有完整拿到成员名单才会比对。拉取不完整（返回 None）
        就直接放弃这一轮 —— 残缺名单会被当成"这些人退群了"，把在群用户的
        权限误收回。
        """
        if not self.enabled():
            return [], text.text("messages.reconcile.feature_off")
        if self._registry is None:
            return [], text.text("messages.reconcile.no_registry")
        if not self._registry.available():
            return [], text.text("messages.reconcile.unavailable")

        if guild_id is None:
            targets = self.target_guilds(known_guild_ids)
            if not targets:
                if self._settings.guild_ids():
                    return [], text.text("messages.reconcile.no_guild_enabled")
                return [], text.text("messages.reconcile.no_guild_configured")
            removed, notes = [], []
            for item in targets:
                # 汇总里用频道名而不是冷冰冰的 guild_id
                label = self.label_of(item)
                if not self._cfg(item).get("access_reconcile", True):
                    notes.append(
                        text.text("messages.reconcile.guild_disabled", guild=label)
                    )
                    continue
                found, hint = await self.reconcile(item, known_guild_ids=known_guild_ids)
                removed += found
                notes.append(
                    text.text("messages.reconcile.guild_note", guild=label, hint=hint)
                )
            return removed, "；".join(notes)

        guild_id = str(guild_id or "")
        group_ids = self.group_openids(guild_id)
        if not group_ids:
            return [], text.text(
                "messages.reconcile.no_group_for_guild", guild=self.label_of(guild_id)
            )

        if member_ids is None:
            # 配了多个兑换群时，名单取**所有群的并集** —— 用户在哪个群兑换的
            # 绑定表里记着，只要他在其中任意一个群里就视为还在
            member_ids = set()
            for group_id in group_ids:
                members = await self._registry.fetch_members(group_id)
                if members is None:
                    return [], text.text("messages.reconcile.incomplete")
                member_ids |= {
                    str(item.get("member_openid") or "")
                    for item in members
                    if isinstance(item, dict) and item.get("member_openid")
                }
        member_ids = {str(item) for item in member_ids}

        removed = []
        for key, binding in list(self._data["bindings"].items()):
            # 按**绑定时的那个群**对账；不在配置的兑换群里的绑定不归这里管
            if str(binding.get("group_openid")) not in group_ids:
                continue
            member_openid = str(binding.get("member_openid") or "")
            if not member_openid or member_openid in member_ids:
                continue

            user_id = str(binding.get("user_id") or "")
            ok, hint = await self.revoke(user_id, binding.get("channel_id"))
            if ok:
                self._data["bindings"].pop(key, None)
                removed.append((user_id, text.text("messages.reconcile.reason_left")))
                _log.info(
                    f"[botpy] 对账（频道 {guild_id}）：群成员 {member_openid} 不在群名单里，"
                    f"已收回频道用户 {user_id} 的板块权限"
                )
            else:
                # 收回失败别丢记录，标上 left_at 交给 sweep 的重试分支
                binding["left_at"] = time.time()
                binding["revoke_failed"] = hint
                _log.warning(
                    f"[botpy] 对账（频道 {guild_id}）：{user_id} 已不在群，"
                    f"但收回权限失败：{hint}（已保留记录，稍后自动重试）"
                )

        if removed:
            self._save()
        return removed, text.text(
            "messages.reconcile.summary", count=len(member_ids), removed=len(removed)
        )

    async def sweep_loop(self):
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            try:
                await self.sweep()
            except Exception as error:
                _log.warning(f"[botpy] 授权回收任务异常: {error}")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def binding_of(self, user_id, guild_id=None):
        """某个人的绑定。

        给了 guild_id 就只看那个频道的（多频道下同一个人在两边可以各有一条）；
        没给则返回他的任意一条（兼容只关心"这个人绑没绑过"的旧调用）。
        """
        user_id = str(user_id or "")
        if guild_id is not None:
            return self._data["bindings"].get(self._key(guild_id, user_id))
        for binding in self._data["bindings"].values():
            if str(binding.get("user_id") or "") == user_id:
                return binding
        return None

    def _drop(self, binding):
        """按绑定记录本身删掉（主键是「频道::用户」，不能直接拿 user_id 删）。"""
        user_id = str((binding or {}).get("user_id") or "")
        guild_id = str((binding or {}).get("guild_id") or "")
        return self._data["bindings"].pop(self._key(guild_id, user_id), None)

    def forget(self, user_id, guild_id=None):
        """只删记录（权限已由调用方收回时用）。

        没给 guild_id 就把这个人的所有频道绑定都删掉（手动「撤销」时用）。
        """
        user_id = str(user_id or "")
        if guild_id is not None:
            removed = self._data["bindings"].pop(self._key(guild_id, user_id), None)
            if removed:
                self._save()
            return removed

        removed = None
        for key, binding in list(self._data["bindings"].items()):
            if str(binding.get("user_id") or "") == user_id:
                removed = self._data["bindings"].pop(key, None)
        if removed is not None:
            self._save()
        return removed

    def find_by_member(self, group_openid, member_openid, guild_id=None):
        member_openid = str(member_openid or "")
        group_openid = str(group_openid or "")
        for binding in self._data["bindings"].values():
            if str(binding.get("member_openid")) != member_openid:
                continue
            if guild_id is not None and str(binding.get("guild_id") or "") != str(guild_id):
                continue
            # 群对得上，或者对方事件里没给群（兼容）
            if not group_openid or str(binding.get("group_openid")) == group_openid:
                return binding
        return None

    def all_bindings(self):
        return sorted(
            self._data["bindings"].values(),
            key=lambda item: float(item.get("granted") or 0),
            reverse=True,
        )

    def __len__(self):
        return len(self._data["bindings"])

    def label_of(self, guild_id):
        """频道显示名：优先用频道配置里填的 label，否则退回 guild_id。"""
        label = str(self._settings.profile(guild_id).get("label") or "").strip()
        return label or str(guild_id or "") or text.text("labels.fallback_guild")

    def _expire_label(self, expire):
        """有效期的显示文本：有到期时间就格式化，没有就是"长期有效"。

        status_text 和 describe_lines 都要这句，抽出来免得两处各写一遍。
        """
        if not expire:
            return text.text("labels.long_term")
        return text.text("messages.access.expire_at", time=_format_ts(expire))

    def status_text(self, user_id, guild_id=None, channel_name=""):
        """用户在频道里发「我的资料」时的回复。"""
        binding = self.binding_of(user_id, guild_id)
        if not binding:
            return text.text(
                "messages.access.status_none",
                group=self.group_label(guild_id),
                channel=channel_name or text.text("labels.default_channel"),
            )
        lines = [
            text.text(
                "messages.access.status_ok_header",
                channel=channel_name or text.text("labels.default_channel"),
            ),
            text.text(
                "messages.access.status_granted",
                time=_format_ts(binding.get("granted")),
            ),
            text.text(
                "messages.access.status_expire",
                value=self._expire_label(binding.get("expire")),
            ),
        ]
        if binding.get("left_at"):
            lines.append(text.text("messages.access.status_left"))
        return "\n".join(lines)

    def describe(self):
        """给管理指令 / 启动日志看的一句话概况（多频道时分频道列出）。"""
        ids = self._settings.guild_ids()
        if not ids:
            state = "开" if self.enabled() else "关"
            return (
                f"加群解锁：{state}；板块 {self.channel_id() or '未配置'}；"
                f"群 {self.group_openid() or '未配置'}；已授权 {len(self)} 人"
            )

        parts = []
        for guild_id in ids:
            if not self.enabled(guild_id):
                parts.append(f"{self.label_of(guild_id)}：关")
                continue
            parts.append(
                f"{self.label_of(guild_id)}：板块 {self.channel_id(guild_id) or '未配置'}，"
                f"群 {self.group_openid(guild_id) or '未配置'}"
            )
        return (
            f"加群解锁（{len(ids)} 个频道）：" + "；".join(parts)
            + f"；已授权 {len(self)} 人"
        )

    def describe_lines(self, limit=50):
        """「绑定列表」指令的输出（多频道下带上频道名）。"""
        bindings = self.all_bindings()
        if not bindings:
            return [text.text("messages.group.bindings_empty")]
        show_guild = bool(self._settings.guild_ids())
        lines = []
        for item in bindings[:limit]:
            prefix = f"{self.label_of(item.get('guild_id'))}　" if show_guild else ""
            lines.append(
                text.text(
                    "messages.group.binding_line",
                    prefix=prefix,
                    user=str(item.get("user_id"))[:10],
                    granted=_format_ts(item.get("granted")),
                    expire=self._expire_label(item.get("expire")),
                    flag=(
                        text.text("messages.group.binding_left")
                        if item.get("left_at")
                        else ""
                    ),
                )
            )
        if len(bindings) > len(lines):
            lines.append(
                text.text(
                    "messages.group.bindings_more", count=len(bindings) - len(lines)
                )
            )
        return lines

    def intro(self, guild_id=None):
        """给用户看的操作引导（频道里发「领资料」时用）。"""
        return text.text("messages.access.intro", group=self.group_label(guild_id))
