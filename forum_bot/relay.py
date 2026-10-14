# -*- coding: utf-8 -*-
"""群消息互转：把一个群的消息转发到另一个群。

四个绕不过去的平台限制
====================
这些都是官方规则，不是实现问题，先说清楚免得白折腾：

1. **需要「接收所有消息」全量模式。**
   默认机器人只收得到 @它 的消息（GROUP_AT_MESSAGE_CREATE），
   谈不上"同步"。开了全量才会收到每一条（GROUP_MESSAGE_CREATE）。

2. **转过去的一定是「主动消息」，吃的是严格配额。**
   目标群里没有对应的事件，拿不到 msg_id，用不了"被动回复"那条通道
   （被动回复要求 msg_id + msg_seq，5 分钟 5 次）。
   主动消息的硬限：每个群**每天 1000 条**、20 qpm，撞上去报 40034100。
   平台不会提前告诉你还剩多少，所以必须自己记账 —— 见 RelayQuota。

3. **消息里不能有 URL。** 平台直接拒（40054010 不允许发送 URL），
   所以含链接的消息默认跳过（relay_skip_url）。

4. **拿不到 QQ 号。**
   群消息事件和「获取群成员信息」接口都只给 member_openid / union_openid /
   username，官方文档明确写了不含 QQ 号（原文：「与真实的 ID 无关。
   请不要理解为真实的 ID」）。

   所以转发里括号中的那个标识按这个顺序退：

     1. relay_notes —— 管理员在配置页手工填的 `openid=备注`。**优先**，
        因为管理员得能纠正识别错的、也得给识别不上的人补上
     2. **OneBot 自动识别的 QQ 号** —— 旁路小号拉群成员匹配（见 onebot/bindings 模块）
     3. member_openid 前 8 位 —— 稳定、能区分人，但它**不是** QQ 号

   QQ 号在这里**只是显示用的标签**，权限判定必须继续
   按 openid（admin_openids）—— 详见 bindings 模块开头。

5. **可以只转发已绑定 QQ 号的成员的消息**（relay_bound_only，默认关）。
   这才是自助绑定真正有用的地方：让**群成员自己**决定"我的消息要不要
   被转到别的群"，而不是由管理员替全场决定。
   没绑定的按"他不想被转"处理 —— 宁可少转，不可替人公开。

   注意"已绑定"包含两种：成员自助绑的，和管理员在 relay_notes 里填过的。
   管理员明确识别过的人，信号比自助绑定还强，没理由排除。

6. **关键词过滤**（relay_block_keywords，默认空）。
   正文**包含**其中任意一条就整条不转发。挡住的多半是广告词、推广话术。

   它和「拉黑名单」是两件事，别混：拉黑是**撤回**消息（原群里也没了），
   这里只是**不转过去** —— 原群照常说，只是对面收不到。
   所以它能用在一个不想为几句广告词就动撤回的群里。

   图片不参与过滤（过滤的是正文）：一条只有图的广告，正文是空的，
   过滤词再全也挡不住 —— 这种情况用「只转发已绑定成员」更合适。

环路防护
========
双向互转（A->B 和 B->A 同时配）时，A 转过去的消息会再次触发 B 的事件，
再转回 A……几分钟就能把配额烧光。

防护靠一条：**机器人自己发的消息在事件里带 author.bot=true，直接跳过。**
转发出去的内容是以机器人身份发的，所以在目标群里天然会被这条挡掉，环路断开。

（auto_recall 和 members.remember 用的是同一个判据，是这套代码里既有的、
实测可靠的做法。）

规则里怎么指群：填群号，不用填 openid
=====================================
群 openid 是 32 位十六进制，在手机上根本没法复制核对 —— 让用户填它是在为难人。
所以规则里**两种写法都认**：

    123456789 -> 123456789          ← 群号（推荐）
    39FD6F7D... -> A1B2C3D4...      ← openid（老写法，继续支持）

群号 → openid 的映射来自两处，后者优先：
    1. 群备注（配置页「群聊」页填的那个，填的就是群号）
    2. 设置 relay_group_ids（一行 `群号=openid`），给没填备注的群兜底

解析不出来的规则会被跳过并在配置页/日志里点名 —— 不会静默失效。

转发图片
========
可以做，但要清楚它比转文字贵：

    · 图片得先「上传」拿到 file_info，再发一条富媒体消息（msg_type=7）——**两步**；
    · 每条图片消息各占 1 条主动消息配额（所以"文字+1张图"= 2 条配额）；
    · 上传时给的是图片 URL，**平台自己去抓**。群消息里的 attachments 就是 QQ 自己的
      CDN 地址，能直接喂回去，但它带签名、会过期，所以必须收到就立刻转，
      隔几分钟再转基本必失败（日志里会看到"上传失败"）。
    · 一次只能发一份 media，多张图就是多次发送、多份配额 —— 所以默认只转 1 张
      （relay_media_limit）。
"""
import json
import re
import time

from botpy import logging

from . import config, text
from .admins import is_bot_message, member_name_of, member_openid_of, role_label
from .group_info import digits_of, read_notes
from .group_message import (
    message_index_of,
    quoted_index_of,
    quoted_preview_of,
    readable_content,
)
from .settings import split_lines

__all__ = [
    "RelayGuard",
    "RelayQuota",
    "split_rule",
    "group_number_map",
    "parse_rules",
    "block_keywords",
]

_log = logging.get_logger()

# 平台硬限：每个群每天最多接收 1000 条主动消息。
# 默认只用到 900，留 10% 余量给"以后加别的主动推送功能"。
DEFAULT_DAILY_LIMIT = 900

# 默认格式：昵称(标识)：正文
# {uid} 显示什么取决于 relay_notes 和自助绑定（见 _display_id）
DEFAULT_FORMAT = "{nick}({uid})：{content}"

