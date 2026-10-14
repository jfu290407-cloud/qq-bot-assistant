# -*- coding: utf-8 -*-
"""转发策略：把帖子按设置攒起来再推送。

三种策略（可在配置页随时切换，改完立即生效）：
    instant  收到就推
    batch    攒够 batch_count 条立即推；攒不够也会在 batch_max_wait_seconds 秒后推
    digest   每 digest_interval_minutes 分钟汇总推一次

另外支持免打扰时段（时段内只攒不发）、文案模板、超长自动拆分、
Markdown 卡片 + 「查看帖子」跳转按钮。

多频道
------
机器人可以同时加多个频道，**每个频道有自己的推送策略、文案和目标群**。
所以：

  · 缓冲区是一份，但推送时按 guild_id 分组，每组用自己的设置渲染；
  · 目标群也按频道算（settings.targets），A 频道的帖子绝不会推给 B 频道的群；
  · 免打扰、汇总周期都是每频道各算各的（各自一个 deadline / 上次推送时间）。

等待时间不再用"一个全局定时器"，而是每个频道记一个 deadline，由一个
每 5 秒的 tick 统一检查 —— 否则 A 频道的定时器会顺手把 B 频道的帖子也提前推了。
"""
import asyncio
import time
from datetime import datetime

from botpy import logging

from .settings import TITLE_PLACEHOLDERS

__all__ = ["Forwarder", "chunk_lines"]

_log = logging.get_logger()

# 多久检查一次「有没有频道的等待时间到了」
TICK_SECONDS = 5

# digest 模式检查间隔（每个频道按自己的周期决定是否需要推）
DIGEST_TICK_SECONDS = 60


def chunk_lines(lines, limit):
    """把若干段文案合并成不超过 limit 字符的消息列表。"""
    chunks = []
    current = ""
    for line in lines:
        # 单条本身就超长：直接按长度硬拆
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(line[index:index + limit] for index in range(0, len(line), limit))
            continue

        candidate = "%s\n\n%s" % (current, line) if current else line
        if current and len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


