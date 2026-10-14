# -*- coding: utf-8 -*-
"""群消息撤回 + 拉黑名单。

官方接口是 DELETE /v2/groups/{group_openid}/messages/{message_id}，
限制是硬性的：
  · 只能撤回**发送不超过 2 分钟**的消息，超时报 40064004；
  · 机器人是**群管理员**时，才能撤回普通群成员的消息（否则 40062003）；
    普通成员机器人只能撤回自己发的消息；
  · 想对「群里任意一条消息」生效，需要开启「接收所有消息」全量模式。

两种撤回规则：
  · 拉黑名单：消息**包含**名单里任意一条内容就撤（blacklist_enabled，默认开，名单空则不动）；
  · 撤回关键词：消息命中关键词就撤（recall_enabled，默认关）。

引用撤回的坑：QQ群消息的引用只给 msg_idx（ref_msg_idx），撤回接口要 message_id，
两者靠 MessageIndex 换算。

拉黑名单的读写落在 blacklist 模块（内容存 settings，来源存本地文件）。

给用户看的文案全部在 text.yaml 的 messages.recall / messages.blacklist 下。
注意：本模块里**不能**再用名叫 text 的局部变量 —— 会盖住文案模块。
"""
import asyncio
import collections
import time

from botpy import logging

from . import ai_split, text
from .admins import member_name_of, member_openid_of
from .blacklist import Blacklist, MemberBlacklist
from .group_message import quoted_index_of, quoted_preview_of, clean_content
from .settings import split_lines

__all__ = ["RecallGuard", "split_keywords"]

_log = logging.get_logger()


def split_keywords(raw):
    """逗号分隔的关键词（撤回关键词用逗号，拉黑内容用换行）。"""
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


