# -*- coding: utf-8 -*-
"""频道帖子：反查帖子列表 + 生成帖子链接。

两个实测结论：
  1. OPEN_FORUM_THREAD_CREATE 事件只下发 guild_id / channel_id / author_id，
     没有 thread_id，所以必须调「获取子频道帖子列表」反查；
  2. 帖子链接 pd.qq.com/g/ 后面要跟的是**频道号**（频道头像 URL 里的数字），
     把事件里的 guild_id 放进去会退回站点首页。
"""
import asyncio
import datetime
import json

from botpy import logging

from . import config
from .inventory import parse_channel_number

__all__ = ["ChannelForum", "extract_text", "parse_date_time", "is_recent"]

_log = logging.get_logger()

# 反查帖子列表的失败重试次数 / 间隔（刚发帖时接口可能有同步或审核延迟）
LOOKUP_RETRY = 5
LOOKUP_RETRY_INTERVAL = 3


def parse_date_time(value):
    """解析接口里的 date_time（ISO 8601，可能带也可能不带时区）。

    解析不了返回 None —— 调用方自己决定"时间未知"时怎么处理。
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def is_recent(value, seconds):
    """这条时间戳是不是在最近 seconds 秒内。

    用来防止"事件把旧帖重新翻出来"：帖子楼层事件也会带 author_id，
    反查会取到那个人**最新的一条帖**，可能是几天前的旧帖。
    """
    parsed = parse_date_time(value)
    if parsed is None:
        return False
    now = datetime.datetime.now(datetime.timezone.utc)
    delta = (now - parsed).total_seconds()
    return -60 <= delta <= seconds


def extract_text(node) -> str:
    """把论坛富文本（title / content）转成纯文本。

    帖子标题/正文在不同接口里可能是：
      1. 富文本数组 [{"type": 1, "text_info": {"text": "xx"}}, ...]
      2. 上面的 JSON 字符串
      3. 已经是纯文本字符串
    这里统一处理，解析不出来就返回空串，不影响转发。

    **契约：永远返回 str**（调用方会拿去做 join / 字符串判断）。
    平台字段形态很杂，所以这里统一兜一层，宁可丢内容也不能抛异常。
    """
    text = _extract(node)
    return text if isinstance(text, str) else ("" if text is None else str(text))


def _extract(node):
    if node is None:
        return ""
    if isinstance(node, str):
        text = node.strip()
        if text[:1] in ("[", "{"):
            try:
                return _extract(json.loads(text))
            except Exception:
                return node
        return node
    if isinstance(node, (list, tuple)):
        return "".join(extract_text(item) for item in node)
    if isinstance(node, dict):
        # 容器节点：{"paragraphs": [...]} 或 {"elems": [...]}
        for key in ("paragraphs", "elems"):
            if key in node:
                return extract_text(node[key])
        elem_type = node.get("type")
        # type=2 是 at 信息。之前没处理，@机器人 会被解析成空串丢掉，
        # 导致"帖子里 @机器人 领资料"永远认不出来。
        # at_info.type：1=at 特定的人，2=at 身份组，3=at 频道所有人。
        # 这里 at 特定的人统一输出成 <@!id>，正好和 channel_message 的识别对齐。
        if "at_info" in node or elem_type == 2:
            info = node.get("at_info") or {}
            at_type = info.get("type")
            if at_type == 3:
                return " @everyone "
            if at_type == 2:
                role = (info.get("role_info") or {}).get("name") or "身份组"
                return f" @{role} "
            user = info.get("user_info") or {}
            uid = str(user.get("id") or "")
            if uid:
                return f" <@!{uid}> "
            return f" @{user.get('nick') or ''} "
        if "text_info" in node or "text" in node or elem_type == 1:
            info = node.get("text_info") or node.get("text") or {}
            if isinstance(info, str):
                return info
            if isinstance(info, dict):
                value = info.get("text")
                # text 理论上该是字符串，但实测字段形态很杂（可能是富文本数组），
                # 这里不能直接把 value 返回 —— 上层有 "".join()，返回非字符串会直接崩
                return value if isinstance(value, str) else extract_text(value)
            return extract_text(info)
        if "url_info" in node or elem_type == 3:
            info = node.get("url_info") or {}
            return info.get("display_text") or info.get("url") or ""
        if "emoji_info" in node or elem_type == 4:
            return "[表情]"
        if "channel_info" in node or elem_type == 5:
            info = node.get("channel_info") or {}
            return "#" + (info.get("channel_name") or "")
        # 兜底：遇到没见过的类型也尽量把里面的文字抠出来，别整段丢掉
        for key in ("text", "content", "display_text", "desc"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for value in node.values():
            if isinstance(value, (dict, list, tuple)):
                found = extract_text(value)
                if found:
                    return found
        return ""
    return ""


class ChannelForum:
    """负责「事件 -> 帖子 -> 链接」这一段。"""

    def __init__(self, api, link_template=None, channel_number=None, number_lookup=None,
                 cli=None):
        self._api = api
        self._link_template = link_template or config.get_link_template()
        # 配了就用配的，没配则运行时从频道头像解析（旧路径，单频道时期用的）
        self._channel_number = (channel_number if channel_number is not None
                                else config.get_channel_number())
        # 外部数据源：由 bot 注入，能拿到「按频道区分」的频道号
        # （频道配置里手填的，或频道同步时从 icon 自动解析出来的）
        self._number_lookup = number_lookup
        # 官方 CLI 的桥（channel_cli.ChannelCli）。**只用来取帖子短链**，
        # None 表示没有 —— 那就一直用长链模板，别的逻辑完全不受影响。
        self._cli = cli
        self._guild_scope = {}
        self._forwarded = set()

    # ---------------- 频道号 ----------------
    async def resolve_channel_number(self, guild_id):
        """取到 pd.qq.com/g/ 路径里能用的「频道号」。

        实测：事件里的 guild_id 直接放进 /g/{guild_id} 会退回站点首页（解析不出来），
        能解析的是频道头像 URL 里的数字号，例如 groupprohead.gtimg.cn/1234567890123456789/100。

        多频道下**必须按频道区分** —— 每个频道的频道号都不一样，用错了帖子链接
        会跳到别的频道或者打不开。取值顺序：

          1. number_lookup(guild_id)  按频道的数据源（配置手填 / 同步时自动解析）
          2. self._channel_number     config.yaml 里的全局值（单频道时期的遗留配置）
          3. get_guild(guild_id).icon 现问接口（一次调用，之后缓存）
          4. guild_id                 实在拿不到，退回旧行为
        """
        if guild_id in self._guild_scope:
            return self._guild_scope[guild_id]

        scope = ""
        if self._number_lookup:
            try:
                scope = str(self._number_lookup(guild_id) or "")
            except Exception as error:
                _log.warning(f"[botpy] 取频道号失败（guild={guild_id}）: {error}")
            if scope:
                _log.info(f"[botpy] guild_id={guild_id} -> 频道号={scope}（来自频道数据源）")
                self._guild_scope[guild_id] = scope
                return scope

        if self._channel_number:
            _log.info(f"[botpy] guild_id={guild_id} -> 频道号={self._channel_number}（config.yaml）")
            self._guild_scope[guild_id] = self._channel_number
            return self._channel_number

        try:
            info = await self._api.get_guild(guild_id=guild_id) or {}
            _log.info(f"[botpy] 频道信息: {info}")
            scope = parse_channel_number(info.get("icon"))
            if not scope:
                _log.warning("[botpy] 频道头像 URL 里没取到数字频道号，链接退回使用 guild_id")
        except Exception as error:
            _log.warning(f"[botpy] 获取频道信息失败，链接退回使用 guild_id: {error}")

        scope = scope or guild_id
        _log.info(f"[botpy] guild_id={guild_id} -> 网页链接频道标识={scope}")
        self._guild_scope[guild_id] = scope
        return scope

    def build_link(self, channel_number, guild_id, channel_id, thread_id):
        """帖子链接。

        客户端「分享」给出的 https://pd.qq.com/s/xxxxxxxx?b=2 是服务端随机短链，
        机器人侧拿不到；这里的网页版长链是等价形式（频道公开时实测同一条帖子）。
        格式可以在 config.yaml 的 link_template 里覆盖。
        """
        return self._link_template.format(
            channel_number=channel_number,
            guild_id=guild_id,
            channel_id=channel_id,
            thread_id=thread_id,
        )

    async def resolve_link(self, channel_number, guild_id, channel_id, thread_id):
        """帖子链接：优先短链，拿不到就退回长链模板。

        **短链是锦上添花，不是必需品。** 这里永远不抛异常、也不会因为
        CLI 没装 / 限流 / 登录过期而影响转发 —— 那些情况一律静默退回长链。
        取舍的完整说明见 channel_cli.py 的模块文档。
        """
        if self._cli is not None:
            try:
                url = await self._cli.share_url(thread_id)
            except Exception as error:
                # 桥内部已经把已知故障都吞掉了，这里只兜底防意外
                _log.warning(f"[botpy] 取短链时出现意外（{error}），退回长链")
                url = ""
            if url:
                return url
        return self.build_link(channel_number, guild_id, channel_id, thread_id)

    # ---------------- 帖子反查 ----------------
    async def fetch_content(self, channel_id, thread_id):
        """单独取一条帖子的正文。

        列表接口（get_threads）不一定返回 content，所以取不到时再查一次详情。
        帖子楼层事件（OPEN_FORUM_POST_CREATE）的下发体里**没有正文**，
        「帖子里 @机器人 领资料」完全靠这里把正文捞回来。
        """
        try:
            detail = await self._api.get_thread_detail(channel_id=channel_id, thread_id=thread_id)
        except Exception as error:
            _log.warning(f"[botpy] 取帖子详情失败 thread_id={thread_id}: {error}")
            return ""
        # 接口返回的可能是 ThreadInfo 本身，也可能是包了一层的 dict，两种都兜住
        info = detail if isinstance(detail, dict) else {}
        if isinstance(info.get("thread_info"), dict):
            info = info["thread_info"]
        text = extract_text(info.get("content"))
        _log.info(f"[botpy] 帖子 {thread_id} 详情正文: {text!r}")
        return text

    async def lookup_thread(self, channel_id, thread_id=None, author_id=None):
        """取到帖子的 thread_id / 标题 / 时间。

        用事件里给的 thread_id 精确匹配；没有就按 author_id 过滤，
        再按 date_time 倒序取最新的一条。失败会重试几次（可能有同步/审核延迟）。
        """
        for attempt in range(1, LOOKUP_RETRY + 1):
            resp = await self._api.get_threads(channel_id=channel_id)
            items = []
            for thread in (resp or {}).get("threads") or []:
                info = thread.get("thread_info") or {}
                if not info.get("thread_id"):
                    continue
                items.append(
                    {
                        "thread_id": info["thread_id"],
                        "title": extract_text(info.get("title")),
                        # 正文：列表接口有的版本会给，有的不给，collect() 里还有详情兜底
                        "content": extract_text(info.get("content")),
                        "date_time": info.get("date_time") or "",
                        "author_id": thread.get("author_id"),
                    }
                )

            # 1) 事件带了 thread_id：精确匹配
            if thread_id:
                for item in items:
                    if item["thread_id"] == thread_id:
                        return item
            else:
                # 2) 事件没带 thread_id：按作者过滤后取最新一条
                candidates = items
                if author_id:
                    same_author = [item for item in items if str(item["author_id"]) == str(author_id)]
                    if same_author:
                        candidates = same_author
                if candidates:
                    candidates = sorted(candidates, key=lambda item: item["date_time"], reverse=True)
                    return candidates[0]

            # 诊断信息：区分「没权限/没审核通过」和「字段解析不对」
            _log.warning(
                f"[botpy] 第 {attempt}/{LOOKUP_RETRY} 次未取到帖子 "
                f"(channel_id={channel_id}, author_id={author_id}, thread_id={thread_id}, "
                f"接口返回帖子数={len(items)}), 原始返回: {str(resp)[:300]}"
            )
            if attempt < LOOKUP_RETRY:
                await asyncio.sleep(LOOKUP_RETRY_INTERVAL)

        return None

    async def collect(self, guild_id, channel_id, author_id=None, thread_id=None):
        """事件 -> 一条可转发的帖子信息；反查不到或重复则返回 None。"""
        try:
            thread = await self.lookup_thread(channel_id, thread_id=thread_id, author_id=author_id)
        except Exception as error:
            _log.error(f"[botpy] 获取帖子列表失败 channel_id={channel_id}: {error}")
            return None

        if not thread:
            _log.warning(f"[botpy] 未在子频道 {channel_id} 找到对应帖子，忽略本次转发")
            return None

        thread_id = thread["thread_id"]
        if thread_id in self._forwarded:
            _log.debug(f"[botpy] 帖子 {thread_id} 已处理过，跳过")
            return None
        self._forwarded.add(thread_id)

        # 正文：列表里有就用，没有就补一次详情。
        # 转发只用到标题，但「加群解锁」要靠正文判断有没有 @机器人 说「领资料」。
        content = thread.get("content") or ""
        if not content:
            content = await self.fetch_content(channel_id, thread_id)

        channel_number = await self.resolve_channel_number(guild_id)
        # 短链优先，拿不到自动退回长链（见 resolve_link）
        link = await self.resolve_link(channel_number, guild_id, channel_id, thread_id)
        return {
            "title": thread["title"],
            "content": content,
            "link": link,
            "thread_id": thread_id,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "author_id": author_id or thread.get("author_id") or "",
            "date_time": thread.get("date_time") or "",
        }
