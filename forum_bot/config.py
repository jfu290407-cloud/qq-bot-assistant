# -*- coding: utf-8 -*-
"""路径常量与 config.yaml 读取。

**数据文件放在仓库根目录** —— 也就是本包的上一级（见下面的 BASE_DIR）：
    config.yaml                           机器人配置（appid / secret / Web 面板）
    forum_subscribers.json                订阅群
    forum_forward_settings.json           转发/撤回设置 + 管理员名单（配置页编辑的就是它）
    forum_message_index.json              msg_idx <-> message_id 记录
    forum_blacklist.json                  拉黑名单的来源记录（谁/何时/在哪个群拉的）
    forum_seen_members.json               见过的群成员（配置页据此列出可授权的 openid）
    forum_access.json                     「加群解锁板块」的验证码与授权绑定表
    forum_groups.json                     群信息 + 群成员快照（白名单接口，可能为空）
    forum_group_notes.json                群备注（面板上填的群号，平台接口不返回群号）
    forum_known_groups.json               见过的群（平台没有"机器人在哪些群"的接口）
    forum_inventory.json                  机器人所在频道 / 子频道缓存（配置页「频道」用）
    forum_relay.json                      群消息互转的每日配额账本
    forum_ai_suggest.json                 AI 拆分出的**候选**拉黑关键词（待配置页挑选）
    forum_feed_links.json                 帖子短链缓存（thread_id -> pd.qq.com/s/xxx）

这些都在 .gitignore 里，不随代码走：每台机器一份，`git pull` 不会互相覆盖。
本包从 examples/ 提到根目录时，这里"上一级"的含义自动跟着变，代码无需改动。
"""
import os

from botpy.ext.cog_yaml import read

# 仓库根目录（本包的上一级）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_FILE = os.path.join(BASE_DIR, "config.yaml")
SUBSCRIBER_FILE = os.path.join(BASE_DIR, "forum_subscribers.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "forum_forward_settings.json")
INDEX_FILE = os.path.join(BASE_DIR, "forum_message_index.json")
BLACKLIST_FILE = os.path.join(BASE_DIR, "forum_blacklist.json")
# **成员**黑名单（按 member_openid）。和上面那个内容黑名单是两件事：
# 内容黑名单管"这条内容不许再说"，成员黑名单管"这个人说的话都撤"。
MEMBER_BLACKLIST_FILE = os.path.join(BASE_DIR, "forum_member_blacklist.json")
MEMBER_FILE = os.path.join(BASE_DIR, "forum_seen_members.json")
ACCESS_FILE = os.path.join(BASE_DIR, "forum_access.json")
GROUP_FILE = os.path.join(BASE_DIR, "forum_groups.json")
GROUP_NOTE_FILE = os.path.join(BASE_DIR, "forum_group_notes.json")
# 群成员自助绑定的 QQ 号。**平台不提供 QQ 号**，只能让成员自己在群里说
# （见 bindings 模块开头那段说明）
MEMBER_QQ_FILE = os.path.join(BASE_DIR, "forum_member_qq.json")
KNOWN_GROUP_FILE = os.path.join(BASE_DIR, "forum_known_groups.json")
INVENTORY_FILE = os.path.join(BASE_DIR, "forum_inventory.json")
RELAY_FILE = os.path.join(BASE_DIR, "forum_relay.json")
# AI 拆分出的候选拉黑关键词（见 ai_split 模块说明）。候选不是拉黑词 ——
# 必须管理员在配置页挑过才进 forum_forward_settings.json 的名单
AI_SUGGEST_FILE = os.path.join(BASE_DIR, "forum_ai_suggest.json")
# 帖子短链缓存。**缓存不只是省调用** —— CLI 没装/限流/凭证过期时，
# 缓存过的帖子还能继续用短链（见 channel_cli 模块开头）。
CLI_LINK_FILE = os.path.join(BASE_DIR, "forum_feed_links.json")

# 最高管理员 QQ 号默认为空 —— 公开发布的版本不预置任何人的 QQ，
# 需要的自己在 config.yaml 的 super_admin_qq 里填
DEFAULT_SUPER_ADMIN_QQ = ""

# 帖子链接默认模板，可用占位符：{channel_number} {guild_id} {channel_id} {thread_id}
DEFAULT_LINK_TEMPLATE = "https://pd.qq.com/g/{channel_number}/post/{thread_id}"

_config = None


def load_config():
    """读取 config.yaml，只读一次并缓存。"""
    global _config
    if _config is None:
        try:
            _config = read(CONFIG_FILE) or {}
        except Exception:
            _config = {}
    return _config


def get_appid():
    return str(load_config().get("appid") or "")


def get_secret():
    return str(load_config().get("secret") or "")


def get_group_openids():
    """config.yaml 里写死的转发目标群。"""
    return [str(item) for item in (load_config().get("group_openids") or [])]


def get_channel_number():
    """频道号：网页版 pd.qq.com/g/ 后面要跟的那串。

    注意不是事件里的 guild_id —— 实测把 guild_id 放进去会退回站点首页。
    这里留空则运行时自动从频道头像 URL 里解析。
    """
    return str(load_config().get("channel_number") or "").strip()


def get_link_template():
    return str(load_config().get("link_template") or "").strip() or DEFAULT_LINK_TEMPLATE


def get_cli_home():
    """tencent-channel-cli 的 HOME（config.yaml 的 cli_home，可留空）。

    **一般不用填**，机器人会自动推断（见 channel_cli._child_env）。
    但有两种情况必须手动指明，否则症状很难查：

      1. 服务由 systemd 起、unit 里没写 `User=` -> 进程环境里没有 HOME，
         而这个 CLI 没 HOME 会直接报「无法确定 state 文件路径（HOME 不可用）」；
      2. 服务运行用户和**手动扫码登录时用的用户**不是同一个 ->
         凭证不在同一个 `~/.qqcli/` 底下，会表现成"登录过了却说没登录"。
    """
    return str(load_config().get("cli_home") or "").strip()


def get_super_admin_qq():
    """最高管理员 QQ 号（config.yaml 的 super_admin_qq，默认 123456789）。

    说明：QQ 群消息下发里**没有 QQ 号**，只有 member_openid / union_openid
    和 username，所以这个值没法直接用来校验身份，只做两件事：
      1. 启动时打印提示，告诉你把哪个 openid 填进管理员名单；
      2. 配置页上展示「最高管理员是谁」。
    真正的授权名单是配置页的 admin_openids（按 member_openid）。
    """
    value = str(load_config().get("super_admin_qq") or "").strip()
    return value or DEFAULT_SUPER_ADMIN_QQ


def get_web_config():
    """Web 配置界面。

    enabled / host / port    监听设置（默认只监听本机，最省事也最安全）
    username / password      访问口令（HTTP Basic）。绑非本机地址时**必须**设
    cert / key               证书路径；配了就走 HTTPS（自签的也行）
    """
    web = load_config().get("web") or {}
    return {
        "enabled": bool(web.get("enabled", True)),
        "host": str(web.get("host") or "127.0.0.1"),
        "port": int(web.get("port") or 8765),
        "username": str(web.get("username") or "").strip(),
        "password": str(web.get("password") or ""),
        "cert": str(web.get("cert") or "").strip(),
        "key": str(web.get("key") or "").strip(),
    }
