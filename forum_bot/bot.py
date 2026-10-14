# -*- coding: utf-8 -*-
"""机器人主体：把各个功能模块组装起来，接事件、分发群指令。

这个文件只负责「串联」，具体能力都在各自的模块里：
    settings / config_web      设置与配置页
    admins / members           管理员判定 + 见过的群成员名册
    access                     「加群解锁板块」验证码与权限下发
    group_info                 群信息 / 群成员查询（平台白名单接口）
    channel_forum              帖子反查 + 链接
    forwarder                  转发策略
    recall / blacklist         撤回 + 拉黑
    message_index / group_message / subscribers  支撑数据
"""
import asyncio
import collections
import os
import signal
import time

import botpy
from botpy import logging
from botpy.types.channel import ChannelType

from . import config, logbuf, onebot, text
from .access import AccessGuard, looks_like_code
from .admins import AdminGuard, is_bot_message, member_name_of, member_openid_of
from .bindings import QQBindings
from .channel_cli import ChannelCli
from .channel_forum import ChannelForum, is_recent
from .channel_message import extract_content, mentions_bot, strip_inline, unescape_inline
from .config_web import start_settings_web
from .dashboard import Dashboard
from .forwarder import Forwarder
from .group_info import INFO_CACHE_SECONDS, GroupDirectory
from .group_message import clean_content, describe, mentioned_bot, quoted_index_of
from .groups import KnownGroups
from .inventory import (
    Inventory,
    as_int,
    channel_type_label,
    is_specified_user,
    ordered_channels,
    private_type_label,
)
from .members import SeenMembers
from .message_index import MessageIndex
from .recall import RecallGuard
from .relay import RelayGuard
from .settings import Settings, split_lines
from .subscribers import Subscribers

__all__ = [
    "ForumBot",
    "is_command",
    "requires_admin",
    "resolve_command",
    "command_words",
    "main",
]

_log = logging.get_logger()

# ----------------------------------------------------------------------
# 指令表
# ----------------------------------------------------------------------
# 指令**叫什么**由 text.yaml 的 commands 段决定（改文案不用动代码）；
# 这里只定义「有哪些指令、谁能用、哪些能带参数」。
#
# 用「按代数缓存」而不是模块级常量：text.reload() 会把代数 +1，
# 下次取表时自动重建 —— 所以改完 text.yaml 发一句「刷新设置」就生效，不用重启。
#
# 两个列表里的名字是**逻辑名**（跟 text.yaml 的 key 一一对应），不是用户看到的词。
_ANY_USER = ("status", "mode")

# 只有机器人管理员能用：它们都会改动机器人行为。
# 「状态 / 模式」是只读查询，所以放在 _ANY_USER 里，任何人可用。
_ADMIN_ONLY = (
    "subscribe",
    "unsubscribe",
    "flush",
    "reload_settings",
    "recall",
    "recall_last",
    "blacklist_add",
    "blacklist_remove",
    "blacklist_show",
    # 成员黑名单：按人（不是按内容）封，他在任何群说话都被撤。
    # **它不是踢人** —— 平台没有移除群成员的接口，详见 blacklist.MemberBlacklist
    "blacklist_member",
    "unblacklist_member",
    "blacklist_members",
    "bindings",
    "revoke",
    "group_info",
    "group_info_refresh",
    "group_members",
    "reconcile",
    "relay_status",
)

# 允许「指令 + 空格 + 参数」的形式（多频道下订阅要指明频道）
_WITH_ARGUMENT = (
    "recall",
    "blacklist_add",
    "blacklist_remove",
    # 可以带 openid 或昵称；不带参数就用**被引用那条的发送者**
    "blacklist_member",
    "unblacklist_member",
    "revoke",
    "subscribe",
    "unsubscribe",
)

# 帖子超过这个秒数就不当领资料请求（见 _maybe_issue_from_post 的说明）
POST_FRESH_SECONDS = 300


def _message_id_of(result):
    """从 post_group_message 的返回里取消息 id。

    返回的可能是 dict、也可能是对象 —— 两种形态都认
    （relay 模块的 _pick 是为同一件事存在的）。
    取不到就返回空串，调用方据此决定不发撤回任务。
    """
    if isinstance(result, dict):
        return str(result.get("id") or "")
    return str(getattr(result, "id", "") or "")

_TABLE_CACHE = {"generation": None}


# 领资料相关指令词可以在配置页改（settings 键 -> 逻辑名）。指令表是
# 全局一份的，所以这些键只做全局（见 settings._GLOBAL_ONLY_KEYS）
_CUSTOM_WORD_KEYS = {
    "access_grant": "access_cmd_grant",
    "access_status": "access_cmd_status",
}

_SETTINGS_HOLDER = {}


def _custom_access_words(name):
    """配置页里给「领资料 / 我的资料」自定义的指令词；没配返回 ()。

    settings.snapshot() 每次现读，所以配置页改完立即生效，不用重启。
    """
    settings = _SETTINGS_HOLDER.get("settings")
    if settings is None:
        return ()
    key = _CUSTOM_WORD_KEYS.get(name)
    if not key:
        return ()
    raw = str(settings.snapshot().get(key) or "")
    return tuple(line.strip() for line in raw.splitlines() if line.strip())


def _build_tables():
    """把逻辑名翻译成实际指令词，并生成反查表。

    一个指令可以有多套叫法：text.yaml 的 commands.X 写成列表就行
    （比如拉黑/撤回的别名）。但 `text.word()` 只取列表第一项，
    所以反查表必须按 `text.words()` 把**全部**叫法都登记进去 ——
    否则写在列表里的别名会被静默忽略，用户换了写法就说"机器人没反应"。

    （「领资料 / 我的资料」不走这张表 —— 它们只在频道/私信里用
    access_commands() / access_status_commands() 精确匹配，配置页的
    自定义词在那两个函数里生效。）
    """
    words = {}
    aliases = {}
    for name in _ANY_USER + _ADMIN_ONLY:
        value = text.word("commands." + name)
        if value:
            words[name] = value
        aliases[name] = text.words("commands." + name) or ((value,) if value else ())

    reverse = {}
    for name in _ANY_USER + _ADMIN_ONLY:
        for value in aliases.get(name) or ():
            other = reverse.get(value)
            if other == name:
                continue          # 同一个指令里把别名写重了，跳过就好
            if other is not None:
                # 同一个词被两个指令占用 -> 后者会被忽略，用户会莫名进错分支。
                # 这种配置错误本身不会报错，所以点名说出来。
                _log.warning(
                    f"[botpy] 指令词冲突：{other} 和 {name} 都叫「{value}」，"
                    f"{name} 会被忽略 —— 请改 text.yaml 的 commands 段"
                )
                continue
            reverse[value] = name

    return {
        "generation": text.generation(),
        "words": words,
        "aliases": aliases,
        "reverse": reverse,
        "commands": tuple(reverse),
        "admin_commands": tuple(
            value for value, name in reverse.items() if name in _ADMIN_ONLY
        ),
    }


def _tables():
    """取指令表（text.yaml 变过就重建）。"""
    if _TABLE_CACHE.get("generation") != text.generation():
        _TABLE_CACHE.clear()
        _TABLE_CACHE.update(_build_tables())
    return _TABLE_CACHE


def __getattr__(name):
    """兼容旧的模块级常量写法（GROUP_COMMANDS / ADMIN_COMMANDS）。

    它们原来是写死的元组，现在指令词在 text.yaml 里，改成按需生成。
    留着这个是为了让 `from .bot import GROUP_COMMANDS` 之类的旧代码不炸。
    """
    if name == "GROUP_COMMANDS":
        return _tables()["commands"]
    if name == "ADMIN_COMMANDS":
        return _tables()["admin_commands"]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def command_words():
    """逻辑名 -> 指令词。帮助文案、配置页要列出指令时用它。"""
    return dict(_tables()["words"])


def access_commands():
    """「领资料」的各种叫法（频道消息、私信里精确匹配这一组）。

    配置页「加群解锁」页配了自定义词就用配置的，没配回落 text.yaml。
    """
    return _custom_access_words("access_grant") or text.words("commands.access_grant")


def access_status_commands():
    """「我的资料」的各种叫法。配置页可改，见 access_commands。"""
    return _custom_access_words("access_status") or text.words("commands.access_status")


def access_post_hints():
    """帖子正文里出现这些就当成领资料请求。

    注意 text.yaml 里**故意没有「资料」这个泛词** —— 这本来就是个「资料」板块，
    正文里出现"资料"太常见，会被大量误判。
    """
    return text.words("commands.access_post_hints")


def resolve_command(command):
    """把用户发的一条指令解析成 (逻辑名, 参数)；认不出来返回 (None, "")。

    多频道下「订阅 1234567890123456789」这类「指令 + 空格 + 参数」
    也在这里归一化成 ("subscribe", "1234567890123456789")，
    分发逻辑就不用再逐条写 startswith 了。
    """
    tables = _tables()
    name = tables["reverse"].get(command)
    if name is not None:
        return name, ""

    for candidate in _WITH_ARGUMENT:
        word = tables["words"].get(candidate)
        if word and command.startswith(word + " "):
            return candidate, command[len(word) + 1 :].strip()
    return None, ""


