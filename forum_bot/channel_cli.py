# -*- coding: utf-8 -*-
"""tencent-channel-cli 的 Python 桥。

为什么需要它
============
机器人的帖子接口（botpy）拿不到两样东西：

    · 帖子**短链**（`pd.qq.com/s/xxx`）—— `ThreadInfo` 只有 4 个字段，没有 share_url
    · 帖子**评论区** —— 平台没给机器人开放评论接口，评论事件的下发体里
      也就几个 id、没有正文，等于完全够不着

官方 CLI 能拿到，但它用的是**扫码登录的用户身份**，跟机器人的 appid/secret
是两套东西。所以这里做一层桥：Python 调 CLI、解析输出，把「CLI 特有的毛病」
挡在业务代码外面 —— 业务代码只需要"拿到短链或拿不到"，不需要知道限流和扫码。

实测确认（2026-09）
==================
    `thread_id`（botpy 的叫法） == `feed_id`（CLI 的叫法）

    机器人日志：.../post/B_afaba76a8d5d04001441152192449888030X60
    CLI 返回  ：B_afaba76a8d5d04001441152192449888030X60

**同一串字符**，所以不用维护映射层，CLI 直接吃我们手上的 ID。

CLI 的五个毛病，以及这里的对策
==============================
1. **会限流**（retCode 153 / "接口调用已超过申请的频率上限"）
   官方 skill 的做法是 sleep 70 秒再重试。但机器人这边不能阻塞 70 秒 ——
   所以改成「**冷却期**」：撞上限流就记下解禁时间，冷却期内的调用**直接降级返回**，
   不排队、不等待；也绝不把 70 秒的等待传导到"转发一条帖子"这件事上。

2. **凭证会过期**（retCode 8011 / "未登录"）
   **这是最麻烦的一点**：过期后短链会静默退回长链，表面上"还能用"，
   没人会发现。所以这里把凭证失效单独标记出来 + 记 ERROR，让它出现在
   配置页的「运行日志」页里。

3. **输出里混着进度行**
   实测 stdout 长这样：
       ⏳ 用户已授权
       {"data":{...},"success":true}
   所以不能对整个输出做 json.loads，得**从里面挑出 JSON 那一段**。

4. **命令可能没装**（开发机上就没有）
   探测一次，找不到就永久标记不可用，**只记一次日志** ——
   否则每来一条新帖都刷一行 command not found。

5. **是 subprocess，不是 HTTP**
   用 run_in_executor 丢到线程池，不阻塞事件循环 —— 顺带避开
   Windows 上只有 ProactorEventLoop 才能用 create_subprocess_exec 的坑。

缓存
====
短链对一条帖子是**固定**的，所以拿到就永久缓存（forum_feed_links.json）。

**缓存的意义不只是省调用** —— CLI 挂了、限流了、凭证过期了，
缓存过的帖子还能继续用短链。这是这套东西最重要的降级手段。
"""
import asyncio
import json
import os
import shutil
import subprocess
import threading
import time

from botpy import logging

from . import config

__all__ = ["ChannelCli", "extract_json", "parse_guild_list"]

_log = logging.get_logger()

# CLI 命令名。装在服务器的 PATH 里（npm i -g 之后是个 Go 二进制）
DEFAULT_COMMAND = "tencent-channel-cli"

# 单次调用最多等多久。CLI 是本地二进制 + 一次网络请求，20 秒足够；
# 卡住超过这个时间宁可降级，也不要拖着转发的流程。
DEFAULT_TIMEOUT = 20

# 撞上限流后的冷却秒数。官方 skill 让 sleep 70 秒再重试 ——
# 这里只用来记"解禁时间"，不真的等待。
RATE_LIMIT_COOLDOWN = 70

