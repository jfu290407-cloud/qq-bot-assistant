# -*- coding: utf-8 -*-
"""机器人管理员判定：群内「谁能用管理指令」。

为什么不能直接用 QQ 号
--------------------
QQ 群消息下发里**没有 QQ 号**，只有：

    "author": {
      "bot": false,
      "id": "476D2D3DF90A6E51614842420EDF775B",
      "member_openid": "476D2D3DF90A6E51614842420EDF775B",
      "member_role": "owner",          <- 群主 / admin / member
      "union_openid": "476D2D3DF90A6E51614842420EDF775B",
      "username": "张三"
    }

openid 还是**按机器人隔离**的（换一个机器人，同一个人的 openid 就变了），
所以「张三的 QQ 号是 123456789」这件事在机器人侧根本无从校验 ——
唯一能可靠比对的就是 member_openid。

三种放行条件（任一命中即为管理员）
--------------------------------
1. openid 在配置页的「管理员 openid 名单」里（settings.admin_openids）；
2. 群内身份是群主 / 群管理员（member_role = owner / admin），
   由 admin_allow_group_owner 控制是否启用，默认开启；
3. 都不是 -> 普通成员，只能用只读指令。

超级管理员 QQ（config.yaml 的 super_admin_qq，默认 123456789）不参与校验，
只用于提示：告诉你去配置页把哪个 openid 加进名单。
配置页会把「最近见过的群成员」列出来，点一下就能加，不用翻日志。
"""
from . import text
from .settings import split_lines

__all__ = [
    "AdminGuard",
    "role_label",
    "member_openid_of",
    "member_name_of",
    "member_role_of",
]

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"


def role_label(role, fallback=None):
    """群内角色的显示名（文案在 text.yaml 的 labels.group_role 下）。

    认不出来的角色退回"普通成员" —— 平台以后加了新 role 也不会显示成空白。
    """
    value = text.get("labels.group_role.%s" % role) if role else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    if fallback:
        return fallback
    return text.text("labels.group_role.%s" % ROLE_MEMBER)


def __getattr__(name):
    """兼容旧的 ROLE_LABELS 常量（现在按需从文案资源生成）。"""
    if name == "ROLE_LABELS":
        return {
            ROLE_OWNER: role_label(ROLE_OWNER),
            ROLE_ADMIN: role_label(ROLE_ADMIN),
            ROLE_MEMBER: role_label(ROLE_MEMBER),
        }
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _author_of(message):
    raw = getattr(message, "raw", None) or {}
    author = raw.get("author")
    return author if isinstance(author, dict) else {}


def member_openid_of(message):
    """这条消息是谁发的（openid）。取不到返回 None。"""
    author = _author_of(message)
    openid = author.get("member_openid") or author.get("union_openid") or author.get("id")
    if openid:
        return str(openid)
    return getattr(getattr(message, "author", None), "member_openid", None)


def member_name_of(message):
    """发送者昵称（仅用于展示）。"""
    return str(_author_of(message).get("username") or "")


def member_role_of(message):
    """发送者的群内身份：owner / admin / member，取不到返回空串。"""
    return str(_author_of(message).get("member_role") or "")


def is_bot_message(message):
    """这条消息是不是机器人自己发的（避免机器人撤自己/给自己授权）。"""
    return bool(_author_of(message).get("bot"))


class AdminGuard:
    """判断某条群消息的发送者有没有管理权限。"""

    def __init__(self, settings):
        self._settings = settings

    def admin_openids(self):
        return split_lines(self._settings.snapshot().get("admin_openids"))

    def group_owner_allowed(self):
        return bool(self._settings.snapshot().get("admin_allow_group_owner", True))

    def check(self, message):
        """返回 (是否管理员, 身份说明)。

        身份说明用于回给用户看的提示，例如「管理员名单」「群主（张三）」。
        """
        if is_bot_message(message):
            return False, "机器人自己"

        openid = member_openid_of(message)
        if openid and openid in self.admin_openids():
            return True, "管理员名单"

        role = member_role_of(message)
        if self.group_owner_allowed() and role in (ROLE_OWNER, ROLE_ADMIN):
            return True, self._label(message, role)

        return False, self._label(message, role)

    def _label(self, message, role):
        name = member_name_of(message)
        label = role_label(role)
        return f"{label}（{name}）" if name else label

    def describe_sources(self):
        """给配置页/日志用的一句话说明。"""
        count = len(self.admin_openids())
        parts = [f"管理员名单 {count} 人"]
        parts.append("群主/群管理员自动放行：开" if self.group_owner_allowed() else "群主/群管理员自动放行：关")
        return "；".join(parts)