# 「只转发已绑定成员」挡下消息时，最多为多少个不同成员记日志。
# 不记的话用户开了开关却一条都没转，会以为功能坏了；每个成员的每句话都记
# 又会把日志刷爆（一个活跃群几分钟就能刷几百行），真问题反而看不见。
_UNBOUND_LOG_LIMIT = 50

# 含链接的消息发不出去（40054010），提前识别出来跳过
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)

# 富媒体消息的 msg_type（见 botpy/api.py 的 post_group_message 文档）
MEDIA_MSG_TYPE = 7
# 上传时的 file_type：1 图片 / 2 视频 / 3 语音 / 4 文件（平台暂不开放）
MEDIA_FILE_TYPE_IMAGE = 1

# 转发被引用原文时的截断长度：引用块太长会把目标群的回复顶得看不见，
# 而且原文再长也只是上下文，给个大概就够了
QUOTE_MAX_CHARS = 200

# 「源群消息 msg_idx -> 目标群转发 ref_idx」映射的保留时长和条数上限。
# 有它，被引用的消息**以前转发过**时就能直接挂引用（省一条消息）；
# 过期/超限丢了也只是退回"补发一条原文"的老路，不影响正确性
_FORWARDED_REF_TTL = 24 * 3600
_FORWARDED_REF_LIMIT = 2000


# 引用原文里的 URL 要**整段删掉**（_URL_RE 只用于"有没有"的判断，
# 删的时候只删协议前缀会剩下裸域名，照样被 40054010 拒）
_QUOTE_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

# 被引用的消息**本身也是引用消息**时，QQ 给的预览不是纯文本，而是把整条
# 嵌套结构渲染出来的块，形如：
#     === 消息 1 ===
#     [消息内容] 123
#     [消息类型] 引用消息
#     [关联消息]
#     --- 第1条 ---
#     [消息内容] 请问3
# 这种块原样转发出去又长又乱，而且下层内容引用样式自己就会带出来 ——
# 只取最外层的 [消息内容]，[关联消息] 往下全部丢弃
_NESTED_QUOTE_MARK = "[关联消息]"
_MSG_CONTENT_MARK = "[消息内容]"
_MSG_TYPE_MARK = "[消息类型]"


def _outer_quote_content(text):
    """从引用链的渲染块里抽出最外层正文；不是这种块就原样返回。"""
    text = str(text or "")
    if _NESTED_QUOTE_MARK not in text and "=== 消息" not in text:
        return text
    # [关联消息] 之后是被嵌套引用的下层，整段不要
    head = text.split(_NESTED_QUOTE_MARK, 1)[0]
    index = head.find(_MSG_CONTENT_MARK)
    if index >= 0:
        head = head[index + len(_MSG_CONTENT_MARK):]
    # 外层正文后面还挂着 [消息类型] 等渲染行，一并去掉
    return head.split(_MSG_TYPE_MARK, 1)[0]


def _sanitize_quote(text, strip_url=False):
    """把被引用消息的原文整理成适合转发的形态。

    先剥嵌套引用的渲染块（只留最外层正文，见 _outer_quote_content）、
    压成单行（嵌套的换行在引用块里只会更乱）、截断到 200 字；
    「跳过含链接的消息」开着时连引用里的 URL 一起删掉 —— 不然
    40054010 会把**整条互转**（含回复正文）一起拒掉。
    """
    text = " ".join(_outer_quote_content(text).split())
    if strip_url:
        text = _QUOTE_URL_RE.sub("", text)
        text = " ".join(text.split())
    if len(text) > QUOTE_MAX_CHARS:
        text = text[:QUOTE_MAX_CHARS] + "……"
    return text

# 判"这个附件是不是图片"用的后缀黑名单。
# 只列**肯定不是图片**的 —— 判不出来的按图片试，因为群里附件绝大多数是图，
# 而 QQ 的 CDN 地址常常不带扩展名、也没有 content_type 字段。
_NOT_IMAGE_SUFFIXES = (
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm",
    ".silk", ".amr", ".mp3", ".m4a", ".wav", ".aac",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt",
)


def split_rule(line):
    """把一行规则 `源 -> 目标` 拆成 (源, 目标)。

    支持 `->` 和 `→`，两边空白随意。拆不出两个非空值就返回 (None, None)。

    **这里不判「源和目标相同」**：两边可能一个写 openid、一个写群号，
    要解析成 openid 之后才知道是不是同一个群。判等放在 parse_rules 里做，
    这样报错信息能准确说清是哪个群。
    """
    raw = str(line or "").strip()
    if not raw or raw.startswith("#"):
        return None, None
    for arrow in ("->", "→"):
        if arrow in raw:
            source, _, target = raw.partition(arrow)
            source, target = source.strip(), target.strip()
            if source and target:
                return source, target
            return None, None
    return None, None


def looks_like_group_number(token):
    """判断写的是 QQ 群号还是群 openid。

    群号是 **5~12 位纯数字**；群 openid 是 32 位十六进制（含字母）。
    按"纯数字且长度 <= 12"来分，两者不会撞 —— openid 不可能是这么短的纯数字。
    """
    raw = str(token or "").strip()
    return raw.isdigit() and 5 <= len(raw) <= 12