# 认这两类特殊错误的特征串。普通失败（帖子被删了之类）重试没用，
# 但它们俩一个有时间解禁、一个要人重新扫码，处理方式完全不同。
_RATE_LIMIT_HINTS = ("频率上限", "频率限制", "153")
_AUTH_HINTS = ("8011", "未登录", "请先登录", "登录已过期", "重新登录")

# 「查询扫码结果」用的单独超时。
# 官方文档那句"扫码完成后会立即返回"暗示 **还没扫码时它会阻塞等待授权** ——
# 真是这样的话，用默认的 20 秒会让配置页每次轮询都干等 20 秒。
# 所以给它一个短超时，**超时按"还没扫码"处理**（是 pending，不是故障）。
LOGIN_POLL_TIMEOUT = 8

# 认出"还在等扫码"的措辞。CLI 各阶段的文案不完全一样，
# 但只要不是明确的报错，就按"还没好"处理 —— 对轮询来说这是安全的默认。
_PENDING_HINTS = ("等待", "待授权", "未授权", "请扫码", "pending", "授权中", "已过期")

# 认出"根本没有待授权的登录流程了"。CLI 的原话长这样：
#     read state /root/.qqcli/device_auth_state.json:
#     open .../device_auth_state.json: no such file or directory
# 这**不是错误** —— 它只说明没得可查了（多半是上一次已经成功、
# state 文件被消费掉了）。所以单独归一类，别让页面弹红字。
_STATE_GONE_HINTS = ("no such file", "读取授权状态失败", "state 文件")


def extract_json(text):
    """从 CLI 的输出里挑出 JSON 对象。挑不出来返回 None。

    实测 stdout 会混着进度行：

        ⏳ 用户已授权
        {"data":{"status":"authorized"},"success":true}

    所以不能对整个输出做 json.loads。策略是**从后往前**逐行试 ——
    JSON 总在最后，从后往前既快，也不容易被进度行里的字符带偏。
    """
    if not text:
        return None
    raw = str(text).strip()
    try:
        return json.loads(raw)
    except Exception:
        pass

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    for line in reversed(lines):
        if not line.startswith("{"):
            continue
        try:
            return json.loads(line)
        except Exception:
            continue

    # 兜底：JSON 可能和进度行挤在同一行，从最后一个 '{' 截断再试
    index = raw.rfind("{")
    if index >= 0:
        try:
            return json.loads(raw[index:])
        except Exception:
            return None
    return None


# ----------------------------------------------------------------------
# 「我加入的频道」的返回体解析
# ----------------------------------------------------------------------
# `manage get-my-join-guild-info` **没有公开文档**，返回体长什么样只能靠试。
#
# 实测（2026-09）它把一个号的频道**分成三类**放，不是一个大列表：
#     created_guilds   我创建的
#     joined_guilds    我加入的
#     managed_guilds   我管理的
#     total_count      总数
#
# **三类都要收，然后按 id 合并** —— 自己创建的频道通常也会出现在 joined 里，
# 分开返回会让上层看到同一个频道好几遍。
#
# 为什么宁可返回空也不硬猜：猜错了会在页面上列出一个**机器人其实不在**的频道，
# 用户按它去配转发，最后一条都发不出去 —— 那比显示"认不出结构"糟得多。
# 认不出来时调用方会把**实际字段名**显示出来，照着补这里就行。
_GUILD_LIST_KEYS = (
    "created_guilds", "joined_guilds", "managed_guilds",
    # 兜底：换个版本可能叫这些
    "guilds", "list", "items", "records", "guild_list", "guildList",
)

# 列表字段名 -> 显示给用户看的身份标签
_GUILD_RELATIONS = {
    "created_guilds": "我创建的",
    "joined_guilds": "我加入的",
    "managed_guilds": "我管理的",
}

_GUILD_ID_KEYS = (
    "guild_id", "guildId", "guild_openid", "guildOpenid", "id",
)
_GUILD_NAME_KEYS = (
    "guild_name", "guildName", "name", "guild_nick", "nickname", "nick",
)


