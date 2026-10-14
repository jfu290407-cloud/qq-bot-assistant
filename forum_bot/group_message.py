# -*- coding: utf-8 -*-
"""QQ群消息的字段解析。

群里有两个坑（都是实测出来的）：
  1. 「引用」不走 message_reference，而是
     message_type=103 + message_scene.ext 里的 ref_msg_idx=REFIDX_xxx；
  2. 全量模式下 @机器人 会原样写在 content 里，形如 " <@F8232BA2...> 撤回"，
     匹配指令前必须先剥掉 <@openid>。
"""
import base64
import json
import re

__all__ = [
    "QUOTE_MESSAGE_TYPE",
    "FACE_FALLBACK",
    "parse_message_scene",
    "message_index_of",
    "quoted_index_of",
    "quoted_preview_of",
    "mentioned_bot",
    "clean_content",
    "readable_content",
    "face_name",
    "describe",
]

# QQ群「引用消息」的 message_type
QUOTE_MESSAGE_TYPE = 103

# content 里的 @ 占位符，形如 <@F8232BA22E9A45D4FACD2A7FD6C53066>
_MENTION_RE = re.compile(r"<@!?[^>]+>")

# QQ 表情标记。全量模式下它**原样出现在 content 里**，形如：
#     <faceType=1,faceId="492",ext="eyJ0ZXh0Ijoi6K6p5oiR57uG5ZOBIn0=">
# ext 是 **base64 的 JSON**，里面 {"text":"让我细品"} 就是这个表情的名字。
# 不还原的话，转发过去的是一条手机上看不懂的代码（实测过的现象）。
_FACE_RE = re.compile(r'<faceType=\d+,faceId="([^"]*)"(?:,ext="([^"]*)")?\s*/?>')
# 兜底：匹配不到上面那种规范写法的表情标记，统一换成 [表情]
_FACE_LOOSE_RE = re.compile(r"<faceType=[^>]*>")

# ext 解不出来时显示什么
FACE_FALLBACK = "表情"


def parse_message_scene(message):
    """把 message_scene.ext 里的 "k=v" 键值对解析出来。

    引用信息就藏在这里，例如：
        ["ref_msg_idx=REFIDX_xxx", "msg_idx=REFIDX_yyy", "auth_token=..."]
    """
    raw = getattr(message, "raw", None) or {}
    scene = raw.get("message_scene") or {}
    result = {}
    for item in scene.get("ext") or []:
        if isinstance(item, str) and "=" in item:
            key, _, value = item.partition("=")
            result[key.strip()] = value.strip()
    return result


def message_index_of(message):
    """这条消息自己的 msg_idx（消息索引）。"""
    return parse_message_scene(message).get("msg_idx")


def quoted_index_of(message):
    """这条消息引用了谁：返回被引用消息的 msg_idx；不是引用消息则返回 None。"""
    scene = parse_message_scene(message)
    if scene.get("ref_msg_idx"):
        return scene["ref_msg_idx"]

    # 兜底：引用消息的 msg_elements[0] 描述的就是被引用那条
    raw = getattr(message, "raw", None) or {}
    if raw.get("message_type") == QUOTE_MESSAGE_TYPE:
        elements = raw.get("msg_elements") or []
        if elements and isinstance(elements[0], dict):
            return elements[0].get("msg_idx")
    return None


def quoted_preview_of(message):
    """被引用消息的文本预览：引用消息的 msg_elements 里带着它。"""
    raw = getattr(message, "raw", None) or {}
    for element in raw.get("msg_elements") or []:
        if isinstance(element, dict) and element.get("content"):
            return str(element["content"])
    return ""


def mentioned_bot(message):
    """这条消息是否 @了机器人自己（mentions 里 is_you=true 的那条）。"""
    raw = getattr(message, "raw", None) or {}
    for item in raw.get("mentions") or []:
        if isinstance(item, dict) and item.get("is_you"):
            return True
    return False


def clean_content(content):
    """去掉 content 里的 <@openid> 占位符并去首尾空白。"""
    return _MENTION_RE.sub("", content or "").strip()


def face_name(ext):
    """解表情的 ext，返回表情名；解不出来返回空串。

    ext 形如 `eyJ0ZXh0Ijoi6K6p5oiR57uG5ZOBIn0=`，base64 解出来是
    `{"text":"让我细品"}` —— 就是这个表情在 QQ 里的名字。
    """
    raw = str(ext or "").strip()
    if not raw:
        return ""
    try:
        # 平台给的 base64 不一定带够 padding，补上再解
        padded = raw + "=" * (-len(raw) % 4)
        data = json.loads(base64.b64decode(padded).decode("utf-8"))
    except Exception:
        # 解不出来很常见（平台字段会变），不能因此让消息处理失败
        return ""
    if isinstance(data, dict):
        for key in ("text", "face_name", "name"):
            value = str(data.get(key) or "").strip()
            if value:
                return value
    return ""


def readable_content(content):
    """把 content 整理成「人看得懂」的样子（给转发这类要发出去看的场景用）。

    做两件事：
      1. 剥掉 <@openid> 占位符（和 clean_content 一致）；
      2. 把 QQ 表情标记还原成 `[表情名]`，还原不出来就 `[表情]`。

    **故意不并进 clean_content**：那个函数还被撤回 / 拉黑的关键词匹配用着，
    改它的输出等于悄悄改变匹配行为 —— 那是另一件事，不该顺手一起动。
    """
    result = clean_content(content)
    if "<faceType" not in result:
        return result
    # 先按规范写法替换（能拿到表情名），再用宽松规则兜住其他写法
    result = _FACE_RE.sub(
        lambda match: "[%s]" % (face_name(match.group(2)) or FACE_FALLBACK),
        result,
    )
    result = _FACE_LOOSE_RE.sub("[%s]" % FACE_FALLBACK, result)
    return result.strip()


def describe(message):
    """把一条群消息摊开成多行文本，用于打印到控制台排查字段。"""
    author = getattr(message, "author", None)
    raw = getattr(message, "raw", None) or {}
    mentions = [getattr(item, "member_openid", None) for item in (getattr(message, "mentions", None) or [])]
    attachments = [
        getattr(item, "filename", None) or getattr(item, "url", None)
        for item in (getattr(message, "attachments", None) or [])
    ]

    lines = [
        "",
        "================= 群消息 =================",
        f"group_openid : {getattr(message, 'group_openid', None)}",
        f"message_id   : {getattr(message, 'id', None)}",
        f"event_id     : {getattr(message, 'event_id', None)}",
        f"member_openid: {getattr(author, 'member_openid', None)}",
        f"username     : {raw.get('author', {}).get('username')}",
        f"timestamp    : {getattr(message, 'timestamp', None)}",
        f"message_type : {raw.get('message_type')}",
        f"content      : {getattr(message, 'content', None)!r}",
        f"剥离@后的内容 : {clean_content(getattr(message, 'content', None))!r}",
        f"本条 msg_idx  : {message_index_of(message)}",
        f"引用的 msg_idx: {quoted_index_of(message)}",
        f"message_reference : {raw.get('message_reference')}",
        f"mentions     : {mentions}",
        f"attachments  : {attachments}",
    ]
    if raw:
        lines.append("--- 原始下发数据 ---")
        lines.append(json.dumps(raw, ensure_ascii=False, indent=2))
    lines.append("=========================================")
    return "\n".join(lines)