def group_number_map(data=None):
    """{QQ群号: 群openid} —— 把规则里的群号翻译成 openid。

    两个来源，后写的覆盖先写的：
      1. **群备注**（配置页「群聊」页填的那个）—— 那里填的就是群号，
         所以不用再维护一份映射表，填过备注的群直接能用群号写规则；
      2. **relay_group_ids**（一行 `群号=openid`）—— 给"没填备注、
         或备注里写的不是群号"的群兜底，也能用来强制指定。
    """
    data = data if isinstance(data, dict) else {}
    result = {}

    # 1) 群备注反查。备注里可能带别的字（"4群 123456789"），所以抠出数字再看。
    try:
        notes = read_notes()
    except Exception as error:
        _log.warning(f"[botpy] 读群备注失败，群号映射可能不全: {error}")
        notes = {}
    for openid, note in (notes or {}).items():
        number = digits_of(note)
        if looks_like_group_number(number):
            result.setdefault(number, str(openid))

    # 2) 显式映射表（覆盖备注里推出来的）
    for line in split_lines(data.get("relay_group_ids")):
        raw = str(line or "").strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        number, _, openid = raw.partition("=")
        number, openid = digits_of(number), openid.strip()
        if looks_like_group_number(number) and openid:
            result[number] = openid
    return result


def group_openid_map(data=None):
    """{群openid: 群号} —— group_number_map 的反查。

    给「显示」用：日志和「转发状态」指令里出现 32 位 openid 没人看得懂，
    能换成群号就换。同一群号指向多个 openid 时以先出现的为准（几乎不会发生）。
    """
    return {openid: number for number, openid in group_number_map(data).items()}


def block_keywords(data=None):
    """互转过滤词列表（relay_block_keywords，一行一个）。

    写法刻意和「拉黑内容」（blacklist_keywords）保持一致：都是换行分隔。
    这两个设置经常要一起维护（哪些话不该出现、哪些话不该转出去），
    分隔符不一样只会让人填错行。

    留空 = 不过滤。**不做任何加工**：写什么就是什么，
    过滤是最容易被"悄悄改行为"的地方（比如未经允许地大小写不敏感）。
    """
    data = data if isinstance(data, dict) else {}
    return split_lines(data.get("relay_block_keywords"))


def matched_block_keyword(content, data=None):
    """正文命中了哪条过滤词；没命中返回空串（给日志点名用）。"""
    body = str(content or "")
    if not body:
        return ""
    for word in block_keywords(data):
        if word in body:
            return word
    return ""


def resolve_group(token, numbers=None):
    """把一个群标识解析成群 openid，返回 (openid, 失败原因)。

    写法：
      · 群 openid 本身 —— 直接用
      · QQ 群号 —— 查映射表

    失败时返回 ("", 给用户看的原因)，调用方据此记日志 / 面板提示。
    """
    raw = str(token or "").strip()
    if not raw:
        return "", text.text("messages.relay.empty_token")
    if not looks_like_group_number(raw):
        # 不是群号就当成 openid 用 —— 老写法继续有效，不做格式校验
        # （平台以后加长 openid 也不会把这里写死）
        return raw, ""
    openid = (numbers or {}).get(raw)
    if openid:
        return str(openid), ""
    return "", text.text("messages.relay.unknown_number", number=raw)


def parse_rules(data=None):
    """解析 relay_rules，返回 (规则映射, 问题列表)。

    规则映射：{源群openid: [目标群openid, ...]}
    问题列表：[{line, reason}] —— 给配置页和日志用，写错的规则要能一眼看到。

    这是**纯函数**（只依赖传入的设置快照和磁盘上的备注），
    所以配置页能直接调它来显示"规则解析成了什么"，
    显示的必定和机器人实际执行的一致 —— 不会出现"页面说没问题、实际没生效"。
    """
    data = data if isinstance(data, dict) else {}
    numbers = group_number_map(data)
    result = {}
    issues = []

    for line in split_lines(data.get("relay_rules")):
        raw = str(line or "").strip()
        if not raw or raw.startswith("#"):
            continue
        source_token, target_token = split_rule(raw)
        if not source_token:
            issues.append(
                {"line": raw, "reason": text.text("messages.relay.bad_rule")}
            )
            continue

        source, why = resolve_group(source_token, numbers)
        if not source:
            issues.append({"line": raw, "reason": why})
            continue
        target, why = resolve_group(target_token, numbers)
        if not target:
            issues.append({"line": raw, "reason": why})
            continue

        if source == target:
            # 自己转给自己：转出去的内容会在同一个群再触发一次事件，
            # 靠 author.bot 兜住不会无限循环，但等于白白烧配额，直接拒掉
            issues.append(
                {"line": raw, "reason": text.text("messages.relay.same_group")}
            )
            continue

        result.setdefault(source, [])
        if target not in result[source]:
            result[source].append(target)

    return result, issues


class RelayQuota:
    """每群每天的主动消息计数（落盘在 forum_relay.json）。

    为什么必须自己做：平台不提供"还剩多少额度"的查询，撞上去就是
    40034100 硬失败。自己记账至少能在接近上限时提前跳过并留下日志，
    而不是让每条消息都去撞一次错误。
    """

    def __init__(self, path=None):
        self._path = path or config.RELAY_FILE
        self._data = self._load()

    @staticmethod
    def _today():
        # 平台的配额按自然日算（东八区），这里跟着用本地日期
        return time.strftime("%Y-%m-%d")

    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as file:
                data = json.load(file)
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if data.get("day") != self._today():
            # 跨天自动清零 —— 不留历史，免得文件越滚越大
            data = {}
        data.setdefault("day", self._today())
        sent = data.get("sent")
        data["sent"] = sent if isinstance(sent, dict) else {}
        return data

    def _save(self):
        try:
            with open(self._path, "w", encoding="utf-8") as file:
                json.dump(self._data, file, ensure_ascii=False, indent=2)
        except Exception as error:
            _log.warning(f"[botpy] 互转配额写入失败: {error}")

    def used(self, group_openid):
        return int(self._data["sent"].get(str(group_openid)) or 0)

    def allow(self, group_openid, limit):
        return self.used(group_openid) < max(1, int(limit or DEFAULT_DAILY_LIMIT))

    def count(self, group_openid):
        key = str(group_openid)
        self._data["sent"][key] = self.used(key) + 1
        self._save()

    def snapshot(self):
        """今天各群已发出的条数（给「转发状态」指令用）。"""
        return {key: int(value or 0) for key, value in self._data["sent"].items()}