def parse_guild_list(data):
    """从 CLI 返回里抠出「我加入的频道」。

    返回 [{"guild_id", "name", "relations"}, ...]：
    `relations` 是"这个号以什么身份在里面"，比如 `["我创建的", "我加入的"]`。

    三类列表（created / joined / managed）**合并去重**；认不出结构就返回
    **空列表**（而不是猜一个），调用方据此把原始字段名亮出来。
    """
    if isinstance(data, list):
        sources = [("", data)]
    elif isinstance(data, dict):
        sources = [
            (key, data[key])
            for key in _GUILD_LIST_KEYS
            if isinstance(data.get(key), list)
        ]
        if not sources:
            # 也许它本身就是一条频道记录（只有一个频道时会出现）
            if any(key in data for key in _GUILD_ID_KEYS):
                sources = [("", [data])]
    else:
        return []

    merged = {}
    for key, items in sources:
        relation = _GUILD_RELATIONS.get(key, "")
        for item in items:
            if not isinstance(item, dict):
                continue
            guild_id = ""
            for id_key in _GUILD_ID_KEYS:
                if item.get(id_key):
                    guild_id = str(item[id_key])
                    break
            if not guild_id:
                continue
            entry = merged.setdefault(
                guild_id, {"guild_id": guild_id, "name": "", "relations": []}
            )
            # 名字取第一个非空的：同一个频道在两类里的字段可能对不齐
            if not entry["name"]:
                for name_key in _GUILD_NAME_KEYS:
                    if item.get(name_key):
                        entry["name"] = str(item[name_key])
                        break
            if relation and relation not in entry["relations"]:
                entry["relations"].append(relation)

    return list(merged.values())