class RecallGuard:
    # 撤不了就歇多久再试（秒）。管理员可能刚把机器人设成群管理员，
    # 所以**带过期时间**，不能一记就永久放弃。
    NO_PERMISSION_COOLDOWN = 1800

    def __init__(self, client, settings, message_index):
        self._client = client
        self._settings = settings
        self._index = message_index
        self._blacklist = Blacklist(settings)
        # 成员黑名单：按 openid 记**人**（不是内容）。见 blacklist.MemberBlacklist
        self._members = MemberBlacklist()
        # 去重：同一条消息不重复撤（@ 事件和全量模式可能都推同一条）
        self._recall_done = collections.deque(maxlen=200)
        # 撤不了普通成员消息的群（40062003 无操作权限）。记下来**别再白试** ——
        # 不记的话，一个被拉黑的人在群里刷屏，每一条都撞一次接口错误：
        # 日志被刷爆，接口频率还可能被吃满，把真正该做的事挤掉。
        self._no_permission = {}

    def _blocked(self, group_openid):
        return time.time() < self._no_permission.get(str(group_openid or ""), 0)

    def _mark_blocked(self, group_openid):
        self._no_permission[str(group_openid or "")] = (
            time.time() + self.NO_PERMISSION_COOLDOWN
        )

    def blocked(self, group_openid):
        """这个群最近刚被判过「撤不动普通成员的消息」吗（只读）。

        自动撤回规则自己会查；给**偶发**的撤回调用（比如撤「绑定QQ」指令）
        用 —— 不查的话，非管理员群里每来一条绑定指令都要白撞一次接口错误。
        """
        return self._blocked(group_openid)

    def member_blacklist(self):
        """成员黑名单（配置页和指令都要用）。"""
        return self._members

    # ---------------- 基础能力 ----------------
    async def recall(self, group_openid, message_id):
        """撤回一条群消息，返回 (是否成功, 给用户看的提示)。"""
        if not message_id:
            return False, text.text("messages.recall.no_message_id")
        try:
            await self._client.api.recall_group_message(
                group_openid=group_openid, message_id=message_id
            )
        except Exception as error:
            detail = str(error)
            # 错误码是主判据；message 兜底（老版本 botpy 不会把 code 带进异常文案）
            if "40064004" in detail or "撤回时限" in detail:
                hint = text.text("messages.recall.too_late")
            elif "40062003" in detail or "无操作权限" in detail:
                hint = text.text("messages.recall.not_admin")
                # 记下来别再白试（见 _blocked 的说明）。
                # 机器人**能撤自己发的**消息，所以报这个就说明这条不是它发的
                # -> 它在群里不是管理员。这判据是准的。
                self._mark_blocked(group_openid)
            elif "40061001" in detail:
                hint = text.text("messages.recall.bad_id")
            else:
                hint = text.text("messages.recall.failed", error=detail)
            _log.warning(
                f"[botpy] 撤回失败 group={group_openid} message_id={message_id}: {detail}"
            )
            return False, hint
        _log.info(f"[botpy] 已撤回消息 group={group_openid} message_id={message_id}")
        return True, text.text("messages.recall.ok")

    # ---------------- 自动规则 ----------------
    async def auto_recall(self, message):
        """命中「成员黑名单」「拉黑内容」或「撤回关键词」就撤回，返回是否真的撤回了。

        三条规则的**优先级**是有意的：成员黑名单 > 拉黑内容 > 撤回关键词。
        成员黑名单排最前，是因为它**不看内容** —— 放后面的话，
        被拉黑的人发一条不带任何拉黑词的消息就会漏过去
        （"这个人说什么都撤"就变成了"这个人说某些话才撤"）。
        """
        data = self._settings.snapshot()

        # 机器人自己的消息不动，避免自己撤自己
        if (getattr(message, "raw", None) or {}).get("author", {}).get("bot"):
            return False

        content = clean_content(getattr(message, "content", None) or "")

        reason = None
        if data.get("blacklist_enabled", True):
            openid = member_openid_of(message)
            if openid and self._members.is_banned(openid):
                reason = text.text(
                    "messages.recall.reason_member",
                    who=member_name_of(message) or openid[:8],
                )

        if reason is None and content and data.get("blacklist_enabled", True):
            for phrase in split_lines(data.get("blacklist_keywords")):
                if phrase in content:
                    reason = text.text(
                        "messages.recall.reason_blacklist", phrase=phrase
                    )
                    break

        if reason is None and content and data.get("recall_enabled"):
            for word in split_keywords(data.get("recall_keywords")):
                if word in content:
                    reason = text.text("messages.recall.reason_keyword", word=word)
                    break

        if reason is None:
            return False

        if message.id in self._recall_done:
            return False
        self._recall_done.append(message.id)

        group_openid = getattr(message, "group_openid", None)
        # 上次在这个群因为"机器人不是管理员"撤失败了 -> 先歇一会儿再试。
        # 不然被拉黑的人刷屏时，每一条都会撞一次接口错误。
        if self._blocked(group_openid):
            _log.info(
                f"[botpy] 自动撤回跳过（这个群上撤回过、机器人多半不是群管理员）："
                f"{reason}"
            )
            return False

        _log.info(f"[botpy] 自动撤回：{reason}，内容={content!r}")
        ok, _hint = await self.recall(group_openid, message.id)
        if not ok and self._blocked(group_openid):
            # recall() 刚记下"没权限"，把话说清楚 —— 用户会以为拉黑没生效
            _log.warning(
                f"[botpy] 这个群（{str(group_openid or '')[:10]}…）撤回不了别人的消息："
                f"机器人得是群管理员才行。已暂停在该群尝试 "
                f"{self.NO_PERMISSION_COOLDOWN // 60} 分钟（设为管理员后自动恢复）"
            )
        if ok and data.get("recall_notify"):
            try:
                await message.reply(
                    content=text.text("messages.recall.notify", reason=reason)
                )
            except Exception as error:
                _log.warning(f"[botpy] 撤回提示发送失败: {error}")
        return ok

    # ---------------- 成员黑名单 ----------------
    def member_banned(self, openid):
        return self._members.is_banned(openid)

    def member_ban_add(self, openid, message=None, by=None):
        return self._members.add(openid, message=message, by=by)

    def member_ban_remove(self, openid):
        return self._members.remove(openid)

    async def member_blacklist_add(self, message, target_openid=None):
        """把一个人加进成员黑名单。

        不带参数时用**被引用消息的发送者**；带参数时按 openid / 昵称找。
        加完顺手把被引用那条也撤掉 —— 和内容拉黑一个道理：
        都已经要封他了，还留着他那条消息说不过去。
        """
        openid, label = self._resolve_member(message, target_openid)
        if not openid:
            await message.reply(content=text.text("messages.member_ban.need_target"))
            return

        # 昵称/所在群取**被引用那条**（它才是"他"说的话）；
        # "是谁下的手"另外传 —— 这两者是不同的人，混一起会把 by_name 记成他本人
        added, total, record = self._members.add(
            openid,
            target_message=self._quoted_message(message),
            by_openid=member_openid_of(message),
            by_name=member_name_of(message),
        )
        if record.get("username"):
            label = record["username"]

        recalled = ""
        ref_idx = quoted_index_of(message)
        if ref_idx:
            recalled = await self.recall_by_index(message, ref_idx)

        lines = [
            text.text(
                "messages.member_ban.already" if not added else "messages.member_ban.added"
            ),
            text.text("messages.member_ban.target", who=label, openid=openid),
            text.text("messages.member_ban.total", total=total),
        ]
        if recalled:
            lines.append(text.text("messages.member_ban.quoted", content=recalled))
        # 说清楚它**不是踢人** —— 平台没这个能力，用户会以为人已经被踢了
        lines.append(text.text("messages.member_ban.not_kick"))
        if self._blocked(message.group_openid):
            lines.append(
                text.text(
                    "messages.member_ban.no_permission",
                    group=str(getattr(message, "group_openid", "") or "")[:10],
                )
            )
        await message.reply(content="\n".join(lines))

    async def member_blacklist_remove(self, message, target=None):
        openid, label = self._resolve_member(message, target)
        if not openid:
            await message.reply(content=text.text("messages.member_ban.need_target"))
            return
        removed, old = self._members.remove(openid)
        await message.reply(
            content=(
                text.text(
                    "messages.member_ban.removed",
                    who=label,
                    left=len(self._members),
                )
                if removed
                else text.text("messages.member_ban.not_found", who=label)
            )
        )

    async def member_blacklist_show(self, message):
        entries = self._members.entries()
        if not entries:
            await message.reply(content=text.text("messages.member_ban.empty"))
            return
        lines = [text.text("messages.member_ban.title", count=len(entries))]
        for entry in entries[:20]:
            who = entry.get("username") or (entry.get("openid") or "")[:8]
            when = str(entry.get("at") or "")[5:16]
            lines.append(f"　{who}　{when}")
        if len(entries) > 20:
            lines.append(text.text("messages.member_ban.more", count=len(entries) - 20))
        # 提醒一句：能不能真的撤得掉，取决于机器人在那个群是不是管理员
        lines.append(text.text("messages.member_ban.hint"))
        await message.reply(content="\n".join(lines))

    # ---------------- 成员解析 ----------------
    @staticmethod
    def _quoted_message(message):
        """取被引用那条的原始数据（昵称/群要从它上面拿，不是从指令发出者）。"""
        raw = getattr(message, "raw", None) or {}
        elements = raw.get("msg_elements") or []
        for element in elements:
            if isinstance(element, dict) and element.get("author"):
                # 造一个只有 author / group_openid 的壳，够 MemberBlacklist.add 用
                shell = type("QuotedMsg", (), {})()
                shell.raw = {"author": element.get("author") or {}}
                shell.group_openid = getattr(message, "group_openid", None)
                return shell
        return None

    def _resolve_member(self, message, target=None):
        """把指令参数（或引用目标）解析成 (member_openid, 展示名)。

        支持三种写法，按可靠性排序：
          1. 直接给 openid（32 位十六进制）—— 最准，配置页能复制
          2. 给昵称 —— 到名册里找，重名的取最近活跃的那个
          3. 不给参数 —— 用**被引用消息的发送者**（最常用）
        """
        raw = str(target or "").strip()
        if not raw:
            openid = self._quoted_openid(message)
            entry = self._client_members_find(openid)
            label = (entry or {}).get("username") or (member_name_of(message) if not openid else "")
            return openid, label or (openid or "")[:8]

        if len(raw) >= 20 and all(char in "0123456789abcdefABCDEF" for char in raw):
            return raw, raw[:8]

        # 按昵称找：名册里可能重名，取最近活跃的那个（配置页也是这个顺序）
        finder = getattr(self._client, "members", None)
        if finder is not None:
            for item in finder.all():
                if str(item.get("username") or "") == raw:
                    return str(item.get("openid") or ""), raw
        return "", raw

    def _quoted_openid(self, message):
        """被引用消息是谁发的。msg_elements 里带着 author。"""
        raw = getattr(message, "raw", None) or {}
        for element in raw.get("msg_elements") or []:
            if isinstance(element, dict):
                author = element.get("author") or {}
                openid = (
                    author.get("member_openid")
                    or author.get("union_openid")
                    or author.get("id")
                )
                if openid:
                    return str(openid)
        return ""

    def _client_members_find(self, openid):
        finder = getattr(self._client, "members", None)
        if finder is None or not openid:
            return {}
        return finder.find(openid) or {}

    # ---------------- 拉黑名单 ----------------
    def blacklist(self):
        """当前拉黑内容列表（落盘在 forum_forward_settings.json）。"""
        return self._blacklist.contents()

    def reset_blacklist_cache(self):
        """清掉本地多余的来源记录（配置页手工删过内容时会产生），启动时调一次。"""
        return self._blacklist.prune()

    async def blacklist_add(self, message, phrase=None):
        """把被引用消息的正文（或显式给的文本）加进拉黑名单。

        引用拉黑时，顺手把**被引用的那条消息本身也撤回** —— 拉黑通常就是因为
        对方正在刷这条内容，只加入名单而不撤掉它说不过去。

        **默认连发送者也一起拉进成员黑名单**（blacklist_members，配置页可关）：
        内容拉黑只管"这条内容不许再说"，挡不住他换个说法接着刷。
        """
        data = self._settings.snapshot()
        target = (phrase or "").strip() or quoted_preview_of(message)
        if not target:
            await message.reply(content=text.text("messages.blacklist.need_quote_add"))
            return

        already, total = self._blacklist.add(target, message)

        lines = [
            text.text(
                "messages.blacklist.already" if already else "messages.blacklist.added"
            ),
            text.text("messages.blacklist.content", content=target),
            text.text("messages.blacklist.total", total=total),
        ]

        # AI 拆分候选关键词：后台跑，群里的回复不等它。
        # 只对「引用拉黑」生效 —— 显式发「拉黑 <词>」本来就是人在挑词，不用再拆
        if not phrase and self._queue_ai_split(message, target):
            lines.append(text.text("messages.blacklist.ai_queued"))

        # 同时把发送者拉进成员黑名单。
        # **只对「引用拉黑」生效**：显式给文本的用法（拉黑 <内容>）本来就是在
        # 屏蔽一个词，没有"发送者"这个概念 —— 也**故意**不去猜"这词是谁说的"。
        if data.get("blacklist_members", True) and not phrase:
            openid = self._quoted_openid(message)
            if openid:
                added, _count, record = self._members.add(
                    openid,
                    target_message=self._quoted_message(message),
                    by_openid=member_openid_of(message),
                    by_name=member_name_of(message),
                )
                if added:
                    lines.append(
                        text.text(
                            "messages.member_ban.added_inline",
                            who=record.get("username") or openid[:8],
                        )
                    )
                    # 只有真新增时才提这句 —— 已经在名单里的话是句废话
                    lines.append(text.text("messages.member_ban.not_kick"))

        # 被引用的那条消息一起撤掉
        recalled = ""
        ref_idx = quoted_index_of(message)
        if ref_idx:
            recalled = await self.recall_by_index(message, ref_idx)
        if recalled:
            lines.append(text.text("messages.blacklist.quoted", content=recalled))
        await message.reply(content="\n".join(lines))

    # ---------------- AI 拆分候选关键词（见 ai_split 模块说明） ----------------
    def _queue_ai_split(self, message, content):
        """把被拉黑的消息交给 AI 拆分。返回是否真的排上了队。

        不 await：拆分要几秒，而这条 async 函数还在拉黑指令的回复路径上，
        卡几秒群里的反馈就太慢。失败（没开/没 key/接口挂了）一律安静地
        不排队，只记日志 —— 拉黑本身已经生效，这只是锦上添花的功能。
        """
        data = self._settings.snapshot()
        if not ai_split.ai_enabled(data):
            return False
        # 已经在拉黑名单里的东西不值得再让 AI 拆一遍
        already = [item for item in self._blacklist.contents() if item in content]
        remaining = content
        for item in already:
            remaining = remaining.replace(item, "")
        if not remaining.strip():
            return False
        task = asyncio.create_task(self._ai_split_task(message, content))
        # 任务自己兜住所有异常：不加这一行，未观察的异常会在 GC 时打一屏警告
        task.add_done_callback(
            lambda done: done.exception()
            and _log.warning(f"[botpy] AI 拆分任务异常: {done.exception()}")
        )
        return True

    async def _ai_split_task(self, message, content):
        data = self._settings.snapshot()
        base_url, model = ai_split.resolve_config(data)
        phrases, error = await ai_split.split_keywords(
            content,
            api_key=str(data.get("ai_api_key") or ""),
            base_url=base_url,
            model=model,
        )
        if error:
            _log.warning(f"[botpy] AI 拆分失败：{error}")
            return
        # 在名单里的短语不再作为候选 —— 勾了也是白勾
        existing = set(self._blacklist.contents())
        fresh = [item for item in phrases if item not in existing]
        if not fresh:
            _log.info("[botpy] AI 拆分完成，但短语都已在拉黑名单里，不产生候选")
            return
        count = ai_split.SuggestStore().add(
            fresh,
            source=content,
            by_name=member_name_of(message),
            group_openid=str(getattr(message, "group_openid", None) or ""),
        )
        _log.info(
            f"[botpy] AI 拆分出 {count} 个候选关键词，已放到配置页「拉黑名单」页待挑选"
        )

    async def blacklist_remove(self, message, phrase=None):
        target = (phrase or "").strip() or quoted_preview_of(message)
        if not target:
            await message.reply(content=text.text("messages.blacklist.need_quote_remove"))
            return

        matched = self._blacklist.remove(target)
        if not matched:
            await message.reply(
                content=text.text("messages.blacklist.not_found", content=target)
            )
            return

        await message.reply(
            content=text.text(
                "messages.blacklist.removed",
                count=len(matched),
                left=len(self.blacklist()),
            )
        )

    async def blacklist_show(self, message):
        entries = self._blacklist.entries()
        if not entries:
            await message.reply(content=text.text("messages.blacklist.empty"))
            return
        lines = self._blacklist.describe_lines()
        await message.reply(
            content=text.text("messages.blacklist.title", count=len(entries))
            + "\n"
            + "\n".join(lines)
        )

    # ---------------- 群内指令 ----------------
    async def recall_by_index(self, message, ref_idx):
        """按被引用消息的 msg_idx 撤回它，返回一句给用户看的说明。"""
        message_id, reason = self._index.resolve_message_id(ref_idx)
        if not message_id:
            preview = quoted_preview_of(message)
            _log.warning(f"[botpy] 引用撤回失败，ref_msg_idx={ref_idx}: {reason}")
            # 能拿到被引用内容就一起告诉他，省得他自己翻
            if not preview:
                return reason
            return text.text(
                "messages.recall.quoted_content", reason=reason, preview=preview
            )

        _log.info(f"[botpy] 引用撤回: ref_msg_idx={ref_idx} -> message_id={message_id}")
        _ok, hint = await self.recall(message.group_openid, message_id)
        return hint

    async def recall_quoted(self, message):
        """撤回指令消息所引用的那条消息。"""
        ref_idx = quoted_index_of(message)
        if not ref_idx:
            await message.reply(content=text.text("messages.recall.need_quote"))
            return
        await message.reply(content=await self.recall_by_index(message, ref_idx))

    async def recall_by_id(self, message, message_id):
        _ok, hint = await self.recall(message.group_openid, message_id)
        await message.reply(content=hint)

    async def recall_last_sent(self, message):
        item, reason = self._index.last_sent(message.group_openid)
        if not item:
            await message.reply(content=reason)
            return
        _ok, hint = await self.recall(message.group_openid, item["message_id"])
        await message.reply(content=hint)