def is_command(command):
    """是不是一条已知指令（带不带参数都算）。"""
    return resolve_command(command)[0] is not None


def requires_admin(command):
    """这条指令是否只有管理员能用。

    只读查询（状态 / 模式）任何人都能用；其余都会改动机器人行为，必须限权。
    """
    name = resolve_command(command)[0]
    if name is None:
        return False
    return name in _ADMIN_ONLY


class ForumBot(botpy.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.subscribers = Subscribers(seed=config.get_group_openids())
        self.settings = Settings(config.SETTINGS_FILE)
        # 模块级的 access_commands() / 指令表构建需要读设置（配置页可改
        # 「领资料」指令词），在这里把实例登记进去
        _SETTINGS_HOLDER["settings"] = self.settings
        # 板块显示名的缓存（channel_id -> (名字, 取的时间)），见 _channel_label
        self._channel_labels = {}
        self.index = MessageIndex()
        self.members = SeenMembers()
        # 群成员**自助绑定**的 QQ 号。平台不给 QQ 号，只能让成员自己说；
        # 它只是显示用的标签，**不能**拿来做权限判定（详见 bindings 模块开头）
        self.bindings = QQBindings(config.MEMBER_QQ_FILE)
        self.groups = KnownGroups()
        self.inventory = Inventory()
        # config.yaml 里写死的转发目标群、以及已订阅的群，都算「已知的群」
        # （平台的群事件以外的信息拿不到，这两处是启动时就确定机器人确实在里面的）
        for group_openid in config.get_group_openids():
            self.groups.remember(group_openid, source="config.yaml 配置")
        for group_openid in self.subscribers.all():
            self.groups.remember(group_openid, source="已订阅")

        self.admins = AdminGuard(self.settings)
        # 群信息目录（平台白名单接口）。access 拿它做「成员对账」，
        # 拿不到权限时它会自己降级，不影响别的功能。
        self.group_info = GroupDirectory(self.api)
        self.access = AccessGuard(self, self.settings, registry=self.group_info)
        # 帖子短链的桥：走官方 CLI（**用户身份**，不是机器人身份），
        # 拿不到就退回长链模板。细节见 channel_cli.py 的模块说明。
        #
        # enabled 传的是回调而不是布尔值 —— 用户在配置页关掉开关要**立刻生效**，
        # 不能等下次重启。回调每次调用时才去读设置。
        self.channel_cli = ChannelCli(
            enabled=lambda: bool(self.settings.snapshot().get("cli_enabled", True)),
            # home 一般留空（自动推断）。但 systemd 起的服务环境里可能没有 HOME，
            # 而 CLI 没 HOME 会直接报「state 文件路径」错误 —— 详见 _child_env
            home=config.get_cli_home(),
        )
        # 帖子链接里的「频道号」必须按频道取 —— 多频道下用错就跳到别的频道去了
        self.forum = ChannelForum(
            self.api, number_lookup=self._channel_number_of, cli=self.channel_cli
        )
        self.forwarder = Forwarder(self, self.settings, self.subscribers, self.index)
        self.recall = RecallGuard(self, self.settings, self.index)
        # 手工在配置页删过的拉黑内容，顺手清掉本地残留记录
        self.recall.reset_blacklist_cache()
        # OneBot 实时识别的匹配器（消息流缓冲 + 内容配对）
        self._onebot_matcher = onebot.LiveMatcher()


        # 群消息互转：把一个群的消息转发到另一个群。
        # 前提是开放平台的「接收所有消息」全量模式，否则只看得到 @机器人 的消息。
        self.relay = RelayGuard(
            self, self.settings, self.members, self.index, bindings=self.bindings
        )

        # 配置页「概况 / 群聊 / 频道」三页的数据来源（只读聚合）
        self.dashboard = Dashboard(
            self.settings, self.inventory, self.members, self.groups, self.subscribers,
            self.admins, group_info=self.group_info,
        )

        # 去重：@事件和全量事件可能推同一条群消息
        self._handled_messages = collections.deque(maxlen=200)

        # 日志留一份在内存里，配置页的「运行日志」页才看得到。
        # 必须放在 _start_web() **之前** —— 否则配置页启动那几行日志会漏掉，
        # 而那几行恰好是排查"面板为什么没起来"时最需要的。
        # （super().__init__() 已经调过 configure_logging，那时 logger 就建好了，
        #   所以这里挂上去就能收到后续所有日志。）
        logbuf.install()

        self.web_url = self._start_web()

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------
    def _start_web(self):
        web = config.get_web_config()
        if not web["enabled"]:
            return ""
        try:
            start_settings_web(
                self.settings,
                host=web["host"],
                port=web["port"],
                members=self.members,
                inventory=self.inventory,
                dashboard=self.dashboard,
                group_info=self.group_info,
                groups=self.groups,
                # 「CLI 登录」页要在网页线程里直接调它，所以把桥传进去
                channel_cli=self.channel_cli,
                username=web["username"],
                password=web["password"],
                cert=web["cert"],
                key=web["key"],
            )
            scheme = "https" if (web["cert"] and web["key"]) else "http"
            url = f"{scheme}://{web['host']}:{web['port']}"
            if web["password"]:
                url += f"（需登录，用户名 {web['username'] or 'admin'}）"
            _log.info(f"[botpy] 配置页面已启动: {url}")
            return url
        except Exception as error:
            _log.error(f"[botpy] 配置页面启动失败（{web['host']}:{web['port']}）: {error}")
            return ""

    def _report_text_source(self):
        """报告文案来源。

        为什么要专门打这一句：文案是多层兜底的，**key 写错了不会报错、只是不生效**。
        不点名的话，你改完发现界面没变会完全摸不着头脑。
        """
        _log.info(
            f"[botpy] 文案来源: {text.source()}（改完发「刷新设置」即可生效，不用重启）"
        )
        unknown = text.unknown_keys()
        if unknown:
            shown = "、".join(unknown[:8]) + ("……" if len(unknown) > 8 else "")
            _log.warning(
                f"[botpy] text.yaml 里有 {len(unknown)} 个未知的 key（多半是拼错了，"
                f"这些不会生效）：{shown}"
            )

    async def on_ready(self):
        _log.info(f"robot 「{self.robot.name}」 on_ready!")
        _log.info(f"[botpy] 当前转发目标群: {sorted(self.subscribers.all())}")
        _log.info(f"[botpy] 当前推送策略: {self.settings.describe()}")
        _log.info(f"[botpy] 权限: {self.admins.describe_sources()}")
        self._report_text_source()
        _log.info(f"[botpy] {self.relay.describe()}")

        # 短链桥自检。**放在启动时做**，是为了让「登录凭证过期」这类问题
        # 在启动日志里就露出来 —— 否则现象是"转发出来的链接某天突然变长了"，
        # 很难联想到要去重新扫码。
        await self.channel_cli.probe()
        _log.info(f"[botpy] {self.channel_cli.describe()}")
        super_admin = config.get_super_admin_qq()
        if super_admin and super_admin not in self.admins.admin_openids():
            _log.info(
                f"[botpy] 最高管理员 QQ {super_admin} 还没绑定：QQ群消息不携带 QQ 号，"
                f"让它在本群发一条消息，然后到配置页「权限管理」里把它的 member_openid 加进管理员名单"
            )
        if self.access.enabled():
            _log.info(f"[botpy] {self.access.describe()}")
            # 多频道下"资料板块 / 解锁群"是每个频道各配一套，所以要逐个自查
            for guild_id in self.access.target_guilds(self._known_guild_ids()):
                missing = self.access.missing_config(guild_id)
                if missing:
                    _log.warning(
                        f"[botpy] 加群解锁（{self._guild_label(guild_id)}）还差这些配置："
                        f"{'、'.join(missing)}"
                    )
            self.loop.create_task(self.access.sweep_loop())
        # 群信息（白名单接口）：顺手查一次，配置页「群聊」页就有群名和人数了。
        # 拿不到权限会打印一次明确提示，之后不再重试。
        self.group_info.set_bot_name(getattr(self.robot, "name", "") or "")
        await self._refresh_group_info(quiet=True)
        # 配置页上的「同步成员」「保存备注」是网页线程发过来的请求，
        # 异步接口只能在事件循环里调，所以这里起个循环去消费。
        self.loop.create_task(self.group_info.serve_loop(self.groups.all))
        if self.web_url:
            _log.info(f"[botpy] 配置页面: {self.web_url}")

        # 配置页「概况」要的机器人元信息
        self.inventory.set_meta(
            bot_name=getattr(self.robot, "name", "") or "",
            appid=config.get_appid(),
            started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            started_ts=time.time(),
            ready=True,
        )

        # 开机同步一次位置信息；之后每 15 秒看一次有没有人来要同步（配置页「同步频道」）
        await self._print_inventory()
        self._sync_guild_profiles()
        self._check_access_readiness()
        self.loop.create_task(self.inventory.sync_loop(self.api))
        self.loop.create_task(self.forwarder.digest_loop())
        # batch 模式的等待、免打扰结束都靠这个 tick（每频道各算各的截止时间）
        self.loop.create_task(self.forwarder.batch_loop())
        # OneBot 旁路的定时自动匹配（onebot_auto 开了才真的干活）
        self.loop.create_task(self._onebot_auto_loop())

    async def _onebot_live_resolve(self, message):
        """OneBot 实时识别：本条消息到达后，拉一次 NapCat 消息历史并配对。

        为什么必须**在这里拉**：历史里要有"当前这条消息"，只能在它到达之后
        拉取 —— 任何提前量（轮询/缓冲）都注定配不上当前消息，这是
        上一版失败的根本原因。配对成功后立刻写绑定表，紧接着的
        relay 就能用上真号。

        超时防护：拉取包在 wait_for(6 秒) 里，NapCat 慢/挂时本条消息
        放弃识别照常转发（批量匹配兜底），流水线最多慢 6 秒且仅此一条。
        同群消息用群锁串行，防止并发重复拉。
        """
        data = self.settings.snapshot()
        if not data.get("onebot_enabled") or not data.get("onebot_live"):
            return
        openid = member_openid_of(message)
        if not openid or is_bot_message(message):
            return
        group_openid = str(getattr(message, "group_openid", None) or "")
        if not group_openid:
            return

        # 这个群配了映射吗
        qq_group_id = ""
        mapped, _map_issues = onebot.parse_group_map(
            data.get("onebot_group_map")
        )
        for mapped_qq, mapped_oid in mapped:
            if mapped_oid == group_openid:
                qq_group_id = mapped_qq
                break
        if not qq_group_id:
            _log.info(
                f"[botpy] OneBot 实时识别：群 {group_openid[:10]}… 没配映射，跳过"
            )
            return

        content = onebot.normalize_content(clean_content(message.content))
        if not content:
            _log.info("[botpy] OneBot 实时识别：纯图/纯表情消息，跳过")
            return

        started = time.monotonic()
        try:
            fresh, fetch_error = await asyncio.wait_for(
                asyncio.to_thread(
                    self._onebot_matcher.refresh,
                    group_openid,
                    str(data.get("onebot_http_url") or ""),
                    str(data.get("onebot_token") or ""),
                    qq_group_id,
                ),
                timeout=6,
            )
        except asyncio.TimeoutError:
            _log.warning("[botpy] OneBot 实时识别：拉取历史超时（6 秒），本条放弃识别")
            return
        except Exception as error:
            _log.warning(f"[botpy] OneBot 实时识别：拉取历史失败: {error}")
            return
        _log.info(
            f"[botpy] OneBot 实时识别：历史拉取 {time.monotonic() - started:.1f}s，"
            f"新入缓冲 {fresh} 条"
            + (f"，警告：{fetch_error}" if fetch_error else "")
        )

        qq, _found = self._onebot_matcher.resolve(
            group_openid, content, time.time()
        )
        if not qq:
            _log.info("[botpy] OneBot 实时识别：本条未配对上（正文不在缓冲或已被消费）")
            return

        state, record = self.bindings.bind(
            openid,
            qq,
            nick=member_name_of(message),
            group_openid=group_openid,
            source="onebot",
        )
        if state in ("ok", "update"):
            _log.info(
                f"[botpy] OneBot 实时识别：{record.get('nick') or openid[:8]} -> "
                f"QQ {record.get('qq')}（{state}）"
            )

    async def _onebot_auto_loop(self):
        """OneBot 旁路的定时自动匹配（onebot_auto 开了才真的干活）。

        HTTP 拉成员是同步阻塞调用，丢进线程池跑，别卡事件循环
        （机器人收发消息全指着这个循环）。频率刻意放低：6 小时一次 ——
        非官方框架的高频调用会提高小号被风控的概率。
        """
        await asyncio.sleep(90)  # 启动后缓一缓，等连接都就绪
        while True:
            try:
                data = self.settings.snapshot()
                if data.get("onebot_enabled") and data.get("onebot_auto"):
                    result = await asyncio.to_thread(
                        onebot.match_and_bind,
                        self.members,
                        str(data.get("onebot_http_url") or ""),
                        str(data.get("onebot_token") or ""),
                        str(data.get("onebot_group_map") or ""),
                    )
                    if result.get("bound"):
                        _log.info(f"[botpy] OneBot 自动匹配：{result.get('msg')}")
                    for issue in result.get("issues") or []:
                        _log.warning(f"[botpy] OneBot 匹配问题: {issue}")
            except Exception as error:
                # 定时任务不许死：任何异常都只记日志，循环继续
                _log.warning(f"[botpy] OneBot 自动匹配失败: {error}")
            await asyncio.sleep(6 * 3600)

    def _check_access_readiness(self):
        """自查「加群解锁」的前提，缺什么就明确报出来。

        这几个坑都很隐蔽：板块是公开的、板块是论坛（没法 @机器人）、
        群的 openid 填成了群号 —— 任意一个都会让整个流程静默失效。
        """
        if not self.access.enabled():
            return

        # 多频道：每个频道的资料板块 / 解锁群都各配一套，逐个查
        guild_ids = self.access.target_guilds(self._known_guild_ids())
        if not guild_ids:
            _log.info("[botpy] 加群解锁：还没有任何频道开启它")
            return

        for guild_id in guild_ids:
            self._check_one_guild(guild_id)

    def _check_one_guild(self, guild_id):
        """自查单个频道的前提。"""
        who = self._guild_label(guild_id)
        channel_id = self.access.channel_id(guild_id)
        if not channel_id:
            _log.warning(f"[botpy] 加群解锁（{who}）：还没设置「资料板块 ID」")
        else:
            target = None
            # 资料板块必须在这个频道自己的子频道列表里找 ——
            # 别的频道的子频道 id 拿来是匹配不上的
            for guild in self.inventory.snapshot()["guilds"]:
                if str(guild.get("id") or "") != str(guild_id):
                    continue
                for channel in guild.get("channels") or []:
                    if channel.get("id") == channel_id:
                        target = channel
            if target is None:
                _log.warning(
                    f"[botpy] 加群解锁（{who}）：这个频道的子频道列表里没找到资料板块 "
                    f"{channel_id}，请到配置页「频道」页核对 ID"
                )
            else:
                name = target.get("name") or channel_id
                if not is_specified_user(target.get("private_type")):
                    _log.warning(
                        f"[botpy] 加群解锁（{who}）：资料板块「{name}」的私有类型是"
                        f"{private_type_label(target.get('private_type'))}。"
                        f"「修改子频道用户权限」只对「指定成员可见」的子频道有效，"
                        f"现在这个样子授权会直接报 301003 —— 请把它改成「指定成员可见」，"
                        f"并把机器人设为频道管理员"
                    )
                if as_int(target.get("type"), -1) != ChannelType.TEXT_CHANNEL.value:
                    _log.warning(
                        f"[botpy] 加群解锁（{who}）：资料板块「{name}」是"
                        f"{channel_type_label(target.get('type'))}子频道 —— "
                        f"用户没法在这里 @机器人 领码（论坛子频道里发帖/评论不产生频道消息事件），"
                        f"请让他们走私信，或者另建一个文字子频道当入口"
                    )

        group_ids = self.access.group_openids(guild_id)
        if not group_ids:
            _log.warning(f"[botpy] 加群解锁（{who}）：还没设置「指定群的 openid」")
        for group_openid in group_ids:
            if len(group_openid) < 20:
                _log.warning(
                    f"[botpy] 加群解锁（{who}）：兑换群 openid「{group_openid}」看起来不是 openid"
                    f"（应该是 32 位十六进制，群号请填在「展示给用户的群号/链接」里），兑换会一直失败"
                )

    async def _refresh_group_info(self, group_openid=None, with_members=False, quiet=False):
        """查 / 刷新群信息（平台白名单接口）。

        quiet=True 时优先用缓存（1 小时），避免每次启动都去打接口。
        不传 group_openid 就查所有已知的群（含订阅的、被拉进来的、解锁用的那个）。
        返回给日志/测试用的一行行文本。
        """
        if not self.group_info.available():
            lines = [f"群信息接口不可用：{self.group_info.denied_hint()}"]
            if not quiet:
                for line in lines:
                    _log.info(f"[botpy] {line}")
            return lines

        targets = []
        if group_openid:
            targets.append(str(group_openid))
        else:
            for item in self.groups.all():
                targets.append(str(item.get("group_openid") or ""))
            # 所有频道配置的兑换群都算"机器人在意的群"（可能是多个）
            for guild_id in self.settings.guild_ids():
                targets.extend(self.access.group_openids(guild_id))
            targets.extend(self.access.group_openids())
        targets = [item for item in dict.fromkeys(targets) if item]

        lines = []
        for item in targets:
            info, hint = await self.group_info.fetch_info(
                item, max_age=INFO_CACHE_SECONDS if quiet else 0
            )
            if hint and not info:
                lines.append(f"群 {item}：{hint}")
                continue
            if with_members:
                members = await self.group_info.fetch_members(item)
                if members is None:
                    lines.append(f"群 {item}：成员名单没拉全（不会拿它去对账）")
                else:
                    lines.append(f"群 {item}：成员共 {len(members)} 人")
            if info:
                lines.append(
                    f"群「{info.get('group_name') or '(无名)'}」"
                    f"    group_openid = {item}"
                    f"    人数 = {info.get('group_member_num') or '?'}"
                )

        for line in lines:
            _log.info(f"[botpy] {line}")
        return lines

    async def _print_inventory(self):
        """启动时打印机器人所在的频道 / 子频道 / 已知群，方便填各种 ID。

        返回打印过的行，便于测试。
        """
        lines = ["==================== 机器人所在位置 ===================="]

        # 频道 / 子频道：真查。顺手把缓存刷一遍，配置页「频道」页也就有数据了。
        snapshot = await self.inventory.sync(self.api)
        guilds = snapshot["guilds"]
        if not guilds:
            lines.append("频道：没查到（机器人可能还没加入任何频道）")

        for guild in guilds:
            guild_id = guild.get("id") or ""
            # 资料板块是**每个频道各配一个**，所以要在各自的子频道列表里比对
            target_channel = self.access.channel_id(guild_id)
            count = guild.get("member_count")
            permissions = guild.get("permissions") or []
            granted = sum(1 for item in permissions if as_int(item.get("auth_status"), -1) == 1)
            number = str(guild.get("channel_number") or "")
            own = "有独立配置" if guild_id in self.settings.guild_ids() else "跟随全局配置"
            lines.append(
                f"频道：{guild.get('name') or '(无名)'}    guild_id = {guild_id}"
                + (f"    频道号 = {number}" if number else "    频道号 = (没解析出来)")
                + (f"    成员数 = {count}" if count else "")
                + (
                    f"    接口权限 = {granted}/{len(permissions)} 已授权"
                    if permissions
                    else ""
                )
                + f"    {own}"
            )
            if guild.get("channel_error"):
                lines.append(f"  └ 拉取子频道失败：{guild['channel_error']}")
            if guild.get("permission_error"):
                lines.append(f"  └ 拉取接口权限失败：{guild['permission_error']}")
            for channel in guild.get("channels") or []:
                channel_id = channel.get("id") or ""
                extra = ""
                if target_channel and channel_id == target_channel:
                    extra = "   <== 加群解锁的「资料」板块"
                    if not is_specified_user(channel.get("private_type")):
                        extra += "（注意：它不是「指定成员可见」，加群解锁不会生效）"
                if channel.get("nested"):
                    branch = "  │  └"
                elif as_int(channel.get("type"), -1) == ChannelType.GROUP_CHANNEL.value:
                    branch = "  ├"
                else:
                    branch = "  └"
                lines.append(
                    f"{branch} [{channel_type_label(channel.get('type'))}]"
                    f" {channel.get('name') or '(无名)'}"
                    f"    channel_id = {channel_id}"
                    f"    私有类型 = {private_type_label(channel.get('private_type'))}{extra}"
                )

        # 群：平台没有「查询机器人在哪些群」的接口，只能列本地记录过的。
        # 但「群名称 / 群人数」有白名单接口，拿得到就一并显示（1 小时缓存，不重复打接口）。
        lines.append("群（平台没有「查询机器人在哪些群」的接口，以下是本地记录过的）：")
        groups = self.groups.all()
        if not groups:
            lines.append("  └ 还没有记录到任何群（把机器人拉进群、或在群里说句话就会出现）")
        for item in groups:
            group_openid = str(item.get("group_openid") or "")
            info, _ = await self.group_info.fetch_info(group_openid, max_age=INFO_CACHE_SECONDS)
            detail = ""
            if info:
                detail = (
                    f"    名称 = {info.get('group_name') or '(没拿到)'}"
                    f"    人数 = {info.get('group_member_num') or '?'}"
                )
            lines.append(
                f"  └ {group_openid}{detail}    "
                f"{item.get('source') or '未知来源'}，最后活跃 {item.get('last_seen') or ''}"
            )
        if not self.group_info.available():
            lines.append(f"  （群名/人数需要白名单接口，当前不可用：{self.group_info.denied_hint()}）")
        lines.append("======================================================")

        for line in lines:
            _log.info(f"[botpy] {line}")
        return lines

    # ------------------------------------------------------------------
    # 一、频道发帖事件
    # ------------------------------------------------------------------
    async def on_open_forum_thread_create(self, open_forum_thread):
        """公域可用：OPEN_FORUM_THREAD_CREATE。

        官方文档只列了 guild_id / channel_id / author_id，实测下发里也不带
        thread_info，所以基本都要靠反查帖子列表拿 thread_id。
        """
        _log.info(f"[botpy] OPEN_FORUM_THREAD_CREATE 原始下发数据: {open_forum_thread.raw}")

        thread_id = None
        thread_info = getattr(open_forum_thread, "thread_info", None)
        if thread_info is not None:
            thread_id = getattr(thread_info, "thread_id", None)

        await self._collect(
            guild_id=open_forum_thread.guild_id,
            channel_id=open_forum_thread.channel_id,
            author_id=open_forum_thread.author_id,
            thread_id=thread_id,
        )

    async def on_forum_thread_create(self, thread):
        """私域机器人（intents 里 forums=True）走这里，事件直接带 thread_id。"""
        thread_id = None
        try:
            thread_id = thread.thread_info.thread_id
        except Exception:
            # botpy 对 title/content 的解析在部分下发格式下会抛异常
            thread_id = None
        await self._collect(
            guild_id=thread.guild_id,
            channel_id=thread.channel_id,
            author_id=thread.author_id,
            thread_id=thread_id,
        )

    async def _collect(self, guild_id, channel_id, author_id=None, thread_id=None):
        post = await self.forum.collect(
            guild_id=guild_id, channel_id=channel_id, author_id=author_id, thread_id=thread_id
        )
        if not post:
            return
        # 先看这条帖子是不是在「领资料」，再转发。
        # collect() 内部有 thread_id 去重，所以同一条帖子只会走到这里一次。
        await self._maybe_issue_from_post(post)
        _log.info(f"[botpy] 检测到新帖: {post['title']!r} -> {post['link']}")
        self.forwarder.submit(post)

    async def _maybe_issue_from_post(self, post):
        """帖子正文里 @机器人 说「领资料」-> 私信发码。

        正文不是事件给的（OPEN_FORUM_POST_CREATE / THREAD_CREATE 的下发体里
        实测只有 guild_id / channel_id / author_id），是 channel_forum 反查
        「帖子列表 / 帖子详情」捞回来的。
        """
        if not self.access.enabled():
            return False

        content = post.get("content") or ""
        if not content:
            _log.info(
                f"[botpy] 帖子 {post.get('thread_id')} 没取到正文，跳过领资料判断"
                f"（转发不受影响）"
            )
            return False

        # 只认最近的帖子：楼层事件也会带 author_id，反查会取到那个人最新的帖，
        # 可能是几天前的旧帖 —— 不加时间窗会莫名其妙给旧帖发码。
        if not is_recent(post.get("date_time"), POST_FRESH_SECONDS):
            _log.info(
                f"[botpy] 帖子 {post.get('thread_id')} 的发布时间 {post.get('date_time')!r} "
                f"不在最近 {POST_FRESH_SECONDS} 秒内，不当成领资料请求"
            )
            return False

        plain = strip_inline(content)
        mentioned = mentions_bot(content, self._bot_user_id())
        hinted = any(word in plain for word in access_post_hints())
        if not (mentioned or hinted):
            return False

        user_id = str(post.get("author_id") or "")
        if not user_id:
            _log.warning("[botpy] 帖子没有 author_id，认不出要授权的人")
            return False

        _log.info(
            f"[botpy] 帖子 {post.get('thread_id')} 命中领资料"
            f"（@机器人={mentioned}，含关键词={hinted}）：{plain!r}"
        )
        guild_id = post.get("guild_id") or self._first_guild_id()
        channel_name = await self._channel_label(guild_id)
        result, reason = self.access.issue(user_id, guild_id, channel_name)
        if not result:
            await self._deliver(user_id, guild_id, reason)
            return True
        await self._deliver(
            user_id, guild_id, self._code_message(result, guild_id, channel_name)
        )
        return True

    # ------------------------------------------------------------------
    # 帖子 / 评论里 @机器人 领资料
    # ------------------------------------------------------------------
    # 论坛子频道里发帖、评论不会产生频道消息事件。
    # 实测这两个事件的下发体里**只有** guild_id / channel_id / author_id，
    # 没有任何正文 —— 所以正文必须靠 channel_forum 反查帖子接口拿。
    async def on_open_forum_post_create(self, data):
        _log.info(f"[botpy] OPEN_FORUM_POST_CREATE 原始下发数据: {data}")
        await self._collect(
            guild_id=(data or {}).get("guild_id"),
            channel_id=(data or {}).get("channel_id"),
            author_id=(data or {}).get("author_id"),
        )

    async def on_open_forum_reply_create(self, data):
        """评论事件。

        下发体同样只有几个 id、没有评论正文，而 botpy 也没有「取评论列表」的接口，
        所以「评论区 @机器人 领资料」目前做不到 —— 这里先原样打日志，
        万一平台以后补上正文，下面的兜底解析就能接住。
        """
        _log.info(f"[botpy] OPEN_FORUM_REPLY_CREATE 原始下发数据: {data}")
        await self._handle_forum_message(data, where="评论")

    def _bot_user_id(self):
        """机器人自己的频道用户 id（用于在正文里认出 <@!自己>）。"""
        return str(getattr(self.robot, "id", "") or "")

    # ------------------------------------------------------------------
    # 多频道：频道名 / 频道号 / 频道 id 的换算
    # ------------------------------------------------------------------
    def _known_guild_ids(self):
        """机器人当前所在的频道 id（来自频道同步的缓存）。"""
        return [
            str(item.get("id") or "")
            for item in self.inventory.snapshot()["guilds"]
            if item.get("id")
        ]

    def _sync_guild_profiles(self):
        """频道同步完成后，把真名和频道号补进**已有**的频道配置。

        不主动创建 —— 否则一同步就冒出一堆空配置，配置页上会看不懂。
        创建走配置页「频道」页上的「启用独立配置」。
        """
        for guild_id in self.settings.guild_ids():
            info = self.inventory.guild_of(guild_id)
            if not info:
                continue
            profile = self.settings.profile(guild_id)
            patch = {}
            if not profile.get("label") and info.get("name"):
                patch["label"] = info["name"]
            if not profile.get("channel_number") and info.get("channel_number"):
                patch["channel_number"] = info["channel_number"]
            if patch:
                self.settings.set_profile(guild_id, patch)
                _log.info(f"[botpy] 已补全频道配置 {guild_id}：{patch}")

    def _guild_label(self, guild_id):
        """频道显示名：配置里填的 label > 频道真名 > guild_id。"""
        guild_id = str(guild_id or "")
        label = str(self.settings.profile(guild_id).get("label") or "").strip()
        if label:
            return label
        info = self.inventory.guild_of(guild_id)
        return str(info.get("name") or "") or guild_id or "(未知频道)"

    def _channel_number_of(self, guild_id):
        """给 ChannelForum 用的「频道号」数据源。

        **多频道下必须按频道区分** —— 每个频道的频道号都不一样，用错了帖子链接
        会跳到别的频道或者打不开。取值顺序：

          1. 频道配置里手填的 channel_number
          2. 频道同步时从 icon 自动解析出来的（一般这一步就够了）
          3. config.yaml 里那个全局值 —— **只在没配任何频道配置时才用**，
             多频道下它是过时的单频道遗留配置，用了必然串频道
        """
        guild_id = str(guild_id or "")
        manual = str(self.settings.profile(guild_id).get("channel_number") or "").strip()
        if manual:
            return manual
        auto = self.inventory.channel_number_of(guild_id)
        if auto:
            return auto
        if not self.settings.guild_ids():
            return config.get_channel_number()
        return ""

    def _guild_by_number(self, value):
        """把用户输入的「频道号 / guild_id / 频道名」对应回 guild_id。

        群内订阅要用它：群消息事件里**没有 guild_id**，机器人没法自己知道
        "这个群属于哪个频道"，只能由用户指明。
        """
        # 参数不能叫 text —— 会盖住文案模块
        value = str(value or "").strip()
        if not value:
            return ""
        # 已经是 guild_id
        if self.inventory.guild_of(value):
            return value
        # 频道号（配置里填的，或同步时自动解析的）
        for guild_id in self.settings.guild_ids():
            if value == str(self.settings.profile(guild_id).get("channel_number") or ""):
                return guild_id
        for guild in self.inventory.snapshot()["guilds"]:
            guild_id = str(guild.get("id") or "")
            number = str(guild.get("channel_number") or "")
            name = str(guild.get("name") or "")
            if value in (number, name, guild_id):
                return guild_id
        # 频道配置里填的 label
        for guild_id in self.settings.guild_ids():
            if value == str(self.settings.profile(guild_id).get("label") or ""):
                return guild_id
        return ""

    def _guild_choices(self):
        """可选频道列表，用于「订阅」命令的提示文案。"""
        choices = []
        seen = set()
        for guild in self.inventory.snapshot()["guilds"]:
            guild_id = str(guild.get("id") or "")
            if not guild_id or guild_id in seen:
                continue
            seen.add(guild_id)
            number = str(guild.get("channel_number") or "")
            name = str(guild.get("name") or "") or "(无名)"
            choices.append(f"　{number or guild_id}　{name}")
        for guild_id in self.settings.guild_ids():
            if guild_id in seen:
                continue
            seen.add(guild_id)
            number = str(self.settings.profile(guild_id).get("channel_number") or "")
            choices.append(f"　{number or guild_id}　{self.settings.profile(guild_id).get('label') or ''}")
        return choices

    async def _handle_forum_message(self, data, where="帖子"):
        """兜底：万一以后事件里带上了正文，这条路也能用（目前拿不到）。"""
        if not self.access.enabled():
            return
        content = extract_content(data)
        if not content:
            _log.info(
                f"[botpy] {where}事件里没解析出正文，跳过"
                f"（字段名未知，对照上面那行原始下发数据）"
            )
            return

        if not mentions_bot(content, self._bot_user_id()):
            return

        command = strip_inline(content)
        if command not in access_commands():
            _log.info(f"[botpy] {where}里 @了机器人，但指令不认识：{command!r}")
            return

        user_id = str((data or {}).get("author_id") or "")
        if not user_id:
            _log.warning(f"[botpy] {where}事件里没有 author_id，认不出要授权的人")
            return

        guild_id = str((data or {}).get("guild_id") or "") or self._first_guild_id()
        channel_name = await self._channel_label(guild_id)
        result, reason = self.access.issue(user_id, guild_id, channel_name)
        if not result:
            await self._deliver(user_id, guild_id, reason)
            return
        await self._deliver(
            user_id, guild_id, self._code_message(result, guild_id, channel_name)
        )

    def _first_guild_id(self):
        guilds = self.inventory.snapshot()["guilds"]
        return str(guilds[0].get("id") or "") if guilds else ""

    async def _channel_label(self, guild_id=None):
        """要开通的板块的**真实名字**（实时查平台，5 分钟缓存）。

        文案里不再写死「资料」二字 —— 板块叫什么由频道管理员建频道时定，
        管理员改了名，机器人发的话也要跟着变。查不到（接口失败/还没同步）
        就退回「资料」，不让一条查询失败挡住领资料流程。
        """
        fallback = text.text("labels.default_channel")
        channel_id = self.access.channel_id(guild_id)
        if not channel_id:
            return fallback
        cached = self._channel_labels.get(channel_id)
        if cached and time.time() - cached[1] < 300:
            return cached[0]
        name = ""
        try:
            info = await self.api.get_channel(channel_id)
            if isinstance(info, dict):
                name = str(info.get("channel_name") or "")
            else:
                name = str(getattr(info, "channel_name", "") or "")
        except Exception as error:
            _log.info(f"[botpy] 实时查板块名失败，改从频道缓存里找: {error}")
        # 接口失败（权限/限频）时从启动时同步的频道列表里找 ——
        # 那份缓存里也有子频道名，多数情况能把名字救回来
        if not name:
            for guild in self.inventory.snapshot().get("guilds") or []:
                if guild_id and str(guild.get("id") or "") != str(guild_id):
                    continue
                for channel in guild.get("channels") or []:
                    if str(channel.get("id") or "") == channel_id:
                        name = str(channel.get("name") or "")
                        break
                if name:
                    break
        if not name:
            # 两个来源都没有 —— 把原因说到明处，别让用户以为功能没生效
            _log.warning(
                f"[botpy] 查不到板块 {channel_id} 的名字，文案只能用默认的「资料」。"
                f"常见原因：板块 ID 配错了（不是本频道的子频道），"
                f"或机器人缺少「获取子频道信息」接口权限"
            )
            return fallback
        self._channel_labels[channel_id] = (name, time.time())
        return name

    def _code_message(self, result, guild_id=None, channel_name=""):
        """验证码文案 —— 帖子、评论、频道三处共用一份资源，改一处三处一起变。

        channel_name 是板块的真实名字（调用方先 await _channel_label 拿），
        文案里不再写死「资料」二字。
        """
        return text.text(
            "messages.access.code",
            code=result["code"],
            minutes=result["expire_minutes"],
            group=self.access.group_label(guild_id),
            channel=channel_name or text.text("labels.default_channel"),
        )

    async def _deliver(self, user_id, guild_id, content):
        """把一段话送到频道用户手上：优先私信，失败就退回到文字子频道里 @他。"""
        # 参数不能叫 text —— 会盖住文案模块
        try:
            payload = await self.api.create_dms(guild_id=guild_id, user_id=user_id)
            dms_guild_id = (payload or {}).get("guild_id")
            if dms_guild_id:
                await self.api.post_dms(guild_id=dms_guild_id, content=content)
                _log.info(f"[botpy] 已私信频道用户 {user_id}")
                return True
            _log.warning(f"[botpy] create_dms 没返回 guild_id: {payload}")
        except Exception as error:
            _log.warning(f"[botpy] 私信 {user_id} 失败: {error}")

        channel_id = self._fallback_channel(guild_id)
        if not channel_id:
            _log.warning(
                f"[botpy] 私信发不出去，频道里也没有可发消息的文字子频道，"
                f"内容没能送到 {user_id}（私信主动推送每日每会话限 2 条）"
            )
            return False
        try:
            # 内嵌格式 <@!user_id> 会渲染成 @他，用户能收到提醒
            await self.api.post_message(
                channel_id=channel_id, content=f"<@!{user_id}> {content}"
            )
            _log.info(f"[botpy] 已在子频道 {channel_id} 里 @ 频道用户 {user_id} 送出内容")
            return True
        except Exception as error:
            _log.warning(f"[botpy] 退回到子频道 {channel_id} 发消息也失败: {error}")
            return False

    def _fallback_channel(self, guild_id):
        """找一个能发消息的文字子频道（「资料」板块本身是文字的就优先用它）。"""
        target = self.access.channel_id(guild_id)
        for guild in self.inventory.snapshot()["guilds"]:
            if guild_id and guild.get("id") != guild_id:
                continue
            text_channels = [
                str(channel.get("id") or "")
                for channel in guild.get("channels") or []
                if as_int(channel.get("type"), -1) == ChannelType.TEXT_CHANNEL.value
            ]
            if target and target in text_channels:
                return target
            if text_channels:
                return text_channels[0]
        return ""

    # ------------------------------------------------------------------
    # 二、频道内 @机器人 / 私信：领资料 / 查状态
    # ------------------------------------------------------------------
    async def on_at_message_create(self, message):
        """频道里 @机器人（公域机器人走这个事件，需要 public_guild_messages）。

        注意：**只有文字子频道里的 @ 才会触发**。论坛子频道里发帖、评论，
        以及日程子频道，都不产生频道消息事件 —— 那种情况请走私信，
        或者在该频道新建一个文字子频道。
        """
        await self._handle_channel_message(message, via="频道")

    async def on_direct_message_create(self, message):
        """频道私信（需要在 intents 里打开 direct_message，并申请私信权限）。

        频道里没有文字子频道可 @ 时，私信是唯一入口，所以两条路都接。
        """
        await self._handle_channel_message(message, via="私信")

    async def on_direct_message_delete(self, message):
        _log.info("[botpy] 收到私信撤回事件")

    def _channel_user_id(self, message):
        return str(getattr(getattr(message, "author", None), "id", None) or "")

    def _is_channel_admin(self, user_id, guild_id=None):
        """频道侧管理指令的鉴权。

        `channel_admin_ids` 是**可以按频道覆盖**的：不同频道的管理员名单不一样，
        所以这里按消息所属的频道取生效值。
        """
        admins = split_lines(self.settings.snapshot(guild_id).get("channel_admin_ids"))
        return str(user_id) in admins

    async def _handle_channel_message(self, message, via="频道"):
        # 抄送会把 < > 转义成 &lt; &gt;，所以先反转义再剥 @占位符，
        # 这样「&lt;@!机器人&gt; 领资料」和「<@!机器人> 领资料」都能认出指令
        content = clean_content(unescape_inline(getattr(message, "content", None) or ""))
        user_id = self._channel_user_id(message)
        channel_id = str(getattr(message, "channel_id", None) or "")
        # 私信的 guild_id 就是私信会话本身，真正的来源频道在 src_guild_id 里
        guild_id = getattr(message, "src_guild_id", None) or getattr(message, "guild_id", None)
        _log.info(
            f"[botpy] {via}消息: {content!r} user={user_id} channel={channel_id} guild={guild_id}"
        )

        my_id_words = {word.upper() for word in text.words("commands.channel_my_id")}
        if content.upper() in my_id_words:
            await message.reply(
                content=text.text(
                    "messages.channel.my_id", user_id=user_id, channel_id=channel_id
                )
            )
            return

        if content in access_commands():
            await self._access_issue(message, user_id, channel_id, guild_id)
            return

        if content in access_status_commands():
            # 同一个 QQ 用户在多个频道里的 user_id 是同一个，
            # 所以查状态必须带上频道，否则会把别的频道的授权显示出来
            channel_name = await self._channel_label(guild_id)
            await message.reply(
                content=self.access.status_text(user_id, guild_id, channel_name)
            )
            return

        if content in text.words("commands.channel_set_access"):
            if not self._is_channel_admin(user_id, guild_id):
                await message.reply(
                    content=text.text(
                        "messages.channel.set_access_denied", user_id=user_id
                    )
                )
                return
            # 写进这个频道自己的配置（多频道下每个频道的资料板块不一样）
            self.settings.update({"access_channel_id": channel_id}, guild_id=guild_id)
            await message.reply(
                content=text.text(
                    "messages.channel.set_access_done",
                    channel_id=channel_id,
                    guild=self._guild_label(guild_id),
                )
            )
            return

        await message.reply(content=self._channel_help(guild_id))

    def _channel_help(self, guild_id=None):
        lines = [text.text("messages.channel.help_title")]
        lines.extend(text.lines("messages.channel.help_lines"))
        if self.access.enabled(guild_id):
            lines.append(self.access.intro(guild_id))
        return "\n".join(lines)

    async def _access_issue(self, message, user_id, channel_id, guild_id=None):
        expected = self.access.channel_id(guild_id)
        channel_name = await self._channel_label(guild_id)
        result, reason = self.access.issue(user_id, guild_id, channel_name)
        if not result:
            # issue() 在"你已经有权限了"时也会返回说明，直接回给用户
            await message.reply(content=reason)
            return
        lines = [self._code_message(result, guild_id, channel_name)]
        if expected and channel_id and channel_id != expected:
            lines.append(
                text.text(
                    "messages.access.channel_mismatch",
                    channel=channel_name,
                    expected=expected,
                    channel_id=channel_id,
                )
            )
        await message.reply(content="\n".join(lines))

    # ------------------------------------------------------------------
    # 三、群消息：撤回 + 指令
    # ------------------------------------------------------------------
    async def on_group_at_message_create(self, message):
        """只 @机器人的消息事件（未开全量模式时走这里）。"""
        await self._handle_group_message(message, is_at=True)

    async def on_group_message_create(self, message):
        """群消息全量模式（需在开放平台开启「接收所有消息」）。

        开了全量模式后，@机器人的消息也会走这个事件（不再走 group_at），
        所以指令处理同样在这里。好处是能拿到群里每条消息的 msg_idx，
        「撤回任何群内消息」才算真正可用。
        """
        await self._handle_group_message(message, is_at=False)

    async def _handle_group_message(self, message, is_at):
        # 一条消息的崩溃**不允许带崩整条流水线**：botpy 串行处理 WS 消息，
        # 处理函数抛异常会一路炸到网关层触发重连 —— 表现就是
        # "机器人突然全无反应、连日志都没有"。这里兜住并留全量堆栈。
        try:
            await self._handle_group_message_inner(message, is_at)
        except Exception:
            _log.exception("[botpy] 处理群消息时发生未捕获异常（本条已跳过，流水线继续）")

    async def _handle_group_message_inner(self, message, is_at):
        if self.settings.snapshot().get("log_group_messages", True):
            _log.info(describe(message))

        # 两个事件可能都推同一条消息，去重
        if message.id in self._handled_messages:
            _log.debug(f"[botpy] 消息 {message.id} 已处理过，跳过")
            return
        self._handled_messages.append(message.id)

        # 记下 msg_idx -> message_id，别人引用这条消息时才能反查
        self.index.remember(message)
        # 记下这个人，配置页才能按昵称认出该授权哪个 openid
        self.members.remember(message)
        # 记下这个群（平台没有「机器人在哪些群」的接口，只能靠平时记录）
        self.groups.remember(message.group_openid, source="收到消息")

        # 命中拉黑内容/撤回关键词的消息先撤掉，撤掉了就不用再走指令了
        if await self.recall.auto_recall(message):
            return

        # 全量模式下 content 里会带着 <@openid>，先剥掉再匹配指令
        command = clean_content(message.content)
        trusted = is_at or mentioned_bot(message)
        quoted = quoted_index_of(message) is not None

        # OneBot 实时识别：这条消息若能在 NapCat 消息流里配对上，
        # 就把发送者的真实 QQ 号当场写进绑定表 —— 转发里立刻能显示真号，
        # 不再依赖"预先绑定"。放在互转之前，本条消息的转发就能用上。
        await self._onebot_live_resolve(message)

        # 群消息互转：配了规则就转发到目标群。
        # 放在这里而不是"@机器人"分支之后 —— 它要覆盖群里的**每一条**消息，
        # 不受"有没有 @ 我"影响（全量模式下的普通闲聊也要转）。
        # is_command 传进去是为了让 relay 按配置跳过指令消息，免得刷屏。
        # 验证码对话**一律不转**：@机器人 + 6 位码是领资料的兑换过程，
        # 转到别的群等于把别人的兑换现场直播出去（见 _redeem_code 的撤回说明）
        code_input = trusted and looks_like_code(command)
        await self.relay.relay(
            message, is_command=is_command(command) or code_input
        )

        # 判定规则：
        #   · @了机器人 -> 当指令处理（不认识的指令也会回帮助）
        #   · 没 @ 但「引用了某条消息 + 内容正好是已知指令」-> 也当指令处理，
        #     这样「引用 + 撤回」「引用 + 拉黑」就不用再 @机器人了
        #   · 其余（全量模式下的普通闲聊）一律不理会，否则会刷屏
        if not trusted and not (quoted and is_command(command)):
            return

        # 群里发的 6 位验证码 -> 兑换资料板块权限。
        # 这条任何人都能用（不需要管理员），但要求 @机器人 或「引用+指令」，
        # 避免全量模式下把闲聊里的 6 个字母误当验证码。
        if looks_like_code(command):
            await self._redeem_code(message, command)
            return

        await self._handle_group_command(message, command, allow_help=trusted)

    async def _handle_group_command(self, message, command, allow_help=True):
        _log.info(f"[botpy] 群指令: {command!r} (group={message.group_openid})")

        is_admin, identity = self.admins.check(message)

        # 权限闸门：会改动机器人行为的指令只有管理员能用。
        # 放在这里而不是 _handle_group_message 里，是为了让「引用 + 指令」
        # 这条免 @ 的路径也一并受控 —— 否则普通成员引用一条消息发「拉黑」
        # 就能把整类内容拉黑、并把原消息撤掉。
        if requires_admin(command) and not is_admin:
            _log.warning(f"[botpy] 已拒绝非管理员指令 {command!r}，身份：{identity}")
            await message.reply(
                content=text.text(
                    "messages.group.no_permission", command=command, identity=identity
                )
            )
            return

        if requires_admin(command):
            _log.info(f"[botpy] 管理员 {identity} 执行指令 {command!r}")

        # 先把指令归一化成「逻辑名 + 参数」再分发。
        # 指令词来自 text.yaml，所以这里不用再逐条写 startswith("订阅 ") 这类硬编码 ——
        # 改了 text.yaml 里的指令词，分发、权限、帮助文案会一起跟着变。
        name, argument = resolve_command(command)
        words = command_words()
        suffix = text.text(
            "messages.group.status_admin"
            if is_admin
            else "messages.group.status_no_admin"
        )

        if name == "subscribe":
            await self._subscribe(message, argument, subscribe=True)
        elif name == "unsubscribe":
            await self._subscribe(message, argument, subscribe=False)
        elif name == "status":
            lines = [
                text.text(
                    "messages.group.status_line", identity=identity, suffix=suffix
                ),
                text.text(
                    "messages.group.status_targets", count=len(self.subscribers)
                ),
                text.text(
                    "messages.group.status_strategy",
                    strategy=self.settings.describe(),
                ),
                text.text(
                    "messages.group.status_buffer",
                    count=self.forwarder.describe_buffer(),
                ),
            ]
            lines.extend(
                "　" + line
                for line in self.forwarder.describe_buffer_lines(self._guild_label)
            )
            await message.reply(content="\n".join(lines))
        elif name == "mode":
            await message.reply(
                content=text.text(
                    "messages.group.mode_now", strategy=self.settings.describe()
                )
            )
        elif name == "flush":
            count = await self.forwarder.flush(force=True)
            await message.reply(
                content=(
                    text.text("messages.group.flush_ok", count=count)
                    if count
                    else text.text("messages.group.flush_empty")
                )
            )
        elif name == "reload_settings":
            self.settings.reload()
            # 顺手把文案也重读一遍：改了 text.yaml 发这一句就生效，不用重启
            text.reload()
            await message.reply(
                content=text.text(
                    "messages.group.reload_ok", strategy=self.settings.describe()
                )
            )
        elif name == "recall":
            if argument:
                await self.recall.recall_by_id(message, argument)
            else:
                await self.recall.recall_quoted(message)
        elif name == "recall_last":
            await self.recall.recall_last_sent(message)
        elif name == "blacklist_add":
            await self.recall.blacklist_add(message, argument or None)
        elif name == "blacklist_remove":
            await self.recall.blacklist_remove(message, argument or None)
        elif name == "blacklist_show":
            await self.recall.blacklist_show(message)
        elif name == "blacklist_member":
            await self.recall.member_blacklist_add(message, argument or None)
        elif name == "unblacklist_member":
            await self.recall.member_blacklist_remove(message, argument or None)
        elif name == "blacklist_members":
            await self.recall.member_blacklist_show(message)
        elif name == "bindings":
            lines = self.access.describe_lines()
            await message.reply(
                content=text.text(
                    "messages.group.bindings_title", count=len(self.access)
                )
                + "\n"
                + "\n".join(lines)
            )
        elif name == "revoke":
            await self._revoke_access(message, argument)
        elif name == "group_info":
            await self._group_info_command(message)
        elif name == "group_info_refresh":
            await self._group_info_command(message, refresh=True)
        elif name == "group_members":
            await self._group_members_command(message)
        elif name == "reconcile":
            await self._reconcile_command(message)
        elif name == "relay_status":
            await self._relay_status_command(message)
        elif allow_help:
            any_user = " / ".join(
                words.get(item, "") for item in _ANY_USER if words.get(item)
            )
            lines = [
                text.text("messages.group.help_any_user", names=any_user),
                text.text(
                    "messages.group.help_identity", identity=identity, suffix=suffix
                ),
            ]
            if is_admin:
                lines += text.lines("messages.group.help_admin_lines")
            else:
                lines.append(text.text("messages.group.help_member_line"))
            await message.reply(content="\n".join(lines))

    # ------------------------------------------------------------------
    # 四、资料板块权限：群内兑换 / 管理员手动撤销
    # ------------------------------------------------------------------
    async def _redeem_code(self, message, code):
        """群内兑换验证码 -> 给对应的频道账号开通板块权限。

        兑换对话（用户发码这条 + 机器人的回复）在 access_recall_seconds 秒后
        **双双自动撤回**：验证码虽被偷也没用（见 access 模块说明），但没必要
        让兑换现场一直挂在聊天记录里、还可能被互转围观的群看到。
        """
        binding, reason = self.access.redeem(
            code=code,
            group_openid=message.group_openid,
            member_openid=member_openid_of(message),
        )
        if not binding:
            _log.warning(f"[botpy] 验证码兑换失败 {code}: {reason}")
            reply = await message.reply(content=reason)
            self._recall_code_trade_later(message, reply)
            return

        ok, hint = await self.access.grant(binding)
        if ok:
            self.access.commit(binding)
            # 用码绑定的频道去查板块名：多频道下各频道的板块不一样
            channel_name = await self._channel_label(binding.get("guild_id"))
            reply = await message.reply(content=text.text(
                "messages.access.redeem_ok", channel=channel_name
            ))
        else:
            # 授权失败就不写绑定表，否则用户会卡在"没权限但已绑定"的状态，
            # 连重新领码都会被 issue() 挡回来。
            reply = await message.reply(
                content=text.text("messages.access.redeem_grant_failed", hint=hint)
            )
        self._recall_code_trade_later(message, reply)

    def _recall_code_trade_later(self, message, reply=None):
        """把验证码对话的两条消息安排到 N 秒后自动撤回。

        - **用户发码那条**：撤别人的消息需要机器人在本群是管理员；
          不是就跳过（recall 模块有 blocked 记账，不会反复白撞接口）。
        - **机器人自己的回复**：自己的消息随时能撤，15 秒远在平台
          2 分钟撤回时限之内。
        - access_recall_seconds 填 0 = 不自动撤回（退回原来的行为）。
        """
        try:
            seconds = int(self.settings.snapshot().get("access_recall_seconds") or 0)
        except (TypeError, ValueError):
            seconds = 15
        if seconds <= 0:
            return

        group = str(getattr(message, "group_openid", None) or "")
        user_msg_id = str(getattr(message, "id", "") or "")
        reply_id = _message_id_of(reply)

        async def job():
            await asyncio.sleep(seconds)
            # 用户那条是**别人的消息**，撤回条件多（要管理员、事件里要有
            # 消息 id、30 分钟失败冷却）—— 每种不撤的情况都说明白，
            # 别让人对着"机器人的回复撤了、用户的没撤"猜
            if not (group and user_msg_id):
                if group:
                    _log.warning(
                        f"[botpy] 群 {group[:10]}… 的验证码消息没撤："
                        f"这条消息的事件里没有消息 id，平台不允许撤回"
                    )
            elif self.recall.blocked(group):
                _log.info(
                    f"[botpy] 群 {group[:10]}… 的验证码消息跳过撤回："
                    f"该群此前撤回失败过（机器人需是群管理员），"
                    f"{self.recall.NO_PERMISSION_COOLDOWN // 60} 分钟冷却内不再尝试"
                )
            else:
                ok, hint = await self.recall.recall(group, user_msg_id)
                if not ok:
                    _log.info(f"[botpy] 验证码消息自动撤回未成功：{hint}")
            if group and reply_id:
                ok, hint = await self.recall.recall(group, reply_id)
                if not ok:
                    _log.info(f"[botpy] 兑换回复自动撤回未成功：{hint}")

        # 异常全吞在任务里：撤回失败只影响体验，不能让任何异常漏出去打警告
        self.loop.create_task(self._silenced(job()))

    async def _silenced(self, coro):
        try:
            await coro
        except Exception as error:
            _log.warning(f"[botpy] 验证码对话自动撤回任务异常: {error}")

    async def _revoke_access(self, message, user_id):
        """管理员手动收回某个频道用户的资料板块权限。

        user_id 从「绑定列表」里拿。**会收回他在所有频道的授权** ——
        群消息里没有 guild_id，机器人在群里没法知道你说的是哪个频道。
        """
        user_id = str(user_id or "")
        bindings = [
            item for item in self.access.all_bindings()
            if str(item.get("user_id") or "") == user_id
        ]
        if not bindings:
            await message.reply(
                content=text.text("messages.group.revoke_not_found", user_id=user_id)
            )
            return

        done, failed = [], []
        for binding in bindings:
            ok, hint = await self.access.revoke(user_id, binding.get("channel_id"))
            if ok:
                self.access.forget(user_id, binding.get("guild_id"))
                done.append(self._guild_label(binding.get("guild_id")))
            else:
                failed.append(f"{self._guild_label(binding.get('guild_id'))}：{hint}")
        lines = []
        if done:
            lines.append(
                text.text(
                    "messages.group.revoke_done",
                    user_id=user_id,
                    count=len(done),
                    labels="、".join(done),
                )
            )
        if failed:
            lines.append(text.text("messages.group.revoke_failed_header"))
            lines.extend("　" + item for item in failed)
        await message.reply(
            content="\n".join(lines) or text.text("messages.group.revoke_nothing")
        )

    async def _subscribe(self, message, argument, subscribe=True):
        """群内「订阅 / 取消订阅」（多频道下要指明频道）。

        **群消息事件里没有 guild_id** —— 机器人在群里，没法知道这个群属于哪个频道。
        所以：
          · 一个频道配置都没有时，沿用旧的全局订阅（单频道时期的行为）；
          · 配了频道之后，必须写清楚是哪个频道：「订阅 1234567890123456789」。
        """
        words = command_words()
        action = words.get("subscribe" if subscribe else "unsubscribe", "")
        group_openid = str(message.group_openid or "")
        argument = str(argument or "").strip()

        # 没配过任何频道 -> 老行为：这个群订阅"全部"（其实就是唯一那个频道）
        if not self.settings.guild_ids():
            if subscribe:
                self.subscribers.add(group_openid)
                await message.reply(content=text.text("messages.subscribe.plain_ok"))
            else:
                self.subscribers.discard(group_openid)
                await message.reply(content=text.text("messages.subscribe.plain_cancel"))
            return

        guild_id = self._guild_by_number(argument)
        if not guild_id:
            lines = [text.text("messages.subscribe.need_guild", action=action)]
            if argument:
                lines.append(
                    text.text("messages.subscribe.not_found", argument=argument)
                )
            choices = self._guild_choices()
            if choices:
                lines.append(text.text("messages.subscribe.choices"))
                lines.extend(choices)
            lines.append(text.text("messages.subscribe.example", action=action))
            await message.reply(content="\n".join(lines))
            return

        label = self._guild_label(guild_id)
        if subscribe:
            self.settings.add_subscriber(guild_id, group_openid)
        else:
            self.settings.remove_subscriber(guild_id, group_openid)

        mine = [
            self._guild_label(item)
            for item in self.settings.guild_ids()
            if group_openid in self.settings.subscribers_of(item)
        ]
        lines = [
            text.text(
                "messages.subscribe.done" if subscribe else "messages.subscribe.done_cancel",
                label=label,
            )
        ]
        if mine:
            lines.append(
                text.text(
                    "messages.subscribe.now", count=len(mine), labels="、".join(mine)
                )
            )
        elif not subscribe:
            lines.append(text.text("messages.subscribe.none"))
        await message.reply(content="\n".join(lines))

    # ------------------------------------------------------------------
    # 五、群信息 / 群成员（平台白名单接口，拿不到权限会降级）
    # ------------------------------------------------------------------
    async def _group_info_command(self, message, refresh=False):
        """「群信息」/「刷新群信息」。"""
        group_openid = str(message.group_openid or "")
        if refresh:
            await self._refresh_group_info(group_openid, with_members=True)
        lines = self.group_info.describe_lines(group_openid)
        lines.append("")
        lines.append(text.text("messages.group_info.notice"))
        await message.reply(content="\n".join(lines))

    async def _group_members_command(self, message):
        """「群成员」：列最近一次拉到的成员快照。"""
        group_openid = str(message.group_openid or "")
        if not self.group_info.available():
            await message.reply(
                content=text.text(
                    "messages.group_info.denied",
                    hint=self.group_info.denied_hint()
                    or text.text("messages.group_info.unavailable"),
                )
            )
            return
        if not self.group_info.members_of(group_openid):
            await self.group_info.fetch_members(group_openid)
        lines = self.group_info.member_lines(group_openid)
        snapshot = self.group_info.members_text(group_openid) or text.text(
            "labels.no_snapshot"
        )
        await message.reply(
            content=text.text("messages.group_info.members_title", snapshot=snapshot)
            + "\n"
            + "\n".join(lines)
        )

    async def _reconcile_command(self, message):
        """「对账」：立刻拿群成员名单比对一次，收回已退群者的权限。"""
        removed, hint = await self.access.reconcile()
        lines = [hint or text.text("messages.reconcile.done")]
        if removed:
            lines.append(
                text.text("messages.reconcile.removed_header", count=len(removed))
            )
            for user_id, reason in removed[:10]:
                lines.append(
                    text.text(
                        "messages.reconcile.removed_item",
                        user=str(user_id)[:10],
                        reason=reason,
                    )
                )
        lines.append(text.text("messages.reconcile.total", count=len(self.access)))
        if not self.group_info.available():
            lines.append("")
            lines.append(text.text("messages.reconcile.hint_unavailable"))
        await message.reply(content="\n".join(lines))

    # ------------------------------------------------------------------
    # 群消息互转
    # ------------------------------------------------------------------
    # 规则和开关在配置页「群消息互转」里配。这里只做一个查看入口，
    # 因为最容易出问题的是「今天还剩多少额度」—— 那个配额是平台硬限，
    # 撞上去整条消息就发不出去了（40034100）。
    async def _relay_status_command(self, message):
        """「转发状态」：看互转规则和今天的配额用量。"""
        data = self.settings.snapshot()
        lines = [
            text.text(
                "messages.relay.status_title",
                state=text.text(
                    "messages.relay.state_on"
                    if data.get("relay_enabled")
                    else "messages.relay.state_off"
                ),
            )
        ]
        lines.extend(self.relay.status_lines())
        await message.reply(content="\n".join(lines))

    # ------------------------------------------------------------------
    # 六、群成员进出：退群即收回权限
    # ------------------------------------------------------------------
    async def on_group_member_add(self, data):
        """新成员加入群（需要申请 GROUP_MEMBER_EVENT 事件权限）。"""
        await self.access.on_member_add(data)

    async def on_group_member_remove(self, data):
        """成员退出或被移出群 -> 立刻收回他在频道里的资料板块权限。

        这是回收权限的主要手段：平台没有「查询群成员」接口，
        只能靠这个事件知道人走了。
        """
        await self.access.on_member_leave(data)

    # ------------------------------------------------------------------
    # 七、群管理事件
    # ------------------------------------------------------------------
    async def on_group_add_robot(self, event):
        """机器人被拉进群 -> 自动订阅该群。"""
        _log.info(f"[botpy] 机器人被添加到群聊: {event.group_openid}")
        self.subscribers.add(event.group_openid)
        self.groups.remember(event.group_openid, source="被拉进群")

    async def on_group_del_robot(self, event):
        """机器人被移出群 -> 自动取消订阅。"""
        _log.info(f"[botpy] 机器人被移出群聊: {event.group_openid}")
        self.subscribers.discard(event.group_openid)
        self.groups.forget(event.group_openid)

    async def on_group_msg_receive(self, event):
        _log.info(f"[botpy] 群 {event.group_openid} 已开启机器人主动消息")

    async def on_group_msg_reject(self, event):
        _log.warning(f"[botpy] 群 {event.group_openid} 已关闭机器人主动消息，帖子将无法转发")


def _flush_index_on_terminate(client):
    """systemd 重启 = 发 SIGTERM。不接住它的话进程按默认行为直接死，
    消息索引最后几秒（节流写盘窗口内）的记录就丢了 —— 重启完
    引用那些消息「撤回/拉黑」全会报"没找到这条消息的记录"。

    做法是接住 SIGTERM、把索引强制写盘，然后**恢复默认处理并重发信号**：
    让进程仍按 systemd 期待的方式退出，只是死前把账记完。
    """
    def handler(signum, _frame):
        try:
            client.index.flush()
        except Exception:  # 退出路径上不能让任何异常拦住"继续死"这个动作
            pass
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    signal.signal(signal.SIGTERM, handler)


def main():
    """入口：读 config.yaml 里的 appid/secret 并启动。"""
    # 公域机器人：
    #   open_forum_event      频道发帖事件（帖子转发）
    #   public_messages       群/C2C 公域消息（群指令、验证码兑换）
    #   public_guild_messages 频道内 @机器人（领资料）
    #   direct_message        频道私信（频道里没有文字子频道时，这是唯一入口）
    #   group_member_event    群成员加入/退出（退群时收回板块权限）
    # 注意：direct_message 与 group_member_event 都需要在开放平台申请对应权限。
    intents = botpy.Intents(
        public_messages=True,
        public_guild_messages=True,
        direct_message=True,
        open_forum_event=True,
        group_member_event=True,
    )

    # 私域机器人：改用 forums（事件里直接带 thread_id，少一次接口调用）
    # intents = botpy.Intents(
    #     public_messages=True, public_guild_messages=True, direct_message=True,
    #     forums=True, group_member_event=True,
    # )

    client = ForumBot(intents=intents)
    _flush_index_on_terminate(client)
    try:
        client.run(appid=config.get_appid(), secret=config.get_secret())
    finally:
        # Ctrl+C（SIGINT）和正常退出走这里；SIGTERM 已在信号处理器里刷过
        try:
            client.index.flush()
        except Exception:
            pass


if __name__ == "__main__":
    main()