class ChannelCli:
    """CLI 的调用封装。失败一律返回空值 / None，不抛异常。

    这样调用方只需要写"拿不到就退回老办法"，不用到处 try。
    """

    def __init__(self, command=None, link_path=None, timeout=None, enabled=None,
                 home=None):
        """
        enabled  可调用对象，每次要调 CLI 时问一次"现在开着吗"。
                 这样用户在配置页关掉开关**立刻生效**，不用重启机器人。
                 传 None 表示一直开着。
        home     CLI 的 HOME（凭证和登录状态都在 `~/.qqcli/` 下）。
                 不传就自动推断 —— **这块必须显式处理，不能指望环境变量**，
                 那个坑完整记在 _child_env 里。
                 对应 config.yaml 的可选项 cli_home。
        """
        self._command = str(command or DEFAULT_COMMAND)
        self._link_path = link_path or config.CLI_LINK_FILE
        self._timeout = int(timeout or DEFAULT_TIMEOUT)
        self._enabled = enabled
        self._home = str(home or "").strip()

        # 所有 CLI 调用**串行**：并发调用是最容易撞限流的做法。
        # 用线程锁而不是 asyncio.Lock —— 因为有两个调用方（机器人的事件循环
        # 和配置页的请求线程），线程锁对两边都成立。见 _execute 的说明。
        self._lock = threading.Lock()
        self._cooldown_until = 0.0
        self._auth_error = ""
        self._missing = False
        self._last_error = ""
        # 上一次调用的结果类别（ok / missing / timeout / error），
        # 给 login_poll 判断"是超时还是真出错"用
        self._last_kind = ""

        # 扫码登录的进行状态（配置页据此显示"等待扫码 / 已过期"）
        self._login_pending = False
        self._login_expires = 0.0

        self._links = self._load_links()

    def _is_enabled(self):
        """配置页那个开关现在是开着的吗。

        读失败按**关闭**处理：设置文件都读不出来了，这时候宁可退回长链，
        也不要继续去调一个外部命令。
        """
        if self._enabled is None:
            return True
        try:
            return bool(self._enabled())
        except Exception as error:
            _log.warning(f"[botpy] 读短链开关失败（{error}），本次按关闭处理")
            return False

    # ------------------------------------------------------------------
    # 短链缓存
    # ------------------------------------------------------------------
    def _load_links(self):
        try:
            with open(self._link_path, "r", encoding="utf-8") as file:
                data = json.load(file)
        except Exception:
            return {}
        links = data.get("links") if isinstance(data, dict) else None
        if not isinstance(links, dict):
            return {}
        result = {}
        for key, value in links.items():
            if isinstance(value, dict) and value.get("url"):
                result[str(key)] = {
                    "url": str(value["url"]),
                    "at": float(value.get("at") or 0),
                }
        return result

    def _save_links(self):
        temp = self._link_path + ".tmp"
        try:
            with open(temp, "w", encoding="utf-8") as file:
                json.dump({"links": self._links}, file, ensure_ascii=False, indent=2)
            os.replace(temp, self._link_path)
        except Exception as error:
            _log.warning(f"[botpy] 短链缓存写入失败: {error}")

    def cached_link(self, feed_id):
        entry = self._links.get(str(feed_id or "")) or {}
        return str(entry.get("url") or "")

    def remember_link(self, feed_id, url):
        feed_id, url = str(feed_id or ""), str(url or "")
        if not feed_id or not url:
            return
        self._links[feed_id] = {"url": url, "at": time.time()}
        self._save_links()

    def link_count(self):
        return len(self._links)

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def status(self):
        """本地状态，**不调接口**（给启动日志和配置页用）。"""
        now = time.time()
        left = int(self._cooldown_until - now)
        login_left = int(self._login_expires - now)
        waiting = self._login_pending and login_left > 0
        available = (
            not self._missing and not self._auth_error and left <= 0 and not waiting
        )
        if self._missing:
            note = f"没找到 {self._command} 命令"
        elif self._auth_error:
            note = f"登录凭证失效（{self._auth_error}）—— 需要在服务器上重新扫码登录"
        elif waiting:
            note = f"正在等待扫码授权（还剩 {login_left} 秒）"
        elif left > 0:
            note = f"触发频率限制，冷却中（还剩 {left} 秒）"
        else:
            note = self._last_error or "就绪"
        return {
            "command": self._command,
            "available": available,
            "installed": not self._missing,
            "logged_in": not self._auth_error,
            "cooldown_left": max(0, left),
            "auth_error": self._auth_error,
            "last_error": self._last_error,
            "cached_links": len(self._links),
            # 扫码登录的进行状态 —— 配置页据此显示二维码和倒计时
            "login_pending": waiting,
            "login_expires_in": max(0, login_left),
            "note": note,
        }

    def describe(self):
        """一句话概况（启动日志用）。"""
        if self._missing:
            return f"tencent-channel-cli 不可用（没找到 {self._command} 命令），短链将退回长链"
        if self._auth_error:
            return (
                f"tencent-channel-cli 登录凭证失效（{self._auth_error}），"
                f"短链将退回长链 —— 需要在服务器上重新扫码登录"
            )
        if self._login_pending and self._login_expires > time.time():
            return "tencent-channel-cli 正在等待扫码授权"
        return f"tencent-channel-cli 就绪，已缓存 {len(self._links)} 条短链"

    def probe_sync(self):
        """真跑一次命令确认能用，返回 status()。

        **同步版**：配置页的「检测」按钮直接调（网页线程没法 await）。
        """
        if not shutil.which(self._command):
            if not self._missing:
                _log.warning(
                    f"[botpy] 没找到 {self._command} 命令 —— "
                    f"短链功能会自动退回长链（装好后重启即可生效）"
                )
            self._missing = True
            return self.status()
        # 允许恢复：命令可能是刚装上的、冷却也可能早就过去了。
        # 不重置的话，"没装过"和"限流中"会一直粘着 —— 用户装好了、
        # 等够了，点「检测」还是不好，那太蠢了。
        self._missing = False
        self._cooldown_until = 0.0
        # 用「我的频道列表」试：不需要任何业务参数，最省事的连通性探针
        self._execute(["manage", "get-my-join-guild-info"])
        return self.status()

    async def probe(self):
        """异步版探针（机器人启动时自检用）。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.probe_sync)

    def my_guilds(self):
        """「我加入的频道」—— **扫码登录的那个 QQ 号**的频道列表。

        返回 (列表, 出错信息, 原始字段名, 命令行报的总数)：

          · 列表：[{"guild_id", "name", "relations"}, ...]，
            created / joined / managed 三类**已合并去重**
          · 认不出结构时列表为空，但**原始字段名**会一起带回来 ——
            页面把它显示出来，照着补 parse_guild_list 就行。
            总比显示个空列表让人以为是"这个号没加任何频道"强
          · 总数：CLI 自己报的 `total_count`。**拿来对账用** ——
            如果我们解析出来的条数和它对不上，说明漏抄了某一类，
            页面上会提示（静默少数几个频道是最难发现的）
        """
        if not shutil.which(self._command):
            self._missing = True
            return [], f"没找到 {self._command} 命令", [], 0
        payload = self._execute(["manage", "get-my-join-guild-info"])
        if not isinstance(payload, dict):
            return [], (self._auth_error or self._last_error or "调用 CLI 失败"), [], 0
        if payload.get("success") is False:
            return [], self._error_message(payload), [], 0

        data = payload.get("data")
        total = 0
        if isinstance(data, dict):
            keys = sorted(str(key) for key in data)
            try:
                total = int(data.get("total_count") or 0)
            except (TypeError, ValueError):
                total = 0
        elif isinstance(data, list):
            keys = ["（顶层直接是一个数组）"]
        else:
            keys = []
        return parse_guild_list(data), "", keys, total

    # ------------------------------------------------------------------
    # 取数
    # ------------------------------------------------------------------
    async def feed_detail(self, feed_id):
        """取帖子详情，返回 `data.feed` 字典；失败返回 {}。

        里面除了 `share_url`，还有 `content` 正文和 `comment_count` 评论数。
        """
        feed_id = str(feed_id or "")
        if not feed_id:
            return {}
        data = await self._run(["feed", "get-feed-detail", "--feed-id", feed_id])
        if not isinstance(data, dict):
            return {}
        feed = data.get("feed")
        return feed if isinstance(feed, dict) else {}

    async def share_url(self, feed_id, refresh=False):
        """取帖子短链。优先用缓存，没有才调 CLI。失败返回空串。

        **这是给业务代码用的入口** —— 它保证：无论 CLI 出什么问题
        （没装 / 限流 / 凭证过期 / 超时），都只是返回空串，让调用方
        安心地退回长链，不会被异常打断转发的流程。
        """
        feed_id = str(feed_id or "")
        if not feed_id:
            return ""
        # 开关关掉就整个不用 —— **包括缓存**。
        # 用户关它就是想看长链，这时候还塞个短链过去只会让人困惑。
        if not self._is_enabled():
            return ""
        if not refresh:
            cached = self.cached_link(feed_id)
            if cached:
                return cached

        feed = await self.feed_detail(feed_id)
        url = str(feed.get("share_url") or "")
        if url:
            self.remember_link(feed_id, url)
            _log.info(f"[botpy] 取到帖子短链 {feed_id[:16]}… -> {url}")
            return url
        return ""

    # ------------------------------------------------------------------
    # 调用
    # ------------------------------------------------------------------
    @staticmethod
    def _error_message(payload):
        """从失败返回里抠出一句人话（给界面显示用）。"""
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            for key in ("message", "hint"):
                value = str(error.get(key) or "").strip()
                if value:
                    return value
        return str(payload)

    @classmethod
    def _classify_error(cls, payload):
        """认出「限流」和「凭证失效」这两类特殊错误。"""
        message = cls._error_message(payload)
        for hint in _RATE_LIMIT_HINTS:
            if hint in message:
                return "rate_limit", message
        for hint in _AUTH_HINTS:
            if hint in message:
                return "auth", message
        return "other", message

    def _child_env(self):
        """给 CLI 子进程准备环境变量 —— **核心是把 HOME 补上**。

        这个坑很难查，完整记一下（线上实测踩过）：

        systemd 起的服务，如果 unit 里**没写 `User=`**，进程环境里就没有 `HOME`
        （systemd 只在指定了 `User=` 时才顺手设置 $HOME）。而 tencent-channel-cli
        要往 `~/.qqcli/` 写授权码和登录状态，拿不到 HOME 就直接报：

            无法确定 state 文件路径（HOME 不可用）

        于是现象非常迷惑，三个都对得上：

          · **手动在终端跑一切正常**（终端有 HOME），机器人里跑就报错；
          · `login` 居然**能成功** —— 它只申请授权码，不用读 state 文件；
            只有 `poll-token` 会炸。所以看起来像"二维码出来了、手机上
            也授权了、但就是查不到结果"；
          · 短链一直退回长链。

        顺带还保证另一件事：凭证在 `~/.qqcli/` 下，机器人必须和**扫码登录时
        用的那个账号**看到同一个 HOME，否则会出现"明明登录过了却说没登录"。
        所以配置里显式写了 home（config.yaml 的 cli_home）就一切以它为准。
        """
        env = dict(os.environ)
        if self._home:
            env["HOME"] = self._home
            return env
        if not env.get("HOME"):
            home = os.path.expanduser("~")
            # expanduser 查不到用户时会**原样返回 "~"**，那种情况退回 /root
            env["HOME"] = home if home and home != "~" else "/root"
        return env

    def _run_blocking(self, args, timeout=None):
        """真正的 subprocess 调用。返回 (kind, stdout, stderr)。

        timeout 可以单独指定 —— 登录轮询要用比默认更短的超时，见 LOGIN_POLL_TIMEOUT。
        """
        command = [self._command] + list(args) + ["--json"]
        try:
            done = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self._timeout,
                # **必须显式给 env**：systemd 起的进程可能没有 HOME，
                # 而这个 CLI 没 HOME 就直接跑不起来（见 _child_env）
                env=self._child_env(),
            )
        except FileNotFoundError:
            return "missing", "", ""
        except subprocess.TimeoutExpired:
            return "timeout", "", ""
        except Exception as error:
            return "error", "", str(error)
        return "ok", done.stdout or "", done.stderr or ""

    def _execute(self, args, classify=True, timeout=None):
        """同步执行一次 CLI，返回**整个 payload**（含 success / error）；失败返回 None。

        **这是唯一碰 subprocess 的地方。** 内核做成同步的，是因为有两个调用方：

          · 机器人的事件循环 —— 走 _run()，丢到线程池里跑，不阻塞；
          · 配置页的请求线程 —— 直接同步调。配置页跑在 ThreadingHTTPServer 上，
            每个请求一个线程，阻塞一两秒完全没关系；而做成异步的话，
            网页线程根本 await 不了（这正是「点击登录」能在页面上做的前提）。

        `classify=False` 时**不做鉴权失效判定**，也不提前挡掉调用 ——
        登录流程要用它：那时候"未登录"正是预期状态，把它记成"凭证失效"
        会让页面上一直挂着一条假的错误。
        """
        if self._missing:
            return None
        if classify and self._auth_error:
            return None

        with self._lock:
            # 冷却检查放在锁里：并发进来的第二个请求要能看到第一个刚设的冷却
            now = time.time()
            if now < self._cooldown_until:
                left = int(self._cooldown_until - now)
                _log.info(f"[botpy] CLI 正在限流冷却（还剩 {left} 秒），本次跳过")
                self._last_kind = "cooldown"
                return None
            kind, stdout, stderr = self._run_blocking(args, timeout)
            self._last_kind = kind

        if kind == "missing":
            self._missing = True
            # 只记一次：之后 _execute 会在开头直接返回，不会刷屏
            _log.warning(
                f"[botpy] 没找到 {self._command} 命令 —— 短链功能退回长链"
                f"（装上 tencent-channel-cli 并重启即可生效）"
            )
            return None
        if kind == "timeout":
            self._last_error = f"调用超时（{timeout or self._timeout} 秒）"
            _log.warning(f"[botpy] CLI 调用超时（{' '.join(args)}），本次跳过")
            return None
        if kind == "error":
            self._last_error = f"调用失败：{stderr}"
            _log.warning(f"[botpy] CLI 调用失败（{' '.join(args)}）: {stderr}")
            return None

        payload = extract_json(stdout)
        if payload is None:
            payload = extract_json(stderr)
        if not isinstance(payload, dict):
            self._last_error = "输出里没有可解析的 JSON"
            _log.warning(
                f"[botpy] CLI 输出无法解析（{' '.join(args)}）：{stdout[:200]!r} {stderr[:200]!r}"
            )
            return None

        if payload.get("success") is False and classify:
            kind, message = self._classify_error(payload)
            if kind == "rate_limit":
                self._cooldown_until = time.time() + RATE_LIMIT_COOLDOWN
                self._last_error = f"接口频率限制：{message}"
                _log.warning(
                    f"[botpy] CLI 触发接口频率限制（{message}）—— "
                    f"{RATE_LIMIT_COOLDOWN} 秒内不再调用"
                )
            elif kind == "auth":
                self._auth_error = message
                # 用 ERROR 而不是 WARNING：凭证失效后短链会**静默**退回长链，
                # 这是最容易"用了半个月才发现"的一类问题，必须显眼
                _log.error(
                    f"[botpy] tencent-channel-cli 登录凭证失效（{message}）—— "
                    f"短链将退回长链。可以到配置页「CLI 登录」页面点一下扫码登录"
                )
            else:
                self._last_error = message
                _log.warning(f"[botpy] CLI 返回失败（{' '.join(args)}）：{message}")
            return None

        if payload.get("success") is not False:
            # 成功一次就把之前的错误擦掉 —— 这样「检测」按钮点一下就能让
            # 粘住的状态（凭证失效 / 上次失败）恢复，不用重启机器人
            self._last_error = ""
            self._auth_error = ""
        return payload

    async def _run(self, args):
        """异步执行，成功返回 `data` 段的内容，失败返回 None。

        **不抛异常** —— 调用方的逻辑统一是"拿不到就降级"。
        """
        if not self._is_enabled():
            return None
        loop = asyncio.get_running_loop()
        payload = await loop.run_in_executor(None, self._execute, list(args))
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        return data if data is not None else {}

    # ------------------------------------------------------------------
    # 扫码登录（配置页点按钮走这里）
    # ------------------------------------------------------------------
    def login_start(self, force=False):
        """发起扫码登录，返回 (data, 出错信息)。

        成功时 data 里有三个字段，正是配置页要的：

            verification_uri   手机打开就能授权的链接（最省事，不必用二维码）
            qr_code            二维码 PNG 的 **base64**，直接塞进
                               `<img src="data:image/png;base64,...">` 就能显示
            expires_in_s       有效期（实测约 600 秒）
        """
        if not shutil.which(self._command):
            self._missing = True
            return {}, f"没找到 {self._command} 命令，请先装好 tencent-channel-cli"
        # 登录期间"未登录"是预期状态 —— classify=False，别记成凭证失效
        args = ["login", "--yes"] if force else ["login"]
        payload = self._execute(args, classify=False)
        if not isinstance(payload, dict):
            return {}, self._last_error or "调用 CLI 失败"
        if payload.get("success") is False:
            return {}, self._error_message(payload)
        data = payload.get("data")
        data = data if isinstance(data, dict) else {}
        self._login_pending = True
        self._login_expires = time.time() + float(data.get("expires_in_s") or 600)
        _log.info("[botpy] 已发起 tencent-channel-cli 扫码登录，等待授权")
        return data, ""

    def login_poll(self):
        """查询扫码结果，返回 (status, 说明)。

        status 取值：
            authorized  登录成功，凭证已更新（顺手清掉之前的"凭证失效"标记）
            pending     还没扫 / 还没授权
            expired     二维码过期了，得重新发起
            idle        **没有正在进行的登录**（多半是上一次已经成功了）
            error       其它失败
        """
        if not self._login_pending:
            # 没有待授权的登录流程 —— **直接返回，压根不要调 CLI**。
            #
            # 这一条很关键，线上踩过：
            # 读 state 文件的那个接口，在"没有待授权流程"时会报
            #     read state .../device_auth_state.json: no such file or directory
            # 而**一次成功的 poll 会把 state 文件消费掉** ——
            # 所以"成功之后紧接着的那次轮询"必然撞上它。
            #
            # 以前这里把它归成 error 显示出来，结果用户刚看到「✅ 登录成功」，
            # 下一秒页面上就冒出几条红字「查询登录结果失败」，以为白扫了、
            # 又去点一次扫码 —— 其实那根本不是错误，是**流程已经结束了**。
            return "idle", ""

        payload = self._execute(
            ["login", "poll-token"], classify=False, timeout=LOGIN_POLL_TIMEOUT
        )
        if not isinstance(payload, dict):
            if self._last_kind == "timeout":
                # 还没扫码时 poll-token 很可能会**阻塞等授权**，超时属于正常现象，
                # 不是故障 —— 当成"还没扫"继续轮询就好。
                # （这正是给它单独一个短超时的原因。）
                return "pending", "等待扫码授权"
            return "error", self._last_error or "调用 CLI 失败"

        if payload.get("success") is False:
            message = self._error_message(payload)
            if "过期" in message or "已被领取" in message:
                self._login_pending = False
                self._login_expires = 0.0
                return "expired", message
            if any(hint in message for hint in _PENDING_HINTS):
                return "pending", message
            if (_STATE_GONE_HINTS and
                    any(hint in message for hint in _STATE_GONE_HINTS)):
                # state 文件没了 = 已经**没有待授权的流程**了
                # （正常结束、被消费掉、或被别的进程取消了）。
                # 归类成 idle 而不是 error —— 它不代表出错，只代表"没得可查了"。
                self._login_pending = False
                self._login_expires = 0.0
                return "idle", message
            # 其它错误：这次登录**已经废了**。
            # 判据：只要 state 文件能读，poll-token 就只会回 pending/authorized；
            # 回别的说明状态本身出了问题（实测最常见的就是"HOME 不可用"）。
            # 不清掉的话，页面会一直挂着"正在等待扫码授权"，而且 CLI 会被判成
            # "不可用"再也不去用 —— 明明凭证是好的。
            self._login_pending = False
            self._login_expires = 0.0
            return "error", message

        data = payload.get("data")
        data = data if isinstance(data, dict) else {}
        status = str(data.get("status") or "")
        if status == "authorized":
            # 登录成功：把之前记下的"凭证失效"和其它错误一次清干净，
            # 短链功能**立刻**恢复，不用重启机器人
            self._login_pending = False
            self._login_expires = 0.0
            self._auth_error = ""
            self._last_error = ""
            _log.info("[botpy] tencent-channel-cli 扫码登录成功，凭证已更新")
            return "authorized", str(data.get("message") or "登录成功")
        return "pending", str(data.get("message") or "等待扫码授权")

    def cancel_login(self):
        """放弃本次扫码（配置页的「取消」按钮）。"""
        self._login_pending = False
        self._login_expires = 0.0