def _pick(source, key):
    """从 dict 或对象里取值 —— 接口返回的形态可能是两种。

    群消息的 `attachments` 是 JSON 解出来的 **dict**，而接口返回的
    `Media` 可能是对象 —— 所以不能只用 getattr 或只用 []，
    两种都得认（原来的 getattr 写法对 dict 一律返回 None）。
    """
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def is_image_attachment(item):
    """判断一个附件是不是图片。

    先看 content_type，没有就看文件名 / URL 的后缀。
    **两者都判不出来时按图片处理** —— QQ 的 CDN 地址往往不带扩展名，
    也没有 content_type；真要是视频，上传会失败并记一条明确的日志，
    不会静默出错（静默才是最难查的）。
    """
    content_type = str(_pick(item, "content_type") or "").strip().lower()
    if content_type:
        return content_type.startswith("image/")
    # 去掉 query（带签名的地址长这样：xxx.mp4?term=2&...）
    name = str(_pick(item, "filename") or _pick(item, "url") or "").lower().split("?")[0]
    return not name.endswith(_NOT_IMAGE_SUFFIXES)


class RelayGuard:
    def __init__(self, client, settings, members=None, index=None, bindings=None):
        self._client = client
        self._settings = settings
        # 名册用来兜底取昵称（消息里一般直接有 username）
        self._members = members
        # 消息索引：把转发出去的消息也记进去，目标群里「撤回上一条」才有效
        self._index = index
        # 成员自助绑定的 QQ 号（bindings 模块）。不传就当成"没人绑定"，
        # 而不是报错 —— 这样它能单独构造出来做测试
        self._bindings = bindings
        self.quota = RelayQuota()
        # 上一次的规则问题集合。rules() 每条群消息都会调，只在"问题变了"时记日志
        self._last_issues = None
        # 「只转发已绑定成员」挡下过哪些人（每个只记一次日志，见 _note_unbound）
        self._unbound_seen = set()
        # 「(目标群, 源群msg_idx) -> 它在目标群转发的 ref_idx」（复用账本，见
        # _remember_forward_ref）。被引用的消息以前转发过时直接挂引用（省一条消息）
        self._forwarded_refs = {}

    # ---------------- 引用复用映射 ----------------
    def _ref_of(self, target, source_idx):
        """这条源群消息在目标群的转发还在不在（没有/过期返回 None）。"""
        if not source_idx:
            return None
        entry = self._forwarded_refs.get((str(target), str(source_idx)))
        if not entry:
            return None
        ref_idx, ts = entry
        if time.time() - ts > _FORWARDED_REF_TTL:
            self._forwarded_refs.pop((str(target), str(source_idx)), None)
            return None
        return ref_idx

    def _remember_forward_ref(self, target, source_idx, ref_idx):
        """记一条账：「源群消息 -> 它在目标群的转发」。

        **所有转发都入账**（含带引用的回复）：被引用时直接挂到转发上，
        绝不补发原文 —— 为"避免引用链"而重复发原文，群里刷屏更糟。
        引用链万一断掉（中间被撤回），由 _send_text 的失效降级重发兜底。
        """
        if not source_idx or not ref_idx:
            return
        now = time.time()
        self._forwarded_refs[(str(target), str(source_idx))] = (ref_idx, now)
        if len(self._forwarded_refs) <= _FORWARDED_REF_LIMIT:
            return
        cutoff = now - _FORWARDED_REF_TTL
        for key in [k for k, v in self._forwarded_refs.items() if v[1] < cutoff]:
            self._forwarded_refs.pop(key, None)
        while len(self._forwarded_refs) > _FORWARDED_REF_LIMIT:
            self._forwarded_refs.pop(next(iter(self._forwarded_refs)))

    # ---------------- 配置解析 ----------------
    def rules(self):
        """解析互转规则，返回 {源群openid: [目标群openid, ...]}。

        规则里可以写**群号**（推荐）或 openid，详见模块文档。
        解析用的是和配置页同一个函数，所以页面上显示的就是实际执行的。
        """
        result, issues = parse_rules(self._settings.snapshot())
        # 只在问题集合**变化**时记日志：rules() 是每条群消息都要走的，
        # 无条件记会把日志刷爆，反而把真正的问题淹掉
        signature = tuple((item["line"], item["reason"]) for item in issues)
        if signature != self._last_issues:
            self._last_issues = signature
            for issue in issues:
                _log.warning(
                    f"[botpy] 互转规则已跳过：{issue['line']!r} —— {issue['reason']}"
                )
        return result

    def rules_report(self):
        """给配置页看的规则解析结果（含每行原文与问题）。

        页面拿它显示"这条规则解析到了哪个群"，写错了当场就能看见，
        不用去翻 journalctl。
        """
        data = self._settings.snapshot()
        rules, issues = parse_rules(data)
        routes = [
            {"source": source, "target": target}
            for source, targets in rules.items()
            for target in targets
        ]
        return {
            "rules": routes,
            "issues": issues,
            "sources": len(rules),
            "routes": len(routes),
            "number_map": group_number_map(data),
            "label": group_openid_map(data),
        }

    def _notes(self):
        """解析成员标识映射，返回 {openid: 备注}。

        一行一条 `openid=备注`。备注里填 QQ 号，转发里就会显示成 `昵称(10086)`。
        """
        raw = self._settings.snapshot().get("relay_notes")
        result = {}
        for line in split_lines(raw):
            if "=" not in str(line):
                continue
            key, _, value = str(line).partition("=")
            key, value = key.strip(), value.strip()
            if key and value:
                result[key] = value
        return result

    def _manual_note(self, openid, notes=None):
        """relay_notes 里管理员手工填的那个标识；没有返回空串。"""
        key = str(openid or "")
        if not key:
            return ""
        return str((notes if notes is not None else self._notes()).get(key) or "")

    def _qq_of(self, openid, notes=None):
        """这个人对应的 QQ 号；没有就返回空串。

        顺序和 _display_id 完全一致（管理员手工 > 自助绑定）——
        两处必须同源，否则会出现「{uid} 显示 123456789 而 {qq} 是空」
        这种自相矛盾的结果。

        管理员在 relay_notes 里填过的也算"已绑定"：他明确识别过这个人，
        信号比自助绑定还强，没理由排除（「只转发已绑定成员」用的就是它）。
        """
        manual = self._manual_note(openid, notes)
        if manual:
            return manual
        if self._bindings is None:
            return ""
        return self._bindings.qq_of(openid)

    def _display_id(self, openid, notes):
        """转发里括号中的那个标识。

        **平台拿不到 QQ 号**，所以只能这样退：
          1. relay_notes 里管理员手工填的（能纠正用户填错的，所以优先）
          2. 成员自助绑定的 QQ 号
          3. member_openid 前 8 位 —— 稳定、能区分人，但它**不是** QQ 号
        """
        qq = self._qq_of(openid, notes)
        if qq:
            return qq
        key = str(openid or "")
        if key:
            return key[:8]
        return text.text("messages.relay.unknown_member")

    def format_values(self, message, data=None, body=None):
        """relay_format 的占位符取值表。

        **所有支持的占位符都必须在这里给值** —— `str.format` 遇到没给值的占位符会
        抛 KeyError，而我们的兜底是"退回默认格式"，结果就是用户写的格式被**静默丢掉**。
        加新占位符时务必同步加到这里（配置页的实时预览用的是同一份取值）。

        body 传了就当 {content} 用（纯图片消息会传「[图片]」这种说明文字）；
        不传就取消息本身的正文（经过 readable_content，表情会还原成 [表情名]）。
        """
        data = data if isinstance(data, dict) else self._settings.snapshot()
        openid = member_openid_of(message)
        nickname = member_name_of(message)
        if not nickname and self._members is not None:
            entry = self._members.find(openid) or {}
            nickname = str(entry.get("username") or "")

        # 源群的 QQ 群号 —— 消息来自哪个群。
        # 平台接口不返回群号，只能从「群聊」页填的群备注推出来（见 group_openid_map）；
        # 没填备注就退回 openid 前 10 位，仍然能区分是哪个群。
        group = self._group_label(
            str(getattr(message, "group_openid", None) or ""), group_openid_map(data)
        )
        content = (
            body if body is not None
            else readable_content(getattr(message, "content", None) or "")
        )
        # notes 只解析一次：_display_id 和 _qq_of 都要用它，
        # 两处必须看到同一份，否则同一行文本里 {uid} 和 {qq} 会对不上
        notes = self._notes()
        return {
            "nick": nickname or text.text("messages.relay.no_nick"),
            "uid": self._display_id(openid, notes),
            # {qq} 是"明确的 QQ 号"，**没绑定就是空串**。
            # 和 {uid} 分开是为了给想自己排版的人一个选择：
            # {uid} 永远有值（会退到 openid 前 8 位），{qq} 是"没有就是没有"
            "qq": self._qq_of(openid, notes),
            "content": content,
            "role": role_label(getattr(getattr(message, "author", None), "member_role", "")),
            # {group} 和 {group_no} 是同一个值：前者短、好写，
            # 后者跟「群号」这个说法对得上，两种都认，省得猜
            "group": group,
            "group_no": group,
        }

    def format_for(self, data, source):
        """来源群专属的转发格式（relay_format_by_group），没有返回空串。

        一行一条 `群号=格式`（左边填群 openid 也认，群号靠备注/
        relay_group_ids 翻译）。命中的群用**自己的格式**；
        没配的群退回全局 relay_format —— 两层是覆盖关系不是替换关系。
        """
        source = str(source or "")
        if not source:
            return ""
        number = group_openid_map(data).get(source, "")
        for line in split_lines(data.get("relay_format_by_group")):
            key, sep, fmt = str(line or "").partition("=")
            key, fmt = key.strip(), fmt.strip()
            if not sep or not fmt:
                continue
            if key == source or (number and key == number):
                return fmt
        return ""

    def render(self, message, data=None, body=None):
        """按转发格式把一条消息渲染成待发送的文本。

        格式取值顺序：来源群专属（relay_format_by_group）> 全局 relay_format
        > 内置默认。
        """
        data = data if isinstance(data, dict) else self._settings.snapshot()
        values = self.format_values(message, data, body=body)
        source = str(getattr(message, "group_openid", None) or "")
        template = (
            self.format_for(data, source)
            or str(data.get("relay_format") or "").strip()
            or DEFAULT_FORMAT
        )
        # 支持 \n 字面写法：文本框里直接回车当然可以，但有人习惯写 "\n"，
        # 或输入环境把回车弄丢了 —— 这里统一转成真实换行
        template = template.replace("\\n", "\n")
        try:
            return template.format(**values)
        except (KeyError, IndexError, ValueError) as error:
            # 格式串写错了也不能让消息丢掉，退回默认格式
            _log.warning(
                f"[botpy] 互转格式串有问题（{error}），本条改用默认格式"
            )
            return DEFAULT_FORMAT.format(**values)

    # ---------------- 主流程 ----------------
    async def relay(self, message, is_command=False):
        """把一条群消息转发到配置的目标群，返回**成功发出的消息条数**。

        注意返回的是"条数"不是"群数"：一条「文字 + 图」的消息转到 1 个群，
        会发出 2 条（文字 1 条、图片 1 条），各占 1 份主动消息配额。
        """
        data = self._settings.snapshot()
        if not data.get("relay_enabled"):
            return 0

        source = str(getattr(message, "group_openid", None) or "")
        targets = self.rules().get(source) or []
        if not targets:
            return 0

        # 环路防护：机器人自己发的消息不转。
        # 双向互转时这条是必须的 —— 转发出去的内容是以机器人身份发的，
        # 在目标群里会被这里挡掉，环路断开。
        # 判据复用 admins.is_bot_message（auto_recall / members 用的是同一个）。
        if is_bot_message(message):
            return 0

        if data.get("relay_bound_only"):
            # 只转发"有对应 QQ 号"的成员的消息（成员自助绑的，或管理员在
            # relay_notes 里填过的）。没绑定的按**他不想被转**处理 ——
            # 宁可少转，不可替人公开。
            # 这正是这个开关存在的意义：让群成员**自己**决定，而不是由管理员
            # 替全场决定要不要把大家的话转到另一个群。
            sender = member_openid_of(message)
            if not self._qq_of(sender):
                self._note_unbound(sender, message)
                return 0

        if is_command and data.get("relay_skip_commands", True):
            return 0

        content = readable_content(getattr(message, "content", None) or "")

        # 关键词过滤：命中就整条不转。
        # **只是不转，不撤回** —— 原群里照常说，只是对面收不到（见模块说明 6）。
        # 日志点名命中了哪个词：过滤词填歪了（比如写成了别人的口头禅）时，
        # 这是唯一能看出"为什么这条没转"的地方。
        hit = matched_block_keyword(content, data)
        if hit:
            _log.info(
                f"[botpy] 互转跳过命中过滤词「{hit}」的消息：{content[:40]!r}"
            )
            return 0

        # 主动消息不允许带 URL（40054010），含链接的正文发不出去，整条跳过。
        # 注意图片不算「带 URL」—— 那个 URL 是**上传接口的参数**，
        # 不是消息内容，平台不会拦。
        if content and data.get("relay_skip_url", True) and _URL_RE.search(content):
            _log.info(
                f"[botpy] 互转跳过含链接的消息（平台不允许主动消息带 URL）：{content[:40]!r}"
            )
            return 0

        images = self._image_urls(message, data)
        if not content and not images:
            # 既没正文也没图（纯视频/语音/文件，或者空消息）—— 没什么可转的
            return 0

        # 被引用的消息：目标群看不到源群的引用上下文，直接转过去会
        # 「不知道他在回复什么」。做法是先转一条**被引用的原文**，
        # 再把回复消息用 message_reference 挂到它上面（官方支持引用
        # 「机器人自己发的消息」，正好是我们刚转发的那条）。
        quoted_text = ""
        if data.get("relay_quote", True):
            quoted_text = _sanitize_quote(
                quoted_preview_of(message),
                strip_url=bool(data.get("relay_skip_url", True)),
            )

        # 图文消息是分开发的：正文一条、每张图各一条。
        # **只有图片、没有正文时，额外补一条说明是谁发的** —— 否则目标群里只会
        # 冒出一张图，完全不知道谁发的（这是线上反馈的问题）。
        #
        # 为什么不把说明塞进富媒体消息的 content：msg_type=7 的 content 行为
        # 各版本不一致（可能被忽略、也可能直接报错），而"先发一条纯文本"走的
        # 是已经验证可靠的通道 —— 确定能显示。代价是多花 1 条配额，
        # 而且只在"整条消息没有正文"时才花。
        if content:
            line = self.render(message, data)
        elif images:
            line = self.render(message, data, body=text.text("messages.relay.media_only"))
        else:
            line = ""

        limit = data.get("relay_daily_limit") or DEFAULT_DAILY_LIMIT
        # 日志和提示里一律显示群号，openid 那串 32 位十六进制没人看得懂
        labels = group_openid_map(data)
        from_label = labels.get(source) or source

        sent = 0
        # 本条消息自己的源群 msg_idx：发出去以后记进复用映射，
        # 以后别人引用它时就能直接挂引用（省一条补发）
        source_idx = message_index_of(message)
        # 被引用消息的源群 msg_idx：拿它查「以前转发过没有」
        quoted_idx = quoted_index_of(message) if quoted_text else None

        for target in targets:
            where = labels.get(target) or target

            # 被引用的原文：**优先复用以前转发过的**（直接挂引用，省一条消息）；
            # 没转发过才补发一条原文，并把它的 ref_idx 记进复用映射
            ref_idx = None
            if quoted_text:
                ref_idx = self._ref_of(target, quoted_idx) if quoted_idx else None
                if ref_idx is None:
                    if self.quota.allow(target, limit):
                        ref_idx, quote_sent = await self._send_quote(
                            from_label, target, quoted_text, where
                        )
                        if quote_sent:
                            sent += 1
                            self._remember_forward_ref(target, quoted_idx, ref_idx)
                    else:
                        self._warn_quota(where, limit)

            if line:
                body = line
                if quoted_text and not ref_idx:
                    # 引用原文没发出去（配额/接口失败）-> 降级成拼接文本，
                    # 上下文还是要给，宁可多一行
                    body += "\n" + text.text(
                        "messages.relay.quote_fallback", quote=quoted_text
                    )
                if self.quota.allow(target, limit):
                    ok, reply_ref = await self._send_text(
                        from_label, target, body, where, reference_id=ref_idx
                    )
                    sent += 1 if ok else 0
                    # **所有**转发都记入复用账本（含带引用的回复）：
                    # 别人引用它时直接挂到这条转发上，绝不再补发原文。
                    # 带引用的回复被引用会形成引用链（C挂A、A挂B）——
                    # 可以接受：链上断一条还有"失效降级重发"兜底，
                    # 而为避免链就重复发原文，群里刷屏更不能接受
                    self._remember_forward_ref(target, source_idx, reply_ref)
                else:
                    self._warn_quota(where, limit)

            for url in images:
                if not self.quota.allow(target, limit):
                    self._warn_quota(where, limit)
                    break
                sent += await self._send_image(target, url, where, from_label)
        return sent

    @staticmethod
    def _warn_quota(where, limit):
        _log.warning(
            f"[botpy] 互转：群 {where} 今天已达上限 {limit} 条，本条跳过"
            f"（平台硬限是每群每天 1000 条主动消息）"
        )

    def _note_unbound(self, openid, message):
        """「只转发已绑定成员」挡下一条消息时记一次日志。

        **每个成员最多记一次**（总上限 _UNBOUND_LOG_LIMIT）：
        一个没绑定的活跃成员每说一句话就刷一行的话，日志里真正的问题会被淹掉；
        但完全不记更糟 —— 用户开了开关却一条都没转，会以为是功能坏了。
        """
        key = str(openid or "")
        if key in self._unbound_seen:
            return
        if len(self._unbound_seen) >= _UNBOUND_LOG_LIMIT:
            return
        # 认不出是谁（key 为空）时记一次就够，不然这类消息会一直刷
        self._unbound_seen.add(key)
        name = member_name_of(message) or (key[:8] if key else "")
        _log.info(
            f"[botpy] 互转：{name or '(未知成员)'} 还没识别到 QQ 号，这条按"
            f"「只转发已绑定成员」跳过（QQ 号自动识别见配置页「QQ 号识别」页）"
        )

    def _image_urls(self, message, data):
        """从群消息里挑出要转的图片地址（最多 relay_media_limit 张）。"""
        if not data.get("relay_media"):
            return []
        try:
            limit = int(data.get("relay_media_limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return []

        attachments = list(getattr(message, "attachments", None) or [])
        urls = []
        for item in attachments:
            if len(urls) >= limit:
                break
            if not is_image_attachment(item):
                continue
            url = _pick(item, "url")
            if url:
                urls.append(str(url))
        if attachments and not urls:
            # 有附件但一张图都没挑出来 —— 多半是视频/语音/文件。
            # 记一条 INFO，免得"配了转图片却什么都没转"查不出原因。
            _log.info("[botpy] 互转：本条消息的附件不是图片，跳过（目前只转图片）")
        return urls

    async def _send_text(self, from_label, target, body, where, reference_id=None):
        """发一条文本。返回 (是否成功, 这条消息在目标群的 ref_idx 或 None)。

        reference_id 传目标群的 ref_idx 时挂引用（引用复用/引用转发都用它）；
        返回的 ref_idx 给复用映射记账 —— 以后别人引用这条消息时直接挂它。
        """
        try:
            result = await self._client.api.post_group_message(
                group_openid=target,
                content=body,
                message_reference=(
                    {"message_id": reference_id, "ignore_get_message_error": True}
                    if reference_id
                    else None
                ),
            )
        except Exception as error:
            if not reference_id:
                _log.warning(
                    f"[botpy] 互转发送失败 {from_label} -> {where}: {error}"
                    f"（若为 40034100 说明主动消息超额，40054010 说明内容含 URL）"
                )
                return 0, None
            # 挂引用失败：多半是引用的锚点已被撤回/删除（复用映射里还记着它）。
            # 去掉引用**再试一次** —— 消息内容本身比引用样式重要，
            # 不能让一条过期的锚点把整条转发拖死
            _log.info(f"[botpy] 引用发送失败（锚点可能已失效），降级重发: {error}")
            try:
                result = await self._client.api.post_group_message(
                    group_openid=target,
                    content=body,
                    message_reference=None,
                )
            except Exception as retry_error:
                _log.warning(
                    f"[botpy] 互转发送失败（含降级重试）{from_label} -> {where}: "
                    f"{retry_error}"
                )
                return 0, None
        self.quota.count(target)
        self._remember_sent(target, result)
        ext = _pick(result, "ext_info")
        ref_idx = _pick(ext, "ref_idx") if ext else None
        _log.info(
            f"[botpy] 互转 {from_label} -> {where}：{body[:40]!r}"
            f"（今日第 {self.quota.used(target)} 条）"
        )
        return 1, (str(ref_idx) if ref_idx else None)

    async def _send_quote(self, from_label, target, quote, where):
        """先转一条**被引用的原文**，返回 (目标群的 ref_idx 或 None, 是否发出)。

        官方限制：message_reference 只能引用「机器人自己发的消息」——
        我们刚转发的这条正好算。失败返回 (None, False)，调用方降级成拼接文本。
        """
        try:
            result = await self._client.api.post_group_message(
                group_openid=target, content=quote
            )
        except Exception as error:
            _log.warning(f"[botpy] 互转引用原文发送失败 {from_label} -> {where}: {error}")
            return None, False
        self.quota.count(target)
        self._remember_sent(target, result)
        ext = _pick(result, "ext_info")
        ref_idx = _pick(ext, "ref_idx") if ext else None
        _log.info(f"[botpy] 互转引用原文 {from_label} -> {where}：{quote[:40]!r}")
        return (str(ref_idx) if ref_idx else None), True

    async def _send_image(self, target, url, where, from_label):
        """把一张图转到目标群，成功返回 1。

        两步走，缺一不可：
          1. POST /v2/groups/{id}/files —— 让平台**自己去抓**这张图，换回 file_info；
          2. POST /v2/groups/{id}/messages（msg_type=7）—— 用 file_info 把图发出去。

        第 1 步的 srv_send_msg 传 False（只上传、不发送）。传 True 虽然一步到位，
        但会绕开我们自己的配额记账 —— 那样"今天发出去多少条"就没数了，
        而配额撞上去是硬失败（40034100），必须自己盯着。
        """
        try:
            media = await self._client.api.post_group_file(
                group_openid=target,
                file_type=MEDIA_FILE_TYPE_IMAGE,
                url=url,
                srv_send_msg=False,
            )
        except Exception as error:
            _log.warning(
                f"[botpy] 互转图片上传失败 {from_label} -> {where}: {error}"
                f"（源图地址带签名会过期 —— 收到后隔太久才转基本必失败）"
            )
            return 0

        file_info = _pick(media, "file_info")
        if not file_info:
            _log.warning(
                f"[botpy] 互转图片上传没返回 file_info -> {where}：{media!r}"
            )
            return 0

        try:
            result = await self._client.api.post_group_message(
                group_openid=target,
                msg_type=MEDIA_MSG_TYPE,
                content="",
                media={"file_info": str(file_info)},
            )
        except Exception as error:
            _log.warning(f"[botpy] 互转图片发送失败 -> {where}: {error}")
            return 0

        self.quota.count(target)
        self._remember_sent(target, result)
        _log.info(
            f"[botpy] 互转图片 {from_label} -> {where}"
            f"（今日第 {self.quota.used(target)} 条）"
        )
        return 1

    def _remember_sent(self, group_openid, result):
        """把转发出去的消息记进索引，目标群里「撤回上一条」才能撤掉它。"""
        if self._index is None:
            return
        message_id = _pick(result, "id")
        if not message_id:
            return
        ext = _pick(result, "ext_info")
        ref_idx = _pick(ext, "ref_idx") if ext else None
        try:
            self._index.remember_sent(group_openid, message_id, ref_idx)
        except Exception as error:
            _log.warning(f"[botpy] 互转记录消息索引失败: {error}")

    # ---------------- 给配置页/指令用 ----------------
    def describe(self):
        """一句话概况（启动日志和「转发状态」指令共用）。"""
        data = self._settings.snapshot()
        if not data.get("relay_enabled"):
            return text.text("messages.relay.state_off")
        rules = self.rules()
        total = sum(len(items) for items in rules.values())
        return text.text(
            "messages.relay.summary", sources=len(rules), routes=total
        )

    @staticmethod
    def _group_label(openid, labels):
        """群里显示用的群标识：能查到群号就用群号，查不到退回 openid 前 10 位。

        规则里现在可以写群号，所以显示也统一成群号 —— 让人能对上自己填的东西。
        """
        key = str(openid or "")
        if key in labels:
            return labels[key]
        return key[:10] + ("…" if len(key) > 10 else "")

    def status_lines(self):
        """「转发状态」指令的正文（不含标题）。"""
        data = self._settings.snapshot()
        limit = data.get("relay_daily_limit") or DEFAULT_DAILY_LIMIT
        lines = [text.text("messages.relay.status_limit", limit=limit)]

        rules, issues = parse_rules(data)
        labels = group_openid_map(data)

        if not rules:
            lines.append(text.text("messages.relay.no_rules"))
        else:
            lines.append(text.text("messages.relay.rules_header", count=len(rules)))
            used = self.quota.snapshot()
            for source, targets in rules.items():
                for target in targets:
                    lines.append(
                        "　"
                        + text.text(
                            "messages.relay.rule_line",
                            source=self._group_label(source, labels),
                            target=self._group_label(target, labels),
                            used=used.get(target, 0),
                            limit=limit,
                        )
                    )

        if issues:
            # 写错的规则**必须**报出来。静默跳过等于"配了却没生效"，
            # 那是最难查的一类问题。
            lines.append(text.text("messages.relay.issues_header", count=len(issues)))
            for issue in issues[:3]:
                lines.append(
                    "　"
                    + text.text(
                        "messages.relay.issue_line",
                        line=str(issue["line"])[:30],
                        reason=issue["reason"],
                    )
                )
            if len(issues) > 3:
                lines.append(
                    text.text("messages.relay.issues_more", count=len(issues) - 3)
                )

        if data.get("relay_media"):
            lines.append(
                text.text(
                    "messages.relay.media_state",
                    count=data.get("relay_media_limit") or 0,
                )
            )

        notes = self._notes()
        lines.append(
            text.text("messages.relay.notes_count", count=len(notes))
            if notes
            else text.text("messages.relay.no_notes")
        )

        # ---- 自助绑定的 QQ 号 ----
        bound = self._bindings.all() if self._bindings is not None else {}
        if bound:
            lines.append(text.text("messages.relay.bound_count", count=len(bound)))
            # 列前几个，管理员在群里就能核对谁绑了什么。
            # 按绑定时间**倒序**：刚绑的排前面 —— 最需要确认的就是刚发生的事。
            recent = sorted(
                bound.items(), key=lambda item: item[1].get("at") or 0, reverse=True
            )
            for openid, entry in recent[:5]:
                lines.append(
                    "　"
                    + text.text(
                        "messages.relay.bound_line",
                        nick=entry.get("nick") or openid[:8],
                        qq=entry.get("qq"),
                    )
                )
            if len(bound) > 5:
                lines.append(
                    text.text("messages.relay.bound_more", count=len(bound) - 5)
                )
        else:
            lines.append(text.text("messages.relay.no_bindings"))

        if data.get("relay_bound_only"):
            # 这个组合最需要一眼看出来：开关开着、却一个人都没绑 -> 一条都不转
            lines.append(
                text.text("messages.relay.bound_only_on", count=len(bound))
                if bound
                else text.text("messages.relay.bound_only_empty")
            )
        return lines