class Forwarder:
    def __init__(self, client, settings, subscribers, message_index):
        self._client = client
        self._settings = settings
        self._subscribers = subscribers
        self._index = message_index
        self._buffer = []
        # guild_id -> 最早该推送的时间戳（batch 等待 / 免打扰结束）
        self._due = {}
        # guild_id -> 上次 digest 推送时间
        self._last_digest = {}

    # ---------------- 收集 ----------------
    @staticmethod
    def _guild_of(post):
        return str((post or {}).get("guild_id") or "")

    @classmethod
    def _group_by_guild(cls, posts):
        groups = {}
        for post in posts:
            groups.setdefault(cls._guild_of(post), []).append(post)
        return groups

    def _pending_of(self, guild_id):
        return [item for item in self._buffer if self._guild_of(item) == guild_id]

    def submit(self, post):
        guild_id = self._guild_of(post)
        # 过滤/策略都按这条帖子所属的频道来判定
        reason = self._settings.filter_reason(post.get("title"), guild_id)
        if reason:
            _log.info(f"[botpy] 帖子被过滤（{reason}）：{post.get('title')!r}")
            return

        data = self._settings.snapshot(guild_id)
        mode = data["mode"]

        if mode == "instant":
            # 即时推送：这条单独发，不和其它帖子合并
            _log.info(f"[botpy] 即时推送：{post.get('title')!r}")
            self._client.loop.create_task(self._flush_posts([post]))
            return

        self._buffer.append(post)
        if mode == "batch":
            same = self._pending_of(guild_id)
            if len(same) >= int(data["batch_count"]):
                # 这个频道攒够了 -> 立刻只推它。
                # 不能顺手把别的频道的帖子也推出去：它们可能还在攒，或者是 digest 模式。
                self._buffer = [
                    item for item in self._buffer if self._guild_of(item) != guild_id
                ]
                _log.info(
                    f"[botpy] 频道 {guild_id or '(未知)'} 攒够 "
                    f"{len(same)} 条，立即推送"
                )
                self._client.loop.create_task(self._flush_posts(same))
                return
            # 攒不够也最多等这么久
            wait = max(1, int(data["batch_max_wait_seconds"]))
            deadline = time.time() + wait
            current = self._due.get(guild_id)
            self._due[guild_id] = min(current, deadline) if current else deadline
        # digest 模式交给周期任务，这里只攒

        _log.info(
            f"[botpy] 帖子进入缓冲区（频道={guild_id or '(未知)'}, 模式={mode}, "
            f"当前 {len(self._buffer)} 条）：{post.get('title')!r}"
        )

    # ---------------- 定时 ----------------
    def _schedule_flush(self, delay, guild_id=""):
        deadline = time.time() + max(1, delay)
        current = self._due.get(guild_id)
        self._due[guild_id] = min(current, deadline) if current else deadline

    async def batch_loop(self):
        """到点了就推（batch 的等待、免打扰结束都走这里，每频道各算各的）。"""
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                await self.flush_due()
            except Exception as error:
                _log.warning(f"[botpy] 定时推送任务异常: {error}")

    async def flush_due(self):
        now = time.time()
        for guild_id, deadline in list(self._due.items()):
            if now < deadline:
                continue
            self._due.pop(guild_id, None)
            await self.flush(guild_id=guild_id)

    async def digest_loop(self):
        """digest 模式下的周期任务，**每个频道各算各的周期**。"""
        while True:
            await asyncio.sleep(DIGEST_TICK_SECONDS)
            try:
                await self.flush_digest_due()
            except Exception as error:
                _log.warning(f"[botpy] 汇总推送任务异常: {error}")

    async def flush_digest_due(self):
        now = time.time()
        for guild_id in list(self._group_by_guild(self._buffer)):
            data = self._settings.snapshot(guild_id)
            if str(data.get("mode")) != "digest":
                continue
            interval = max(1, int(data.get("digest_interval_minutes") or 30)) * 60
            # 第一次见到这个频道时先记个时间起点，避免刚启动就立刻推一次
            started = self._last_digest.setdefault(guild_id, now)
            if now - started < interval:
                continue
            self._last_digest[guild_id] = now
            await self.flush(guild_id=guild_id)

    # ---------------- 推送 ----------------
    async def flush(self, force=False, guild_id=None):
        """把缓冲区里的帖子推出去，返回推送条数。

        guild_id 为空推全部（各频道按自己的设置分别渲染和投递）；
        指定 guild_id 时只推那个频道的（digest 周期到点时用）。
        """
        if not self._buffer:
            return 0

        if guild_id is None:
            posts, self._buffer = self._buffer, []
            return await self._flush_posts(posts, force=force)

        guild_id = str(guild_id)
        picked = self._pending_of(guild_id)
        if not picked:
            return 0
        self._buffer = [item for item in self._buffer if self._guild_of(item) != guild_id]
        return await self._flush_posts(picked, force=force)

    async def _flush_posts(self, posts, force=False):
        """按频道分组推送 —— 分组是必须的：不同频道的目标群和文案都不一样。"""
        if not posts:
            return 0
        sent = 0
        for guild_id, group in self._group_by_guild(posts).items():
            sent += await self._flush_guild(guild_id, group, force=force)
        return sent

    async def _flush_guild(self, guild_id, posts, force=False):
        label = guild_id or "(未知频道)"
        data = self._settings.snapshot(guild_id)

        # 免打扰时段内只攒不发：塞回缓冲区，等时段结束再一起推
        if not force and self._settings.is_quiet_now(guild_id):
            self._buffer = posts + self._buffer
            ends_at = self._settings.quiet_ends_at(guild_id)
            wait = int((ends_at - datetime.now()).total_seconds()) if ends_at else 300
            wait = max(30, wait)
            self._due[guild_id] = time.time() + wait
            _log.info(
                f"[botpy] 频道 {label} 处于免打扰时段，{wait} 秒后再推送"
                f"（当前积压 {len(self._buffer)} 条）"
            )
            return 0

        # 目标群按频道算：这个频道配了 subscribers 就用它，没配才回退全局订阅列表
        channel_ids = {str(item.get("channel_id") or "") for item in posts}
        targets = self._settings.targets(guild_id, channel_ids, self._subscribers.all())
        if not targets:
            _log.warning(
                f"[botpy] 频道 {label} 没有转发目标群 —— 到配置页「频道」页勾选要推送的群，"
                f"或者在那个频道下 @机器人 发送「订阅」"
            )
            return 0

        messages = self._compose(posts, data)
        _log.info(
            f"[botpy] 开始推送频道 {label} 的 {len(posts)} 条帖子，共 {len(messages)} 条消息，"
            f"目标群 {len(targets)} 个（样式={data.get('style')}）"
        )
        for group_openid in sorted(targets):
            for payload in messages:
                try:
                    result = await self._client.api.post_group_message(
                        group_openid=group_openid, **payload
                    )
                    if isinstance(result, dict) and result.get("id"):
                        # ext_info.ref_idx 就是这条消息的 msg_idx，被引用时靠它换算
                        ref_idx = (result.get("ext_info") or {}).get("ref_idx")
                        self._index.remember_sent(group_openid, result["id"], ref_idx)
                    _log.info(f"[botpy] 已推送到群 {group_openid}: {result}")
                except Exception as error:
                    # 常见错误：40034100 触发频控；40034105 没有主动消息权限；
                    # markdown/按钮相关：22006 消息类型与内容不匹配、40034008 markdown 参数有空值
                    _log.error(f"[botpy] 推送到群 {group_openid} 失败: {error}")
        return len(posts)

    # ---------------- 文案渲染 ----------------
    def _compose(self, posts, data):
        """把帖子列表渲染成一条或多条群消息（每项是 post_group_message 的参数包）。

        data 由调用方按频道取好（settings.snapshot(guild_id)），这里不再自己读，
        免得同一批帖子里混了不同频道的设置。
        """
        style = str(data.get("style") or "text").lower()
        if style in ("markdown", "markdown_button"):
            return self._compose_card(posts, data, with_buttons=(style == "markdown_button"))
        return self._compose_text(posts, data)

    def _compose_text(self, posts, data):
        """纯文本消息：msg_type=0。"""
        lines = self._render_lines(posts, data["title_template"] or "{title}\n{link}")
        header = self._header(data, len(posts))

        # 标题头会拼到第一条消息前面，分片时先把它的位置预留出来
        limit = max(1, int(data["max_chars_per_message"]))
        reserve = len(header) + 1 if header else 0
        chunks = chunk_lines(lines, max(1, limit - reserve))
        if chunks and header:
            chunks[0] = f"{header}\n{chunks[0]}"
        return [{"msg_type": 0, "content": chunk} for chunk in chunks]

    def _compose_card(self, posts, data, with_buttons=False):
        """Markdown 卡片：msg_type=2；按钮必须挂在 markdown 消息上。

        卡片里的链接由跳转按钮承载，正文不用重复写 {link}；
        但如果选了不带按钮的 markdown，模板里又没写 {link}，这里会自动补上，
        免得推出去的消息点不开。
        """
        template = data["card_item"] or "{index}. {title}"
        append_link = not with_buttons and "{link}" not in template
        lines = self._render_lines(posts, template, append_link=append_link)
        title = str(data.get("card_title") or "")
        if title and len(posts) > 1:
            title = f"{title}（共 {len(posts)} 条）"

        limit = max(1, int(data["max_chars_per_message"]))
        reserve = len(title) + 2 if title else 0
        chunks = chunk_lines(lines, max(1, limit - reserve))
        if chunks and title:
            chunks[0] = f"{title}\n\n{chunks[0]}"

        messages = []
        for position, chunk in enumerate(chunks):
            payload = {"msg_type": 2, "markdown": {"content": chunk}}
            # 按钮只挂在第一条消息上
            if with_buttons and position == 0:
                keyboard = self._build_keyboard(posts, data)
                if keyboard:
                    payload["keyboard"] = keyboard
            messages.append(payload)
        return messages

    @staticmethod
    def _render_lines(posts, template, append_link=False):
        lines = []
        for index, post in enumerate(posts, start=1):
            values = {key: str(post.get(key) or "") for key in TITLE_PLACEHOLDERS}
            values["index"] = str(index)
            try:
                line = template.format(**values)
            except (KeyError, IndexError, ValueError) as error:
                _log.warning(f"[botpy] 文案模板占位符有误（{error}），已按原样输出: {template!r}")
                line = template
            if append_link and values.get("link"):
                line = f"{line}\n{values['link']}"
            lines.append(line)
        return lines

    @staticmethod
    def _header(data, count):
        if count == 1:
            return data["single_header"] or ""
        return (data["batch_header"] or "").replace("{count}", str(count))

    def _build_keyboard(self, posts, data):
        """构造「查看帖子」跳转按钮（action.type=0，data 放链接）。

        官方限制：最多 5 行、每行最多 5 个按钮；自定义按钮属于内邀开通能力。
        """
        try:
            max_buttons = int(data.get("max_buttons") or 5)
        except (TypeError, ValueError):
            max_buttons = 5
        max_buttons = max(0, min(25, max_buttons))

        label = str(data.get("button_label") or "查看帖子")
        buttons = []
        for index, post in enumerate(posts[:max_buttons], start=1):
            url = str(post.get("link") or "")
            if not url:
                continue
            buttons.append(
                {
                    "id": str(index),
                    "render_data": {"label": label, "visited_label": label, "style": 1},
                    "action": {
                        "type": 0,  # 0 = 跳转按钮，data 里放 http 链接
                        "permission": {"type": 2},  # 2 = 所有人可操作
                        "data": url,
                        "unsupport_tips": "请升级QQ版本后查看",
                    },
                }
            )
        if not buttons:
            return None
        rows = [{"buttons": buttons[start:start + 5]} for start in range(0, len(buttons), 5)]
        return {"content": {"rows": rows[:5]}}

    def describe_buffer(self):
        return len(self._buffer)

    def buffer_by_guild(self):
        """缓冲区按频道分的条数 {guild_id: n}。"""
        return {guild_id: len(items) for guild_id, items in self._group_by_guild(self._buffer).items()}

    def describe_buffer_lines(self, label_of=None):
        """「状态」指令用的积压明细，多频道下一眼看懂是哪个频道在攒。"""
        detail = self.buffer_by_guild()
        if not detail:
            return ["缓冲区是空的"]
        lines = []
        for guild_id, count in sorted(detail.items(), key=lambda item: -item[1]):
            name = label_of(guild_id) if label_of else ""
            lines.append(f"{name or guild_id or '(未知频道)'}：{count} 条")
        return lines
