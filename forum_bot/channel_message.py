# -*- coding: utf-8 -*-
"""频道消息的正文处理：内嵌格式、转义、@机器人 识别。

对应官方「内嵌格式」文档
----------------------
频道消息的 content 里，@ 一个人写成 `<@user_id>` 或 `<@!user_id>`，
子频道是 `<#channel_id>`，系统表情是 `<emoji:id>`，@所有人是字面量 `@everyone`。

而且**消息抄送会转义**：

    源字符   转义后
      &      &amp;
      <      &lt;
      >      &gt;

所以机器人收到的 content 里，`<@!1234>` 有可能是 `&lt;@!1234&gt;`。
判断 @机器人 之前必须先反转义 —— 这就是本模块存在的原因。

帖子/评论事件为什么也要这层
--------------------------
论坛子频道里发帖、评论不会产生频道消息事件，走的是 OPEN_FORUM_POST_CREATE /
OPEN_FORUM_REPLY_CREATE。官方没给这两个事件的字段表，正文叫什么名字不确定，
所以 extract_content() 用"递归找 content/text 字段"的宽容做法，
拿不到就返回空字符串，由调用方打日志暴露出来。
"""
import re

__all__ = [
    "unescape_inline",
    "strip_inline",
    "mentions_bot",
    "extract_content",
    "INLINE_MENTION_RE",
]

# &lt; / &gt; / &amp; 一次扫描替换（不能先换 &lt; 再换 &amp;，
# 否则用户真写的 "&amp;lt;" 会被二次反转义）
_ESCAPE_RE = re.compile(r"&(lt|gt|amp);")
_ESCAPE_MAP = {"lt": "<", "gt": ">", "amp": "&"}

# 内嵌格式占位符：@人 / 子频道 / 系统表情
INLINE_MENTION_RE = re.compile(r"<@!?[^>]*>|<#[^>]*>|<emoji:[^>]*>")

_EVERYONE = "@everyone"

# 递归找正文时优先看的字段名（按优先级）
_CONTENT_KEYS = ("content", "text", "title")


def unescape_inline(text):
    """把抄送时转义过的 &lt; &gt; &amp; 还原。"""
    return _ESCAPE_RE.sub(lambda match: _ESCAPE_MAP[match.group(1)], str(text or ""))


def strip_inline(text):
    """去掉内嵌格式占位符，得到可以拿去做关键词匹配的纯文本。"""
    plain = unescape_inline(text)
    plain = INLINE_MENTION_RE.sub(" ", plain)
    return plain.replace(_EVERYONE, " ").strip()


def mentions_bot(content, bot_id):
    """正文里有没有 @机器人。

    内嵌格式是 <@user_id> 或 <@!user_id>；抄送会转义成 &lt; ... &gt;，
    所以先反转义再匹配，两种写法都认。
    """
    bot_id = str(bot_id or "").strip()
    if not bot_id:
        return False
    plain = unescape_inline(content)
    return f"<@{bot_id}>" in plain or f"<@!{bot_id}>" in plain


def _find_key(data, key, depth):
    """由浅到深地找第一个非空字符串的 `key` 字段。

    注意只往 dict / list 里递归 —— **不要递归进普通字符串值**，
    否则同层的 guild_id / author_id / channel_id 会被当成正文返回。
    """
    if depth < 0:
        return ""
    if isinstance(data, dict):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        for value in data.values():
            if isinstance(value, (dict, list, tuple)):
                found = _find_key(value, key, depth - 1)
                if found:
                    return found
    elif isinstance(data, (list, tuple)):
        for item in data:
            found = _find_key(item, key, depth - 1)
            if found:
                return found
    return ""


def extract_content(data, max_depth=4):
    """从事件下发体里尽力找出正文。

    各事件的字段名不统一（可能有 post_info.content / reply_info.content /
    thread_info.content …），官方对帖子类事件又没给字段表，所以这里递归找。

    优先级是"全局"的：任意位置的 content 都优先于任意位置的 text，text 又优先于
    title —— 否则一个同时带 thread_info.title 和 post_info.content 的下发体，
    结果会取决于字典顺序。
    """
    for key in _CONTENT_KEYS:
        found = _find_key(data, key, max_depth)
        if found:
            return found
    return ""
