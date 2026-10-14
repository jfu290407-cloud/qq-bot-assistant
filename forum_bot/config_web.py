# -*- coding: utf-8 -*-
"""Web 配置界面：纯标准库实现（http.server + threading），不需要任何 Web 框架。

启动机器人时会自动在后台线程起一个页面；也可以单独跑：
    python -m forum_bot.config_web
监听地址/端口在 config.yaml 的 web 段里配置。

界面是常见的后台布局：左侧固定导航 + 顶栏（面包屑 / 保存）+ 内容区。
**没有引入 Vue / Element Plus** —— 这个项目刻意保持零依赖、离线可用，
所以布局、表格、状态提示都用原生 HTML/CSS/JS 复刻。

⚠ 模板字符串的转义（踩过坑）
----------------------------
`_PAGE` 用 **原始字符串 r\"\"\"...\"\"\"**。因为模板里大量是 JS：
在普通字符串里写 `"\n"` 会被 Python 先解释成真换行，直接把 JS 的字符串字面量
拆断成两行 -> 整个 <script> 解析失败 -> 页面上所有按钮都会"没反应"。
用原始字符串后，`\n` 原样输出给 JS，所见即所得。
"""
import base64
import hmac
import json
import re
import ssl
import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from botpy import logging

from . import config, logbuf, text
from . import ai_split
from . import onebot
from .access import read_state
from .bindings import clear_bindings, read_bindings, unbind_qq
from .blacklist import Blacklist, MemberBlacklist
from .group_info import write_note, read_notes
# 「群消息互转」页要显示规则的解析结果，用的必须是机器人**同一个**解析函数 ——
# 否则会出现"页面说没问题、实际没生效"这种最难受的情况
from .relay import (
    group_number_map,
    group_openid_map,
    parse_rules,
    resolve_group,
    split_rule,
)
from .settings import (
    DEFAULT_SETTINGS,
    FIELD_META,
    Settings,
    _GLOBAL_ONLY_KEYS,
    split_lines,
)

__all__ = ["start_settings_web", "build_page", "SECTIONS"]

_log = logging.get_logger()

# 「这个请求的 body 还没读」的标记。
# 用 object() 而不是 None —— 合法的 body 就可能是 `null`，
# 拿 None 当标记会把"body 是 null"和"还没读"混成一件事。
_UNREAD = object()


def _escape(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _settings_view(settings, guild_id=""):
    """返回给配置页的设置：合并后的值 + 一份"当前编辑范围"的元信息。

    合并后的值直接平铺在顶层（表单就是这么读的），元信息放在 `__scope__` 里
    —— 它不是字段表里的键，读写表单时会自动忽略。
    """
    guild_id = str(guild_id or "")
    data = settings.snapshot(guild_id or None)
    data["__scope__"] = {
        "guild_id": guild_id,
        "overridden": settings.overridden_keys(guild_id) if guild_id else [],
        "profiles": settings.guild_ids(),
    }
    return data


def _relay_rule_lines(data):
    """(源openid, 目标openid) -> 那条规则的**原文**。

    页面上每条规则旁边有个「删除」，要精确删掉对应那一行，所以必须能拿到原文 ——
    规则里可能写的是群号、也可能是 openid，光靠 openid 是反推不回原文的。

    这里刻意走和 parse_rules **完全相同**的解析路径（同一个 split_rule /
    resolve_group / 群号映射），否则会出现"页面解析得出来、却删不掉"的规则。
    """
    numbers = group_number_map(data)
    lines = {}
    for line in split_lines(data.get("relay_rules")):
        raw = str(line or "").strip()
        if not raw or raw.startswith("#"):
            continue
        source_token, target_token = split_rule(raw)
        if not source_token:
            continue
        source, _ = resolve_group(source_token, numbers)
        target, _ = resolve_group(target_token, numbers)
        if source and target:
            lines.setdefault((source, target), raw)
    return lines


def _relay_rules_view(settings):
    """互转规则的解析结果：写的是什么、解析到了哪个群、哪条没生效。

    用的是和机器人**同一个** `parse_rules` —— 所以页面上显示的，
    就是机器人实际执行的结果，不会出现"页面说没问题、实际没生效"。
    """
    data = settings.snapshot()
    rules, issues = parse_rules(data)
    labels = group_openid_map(data)
    raw_lines = _relay_rule_lines(data)
    routes = [
        {
            "source": source,
            "target": target,
            "source_label": labels.get(source) or source,
            "target_label": labels.get(target) or target,
            # 原始那一行 —— 页面「删除」按它精确删（见 _relay_rule_lines）
            "line": raw_lines.get((source, target), ""),
        }
        for source, targets in rules.items()
        for target in targets
    ]
    return {
        "rules": routes,
        "issues": issues,
        # 群号 -> openid（写规则用）和 openid -> 群号（显示用），两个方向都给
        "number_map": group_number_map(data),
        "label": labels,
        "routes": len(routes),
    }


def _relay_bindings_view(settings):
    """已识别到 QQ 号的成员（配置页「成员绑定」页）。

    **只读展示 + 删除**。号码来自 OneBot 旁路自动识别（onebot 模块）；
    管理员要给某人手工指定标识，用 relay_notes：
    它优先级更高、还能纠正识别错的。两套入口各管一件事，
    不用在一张表里塞两种来源。
    """
    rows = []
    for openid, entry in read_bindings().items():
        stamp = float(entry.get("at") or 0)
        rows.append(
            {
                "openid": openid,
                "qq": entry.get("qq"),
                "nick": entry.get("nick"),
                "group_openid": entry.get("group_openid"),
                "source": entry.get("source"),
                "at": stamp,
                "at_text": (
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))
                    if stamp > 0 else ""
                ),
            }
        )
    # 最近的排前面：最需要确认的永远是刚发生的事
    rows.sort(key=lambda row: row["at"], reverse=True)
    return {
        "bindings": rows,
        "count": len(rows),
        "bound_only": bool(settings.snapshot().get("relay_bound_only")),
    }


def _member_ban_view():
    """成员黑名单（按 openid 封**人**）。

    **只读 + 删除**，不提供"手工新增" —— 新增走群里的「拉黑成员」指令
    （或者「拉黑」引用一条消息，会连人一起加）。理由：
    在网页上手工敲 32 位 openid 太容易填错，而填错的后果是"把别人封了"
    或者"封了个不存在的 id 还以为生效了"，两种都很难发现。
    这个操作应该发生在有上下文的地方（那条消息旁边）。
    """
    entries = MemberBlacklist().entries()
    return {
        "members": [
            {
                "openid": str(item.get("openid") or ""),
                "username": str(item.get("username") or ""),
                "group_openid": str(item.get("group_openid") or ""),
                "by_name": str(item.get("by_name") or ""),
                "at": str(item.get("at") or ""),
            }
            for item in entries
        ],
        "count": len(entries),
    }


def _common_places_view(settings, dashboard, channel_cli):
    """机器人和「扫码登录那个 QQ 号」**共同在**的地方。

    两栏，但**口径不一样，页面上必须说清楚**：

      · **频道**：CLI 的「我加入的频道」∩ 机器人同步到的频道 —— **真交集**
      · **QQ 群**：**做不了交集**。平台没有"某个 QQ 号加入了哪些群"这种接口，
        机器人只能知道自己被拉进了哪些群（靠群消息事件一点点记下来）。
        所以这一栏是"机器人在的群"，并把原因写在页面上 ——
        悄悄少一栏、或者拿机器人的群冒充交集，都比这糟。

    这个接口**会真调 CLI**（一两秒），所以只在用户点「获取」时才走，
    不在切页时自动拉。
    """
    data = dashboard.guilds() if dashboard is not None else {}
    bot_guilds = data.get("guilds") or []
    known_groups = data.get("known_groups") or []

    # 机器人侧的频道：先按 id 匹配，再按名字兜一层
    # （CLI 那边的 id 字段叫什么还没完全确定，名字对得上也算数）
    by_id, by_name = {}, {}
    for item in bot_guilds:
        guild_id = str(item.get("id") or "")
        name = str(item.get("name") or "").strip()
        if guild_id:
            by_id[guild_id] = item
        if name:
            by_name.setdefault(name, item)

    user_guilds, error, raw_keys, cli_total = [], "", [], 0
    if channel_cli is not None:
        user_guilds, error, raw_keys, cli_total = channel_cli.my_guilds()

    common = []
    for entry in user_guilds:
        guild_id = str(entry.get("guild_id") or "")
        name = str(entry.get("name") or "").strip()
        matched = by_id.get(guild_id) or (by_name.get(name) if name else None)
        if matched is None:
            continue  # 只要交集
        common.append(
            {
                "guild_id": str(matched.get("id") or guild_id),
                "name": str(matched.get("name") or name or ""),
                "channel_number": str(matched.get("channel_number") or ""),
                # 这个号以什么身份在里面（我创建的 / 我加入的 / 我管理的）
                "relations": list(entry.get("relations") or []),
            }
        )

    groups = [
        {
            "openid": str(row.get("group_openid") or ""),
            "number": str(row.get("number") or ""),
            "name": str(row.get("group_name") or ""),
            "label": str(row.get("label") or ""),
        }
        for row in known_groups
    ]

    return {
        "ok": True,
        "error": error,
        # 认不出 CLI 返回结构时，把**实际字段名**带回去让页面显示出来 ——
        # 照着补 parse_guild_list 就行，不用再让用户去服务器上手动跑一遍
        "raw_keys": raw_keys,
        "guilds": common,
        "groups": groups,
        "user_total": len(user_guilds),
        "bot_total": len(bot_guilds),
        # CLI 自己报的总数。和 user_total 对不上就说明漏抄了一类，
        # 页面上会提示出来 —— 静默少数几个频道是最难发现的
        "cli_total": cli_total,
    }


# ----------------------------------------------------------------------
# 侧栏菜单：key 要和 settings.FIELD_META 里的分组名对得上
# ----------------------------------------------------------------------
SECTIONS = (
    ("overview", "概况", "机器人现状与功能开关"),
    ("groups", "群聊", "机器人所在的群"),
    ("guilds", "频道", "频道与板块"),
    ("config", "配置中心", "全部设置按模块折叠分组"),
    ("strategy", "推送策略", "何时推送、免打扰时段"),
    ("content", "过滤与文案", "关键词过滤与消息文案"),
    ("card", "卡片样式", "Markdown 卡片与跳转按钮"),
    ("recall", "群消息撤回", "自动撤回与撤回提示"),
    ("block", "拉黑名单", "命中即撤回的内容"),
    ("ai", "AI 服务", "选厂商、填 API Key；各功能页单独决定要不要用"),
    ("admin", "权限管理", "谁能使用管理指令"),
    ("access", "加群解锁", "加群后开放板块权限"),
    # 互转拆成三页：一个页面塞十几项设置，真正要找的那一项反而找不到
    ("relay", "互转规则", "选群、规则与转发格式"),
    ("relay_filter", "互转过滤", "哪些消息不往对面群转"),
    ("relay_bind", "成员绑定", "已识别的 QQ 号与转发开关"),
    ("onebot", "QQ 号识别", "OneBot 旁路自动获取真实 QQ 号"),
    ("preview", "效果预览", "看看在群里长什么样"),
    ("logs", "运行日志", "机器人的实时输出"),
    ("cli", "QQ 登录", "两个登录入口：频道凭证（CLI）与群识别小号（NapCat）"),
    ("debug", "调试", "日志与排错"),
)

# 侧栏分组（按此顺序渲染菜单；key 必须在 SECTIONS 里）
MENU_GROUPS = (
    ("总览", ("overview",)),
    ("所在位置", ("groups", "guilds")),
    ("配置", ("config", "preview")),
    ("系统", ("cli", "logs", "debug")),
)

# 侧栏图标（内联 SVG 路径，描边风格）
_ICONS = {
    "overview": '<rect x="3" y="3" width="8" height="8" rx="1.6"/><rect x="13" y="3" width="8" height="5" rx="1.6"/><rect x="3" y="13" width="8" height="8" rx="1.6"/><rect x="13" y="10" width="8" height="11" rx="1.6"/>',
    "groups": '<path d="M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z"/><path d="M17 11.5a2.6 2.6 0 1 0 0-5.2 2.6 2.6 0 0 0 0 5.2z"/><path d="M2.5 20.5c0-3.3 2.9-5.5 6.5-5.5s6.5 2.2 6.5 5.5"/><path d="M16.5 15.2c2.8.2 5 2.1 5 5.3"/>',
    "guilds": '<circle cx="12" cy="12" r="9"/><path d="M3.2 9.5h17.6M3.2 14.5h17.6"/><path d="M12 3c2.6 3 2.6 15 0 18M12 3c-2.6 3-2.6 15 0 18"/>',
    # 滑杆：配置中心
    "config": '<path d="M4 7h9M17 7h3M4 12h3M11 12h9M4 17h13"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="19" cy="17" r="2"/>',
    "strategy": '<circle cx="12" cy="12" r="9"/><path d="M12 7.5V12l3.2 1.9"/>',
    "content": '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/><path d="M9 12h6M9 16h4"/>',
    "card": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 9.5h10M7 13.5h6"/>',
    "recall": '<path d="M4 10h9a5 5 0 0 1 0 10H8"/><path d="M4 10l4-4M4 10l4 4"/>',
    "block": '<circle cx="12" cy="12" r="9"/><path d="M6.2 6.2l11.6 11.6"/>',
    # 四角星：AI
    "ai": '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M18.5 15.5l.9 2.1 2.1.9-2.1.9-.9 2.1-.9-2.1-2.1-.9 2.1-.9z"/>',
    "admin": '<path d="M12 3l7 2.8v5.6c0 4.7-2.9 7.9-7 9.6-4.1-1.7-7-4.9-7-9.6V5.8z"/><path d="M9.2 12l2 2 3.6-3.8"/>',
    "access": '<rect x="4" y="10" width="16" height="10" rx="2"/><path d="M8 10V7.5a4 4 0 0 1 8 0V10"/>',
    # 双向箭头：一个群的消息流向另一个群
    "relay": '<path d="M4 8.5h13"/><path d="M14 5.5l3 3-3 3"/><path d="M20 15.5H7"/><path d="M10 12.5l-3 3 3 3"/>',
    # 漏斗：把不要的挡在外面
    "relay_filter": '<path d="M3.5 5h17l-6.5 7.6V20l-4-2.2v-5.2z"/>',
    # 人 + 牌子：成员身上挂个标识
    "relay_bind": '<circle cx="9.5" cy="8" r="3.5"/><path d="M3.5 20c0-3.3 2.7-5.4 6-5.4 1.2 0 2.3.3 3.2.8"/><path d="M14.5 17.2h6M17.5 14.2v6"/>',
    # 号码牌：真实 QQ 号识别
    "onebot": '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="8.5" cy="12" r="2"/><path d="M13 9.5h5M13 14.5h5M6 20h12"/>',
    "preview": '<path d="M2.5 12S6.5 6.5 12 6.5 21.5 12 21.5 12 17.5 17.5 12 17.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.6"/>',
    "debug": '<circle cx="12" cy="13.5" r="5"/><path d="M12 8.5V5.5M7.4 10.4L5 8M16.6 10.4L19 8M6.5 15.5H3.5M20.5 15.5h-3M7.4 18.4L5 20.8M16.6 18.4L19 20.8"/>',
}


def _icon(name):
    return (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">%s</svg>'
        % _ICONS.get(name, "")
    )


# ----------------------------------------------------------------------
# 字段渲染
# ----------------------------------------------------------------------
def _render_field(key, label, hint, kind, options, value):
    if kind == "select":
        opts = "".join(
            '<option value="%s"%s>%s</option>' % (val, " selected" if str(value) == val else "", text)
            for val, text in options
        )
        control = '<select id="%s">%s</select>' % (key, opts)
    elif kind == "checkbox":
        checked = " checked" if value else ""
        control = '<input type="checkbox" id="%s"%s>' % (key, checked)
    elif kind == "textarea":
        text = _escape("" if value is None else value)
        control = '<textarea id="%s" rows="5">%s</textarea>' % (key, text)
    else:
        control = '<input type="%s" id="%s" value="%s">' % (
            "number" if kind == "number" else "text",
            key,
            _escape("" if value is None else value),
        )
    # 标签旁的「?」tooltip：悬停显示纯文本版提示（hint 里的 HTML 标签剥掉）。
    # 下面的 hint 小字保留 —— 关键警告不能只藏在悬停里
    tip = re.sub(r"<[^>]+>", "", hint).strip()
    return (
        '<div class="field%s">'
        '<div class="lrow"><label for="%s">%s</label>'
        '<span class="tip" title="%s">?</span></div>'
        '%s<span class="hint">%s</span></div>'
        % (
            " wide" if kind == "textarea" else "",
            key,
            label,
            _escape(tip),
            control,
            hint,
        )
    )


# 表格骨架
_MEMBER_TABLE = """
        <table class="list" id="memberTable">
          <thead><tr><th>昵称</th><th>群内身份</th><th>member_openid</th><th>最后活跃</th><th></th></tr></thead>
          <tbody></tbody>
        </table>"""

_BLOCK_TABLE = """
        <table class="list" id="blockTable">
          <thead><tr><th>#</th><th>拉黑内容</th><th>来源</th></tr></thead>
          <tbody></tbody>
        </table>"""

# AI 拆分出的候选拉黑关键词（blacklist_ai_split）。列表由 JS 渲染：
# 每批候选一排勾选框，勾中「加入选中的」才真正进名单 —— AI 的建议必须人过目
_AI_SUGGEST_CARD = """
        <div class="card" style="margin-top:16px" id="aiSuggestCard">
          <h2>AI 候选关键词</h2>
          <p class="lead">到「AI 服务」页选好厂商、填好 API Key，并打开上面的
          「拉黑消息时用 AI 拆分候选关键词」之后，群里每拉黑一条消息，
          AI 会把正文拆成几个候选短语放在这里。
          <b>勾选后点「加入选中的」才真正进上面的拉黑名单</b>，AI 不会自动加。
          连接是否正常，用「AI 服务」页的「测试 AI 连接」按钮验证。</p>
          <div id="aiSuggestList"></div>
          <div id="aiSuggestFoot"></div>
        </div>"""

_MEMBER_BAN_TABLE = """
        <table class="list" id="memberBanTable">
          <thead><tr><th>昵称</th><th>member_openid</th><th>加入时间</th><th>来源群</th><th></th></tr></thead>
          <tbody></tbody>
        </table>"""

_ACCESS_TABLE = """
        <div id="accessTable"></div>"""


_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- 显式声明浅色：不然系统开了深色模式时，浏览器自带的表单控件和滚动条
     会按深色渲染，和页面的浅色设计打架 -->
<meta name="color-scheme" content="light">
<title>频道机器人 · 配置台</title>
<!-- 内联 favicon：之前没有，浏览器每次都会多打一个 /favicon.ico 的 404，
     而且浏览器标签页里十几个本地服务根本分不清哪个是哪个 -->
<link rel="icon" href='data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect width="24" height="24" rx="6" fill="%232f5fe0"/><path d="M7 9h10M7 13h6" stroke="white" stroke-width="2.2" stroke-linecap="round"/><circle cx="15.5" cy="16" r="1.7" fill="white"/></svg>'>
<style>
  /* ---------- 设计变量（改样式先改这里） ----------
     这个页面自托管、不能引外部字体，字体就用系统栈 —— 但其余视觉决定
     全部收敛成变量。之前的问题是混着两套灰（#eaecf0 系和 #d1d5db 系）、
     七八种手写浅灰底、圆角从 4 到 16 想到哪写到哪，看起来"哪里都差一点"，
     根源就是没有 token。 */
  :root {
    color-scheme: light;
    --sidebar-w: 248px;

    /* 灰阶：只用一个冷灰家族 */
    --bg: #f4f5f7;           /* 页面底 */
    --surface: #ffffff;      /* 卡片 */
    --surface-2: #f7f8fa;    /* 表头 / 次级底 */
    --line: #e4e7ec;         /* 常规边框 */
    --line-soft: #eef0f3;    /* 更浅的分隔线 */
    --line-strong: #d0d5dd;  /* 输入框描边 */

    --ink: #1a202c;          /* 主文字 */
    --ink-2: #414651;        /* 次级文字 */
    --muted: #667085;        /* 说明文字 */
    --faint: #98a2b3;        /* 最弱 */

    /* 唯一主色。原 #2b64f5 饱和度 91%，降一档更耐看，品牌感不变 */
    --accent: #2f5fe0;
    --accent-hover: #2651c7;
    --accent-soft: #eef3ff;
    --accent-line: #c6d6fa;

    --ok-ink: #067647;   --ok-bg: #e9f9f0;   --ok-line: #a9e8c6;
    --warn-ink: #b45309; --warn-bg: #fffaeb; --warn-line: #fde0b8;
    --err-ink: #b42318;  --err-bg: #fef3f2;  --err-line: #fecdca;

    /* 圆角：全页只用这四档 */
    --r-sm: 8px;    /* 按钮 / 输入框 / 小件 */
    --r-md: 12px;   /* 卡片 */
    --r-lg: 16px;   /* 头像 / 大块 */
    --r-pill: 999px;

    --shadow-sm: 0 1px 2px rgba(16, 24, 40, .05);
    --shadow-md: 0 6px 18px rgba(16, 24, 40, .08);
    --ring: 0 0 0 3px rgba(47, 95, 224, .18);

    --t: 160ms;
    --ease: cubic-bezier(.2, .7, .3, 1);
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink);
         font: 14px/1.65 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         -webkit-font-smoothing: antialiased; }
  a { color: inherit; text-decoration: none; }
  code { font-family: ui-monospace, Consolas, "Courier New", monospace; font-size: .92em; }
  /* 等宽数字：表格、统计卡、时间戳里的数字不会因为位数变化而抖动，
     这是数据面板最值得做的一项排版升级 */
  .num, table.list td, table.desc td, .stat .v, .clistat, .gt-when, .meta {
    font-variant-numeric: tabular-nums; }

  /* 键盘焦点：之前只有输入框有可见焦点，按钮 / 菜单 / 页签全靠"看不出来"，
     键盘用户根本不知道自己停在哪个控件上 */
  :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  input:focus-visible, select:focus-visible, textarea:focus-visible { outline: none; }

  /* 滚动条：浏览器默认的又粗又突兀，在深色日志框里尤其明显 */
  * { scrollbar-width: thin; scrollbar-color: #cfd4dc transparent; }
  *::-webkit-scrollbar { width: 9px; height: 9px; }
  *::-webkit-scrollbar-thumb { background: #cfd4dc; border-radius: var(--r-pill);
                               border: 2px solid transparent; background-clip: content-box; }
  *::-webkit-scrollbar-thumb:hover { background-color: #b3bac5; }
  *::-webkit-scrollbar-track { background: transparent; }

  /* ---------- 布局 ---------- */
  /* dvh 而不是 vh：手机浏览器的地址栏会让 100vh 比可视区高出一截，
     侧栏底部会被裁掉一条 */
  .layout { display: flex; min-height: 100dvh; align-items: flex-start; }
  .sidebar { width: var(--sidebar-w); flex: 0 0 var(--sidebar-w); height: 100dvh;
             position: sticky; top: 0; display: flex; flex-direction: column;
             background: var(--surface); border-right: 1px solid var(--line); }
  .brand { height: 56px; flex: 0 0 56px; display: flex; align-items: center; gap: 10px;
           padding: 0 16px; border-bottom: 1px solid var(--line); }
  .brand-mark { width: 28px; height: 28px; flex: 0 0 28px; border-radius: var(--r-sm);
                background: linear-gradient(135deg, var(--accent), #6b93f0); color: #fff;
                display: flex; align-items: center; justify-content: center;
                font-weight: 700; font-size: 14px; }
  .brand-name { font-weight: 600; letter-spacing: .01em; }
  .menu { flex: 1; min-height: 0; overflow-y: auto; padding: 6px 8px 14px; }
  .menu-group { padding: 14px 12px 6px; font-size: 11px; font-weight: 600;
                letter-spacing: .08em; color: var(--faint); }
  .menu-item { display: flex; align-items: center; gap: 10px; padding: 9px 12px;
               border-radius: var(--r-sm); color: var(--ink-2); cursor: pointer;
               user-select: none;
               transition: background var(--t) var(--ease), color var(--t) var(--ease); }
  .menu-item:hover { background: var(--surface-2); color: var(--ink); }
  .menu-item.active { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
  .menu-item svg { width: 17px; height: 17px; flex: 0 0 17px; }
  .sidebar-foot { flex: 0 0 auto; border-top: 1px solid var(--line); padding: 12px 16px;
                  font-size: 12px; color: var(--muted); }
  .sidebar-foot b { display: block; color: var(--ink); font-size: 13px; font-weight: 600; }

  .main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
  .topbar { position: sticky; top: 0; z-index: 8; min-height: 56px;
            display: flex; align-items: center; justify-content: space-between; gap: 16px;
            padding: 0 20px; background: var(--surface); border-bottom: 1px solid var(--line); }
  .crumb { min-width: 0; }
  /* 页标题和卡片标题之前同为 15px，整个页面没有视觉层级可言；
     拉开一档，页面在哪一页、卡片讲什么，扫一眼就能分清 */
  .crumb b { font-size: 16.5px; letter-spacing: -.01em; text-wrap: balance; }
  .crumb span { color: var(--faint); font-size: 12px; margin-left: 10px; }
  .topbar-actions { display: flex; align-items: center; gap: 10px; }
  .scope { display: flex; align-items: center; gap: 6px; font-size: 12.5px;
           color: var(--muted); white-space: nowrap; }
  .scope select { width: auto; max-width: 240px; padding: 6px 8px; font-size: 13px; }
  .scopebar { display: flex; align-items: center; gap: 10px; margin-bottom: 16px;
              padding: 10px 14px; border-radius: var(--r-sm); font-size: 13px;
              background: var(--warn-bg); border: 1px solid var(--warn-line);
              color: var(--warn-ink); }
  .scopebar .grow { flex: 1; min-width: 0; }
  .scopebar button { padding: 4px 10px; font-size: 12px; font-weight: 500; }
  .ov-badge { margin-left: 6px; }

  .content { padding: 20px 22px 28px; max-width: 1120px; }

  /* ---------- 卡片与表单 ---------- */
  .card { background: var(--surface); border: 1px solid var(--line);
          border-radius: var(--r-md); padding: 18px 20px 20px; box-shadow: var(--shadow-sm); }
  .card h2 { font-size: 15px; font-weight: 600; margin: 0 0 4px; letter-spacing: -.005em; }
  .card .lead { color: var(--muted); font-size: 12.5px; margin: 0 0 16px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px 20px; }
  .field { display: flex; flex-direction: column; gap: 6px; }
  .lrow { display: flex; align-items: center; gap: 6px; }
  .lrow label { font-weight: 600; color: var(--ink-2); }
  .field > label { font-weight: 600; color: var(--ink-2); }
  .field.wide { grid-column: 1 / -1; }
  .hint { color: var(--faint); font-size: 12px; }
  /* 标签旁的「?」帮助图标：悬停出原生 tooltip（title），克制不花哨 */
  .tip { display: inline-flex; align-items: center; justify-content: center;
         width: 15px; height: 15px; border-radius: 50%;
         border: 1px solid var(--line-strong); color: var(--faint);
         font-size: 10.5px; line-height: 1; cursor: help; user-select: none;
         transition: color var(--t) var(--ease), border-color var(--t) var(--ease); }
  .tip:hover { color: var(--accent); border-color: var(--accent-line); }
  /* 扫码登录的二维码（CLI 和 NapCat 共用） */
  img.qr { width: 208px; height: 208px; border: 1px solid var(--line);
           border-radius: var(--r-sm); background: #fff; }

  /* ---------- 群映射选择器 ---------- */
  .obg-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 10px; }
  .obg-list { max-height: 240px; overflow-y: auto; border: 1px solid var(--line);
              border-radius: var(--r-sm); background: var(--surface); }
  .obg-item { display: flex; align-items: center; gap: 8px; padding: 8px 12px;
              cursor: pointer; font-size: 13px; border-bottom: 1px solid var(--line);
              transition: background var(--t) var(--ease); }
  .obg-item:last-child { border-bottom: 0; }
  .obg-item:hover { background: var(--surface-2); }
  .obg-item.sel { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
  .obg-item .empty { padding: 12px; }

  /* ---------- 配置中心：Collapse 折叠分组 ---------- */
  /* 展开/收起用 grid 0fr->1fr 过渡：不用 JS 量高度，内容再高也平滑 */
  .collapse { display: flex; flex-direction: column; gap: 12px; }
  .citem { background: var(--surface); border: 1px solid var(--line);
           border-radius: var(--r-md); box-shadow: var(--shadow-sm); }
  .chead { display: flex; align-items: center; gap: 12px; width: 100%;
           padding: 14px 18px; border-radius: 0; text-align: left;
           background: var(--surface); color: var(--ink); box-shadow: none; }
  .chead:hover { background: var(--surface-2); box-shadow: none; }
  .chead:active { transform: none; }
  .chead .ct { font-size: 14.5px; font-weight: 600; flex: 0 0 auto; }
  .chead .cd { color: var(--faint); font-size: 12px; flex: 1; min-width: 0;
               overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .carrow { width: 17px; height: 17px; flex: 0 0 17px; color: var(--faint);
            transition: transform var(--t) var(--ease); }
  .citem.open .carrow { transform: rotate(180deg); }
  .cbody { display: grid; grid-template-rows: 0fr;
           transition: grid-template-rows 240ms var(--ease); }
  .citem.open .cbody { grid-template-rows: 1fr; }
  .cinner { min-height: 0; overflow: hidden; padding: 0 18px; }
  .citem.open .cinner { padding: 16px 18px 18px; border-top: 1px solid var(--line); }
  /* 底部操作条：吸在内容流里（不悬浮，克制） */
  .cfg-footer { display: flex; align-items: center; gap: 12px;
                margin-top: 16px; padding: 12px 18px;
                background: var(--surface); border: 1px solid var(--line);
                border-radius: var(--r-md); box-shadow: var(--shadow-sm); }
  .cfg-footer .grow { flex: 1; min-width: 0; }
  .cfg-head { margin-bottom: 14px; }
  .cfg-head h2 { font-size: 17px; }
  input[type=text], input[type=number], select, textarea {
    width: 100%; padding: 8px 10px; border: 1px solid var(--line-strong);
    border-radius: var(--r-sm);
    font: inherit; background: var(--surface); color: var(--ink);
    transition: border-color var(--t) var(--ease), box-shadow var(--t) var(--ease); }
  textarea { resize: vertical; line-height: 1.55; font-family: inherit; }
  input:focus, select:focus, textarea:focus {
    outline: none; border-color: var(--accent); box-shadow: var(--ring); }
  input[type=checkbox] { width: 18px; height: 18px; accent-color: var(--accent); }
  .note { margin: 14px 0 0; padding: 10px 12px; border-radius: var(--r-sm);
          background: var(--surface-2); border: 1px solid var(--line);
          color: var(--muted); font-size: 12.5px; }
  .note.warn { background: var(--warn-bg); border-color: var(--warn-line);
               color: var(--warn-ink); }

  /* ---------- 按钮 ---------- */
  button { padding: 8px 18px; border: 0; border-radius: var(--r-sm);
           background: var(--accent);
           color: #fff; font: inherit; font-weight: 600; cursor: pointer;
           transition: background var(--t) var(--ease), transform var(--t) var(--ease),
                       box-shadow var(--t) var(--ease), opacity var(--t) var(--ease); }
  /* hover 抬一下、active 按下去：按压反馈是"这东西真的能点"的最直接信号 */
  button:hover { background: var(--accent-hover); box-shadow: var(--shadow-sm); }
  button:active { transform: translateY(1px) scale(.99); box-shadow: none; }
  /* disabled 不只降透明度：连阴影和位移一起关掉，和"请求中"的样式区分开 */
  button:disabled { opacity: .5; cursor: default; box-shadow: none; transform: none; }
  button.ghost { background: var(--surface); color: var(--ink-2);
                 border: 1px solid var(--line-strong); font-weight: 500; }
  button.ghost:hover { background: var(--surface-2); border-color: var(--line);
                       color: var(--ink); box-shadow: none; }
  button.ghost:active { background: var(--surface-2); }
  .status { font-size: 13px; color: var(--muted); min-height: 20px;
            transition: color var(--t) var(--ease); }
  .status.ok { color: var(--ok-ink); font-weight: 600; }
  .status.err { color: var(--err-ink); font-weight: 600; }

  /* ---------- 表格 ---------- */
  table.list { width: 100%; border-collapse: collapse; margin-top: 14px; font-size: 13px; }
  /* 窄屏上宽表格（openid 列全是长串）会把卡片撑破 —— 让表格自己横向滚，
     而不是把整个页面带出横向滚动条 */
  table.list, table.desc { display: block; overflow-x: auto; }
  table.list th, table.list td { text-align: left; padding: 9px 10px;
        border-bottom: 1px solid var(--line-soft); vertical-align: top; }
  table.list th { color: var(--muted); font-weight: 600; background: var(--surface-2);
                  white-space: nowrap; }
  table.list tr:last-child td { border-bottom: 0; }
  table.list td code { font-size: 12px; word-break: break-all; color: var(--ink-2); }
  table.list td.empty { color: var(--faint); text-align: center; padding: 22px 10px; }
  table.list button { padding: 3px 10px; font-size: 12px; font-weight: 500;
                      white-space: nowrap; }
  table.list tr { transition: background var(--t) var(--ease); }
  table.list tbody tr:hover { background: var(--surface-2); }

  pre { background: #0f172a; color: #e2e8f0; padding: 16px; border-radius: var(--r-sm);
        white-space: pre-wrap; word-break: break-word; margin: 0; font-size: 13px;
        font-family: ui-monospace, Consolas, monospace; min-height: 120px; }

  /* ---------- 面板切换 ---------- */
  .panel { display: none; }
  .panel.active { display: block; }
  .panel > * + * { margin-top: 16px; }

  /* ---------- 概况 ---------- */
  .bot-card { display: flex; align-items: flex-start; gap: 18px; background: #fff;
              border: 1px solid var(--line); border-radius: 12px; padding: 18px 20px; }
  .avatar { position: relative; width: 58px; height: 58px; flex: 0 0 58px; border-radius: var(--r-lg);
            background: linear-gradient(135deg, var(--accent), #6b93f0); color: #fff;
            display: flex; align-items: center; justify-content: center;
            font-size: 22px; font-weight: 700; }
  .avatar i { position: absolute; right: -1px; bottom: -1px; width: 14px; height: 14px;
              border-radius: 50%; border: 2px solid #fff; background: #98a2b3; }
  .avatar i.on { background: #12b76a; }
  .bot-card h1 { font-size: 18px; margin: 0; display: flex; align-items: center; gap: 8px; }
  .bot-card .meta { color: var(--muted); font-size: 12.5px; margin-top: 6px;
                    display: flex; flex-wrap: wrap; gap: 4px 16px; }
  .bot-card .meta b { color: #344054; font-weight: 600; }

  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; }
  .stat { background: var(--surface); border: 1px solid var(--line);
          border-radius: var(--r-md); padding: 14px 16px; box-shadow: var(--shadow-sm);
          cursor: pointer;
          transition: border-color var(--t) var(--ease), box-shadow var(--t) var(--ease),
                      transform var(--t) var(--ease); }
  .stat:hover { border-color: var(--accent-line); box-shadow: var(--shadow-md);
                transform: translateY(-1px); }
  .stat:active { transform: translateY(0); box-shadow: var(--shadow-sm); }
  .stat .k { color: var(--muted); font-size: 12.5px; font-weight: 600; }
  .stat .v { font-size: 24px; font-weight: 700; margin-top: 6px; letter-spacing: -.01em; }
  .stat .v small { font-size: 12px; font-weight: 500; color: var(--muted); margin-left: 6px; }
  .stat .n { color: var(--faint); font-size: 12px; margin-top: 4px; }

  .chips { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px; }
  .chip { display: flex; align-items: center; justify-content: space-between; gap: 10px;
          border: 1px solid var(--line); border-radius: var(--r-sm); padding: 9px 12px;
          cursor: pointer;
          transition: background var(--t) var(--ease), border-color var(--t) var(--ease),
                      box-shadow var(--t) var(--ease); }
  .chip:hover { background: var(--surface); border-color: var(--accent-line);
                box-shadow: var(--shadow-sm); }
  .chip .name { font-weight: 500; min-width: 0; }
  .chip .name small { display: block; color: var(--faint); font-weight: 400; font-size: 11.5px;
                      white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .badge { flex: 0 0 auto; font-size: 11px; padding: 2px 8px; border-radius: var(--r-pill);
           border: 1px solid var(--line); color: var(--muted); white-space: nowrap; }
  .badge.on { color: var(--ok-ink); background: var(--ok-bg); border-color: var(--ok-line); }
  .badge.off { color: var(--ink-2); background: var(--surface-2); }
  .badge.warn { color: var(--warn-ink); background: var(--warn-bg);
                border-color: var(--warn-line); }
  .badge.info { color: var(--accent); background: var(--accent-soft);
                border-color: var(--accent-line); }

  /* ---------- 频道 / 群聊 ---------- */
  .toolbar { display: flex; align-items: center; gap: 12px; margin-top: 14px; flex-wrap: wrap; }
  .toolbar .grow { flex: 1; min-width: 200px; }
  .guild { border: 1px solid var(--line); border-radius: var(--r-sm); overflow: hidden; }
  .guild + .guild { margin-top: 12px; }
  .guild-hd { display: flex; align-items: center; gap: 10px; padding: 12px 14px;
              background: var(--surface-2); border-bottom: 1px solid var(--line);
              flex-wrap: wrap; }
  .guild-hd .nm { font-weight: 600; }
  .guild-hd .gid { color: var(--faint); font-size: 12px; font-family: ui-monospace, Consolas, monospace; }
  .guild-hd .sp { flex: 1; }
  .ch { display: flex; align-items: center; gap: 10px; padding: 8px 14px;
        border-bottom: 1px solid var(--line-soft); font-size: 13px;
        transition: background var(--t) var(--ease); }
  .ch:hover { background: var(--surface-2); }
  .ch:last-child { border-bottom: 0; }
  .ch .bullet { color: var(--line-strong); flex: 0 0 auto; font-family: ui-monospace, Consolas, monospace; }
  .ch .nm { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ch .cid { color: var(--faint); font-size: 12px; font-family: ui-monospace, Consolas, monospace; }
  .ch.target { background: var(--accent-soft); }
  .ch.target .nm { font-weight: 600; color: var(--accent); }
  .empty { color: var(--faint); font-size: 13px; padding: 18px 4px; }
  .perm-head { background: var(--surface-2); }
  .perm-head button { padding: 3px 10px; font-size: 12px; font-weight: 500; }
  .perms { background: var(--surface-2); border-top: 1px solid var(--line-soft);
           padding: 4px 0 8px;
           max-height: 320px; overflow-y: auto; }
  .perm { display: flex; align-items: center; gap: 10px; padding: 4px 14px; font-size: 12.5px; }
  .perm .nm { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .method-badge { flex: 0 0 auto; font-family: ui-monospace, Consolas, monospace; font-size: 11px;
                  padding: 1px 6px; border-radius: 4px; background: var(--line-soft);
                  color: var(--ink-2); }

  /* ---------- 群管理列表 ---------- */
  table.gtable td { vertical-align: middle; }
  table.gtable th.r, table.gtable td.r { text-align: right; }
  .gt { display: flex; align-items: center; gap: 10px; min-width: 0; }
  .gt-avatar { width: 38px; height: 38px; flex: 0 0 38px; border-radius: 50%; overflow: hidden;
               background: #eef4f0; color: #166534; display: flex; align-items: center;
               justify-content: center; font-weight: 600; font-size: 15px; }
  .gt-avatar img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .gt-main { min-width: 0; }
  .gt-name { font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .gt-sub { color: var(--faint); font-size: 12px; font-family: ui-monospace, Consolas, monospace;
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .gt-sub.pick { cursor: pointer; }
  .gt-sub.pick:hover { color: var(--accent); text-decoration: underline; }
  .gt-actions button + button { margin-left: 6px; }
  .gt-when { font-size: 12.5px; }
  .gt-when small { display: block; color: var(--faint); font-size: 11.5px; }

  /* ---------- 群详情 ---------- */
  .detail-hd { display: flex; align-items: center; gap: 10px; }
  .back-btn { width: 32px; height: 32px; flex: 0 0 32px; padding: 0; border-radius: 50%;
              background: var(--surface); border: 1px solid var(--line-strong);
              color: var(--ink-2);
              font-size: 15px; font-weight: 500; line-height: 1;
              display: flex; align-items: center; justify-content: center;
              transition: background var(--t) var(--ease), color var(--t) var(--ease); }
  .back-btn:hover { background: var(--surface-2); color: var(--ink); }
  .detail-name { font-size: 17px; font-weight: 600; display: flex; align-items: center;
                 gap: 8px; min-width: 0; }
  .detail-name .nm { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .detail-sub { color: var(--faint); font-size: 12px; margin-top: 2px;
                font-family: ui-monospace, Consolas, monospace; }
  .detail-head { display: flex; align-items: center; gap: 12px; }
  .detail-head .sp { flex: 1; min-width: 0; }

  .tabs { display: flex; gap: 2px; border-bottom: 1px solid var(--line); margin-top: 18px; }
  .tab { padding: 8px 14px; cursor: pointer; font-size: 13.5px; color: var(--ink-2);
         border-bottom: 2px solid transparent; margin-bottom: -1px; user-select: none;
         transition: color var(--t) var(--ease), border-color var(--t) var(--ease); }
  .tab:hover { color: var(--accent); }
  .tab.active { color: var(--accent); font-weight: 600; border-bottom-color: var(--accent); }
  .tabpane { display: none; padding-top: 14px; }
  .tabpane.active { display: block; }

  table.desc { width: 100%; border-collapse: collapse; font-size: 13px;
               border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
  table.desc th, table.desc td { padding: 9px 12px; border-bottom: 1px solid var(--line);
        text-align: left; vertical-align: top; }
  table.desc tr:last-child th, table.desc tr:last-child td { border-bottom: 0; }
  table.desc th { width: 132px; background: var(--surface-2); color: var(--muted);
                  font-weight: 600; white-space: nowrap; }
  table.desc td code { font-size: 12px; word-break: break-all; }
  .member-box { border: 1px solid var(--line); border-radius: 8px; overflow: hidden;
                max-height: 460px; overflow-y: auto; }
  .member-row { display: flex; align-items: center; gap: 10px; padding: 8px 14px;
                border-bottom: 1px solid var(--line-soft); font-size: 13px; }
  .member-row:last-child { border-bottom: 0; }
  .member-row .nm { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;
                    white-space: nowrap; }
  .member-row .jid { color: var(--faint); font-size: 11.5px;
                     font-family: ui-monospace, Consolas, monospace; }
  .member-row .jid .who { cursor: pointer; }
  .member-row .jid .who:hover { color: var(--accent); text-decoration: underline; }

  /* ---------- 运行日志 ---------- */
  /* 深色底：日志里混着各种级别的行，浅底上颜色区分度不够 */
  .logbox { margin-top: 12px; max-height: 62vh; overflow-y: auto; background: #0f172a;
            border-radius: var(--r-sm); padding: 10px 12px; color: #cbd5e1;
            font-family: ui-monospace, Menlo, Consolas, "Courier New", monospace;
            font-size: 12px; line-height: 1.7; }
  .logbox .empty { color: #64748b; }
  .logrow { display: flex; gap: 8px; align-items: flex-start; }
  .logrow .t { flex: 0 0 auto; color: #64748b; }
  .logrow .lv { flex: 0 0 58px; font-weight: 600; }
  .logrow .src { flex: 0 0 auto; color: #7c8ba1; }
  .logrow .tx { flex: 1; min-width: 0; white-space: pre-wrap; word-break: break-word; }
  .logrow.debug .lv { color: #94a3b8; }
  .logrow.info .lv { color: #4ade80; }
  .logrow.warning .lv { color: #fbbf24; }
  .logrow.warning .tx { color: #fde68a; }
  .logrow.error .lv, .logrow.critical .lv { color: #f87171; }
  .logrow.error .tx, .logrow.critical .tx { color: #fca5a5; }

  /* ---------- 转发目标群勾选 ---------- */
  /* 以前这里显示 openid 前 10 位（39FD6F7D3A…），等于让人盲选。
     现在显示群号（用户在「群聊」页填的备注）和群名 —— 两个都看着才敢确认。 */
  .gpick { display: inline-flex; align-items: center; gap: 5px; padding: 3px 9px;
           border: 1px solid var(--line); border-radius: var(--r-pill);
           background: var(--surface);
           font-size: 12.5px; cursor: pointer; white-space: nowrap;
           transition: background var(--t) var(--ease), border-color var(--t) var(--ease); }
  .gpick:hover { border-color: var(--accent-line); }
  .gpick.on { background: var(--accent-soft); border-color: var(--accent-line); }
  .gpick .gname { font-weight: 600; }
  .gpick .gsub { color: var(--muted); font-size: 11.5px; }

  /* ---------- CLI 扫码登录 ---------- */
  .qrbox { display: inline-flex; flex-direction: column; align-items: center; gap: 8px;
           padding: 12px; background: var(--surface); border: 1px solid var(--line);
           border-radius: var(--r-md); box-shadow: var(--shadow-sm); }
  /* 二维码必须是**纯黑白**才扫得动，所以显式给白底、不许图片被缩放模糊 */
  .qrbox img { width: 240px; height: 240px; image-rendering: pixelated; display: block; }
  .qrbox .qr-tip { color: var(--muted); font-size: 12.5px; }
  .clistat { display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: center;
             font-size: 12.5px; color: var(--ink-2); }
  .clistat code { font-family: ui-monospace, Consolas, monospace; color: var(--ink); }
  .clistat .k { color: var(--muted); }

  /* ---------- 窄屏 ---------- */
  @media (max-width: 880px) {
    .layout { flex-direction: column; }
    .sidebar { position: static; width: 100%; flex: none; height: auto; border-right: 0;
               border-bottom: 1px solid var(--line); }
    .brand { height: 50px; flex-basis: 50px; }
    .menu { display: flex; gap: 6px; overflow-x: auto; padding: 8px; }
    .menu-group, .sidebar-foot { display: none; }
    .menu-item { white-space: nowrap; }
    .content { padding: 14px; }
    .topbar { padding: 8px 14px; flex-wrap: wrap; }
  }
</style>
</head>
<body>

<script>
/* 放在主脚本之前：这样主脚本哪怕解析失败（SyntaxError），也能把错误显示出来，
   而不是"点了没反应、什么都不说"。 */
(function () {
  function report(text) {
    var box = document.getElementById("msg");
    if (box) { box.className = "status err"; box.textContent = "页面脚本出错：" + text; }
  }
  window.addEventListener("error", function (e) { report(e.message || e.type); });
  window.addEventListener("unhandledrejection", function (e) {
    report((e.reason && e.reason.message) || String(e.reason || ""));
  });
})();
</script>

<div class="layout">
  <aside class="sidebar">
    <div class="brand">
      <span class="brand-mark">B</span>
      <span class="brand-name">频道机器人</span>
    </div>
    <nav class="menu" id="menu">
__MENU__
    </nav>
    <div class="sidebar-foot">
      <b>__APPID_LABEL__</b>
      <span>__FOOT_NOTE__</span>
    </div>
  </aside>

  <div class="main">
    <header class="topbar">
      <div class="crumb">
        <b id="crumb-title"></b><span id="crumb-desc"></span>
      </div>
      <div class="topbar-actions">
        <label class="scope">
          生效范围
          <select id="scopePicker" onchange="setScope(this.value)"></select>
        </label>
        <span class="status" id="msg"></span>
        <button class="ghost" onclick="load()">重新读取</button>
        <button id="saveBtn" onclick="save()">保存设置</button>
      </div>
    </header>

    <div class="content">
      <div class="scopebar" id="scopeBar" style="display:none">
        <span class="grow" id="scopeText"></span>
        <button class="ghost" onclick="clearOverrides()">清除本频道的字段覆盖</button>
        <button class="ghost" onclick="dropProfile()">删除本频道配置</button>
      </div>
__PANELS__
    </div>
  </div>
</div>

<script>
const SECTIONS = __SECTIONS__;
const FIELDS = __FIELDS__;

function el(id) { return document.getElementById(id); }

/* ---------- 面板切换 ---------- */
/* 旧版按模块分页的 key 统一落到「配置中心」（概况页的卡片跳转还在用这些 key），
   落地时顺手把对应的折叠组展开 */
const PANEL_ALIAS = {
  strategy: "config", content: "config", card: "config", recall: "config",
  block: "config", ai: "config", relay: "config", relay_filter: "config",
  relay_bind: "config", onebot: "config", admin: "config", access: "config",
};

function toggleGroup(head) {
  /* 折叠组的开合：只切 class，展开/收起动画全交给 CSS 的 grid 过渡 */
  head.closest(".citem").classList.toggle("open");
}

function openGroup(key) {
  const item = document.querySelector('.citem[data-group="' + key + '"]');
  if (item) item.classList.add("open");
}

function show(rawKey, remember) {
  const key = PANEL_ALIAS[rawKey] || rawKey;
  let hit = false;
  document.querySelectorAll(".menu-item").forEach(function (item) {
    const on = item.dataset.key === key;
    item.classList.toggle("active", on);
    if (on) hit = true;
  });
  document.querySelectorAll(".panel").forEach(function (panel) {
    panel.classList.toggle("active", panel.dataset.key === key);
  });
  const meta = SECTIONS.find(function (s) { return s.key === key; });
  el("crumb-title").textContent = meta ? meta.label : "";
  el("crumb-desc").textContent = meta ? meta.desc : "";
  // 离开「群聊」页时把群详情收起来，下次进来回到列表
  if (key !== "groups" && el("groupDetailView") && el("groupDetailView").style.display !== "none") {
    closeGroup();
  }
  if (remember !== false && hit) {
    try { localStorage.setItem("forum_bot_section", key); } catch (e) { /* 忽略 */ }
  }
  // 从概况页卡片跳过来的旧 key：落地后把对应模块的折叠组展开，少点一下
  if (PANEL_ALIAS[rawKey]) openGroup(rawKey);
  // 「运行日志」页：进去才开始轮询，离开就停 —— 一直挂着会一直拉接口
  if (key === "logs") startLogs(); else stopLogs();
  // 「配置中心」：进入时刷新各组的动态列表（成员黑名单/绑定/规则等）
  if (key === "config") {
    loadRelayRules(); loadRelayGroups(); loadRelayBindings();
    loadMemberBan(); loadAiSuggest();
  }
  // 「QQ 登录」页：进去读两个凭证的状态；离开就停两边的轮询
  if (key === "cli") { loadCli(); loadNapcat(); }
  else { stopCliPoll(); stopNapcatPoll(); }
  return hit;
}

/* ---------- 表单读写 ---------- */
function readForm() {
  const values = {};
  for (const f of FIELDS) {
    const node = el(f.key);
    if (!node) {                       // 少一个控件也不该让整个保存挂掉
      console.warn("找不到控件:", f.key);
      continue;
    }
    values[f.key] = f.kind === "checkbox" ? node.checked : node.value;
  }
  return values;
}

function fill(tpl) {
  return String(tpl || "")
      .replace(/{title}/g, "一条示例帖子的标题")
      .replace(/{link}/g, "https://pd.qq.com/g/1234567890123456789/post/B_xxxx")
      .replace(/{index}/g, "1")
      .replace(/{thread_id}/g, "B_xxxx")
      .replace(/{channel_id}/g, "682799315")
      .replace(/{author_id}/g, "17436152526227240283")
      .replace(/{date_time}/g, "2026-09-14T15:30:00+08:00");
}

function renderPreview() {
  const v = readForm();
  const item = fill(v.title_template || "{title}\n{link}");
  let text;

  if (v.style === "markdown" || v.style === "markdown_button") {
    const title = fill(v.card_title || "");
    const tpl = v.card_item || "{index}. {title}";
    let card = fill(tpl);
    // 不带按钮的 markdown 模式会自动补上链接，预览里保持一致
    if (v.style === "markdown" && tpl.indexOf("{link}") === -1) {
      card += "\n" + "https://pd.qq.com/g/1234567890123456789/post/B_xxxx";
    }
    const rows = [];
    if (v.mode === "instant") {
      rows.push(card);
    } else {
      rows.push(card, card.replace(/^1\./, "2."), card.replace(/^1\./, "3."));
    }
    let head = title;
    if (v.mode !== "instant" && head) head = title + "（共 3 条）";
    text = (head ? head + "\n\n" : "") + rows.join("\n\n");
    if (v.style === "markdown_button") {
      const n = Math.min(3, Math.max(1, parseInt(v.max_buttons) || 5));
      const btns = [];
      for (let i = 0; i < n; i++) btns.push("[ " + (v.button_label || "查看") + " ]");
      text += "\n\n" + btns.join(" ");
    }
    text = "（Markdown 卡片，客户端里渲染为富文本）\n" + text;
  } else if (v.mode === "instant") {
    text = (v.single_header ? v.single_header + "\n" : "") + item;
  } else {
    const header = (v.batch_header || "").replace("{count}", "3");
    text = (header ? header + "\n" : "") + [item, item, item].join("\n\n");
  }
  el("preview").textContent = text;
}

/* ---------- 拉数据 ---------- */
async function getJSON(url) {
  try {
    const res = await fetch(url);
    if (!res.ok) return null;
    return await res.json();
  } catch (e) {
    console.warn("请求失败:", url, e);
    return null;
  }
}

async function postJSON(url, body) {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body || {})
    });
    return await res.json();
  } catch (e) {
    console.warn("请求失败:", url, e);
    return {ok: false, msg: String(e)};
  }
}

function adminList() {
  const node = el("admin_openids");
  if (!node) return [];
  return node.value.split("\n").map(function (s) { return s.trim(); }).filter(Boolean);
}

function addAdmin(openid) {
  const node = el("admin_openids");
  if (!node) return;
  const lines = adminList();
  if (lines.indexOf(openid) === -1) lines.push(openid);
  node.value = lines.join("\n") + "\n";
  renderPreview();
  const msg = el("msg");
  msg.className = "status ok";
  msg.textContent = "已加入管理员名单，记得点「保存设置」";
  loadMembers();
}

async function loadMembers() {
  const tbody = document.querySelector("#memberTable tbody");
  if (!tbody) return;
  const list = (await getJSON("/api/members")) || [];
  const admins = adminList();
  tbody.innerHTML = "";
  if (!list.length) {
    tbody.innerHTML = '<tr><td class="empty" colspan="5">'
        + '还没有记录到群成员 —— 让要授权的人在群里发一条消息就行</td></tr>';
    return;
  }
  for (const m of list) {
    const tr = document.createElement("tr");
    const name = document.createElement("td");
    name.textContent = m.username || "(没拿到昵称)";
    const role = document.createElement("td");
    role.textContent = m.role_label || "普通成员";
    const id = document.createElement("td");
    const code = document.createElement("code");
    code.textContent = m.openid;
    id.appendChild(code);
    const seen = document.createElement("td");
    seen.textContent = m.last_seen || "";
    const act = document.createElement("td");
    if (admins.indexOf(m.openid) === -1) {
      const btn = document.createElement("button");
      btn.className = "ghost";
      btn.textContent = "加入名单";
      btn.onclick = function () { addAdmin(m.openid); };
      act.appendChild(btn);
    } else {
      act.textContent = "已是管理员";
    }
    tr.append(name, role, id, seen, act);
    tbody.appendChild(tr);
  }
}

async function loadBlacklist() {
  const tbody = document.querySelector("#blockTable tbody");
  if (!tbody) return;
  const list = (await getJSON("/api/blacklist")) || [];
  tbody.innerHTML = "";
  if (!list.length) {
    tbody.innerHTML = '<tr><td class="empty" colspan="3">拉黑名单是空的</td></tr>';
    return;
  }
  for (const item of list) {
    const tr = document.createElement("tr");
    const idx = document.createElement("td");
    idx.textContent = item.index;
    const content = document.createElement("td");
    content.textContent = item.content;
    const source = document.createElement("td");
    if (item.manual) source.textContent = "在设置页手动添加";
    else source.textContent = [item.by_name, item.at, item.group_openid].filter(Boolean).join(" / ");
    tr.append(idx, content, source);
    tbody.appendChild(tr);
  }
}

async function loadAccess() {
  const box = document.querySelector("#accessTable");
  if (!box) return;
  loadAccessGroupPicker();  // 兑换群选择器跟绑定表一起刷新（不阻塞）
  const state = (await getJSON("/api/access"))
      || {bindings: [], pending_codes: 0, guild_labels: {}};
  const list = state.bindings || [];
  const labels = state.guild_labels || {};
  box.innerHTML = "";
  if (!list.length) {
    const pending = state.pending_codes ? "（当前有 " + state.pending_codes + " 个待兑换的验证码）" : "";
    box.innerHTML = '<p class="hint">还没有人领取' + pending + '</p>';
    return;
  }
  // **按频道分组**：两个频道各一块，互相看混了都不知道该找谁
  const groups = new Map();
  for (const item of list) {
    const gid = item.guild_id || "";
    if (!groups.has(gid)) groups.set(gid, []);
    groups.get(gid).push(item);
  }
  for (const [gid, rows] of groups) {
    const label = labels[gid]
        || (gid ? "频道 " + gid.slice(0, 10) + "…" : "（旧数据，未分频道）");
    const card = document.createElement("div");
    card.className = "card";
    card.style.cssText = "margin:10px 0;border:1px solid var(--line,#ddd);border-radius:8px;padding:10px 12px";
    const head = document.createElement("p");
    head.innerHTML = "<b>" + label + "</b>　<span class='hint'>授权 "
        + rows.length + " 人</span>";
    card.appendChild(head);
    const table = document.createElement("table");
    table.className = "list";
    table.innerHTML = "<thead><tr><th>频道用户</th><th>来源群</th>"
        + "<th>授权时间</th><th>有效期</th><th>状态</th></tr></thead><tbody></tbody>";
    const tbody = table.querySelector("tbody");
    for (const item of rows) {
      const tr = document.createElement("tr");
      const who = document.createElement("td");
      const code = document.createElement("code");
      code.textContent = item.user_id;
      who.appendChild(code);
      const group = document.createElement("td");
      group.textContent = item.group_openid || "";
      const when = document.createElement("td");
      when.textContent = item.granted || "";
      const exp = document.createElement("td");
      exp.textContent = item.expire || "";
      const stateCell = document.createElement("td");
      if (item.left) stateCell.textContent = "已退群，待收回";
      else if (item.revoke_failed) stateCell.textContent = "收回失败，将重试";
      else stateCell.textContent = "生效中";
      tr.append(who, group, when, exp, stateCell);
      tbody.appendChild(tr);
    }
    card.appendChild(table);
    box.appendChild(card);
    }
    }

    /* ---------- 兑换群选择器 ---------- */
    /* 已知群列表勾选代替手填 openid；写入的仍是 access_group_openid 设置
    （一行一个），写到哪个频道由页面右上角「生效范围」决定 */
    async function loadAccessGroupPicker() {
    const box = el("accessGroupPicker");
    if (!box) return;
    const data = await getJSON("/api/groups");
    const rows = (data && data.rows) || [];
    const field = el("access_group_openid");
    const current = new Set(String((field && field.value) || "")
    .split(/\n|,/).map(function (s) { return s.trim(); }).filter(Boolean));
    const scopeEl = el("accessPickerScope");
    if (scopeEl) scopeEl.textContent = SCOPE ? "当前频道（独立配置）" : "全局";
    box.innerHTML = "";
    if (!rows.length) {
    box.innerHTML = '<p class="hint">机器人还没见过任何群 —— 让群里有人发条消息，'
      + '或到「群聊」页同步后再来</p>';
    return;
    }
    for (const row of rows) {
    const id = row.group_openid || "";
    if (!id) continue;
    const label = document.createElement("label");
    label.style.cssText = "display:block;margin:3px 0";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "acc-group";
    cb.value = id;
    cb.checked = current.has(id);
    const shown = [row.group_no, row.name, row.note && !row.group_no ? "（" + row.note + "）" : ""]
      .filter(Boolean).join(" ");
    label.append(cb, document.createTextNode(" " + (shown || id.slice(0, 10) + "…")));
    box.appendChild(label);
    }
    // 配置里有、但不在已知列表的群也列出来（打勾且不可取消），避免保存时静默丢
    for (const id of current) {
    if (rows.some(function (r) { return r.group_openid === id; })) continue;
    const label = document.createElement("label");
    label.style.cssText = "display:block;margin:3px 0";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.className = "acc-group"; cb.value = id; cb.checked = true;
    label.append(cb, document.createTextNode(" " + id.slice(0, 10) + "…（不在已知群列表）"));
    box.appendChild(label);
    }
    }

    async function saveAccessGroups() {
    const picked = Array.from(
    document.querySelectorAll("#accessGroupPicker .acc-group:checked")
    ).map(function (n) { return n.value; });
    const res = await postJSON("/api/settings", {
    guild_id: SCOPE, values: {access_group_openid: picked.join("\n")},
    });
    if (res && res.__scope__) {
    OVERRIDES = res.__scope__.overridden || [];
    markOverrides(OVERRIDES);
    }
    const field = el("access_group_openid");
    if (field) field.value = picked.join("\n");
    const msg = el("msg");
    if (msg) {
    msg.className = "status ok";
    msg.textContent = "已保存兑换群（" + picked.length + " 个）"
      + (SCOPE ? "（当前频道）" : "（全局）");
    }
    }

/* ---------- 概况 ---------- */
async function loadOverview() {
  const botBox = el("overviewBot");
  const statsBox = el("overviewStats");
  const chipsBox = el("overviewModules");
  if (!botBox) return;
  const data = await getJSON("/api/overview");
  if (!data) {
    botBox.innerHTML = '<div class="empty">读不到概况数据（配置页可能是单独启动的）</div>';
    statsBox.innerHTML = "";
    chipsBox.innerHTML = "";
    return;
  }

  const bot = data.bot || {};
  botBox.innerHTML = "";
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = String(bot.name || "B").slice(0, 1).toUpperCase();
  if (bot.ready) avatar.appendChild(Object.assign(document.createElement("i"), {className: "on"}));
  const info = document.createElement("div");
  const title = document.createElement("h1");
  title.textContent = bot.name || "（未就绪）";
  const tag = document.createElement("span");
  tag.className = "badge " + (bot.ready ? "on" : "off");
  tag.textContent = bot.ready ? "在线" : "未就绪";
  title.appendChild(tag);
  const meta = document.createElement("div");
  meta.className = "meta";
  const bits = [];
  if (bot.appid) bits.push(["appid", bot.appid]);
  if (bot.uptime) bits.push(["已运行", bot.uptime]);
  if (bot.online_since) bits.push(["启动于", bot.online_since]);
  for (const pair of bits) {
    const span = document.createElement("span");
    span.append(pair[0] + "：");
    const b = document.createElement("b");
    b.textContent = pair[1];
    span.appendChild(b);
    meta.appendChild(span);
  }
  info.append(title, meta);
  botBox.append(avatar, info);

  statsBox.innerHTML = "";
  for (const item of data.stats || []) {
    const box = document.createElement("div");
    box.className = "stat";
    box.title = "点击进入设置";
    box.onclick = function () { if (item.panel) show(item.panel); };
    const k = document.createElement("div"); k.className = "k"; k.textContent = item.label;
    const v = document.createElement("div"); v.className = "v"; v.textContent = item.value;
    const small = document.createElement("small"); small.textContent = item.unit || "";
    v.appendChild(small);
    const n = document.createElement("div"); n.className = "n"; n.textContent = item.note || "";
    box.append(k, v, n);
    statsBox.appendChild(box);
  }

  chipsBox.innerHTML = "";
  for (const item of data.modules || []) {
    const chip = document.createElement("div");
    chip.className = "chip";
    chip.onclick = function () { if (item.panel) show(item.panel); };
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = item.label;
    const small = document.createElement("small");
    small.textContent = item.note || "";
    name.appendChild(small);
    const badge = document.createElement("span");
    badge.className = "badge " + (item.enabled ? "on" : "off");
    badge.textContent = item.enabled ? "已启用" : "未开启";
    chip.append(name, badge);
    chipsBox.appendChild(chip);
  }
}

/* ---------- 群聊 ---------- */
function badgeOf(text, kind) {
  const span = document.createElement("span");
  span.className = "badge " + (kind || "off");
  span.textContent = text;
  return span;
}

function buttonOf(label, cls, onclick) {
  const btn = document.createElement("button");
  btn.className = cls;
  btn.textContent = label;
  btn.onclick = onclick;
  return btn;
}

function groupAvatar(row) {
  const box = document.createElement("span");
  box.className = "gt-avatar";
  const letter = (row.name || "#").slice(0, 1);
  if (row.avatar) {
    // 群头像地址要群号才拼得出来，所以只有填了备注才有
    const img = document.createElement("img");
    img.src = row.avatar;
    img.alt = (row.name || "群") + "的头像";
    img.onerror = function () { box.textContent = letter; };
    box.appendChild(img);
  } else {
    box.textContent = letter;
  }
  return box;
}

function whenCell(main, sub) {
  const td = document.createElement("td");
  td.className = "gt-when";
  td.textContent = main || "—";
  if (sub) {
    const small = document.createElement("small");
    small.textContent = sub;
    td.appendChild(small);
  }
  return td;
}

/* ---------- 机器人在群内的平台状态（bot_state 接口） ---------- */
/* available=false 是"还没查过"，和"查到了但值不对劲"必须分开说 ——
   前者点一次同步就行，后者要去群设置里改，处理办法完全不同。 */
function botStateHint(row) {
  const box = document.createElement("div");
  box.className = "hint";
  const bs = row.bot_state || {};
  if (!bs.available) {
    box.textContent = "群内状态：还没查过";
    box.title = "点右侧「同步成员」会一起拉取；"
        + "该接口是平台白名单能力，未开通会返回 11253";
    return box;
  }
  const bits = [];
  if (bs.recv_msg_setting) {
    const bad = bs.recv_msg_setting !== "all";
    bits.push([bad ? "仅收 @ 消息" : "收全部消息", bad ? "warn" : "on"]);
  }
  if (bs.allow_proactive_msg === false) {
    bits.push(["不支持主动消息", "warn"]);
  }
  if (!bits.length) {
    box.textContent = "群内状态：已同步（平台没返回可用的设置项）";
    return box;
  }
  for (const bit of bits) box.appendChild(badgeOf(bit[0], bit[1]));
  return box;
}

function botStateCell(row) {
  const bs = row.bot_state || {};
  if (!bs.available) {
    return "还没查过（点「同步成员」会一起拉；平台白名单能力，未开通返回 11253）";
  }
  return (bs.member_role_label || bs.member_role || "未知")
      + (bs.joined_at ? "　入群 " + bs.joined_at : "");
}

function recvSettingCell(row) {
  const bs = row.bot_state || {};
  if (!bs.available) return "还没查过";
  if (!bs.recv_msg_setting) return "平台没返回";
  const label = bs.recv_msg_label || bs.recv_msg_setting;
  if (bs.recv_msg_setting === "all") return label;
  // 不是 all 就要显眼：这直接决定群消息互转收不收得到群里的普通聊天
  const span = document.createElement("span");
  span.appendChild(badgeOf(label, "warn"));
  span.appendChild(document.createTextNode("　只有「接收全部消息」才收得到群里的普通聊天"));
  return span;
}

function proactiveCell(row) {
  const bs = row.bot_state || {};
  if (!bs.available) return "还没查过";
  if (bs.allow_proactive_msg === true) return "接受";
  if (bs.allow_proactive_msg === false) {
    const span = document.createElement("span");
    span.appendChild(badgeOf("不接受", "warn"));
    span.appendChild(document.createTextNode("　主动消息（互转 / 帖子转发）会被平台拒"));
    return span;
  }
  return "平台没返回";
}

async function loadGroups() {
  const tbody = document.querySelector("#groupTable tbody");
  if (!tbody) return;
  const data = await getJSON("/api/groups");
  const rows = (data && data.rows) || [];
  el("groupsNote").textContent = (data && data.note) || "";
  el("groupsSummary").textContent = rows.length
      ? "共 " + rows.length + " 个群，其中已订阅 " + (data.subscribed_total || 0) + " 个"
      : "";

  const hint = el("groupsApiHint");
  if (hint) {
    hint.innerHTML = "";
    if (data && data.api_available === false) {
      const box = document.createElement("div");
      box.className = "note warn";
      box.textContent = (data.api_hint || "群信息接口不可用")
          + " —— 群名、群人数、群成员都拉不到，这里只能显示本地记录到的信息。";
      hint.appendChild(box);
    }
    // 互转开着、但某个群没把消息推给机器人 —— 这是"规则配了却不生效"最常见的原因，
    // 而且不报错，只会在改规则上白费功夫。所以摆在最显眼的地方。
    if (data && data.relay_warning) {
      const box = document.createElement("div");
      box.className = "note warn";
      box.textContent = data.relay_warning;
      hint.appendChild(box);
    }
  }

  tbody.innerHTML = "";
  if (!rows.length) {
    tbody.innerHTML = '<tr><td class="empty" colspan="5">还没有记录到任何群 —— '
        + '把机器人拉进群、或在群里说句话就会出现</td></tr>';
    return;
  }
  for (const row of rows) {
    tbody.appendChild(groupRow(row));
  }
}

function groupRow(row) {
  const tr = document.createElement("tr");

  // 群：头像 + 群名 + （群号 / openid）
  const cell = document.createElement("td");
  const box = document.createElement("div");
  box.className = "gt";
  box.appendChild(groupAvatar(row));

  const main = document.createElement("div");
  main.className = "gt-main";

  const name = document.createElement("div");
  name.className = "gt-name";
  name.textContent = row.name || "（还没有群名）";
  if (row.is_access_group) {
    name.appendChild(document.createTextNode(" "));
    name.appendChild(badgeOf("加群解锁", "info"));
  }
  if (row.subscribed) {
    name.appendChild(document.createTextNode(" "));
    name.appendChild(badgeOf("已订阅", "on"));
  }
  main.appendChild(name);

  // 第二行：有备注（群号）就显示群号，否则退化成 openid。
  // 点一下可以补填 —— 平台接口不返回群号，只能本地记。
  const sub = document.createElement("div");
  sub.className = "gt-sub pick";
  sub.textContent = row.group_no ? row.group_no : row.group_openid;
  sub.title = row.group_no
      ? "群号（本地备注），点击可修改"
      : "平台接口不返回群号，点击填写备注（填了就有群号和群头像）";
  sub.onclick = function () { editGroupNote(row.group_openid, row.note, row.name); };
  main.appendChild(sub);

  box.appendChild(main);
  cell.appendChild(box);

  // 机器人身份：从成员快照里的 bot 成员推出来
  const roleCell = document.createElement("td");
  if (row.bot_role_label) {
    const kind = (row.bot_role === "owner" || row.bot_role === "admin") ? "on" : "off";
    roleCell.appendChild(badgeOf(row.bot_role_label, kind));
  } else {
    roleCell.appendChild(badgeOf("未知", "warn"));
    const tip = document.createElement("div");
    tip.className = "hint";
    tip.textContent = row.api_available === false ? "接口不可用" : "还没有成员快照";
    roleCell.appendChild(tip);
  }

  // 平台侧的群内状态（bot_state 接口）。这里只挑**影响功能能不能用**的显示：
  //   · 消息接收设置不是「接收全部消息」-> 群消息互转收不到普通聊天（最容易被误解）
  //   · 不接受主动消息 -> 互转 / 帖子转发发一条失败一条
  roleCell.appendChild(botStateHint(row));

  const synced = whenCell(
    row.member_at || "还没有同步",
    row.member_snapshot ? row.member_snapshot + " 人" : ""
  );
  const last = whenCell(row.last_seen || "", "首见 " + (row.first_seen || "—"));

  const act = document.createElement("td");
  act.className = "r";
  act.appendChild(buttonOf("详情", "ghost", function () { openGroup(row.group_openid); }));
  const syncBtn = buttonOf(row.refreshing ? "同步中…" : "同步成员", "ghost", function () {
    syncMembers(row.group_openid, loadGroups);
  });
  syncBtn.disabled = !!row.refreshing;
  act.appendChild(syncBtn);

  tr.append(cell, roleCell, synced, last, act);
  return tr;
}

async function syncMembers(groupOpenid, after) {
  const res = await postJSON("/api/groups/members", {group_openid: groupOpenid || ""});
  const msg = el("groupsSummary");
  if (msg) msg.textContent = (res && res.msg) || "已请求同步";
  // 真正的拉取发生在机器人的循环里（最多 5 秒后开始），所以隔一会儿再看结果
  if (after) {
    setTimeout(after, 5000);
    setTimeout(after, 12000);
  }
}

function syncAllMembers() {
  syncMembers("", loadGroups);
}

async function editGroupNote(groupOpenid, current, name) {
  const answer = window.prompt(
    "给「" + (name || groupOpenid) + "」填备注" + String.fromCharCode(10)
    + "（填 QQ 群号就会显示群号并加载群头像；清空表示删除）",
    current || ""
  );
  if (answer === null) return;
  const res = await postJSON("/api/groups/note", {group_openid: groupOpenid, note: answer});
  const msg = el("groupsSummary");
  if (msg) msg.textContent = (res && res.ok) ? "备注已保存" : ((res && res.msg) || "保存失败");
  loadGroups();
  if (el("groupDetailView") && el("groupDetailView").style.display !== "none") {
    openGroup(groupOpenid);
  }
}

/* ---------- 群详情 ---------- */
async function openGroup(groupOpenid) {
  const data = await getJSON("/api/groups/detail?group_openid=" + encodeURIComponent(groupOpenid));
  if (!data) return;
  renderGroupDetail(data);
}

function closeGroup() {
  const detail = el("groupDetailView");
  const list = el("groupListView");
  if (!detail || !list) return;
  detail.style.display = "none";
  detail.innerHTML = "";
  list.style.display = "";
}

function switchGroupTab(name) {
  document.querySelectorAll("#groupDetailView .tab").forEach(function (tab) {
    tab.classList.toggle("active", tab.dataset.tab === name);
  });
  document.querySelectorAll("#groupDetailView .tabpane").forEach(function (pane) {
    pane.classList.toggle("active", pane.dataset.pane === name);
  });
}

function codeOf(text) {
  const code = document.createElement("code");
  code.textContent = text;
  return code;
}

function descTable(pairs) {
  const table = document.createElement("table");
  table.className = "desc";
  const tbody = document.createElement("tbody");
  for (const pair of pairs) {
    const tr = document.createElement("tr");
    const th = document.createElement("th");
    th.textContent = pair[0];
    const td = document.createElement("td");
    if (pair[1] instanceof Node) td.appendChild(pair[1]);
    else if (pair[1] === 0 || pair[1]) td.textContent = String(pair[1]);
    else td.textContent = "—";
    tr.append(th, td);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  return table;
}

function renderGroupDetail(row) {
  const box = el("groupDetailView");
  if (!box) return;
  box.innerHTML = "";
  box.style.display = "";
  el("groupListView").style.display = "none";

  const card = document.createElement("div");
  card.className = "card";

  // 头部：返回 + 头像 + 群名 + 群号
  const head = document.createElement("div");
  head.className = "detail-head";

  const back = buttonOf("←", "back-btn", closeGroup);
  back.title = "返回群列表";
  head.appendChild(back);
  head.appendChild(groupAvatar(row));

  const info = document.createElement("div");
  info.className = "sp";
  const title = document.createElement("div");
  title.className = "detail-name";
  const nm = document.createElement("span");
  nm.className = "nm";
  nm.textContent = row.name || "（还没有群名）";
  title.appendChild(nm);
  if (row.is_access_group) title.appendChild(badgeOf("加群解锁群", "info"));
  if (row.subscribed) title.appendChild(badgeOf("已订阅", "on"));
  if (!row.found) title.appendChild(badgeOf("没有缓存数据", "warn"));
  info.appendChild(title);

  const subline = document.createElement("div");
  subline.className = "detail-sub";
  subline.textContent = (row.group_no ? row.group_no + " · " : "") + row.group_openid;
  info.appendChild(subline);
  head.appendChild(info);

  const sync = buttonOf(row.refreshing ? "同步中…" : "同步成员", "ghost", function () {
    syncMembers(row.group_openid, function () { openGroup(row.group_openid); });
  });
  sync.disabled = !!row.refreshing;
  head.appendChild(sync);
  card.appendChild(head);

  // 页签
  const tabs = document.createElement("div");
  tabs.className = "tabs";
  const panes = document.createElement("div");
  const defs = [["info", "群信息"], ["members", "群成员"], ["features", "功能管理"]];
  for (const def of defs) {
    const tab = document.createElement("div");
    tab.className = "tab" + (def[0] === "info" ? " active" : "");
    tab.dataset.tab = def[0];
    tab.textContent = def[1];
    tab.onclick = function () { switchGroupTab(def[0]); };
    tabs.appendChild(tab);

    const pane = document.createElement("div");
    pane.className = "tabpane" + (def[0] === "info" ? " active" : "");
    pane.dataset.pane = def[0];
    panes.appendChild(pane);
  }
  card.appendChild(tabs);
  card.appendChild(panes);

  // --- 群信息 ---
  const noteCell = document.createElement("span");
  noteCell.textContent = row.note || "未设置";
  noteCell.appendChild(document.createTextNode(" "));
  noteCell.appendChild(buttonOf("编辑", "ghost", function () {
    editGroupNote(row.group_openid, row.note, row.name);
  }));

  panes.children[0].appendChild(descTable([
    ["群名称", row.name || "未获取（需要群信息接口权限）"],
    ["群备注", noteCell],
    ["QQ群号", row.group_no || "未填写（平台接口不返回群号）"],
    ["群介绍", row.memo || "未设置"],
    ["群分类", row.class_text || "—"],
    ["群标签", (row.tags && row.tags.length) ? row.tags.join("、") : "—"],
    ["群人数", row.member_num || "—"],
    ["机器人身份", row.bot_role_label || "未知（还没有成员快照）"],
    // 平台侧的群内状态（bot_state 接口）—— 这两项决定"互转 / 帖子转发能不能用"，
    // 比群名、群人数重要得多，所以放在显眼位置
    ["群内身份（平台）", botStateCell(row)],
    ["消息接收设置", recvSettingCell(row)],
    ["接受主动消息", proactiveCell(row)],
    ["成员快照", row.member_snapshot
        ? row.member_snapshot + " 人 · " + row.member_at : "还没有同步过"],
    ["最近发现", row.last_seen || "—"],
    ["发现来源", row.source || "—"],
    ["群 openid", codeOf(row.group_openid)]
  ]));

  // --- 群成员 ---
  const members = row.members || [];
  if (!members.length) {
    const tip = document.createElement("div");
    tip.className = "note";
    tip.textContent = (row.api_available === false)
        ? ((row.api_hint || "群信息接口不可用") + " —— 拿不到群成员列表。")
        : "还没有成员快照，点右上角「同步成员」拉一份。";
    panes.children[1].appendChild(tip);
  } else {
    const line = document.createElement("p");
    line.className = "hint";
    line.style.margin = "0 0 10px";
    line.textContent = "共 " + members.length + " 人（同步于 " + (row.member_at || "?") + "）";
    panes.children[1].appendChild(line);

    const mbox = document.createElement("div");
    mbox.className = "member-box";
    for (const m of members) {
      const r = document.createElement("div");
      r.className = "member-row";
      const nameSpan = document.createElement("span");
      nameSpan.className = "nm";
      nameSpan.textContent = (m.username || "(没拿到昵称)") + (m.bot ? "（机器人）" : "");
      r.appendChild(nameSpan);
      r.appendChild(badgeOf(m.role_label, m.role === "member" ? "off" : "on"));
      const joined = document.createElement("span");
      joined.className = "jid";
      joined.textContent = "入群 " + (m.joined_at || "?");
      r.appendChild(joined);
      mbox.appendChild(r);
    }
    panes.children[1].appendChild(mbox);
  }

  // --- 功能管理 ---
  const pane2 = panes.children[2];
  const chips = document.createElement("div");
  chips.className = "chips";
  for (const item of (row.features || [])) {
    const chip = document.createElement("div");
    chip.className = "chip";
    if (item.panel) chip.onclick = function () { show(item.panel); };
    const wrap = document.createElement("span");
    wrap.className = "name";
    wrap.textContent = item.label;
    const small = document.createElement("small");
    small.textContent = item.note || "";
    wrap.appendChild(small);
    chip.appendChild(wrap);
    chip.appendChild(badgeOf(item.enabled ? "已启用" : "未开启", item.enabled ? "on" : "off"));
    chips.appendChild(chip);
  }
  pane2.appendChild(chips);

  if (row.binding_count) {
    const authTitle = document.createElement("p");
    authTitle.className = "hint";
    authTitle.style.margin = "16px 0 0";
    authTitle.textContent = "本群已解锁「资料板块」的用户（共 " + row.binding_count + " 人）";
    pane2.appendChild(authTitle);
    const pairs = (row.bindings || []).slice(0, 20).map(function (b, index) {
      return [
        String(index + 1),
        (b.user_id || "") + "　" + (b.granted || "") + " 授权　"
          + (b.expire || "长期有效") + (b.left ? "　（已退群，待收回）" : "")
      ];
    });
    pane2.appendChild(descTable(pairs));
  }

  box.appendChild(card);
}

/* ---------- 频道 ---------- */

// 单个频道的「多频道配置」块：状态 + 转发目标群勾选 + 生效值一览
function guildConfigBlock(guild, knownGroups, globalSubscribers) {
  const wrap = document.createElement("div");
  wrap.className = "ch";
  wrap.style.flexDirection = "column";
  wrap.style.alignItems = "stretch";
  wrap.style.gap = "8px";
  // 内联样式同样走变量 —— 以后改主题色这里不用跟着改
  wrap.style.background = "var(--surface-2)";

  const line = document.createElement("div");
  line.style.display = "flex";
  line.style.alignItems = "center";
  line.style.gap = "8px";
  line.style.flexWrap = "wrap";

  const title = document.createElement("span");
  title.className = "nm";
  title.style.flex = "0 0 auto";
  title.style.fontWeight = "600";
  title.textContent = "多频道配置";
  line.appendChild(title);

  if (guild.has_profile) {
    line.appendChild(badgeOf(
      guild.override_count
        ? "独立配置 · 已覆盖 " + guild.override_count + " 项"
        : "独立配置 · 全部跟随全局",
      "info"
    ));
  } else {
    line.appendChild(badgeOf("跟随全局", "off"));
  }
  if (guild.channel_number) {
    const no = document.createElement("span");
    no.className = "cid";
    no.textContent = "频道号 " + guild.channel_number;
    line.appendChild(no);
  }
  const sp = document.createElement("span");
  sp.className = "sp";
  sp.style.flex = "1";
  line.appendChild(sp);

  line.appendChild(buttonOf("编辑本频道设置", "ghost", function () {
    setScope(guild.id);
    show("strategy");
  }));
  if (guild.has_profile) {
    line.appendChild(buttonOf("删除配置", "ghost", async function () {
      const who = guild.label || guild.name;
      if (!window.confirm("删除「" + who + "」的独立配置？\n（字段覆盖、转发目标群、频道号都会一起删掉）")) {
        return;
      }
      const res = await postJSON("/api/guilds/profile", {guild_id: guild.id, action: "drop"});
      const msg = el("msg");
      msg.className = res && res.ok ? "status ok" : "status err";
      msg.textContent = res && res.ok ? "已删除「" + who + "」的独立配置" : "删除失败";
      loadGuilds();
    }));
  } else {
    line.appendChild(buttonOf("启用独立配置", "", async function () {
      // 顺手把真名/频道号/当前全局订阅带进去，启用之后不会突然一条都不推
      const res = await postJSON("/api/guilds/profile", {
        guild_id: guild.id,
        action: "enable",
        label: guild.label || guild.name,
        channel_number: guild.channel_number || "",
        subscribers: globalSubscribers || []
      });
      const msg = el("msg");
      msg.className = res && res.ok ? "status ok" : "status err";
      msg.textContent = res && res.ok ? "已启用独立配置" : "启用失败";
      loadGuilds();
    }));
  }
  wrap.appendChild(line);

  // 转发目标群
  const subLine = document.createElement("div");
  subLine.style.display = "flex";
  subLine.style.alignItems = "center";
  subLine.style.gap = "8px";
  subLine.style.flexWrap = "wrap";

  const subLabel = document.createElement("span");
  subLabel.className = "hint";
  subLabel.style.flex = "0 0 auto";
  subLabel.textContent = "这个频道的帖子推给哪些群：";
  subLine.appendChild(subLabel);

  // 群的展示名：**群号优先** —— 那是用户在「群聊」页自己填的备注，一定认得出来；
  // 其次群名；最后才退回 openid 前 10 位。
  // （以前这里只有 openid 前 10 位，等于让人从一串乱码里盲选。）
  const groupLabel = {};
  const groupName = {};
  for (const group of (knownGroups || [])) {
    const id = group.group_openid || "";
    if (!id) continue;
    groupLabel[id] = group.label || (id.slice(0, 10) + "…");
    groupName[id] = group.group_name || "";
  }
  function nameOf(id) { return groupLabel[id] || (id.slice(0, 10) + "…"); }
  function titleOf(id) {
    return (groupName[id] ? groupName[id] + "\n" : "") + id;
  }

  if (!guild.has_profile) {
    const tip = document.createElement("span");
    tip.className = "hint";
    const names = (globalSubscribers || []).map(nameOf);
    tip.textContent = "正在用全局订阅（" + names.length + " 个群"
        + (names.length ? "：" + names.join("、") : "")
        + "）。启用独立配置后，就能只推给指定的几个群。";
    subLine.appendChild(tip);
  } else {
    const current = new Set(guild.subscribers || []);
    const pool = [];
    const seen = new Set();
    // 已知群按后端排好的顺序（有群号的在前）
    for (const group of (knownGroups || [])) {
      const id = group.group_openid || "";
      if (!id || seen.has(id)) continue;
      seen.add(id);
      pool.push(id);
    }
    // **配了但已不在已知列表里的群也要保留** —— 否则一保存就被静默丢掉
    for (const id of current) {
      if (!seen.has(id)) { seen.add(id); pool.push(id); }
    }

    if (!pool.length) {
      const tip = document.createElement("span");
      tip.className = "hint";
      tip.textContent = "还没有已知的群 —— 把机器人拉进群、或在群里发条消息就会出现";
      subLine.appendChild(tip);
    }

    const boxes = [];
    for (const id of pool) {
      const label = document.createElement("label");
      label.className = "gpick" + (current.has(id) ? " on" : "");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = current.has(id);
      box.value = id;
      boxes.push(box);
      label.appendChild(box);
      const text = document.createElement("span");
      text.className = "gname";
      text.textContent = nameOf(id);
      text.title = titleOf(id);
      label.appendChild(text);
      // 群号和群名都能显示时，把另一个作为次要信息带上 —— 两个都看着才敢确认
      const other = groupName[id] && groupLabel[id] !== groupName[id] ? groupName[id] : "";
      if (other) {
        const sub = document.createElement("span");
        sub.className = "gsub";
        sub.textContent = other;
        label.appendChild(sub);
      }
      box.addEventListener("change", function () {
        label.className = "gpick" + (box.checked ? " on" : "");
        count.textContent = "已选 " + boxes.filter(function (b) { return b.checked; }).length + " 个";
      });
      subLine.appendChild(label);
    }

    const count = document.createElement("span");
    count.className = "hint";
    count.textContent = "已选 " + current.size + " 个";
    subLine.appendChild(count);

    subLine.appendChild(buttonOf("全选", "ghost", function () {
      boxes.forEach(function (b) { b.checked = true; b.dispatchEvent(new Event("change")); });
    }));
    subLine.appendChild(buttonOf("清空", "ghost", function () {
      boxes.forEach(function (b) { b.checked = false; b.dispatchEvent(new Event("change")); });
    }));
    subLine.appendChild(buttonOf("保存目标群", "ghost", async function () {
      const picked = boxes
          .filter(function (item) { return item.checked; })
          .map(function (item) { return item.value; });
      const res = await postJSON("/api/guilds/profile", {
        guild_id: guild.id, action: "set", subscribers: picked
      });
      const msg = el("msg");
      msg.className = res && res.ok ? "status ok" : "status err";
      msg.textContent = res && res.ok
          ? "已保存「" + (guild.label || guild.name) + "」的转发目标群（"
            + picked.map(nameOf).join("、") + "）"
          : "保存失败";
      loadGuilds();
    }));
  }
  wrap.appendChild(subLine);

  // 这个频道当前生效的值（来自它自己的配置，没配则来自全局）
  const eff = document.createElement("div");
  eff.className = "hint";
  eff.textContent = "加群解锁：" + (guild.access_enabled ? "开" : "关")
      + "　资料板块：" + (guild.access_channel_id || "未设置")
      + "　解锁群：" + (guild.access_group_openid
          ? String(guild.access_group_openid).slice(0, 12) + "…" : "未设置")
      + "　推送模式：" + (guild.mode || "-");
  wrap.appendChild(eff);

  return wrap;
}

async function loadGuilds() {
  const box = el("guildList");
  if (!box) return;
  const data = await getJSON("/api/guilds");
  if (!data) { box.innerHTML = '<div class="empty">读不到频道数据</div>'; return; }
  el("guildsSync").textContent = [
    data.synced_text ? "上次同步 " + data.synced_text : "还没同步过",
    data.syncing ? "正在同步…" : ""
  ].filter(Boolean).join("，");
  el("syncBtn").disabled = !!data.syncing;
  // 顶部「生效范围」下拉：可选频道就是真查到的这些（加上已配过但已不在的）
  fillScopePicker(data.guilds, SCOPE_PROFILES);

  box.innerHTML = "";
  if (data.last_error) {
    const warn = document.createElement("div");
    warn.className = "note warn";
    warn.style.marginTop = "0";
    warn.textContent = data.last_error;
    box.appendChild(warn);
  }
  if (!data.has_data) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "还没有频道数据。机器人启动时会自动同步一次。";
    box.appendChild(empty);
    return;
  }
  for (const guild of data.guilds) {
    const block = document.createElement("div");
    block.className = "guild";
    const head = document.createElement("div");
    head.className = "guild-hd";
    const nm = document.createElement("span"); nm.className = "nm"; nm.textContent = guild.name;
    const gid = document.createElement("span"); gid.className = "gid"; gid.textContent = guild.id;
    const sp = document.createElement("span"); sp.className = "sp";
    head.append(nm, gid, sp);
    const cnt = document.createElement("span");
    cnt.className = "badge"; cnt.textContent = guild.channel_count + " 个板块";
    head.appendChild(cnt);
    if (guild.member_count) {
      const mc = document.createElement("span");
      mc.className = "badge"; mc.textContent = guild.member_count + " 成员";
      head.appendChild(mc);
    }
    if (guild.owner) {
      const ow = document.createElement("span");
      ow.className = "badge info"; ow.textContent = "频道主";
      head.appendChild(ow);
    }
    block.appendChild(head);
    // 多频道：这个频道的独立配置（转发目标群 / 生效值一览）
    block.appendChild(guildConfigBlock(guild, data.known_groups, data.global_subscribers));

    if (guild.channel_error) {
      const err = document.createElement("div");
      err.className = "ch";
      err.textContent = "拉取板块失败：" + guild.channel_error;
      block.appendChild(err);
    }
    for (const channel of guild.channels) {
      const row = document.createElement("div");
      row.className = "ch" + (channel.is_target ? " target" : "");
      const bullet = document.createElement("span");
      bullet.className = "bullet";
      bullet.textContent = channel.nested ? "└" : "•";
      const nameCell = document.createElement("span");
      nameCell.className = "nm";
      nameCell.textContent = channel.name;
      const typeTag = document.createElement("span");
      typeTag.className = "badge"; typeTag.textContent = channel.type;
      const pvTag = document.createElement("span");
      pvTag.className = "badge" + (channel.is_target && !channel.target_ok ? " warn" : "");
      pvTag.textContent = channel.private;
      row.append(bullet, nameCell, typeTag, pvTag);
      if (channel.is_target) {
        const t = document.createElement("span");
        t.className = "badge " + (channel.target_ok ? "on" : "warn");
        t.textContent = channel.target_ok ? "资料板块" : "资料板块（需改为指定成员可见）";
        row.appendChild(t);
      }
      const cid = document.createElement("span");
      cid.className = "cid"; cid.textContent = channel.id;
      row.appendChild(cid);
      block.appendChild(row);
    }

    // 机器人在这个频道里能用的接口权限
    if (guild.permission_error) {
      const err = document.createElement("div");
      err.className = "ch";
      err.textContent = "拉取接口权限失败：" + guild.permission_error;
      block.appendChild(err);
    } else if (guild.permission_total) {
      const head2 = document.createElement("div");
      head2.className = "ch perm-head";
      const label = document.createElement("span");
      label.className = "nm";
      label.textContent = "接口权限：" + guild.permission_granted
          + " / " + guild.permission_total + " 已授权";
      const btn = document.createElement("button");
      btn.className = "ghost";
      btn.textContent = "展开";
      btn.onclick = function () {
        const list = head2.nextElementSibling;
        const opened = list.style.display !== "none";
        list.style.display = opened ? "none" : "block";
        btn.textContent = opened ? "展开" : "收起";
      };
      head2.append(label, btn);
      block.appendChild(head2);

      const list = document.createElement("div");
      list.className = "perms";
      list.style.display = "none";
      for (const item of guild.permissions) {
        const line = document.createElement("div");
        line.className = "perm";
        const method = document.createElement("span");
        method.className = "method-badge";
        method.textContent = item.method || "-";
        const name = document.createElement("span");
        name.className = "nm";
        name.textContent = item.desc || item.path;
        const path = document.createElement("span");
        path.className = "cid";
        path.textContent = item.path;
        const status = document.createElement("span");
        status.className = "badge " + (item.granted ? "on" : "warn");
        status.textContent = item.status_text;
        line.append(method, name, path, status);
        list.appendChild(line);
      }
      block.appendChild(list);
    }

    box.appendChild(block);
  }

  if (data.permission_note) {
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = data.permission_note;
    box.appendChild(note);
  }
}

async function syncGuilds() {
  const btn = el("syncBtn");
  btn.disabled = true;
  el("guildsSync").textContent = "已请求同步，等机器人拉取…";
  try {
    await fetch("/api/sync", {method: "POST"});
  } catch (e) {
    console.warn("请求同步失败:", e);
  }
  // 同步发生在机器人的循环里，这里轮询几次看结果
  let tries = 0;
  const timer = setInterval(async function () {
    tries += 1;
    await loadGuilds();
    if (tries >= 8) clearInterval(timer);
  }, 2000);
}

/* ---------- 生效范围（全局 / 某个频道） ---------- */
let SCOPE = "";
let SCOPE_PROFILES = [];
let OVERRIDES = [];

function scopeKey() {
  try { return localStorage.getItem("forum_bot_scope") || ""; } catch (e) { return ""; }
}

function setScope(guildId) {
  SCOPE = guildId || "";
  try { localStorage.setItem("forum_bot_scope", SCOPE); } catch (e) { /* 忽略 */ }
  applyScopeLock();
  load();
}

/* 频道范围下禁用「仅全局」字段（指令词、互转、AI Key 这类）——
   它们在频道范围下保存会被后端丢弃，以前是静默丢，用户填了白填。
   禁用 + 写明原因，比让人对着"保存了却不生效"猜强 */
function applyScopeLock() {
  for (const f of FIELDS) {
    if (!f.global_only) continue;
    const node = el(f.key);
    if (node) node.disabled = !!SCOPE;
    const box = node ? node.closest(".field") : null;
    if (!box) continue;
    let tip = box.querySelector(".global-tip");
    if (SCOPE) {
      if (!tip) {
        tip = document.createElement("div");
        tip.className = "hint global-tip";
        tip.textContent = "此项仅全局有效 —— 请把生效范围切回「全局」再修改";
        box.appendChild(tip);
      }
    } else if (tip) {
      tip.remove();
    }
  }
}

function fillScopePicker(guilds, merged) {
  const picker = el("scopePicker");
  if (!picker) return;
  const keep = SCOPE;
  picker.innerHTML = "";
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "全局（默认，所有频道）";
  picker.appendChild(all);
  for (const guild of (guilds || [])) {
    const opt = document.createElement("option");
    opt.value = guild.id;
    opt.textContent = (guild.channel_number ? guild.channel_number + " " : "")
        + (guild.name || guild.id)
        + (guild.has_profile ? (guild.override_count ? "（已覆盖 " + guild.override_count + " 项）" : "（已独立配置）") : "");
    picker.appendChild(opt);
  }
  // 配置里存在但当前不在任何频道的（比如机器人已被移出）也留着，不然删不掉
  const known = new Set((guilds || []).map(function (g) { return g.id; }));
  for (const id of (merged || [])) {
    if (known.has(id)) continue;
    const opt = document.createElement("option");
    opt.value = id;
    opt.textContent = id + "（已不在该频道）";
    picker.appendChild(opt);
  }
  picker.value = keep;
  if (picker.value !== keep) {         // 选中的频道没了 -> 回退全局
    SCOPE = "";
    picker.value = "";
  }
}

function updateScopeBar(overridden) {
  const bar = el("scopeBar");
  if (!bar) return;
  if (!SCOPE) {
    bar.style.display = "none";
    return;
  }
  bar.style.display = "flex";
  const name = el("scopePicker") ? el("scopePicker").selectedOptions[0].textContent : SCOPE;
  const count = (overridden || []).length;
  el("scopeText").textContent = count
      ? "正在编辑「" + name + "」的独立配置：已覆盖 " + count + " 项，其余字段跟随全局。"
      : "正在编辑「" + name + "」的独立配置：目前全部字段都跟随全局，改哪项就覆盖哪项。";
}

function markOverrides(overridden) {
  const set = new Set(overridden || []);
  for (const f of FIELDS) {
    const node = el(f.key);
    const box = node ? node.closest(".field") : null;
    if (!box) continue;
    const label = box.querySelector("label");
    let badge = box.querySelector(".ov-badge");
    if (set.has(f.key)) {
      if (!badge && label) {
        badge = document.createElement("span");
        badge.className = "badge warn ov-badge";
        badge.textContent = "已覆盖";
        label.appendChild(badge);
      }
    } else if (badge) {
      badge.remove();
    }
  }
}

async function clearOverrides() {
  if (!SCOPE) return;
  const res = await postJSON("/api/guilds/profile", {guild_id: SCOPE, action: "clear"});
  const msg = el("msg");
  msg.className = res && res.ok ? "status ok" : "status err";
  msg.textContent = res && res.ok ? "已清除本频道的字段覆盖，全部回到跟随全局" : "清除失败";
  await load();
}

async function dropProfile() {
  if (!SCOPE) return;
  const name = el("scopePicker") ? el("scopePicker").selectedOptions[0].textContent : SCOPE;
  if (!window.confirm("删除「" + name + "」的全部独立配置？\n（转发目标群、字段覆盖、频道号都会一起删掉，之后跟随全局）")) {
    return;
  }
  const res = await postJSON("/api/guilds/profile", {guild_id: SCOPE, action: "drop"});
  const msg = el("msg");
  msg.className = res && res.ok ? "status ok" : "status err";
  msg.textContent = res && res.ok ? "已删除该频道的独立配置" : "删除失败";
  SCOPE = "";
  try { localStorage.setItem("forum_bot_scope", ""); } catch (e) { /* 忽略 */ }
  await load();
}

/* ---------- 加载 / 保存 ---------- */
async function load() {
  const msg = el("msg");
  msg.className = "status";
  msg.textContent = "读取中…";
  // 首次进页面时 SCOPE 可能从 localStorage 恢复成了某个频道 ——
  // 全局字段的禁用状态要跟范围对齐
  applyScopeLock();
  try {
    const res = await fetch(
      "/api/settings?guild_id=" + encodeURIComponent(SCOPE), {cache: "no-store"});
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    for (const f of FIELDS) {
      const node = el(f.key);
      if (!node) continue;
      if (f.kind === "checkbox") node.checked = !!data[f.key];
      else node.value = data[f.key];
    }
    const scope = data.__scope__ || {};
    SCOPE_PROFILES = scope.profiles || [];
    OVERRIDES = scope.overridden || [];
    markOverrides(OVERRIDES);
    renderPreview();
    msg.textContent = "";
  } catch (e) {
    msg.className = "status err";
    msg.textContent = "读取失败：" + e.message;
    return;
  }
  await Promise.all([
    loadMembers(), loadBlacklist(), loadAccess(),
    loadOverview(), loadGroups(), loadGuilds(), loadRelayRules(),
    loadRelayGroups(), loadRelayBindings(), loadMemberBan(), loadAiSuggest(),
  ]);
  updateScopeBar(OVERRIDES);
}

/* 保存前校验：数字必须是整数、服务地址必须是 http(s)。
   返回第一条错误（带字段中文名），通过返回空串 */
function validateForm() {
  for (const f of FIELDS) {
    const node = el(f.key);
    if (!node) continue;
    const v = String(node.value || "").trim();
    if (f.kind === "number" && v !== "" && !/^-?\d+$/.test(v)) {
      return "「" + f.label + "」要填整数（现在填的是：" + v + "）";
    }
    if ((f.key === "onebot_http_url" || f.key === "ai_base_url")
        && v !== "" && !/^https?:\/\//i.test(v)) {
      return "「" + f.label + "」要以 http:// 或 https:// 开头";
    }
  }
  return "";
}

async function save() {
  const btn = el("saveBtn");
  const footBtn = el("cfgSaveBtn");
  const msg = el("msg");
  msg.className = "status";
  // 先校验再提交 —— 数值/地址格式错了直接拦下，省一次无意义的请求
  const problem = validateForm();
  if (problem) {
    msg.className = "status err";
    msg.textContent = "校验失败：" + problem;
    return;
  }
  msg.textContent = "保存中…";
  btn.disabled = true;
  if (footBtn) footBtn.disabled = true;
  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({guild_id: SCOPE, values: readForm()})
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    const scope = data.__scope__ || {};
    OVERRIDES = scope.overridden || [];
    markOverrides(OVERRIDES);
    updateScopeBar(OVERRIDES);
    msg.className = "status ok";
    msg.textContent = "已保存 " + new Date().toLocaleTimeString()
        + (SCOPE ? "（当前频道）" : "（全局）");
  } catch (e) {
    msg.className = "status err";
    msg.textContent = "保存失败：" + e.message;
  } finally {
    btn.disabled = false;
    if (footBtn) footBtn.disabled = false;
  }
  await Promise.all([
    loadMembers(), loadBlacklist(), loadAccess(), loadOverview(), loadGroups(),
    loadRelayRules(), loadRelayGroups(), loadRelayBindings(), loadMemberBan(),
    loadAiSuggest(),
  ]);
}

/* ---------- 运行日志 ---------- */
/* 轮询而不是 WebSocket：配置页跑在一个极简的 http.server 线程里，
   加 WebSocket 要动整个服务端结构，而 2 秒一次的增量拉取（只传新增的行）
   在体感上已经够"实时"，代价小得多。 */
let LOG_SEQ = 0;
let LOG_TIMER = null;
let LOG_SHOWN = 0;
// 页面上最多留多少行 DOM。开着一整天不关的话，不加这个会越来越卡
const LOG_MAX_ROWS = 500;

function logRowOf(item) {
  const row = document.createElement("div");
  row.className = "logrow " + String(item.level || "").toLowerCase();
  const t = document.createElement("span");
  t.className = "t"; t.textContent = item.time || "";
  const lv = document.createElement("span");
  lv.className = "lv"; lv.textContent = item.level || "";
  const src = document.createElement("span");
  src.className = "src"; src.textContent = item.source || "";
  const tx = document.createElement("span");
  tx.className = "tx"; tx.textContent = item.text || "";
  row.append(t, lv, src, tx);
  return row;
}

async function pullLogs() {
  const box = el("logBox");
  if (!box) return;
  const sel = el("logLevel");
  const data = await getJSON(
      "/api/logs?since=" + LOG_SEQ + "&level=" + encodeURIComponent(sel ? sel.value : ""));
  if (!data) return;

  // 用户手动往上翻的时候别把他拽回底部
  const follow = (!el("logFollow") || el("logFollow").checked);
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;

  const entries = data.entries || [];
  if (entries.length && box.querySelector(".empty")) box.innerHTML = "";
  for (const item of entries) {
    box.appendChild(logRowOf(item));
    LOG_SHOWN += 1;
  }
  while (box.childElementCount > LOG_MAX_ROWS) box.removeChild(box.firstElementChild);

  LOG_SEQ = data.cursor || LOG_SEQ;
  const st = data.stats || {};
  if (el("logStats")) {
    el("logStats").textContent = "缓冲 " + (st.kept || 0) + "/" + (st.capacity || 0) + " 条"
        + (st.dropped ? "（已滚掉 " + st.dropped + " 条）" : "")
        + "　本次已显示 " + LOG_SHOWN + " 条";
  }
  if (!box.childElementCount) {
    box.innerHTML = '<div class="empty">' + (data.available === false
        ? "没接上日志流（配置页可能是单独启动的，没有机器人进程）" : "还没有日志") + "</div>";
  }
  if (follow && nearBottom) box.scrollTop = box.scrollHeight;
}

async function resetLogs() {
  LOG_SEQ = 0;
  LOG_SHOWN = 0;
  const box = el("logBox");
  if (box) box.innerHTML = '<div class="empty">读取中…</div>';
  await pullLogs();
}

function startLogs() {
  resetLogs();
  if (LOG_TIMER) clearInterval(LOG_TIMER);
  LOG_TIMER = setInterval(pullLogs, 2000);
}

function stopLogs() {
  if (LOG_TIMER) { clearInterval(LOG_TIMER); LOG_TIMER = null; }
}

async function clearLogs() {
  await postJSON("/api/logs/clear", {});
  await resetLogs();
}

/* ---------- 互转规则：解析预览 + 选群添加/删除 ---------- */
/* 选群添加的目的只有一个：**别让人抄 openid**。
   写进规则的是群 openid（机器生成的），所以不依赖「群聊」页的备注有没有填；
   备注填了的话，下面的解析结果和群里的「转发状态」照样会显示成群号。 */
let RELAY_GROUPS = [];
let RELAY_NUMBER_MAP = {};   // {群号: openid}，判重用 —— 群号写的规则和 openid 写的是同一条

function relayGroupLabel(row) {
  const no = String(row.group_no || "");
  const name = String(row.name || "");
  const openid = String(row.group_openid || "");
  if (no && name) return no + " " + name;
  if (no) return no;
  if (name) return name;
  if (row.note) return String(row.note);
  return openid ? openid.slice(0, 10) + "…" : "（未命名群）";
}

async function loadRelayGroups() {
  const source = el("relaySource");
  const target = el("relayTarget");
  if (!source || !target) return;
  const data = await getJSON("/api/groups");
  const rows = (data && data.rows) || [];
  RELAY_GROUPS = rows;

  const keepSource = source.value;
  const keepTarget = target.value;
  source.innerHTML = "";
  target.innerHTML = "";
  for (const row of rows) {
    for (const box of [source, target]) {
      const opt = document.createElement("option");
      opt.value = String(row.group_openid || "");
      opt.textContent = relayGroupLabel(row);
      box.appendChild(opt);
    }
  }
  if (keepSource) source.value = keepSource;
  if (keepTarget) target.value = keepTarget;
  if (!source.value && rows.length) source.value = String(rows[0].group_openid || "");
  if (!target.value && rows.length > 1) target.value = String(rows[1].group_openid || "");

  const hint = el("relayPickerHint");
  if (hint) {
    hint.textContent = rows.length
        ? "共 " + rows.length + " 个已知群（来自事件记录：被拉进群 / 收到过群消息）。"
        : "还没有已知的群：机器人被拉进群、或在群里收到过消息之后才会出现在这里。";
  }
  // 互转规则是**全局**设置（群消息事件里没有频道信息），
  // 在「某个频道的编辑范围」下改它会静默不生效 —— 所以直接说清楚
  const warn = el("relayPickerWarn");
  if (warn) {
    if (SCOPE) {
      warn.style.display = "";
      warn.textContent = "互转规则属于全局设置。当前正在编辑某个频道的独立配置，"
          + "这里添加 / 删除的规则会直接写到全局。";
    } else {
      warn.style.display = "none";
    }
  }
}

function splitRuleLines(raw) {
  return String(raw || "").split(/\r?\n/).filter(function (line) {
    return line.trim() !== "";
  });
}

/* 群号 -> openid。规则里两种写法都合法，判重时必须换算成同一个东西，
   否则「123456789 -> 123456789」和「<openid> -> <openid>」会被当成两条。 */
function relayTokenToOpenid(token) {
  const raw = String(token || "").trim();
  if (!raw) return "";
  if (/^\d{5,12}$/.test(raw)) return String(RELAY_NUMBER_MAP[raw] || "");
  return raw;
}

function relayRulePairs() {
  const pairs = [];
  const raw = el("relay_rules") ? el("relay_rules").value : "";
  for (const line of splitRuleLines(raw)) {
    const item = line.trim();
    if (!item || item.startsWith("#")) continue;
    let source = "", target = "";
    for (const arrow of ["->", "→"]) {
      const at = item.indexOf(arrow);
      if (at > 0) {
        source = item.slice(0, at).trim();
        target = item.slice(at + arrow.length).trim();
        break;
      }
    }
    if (!source || !target) continue;
    pairs.push([relayTokenToOpenid(source), relayTokenToOpenid(target)]);
  }
  return pairs;
}

function relayMsg(message, isErr) {
  const box = el("msg");
  if (!box) return;
  box.className = isErr ? "status err" : "status ok";
  box.textContent = message;
}

async function addRelayRule(both) {
  const source = el("relaySource");
  const target = el("relayTarget");
  if (!source || !target || !source.value || !target.value) {
    relayMsg("请先选择来源群和目标群", true);
    return;
  }
  if (source.value === target.value) {
    relayMsg("来源群和目标群不能是同一个群（自己转给自己只会白烧配额）", true);
    return;
  }
  const pairs = relayRulePairs();
  const lines = splitRuleLines(el("relay_rules").value);
  const wanted = [[source.value, target.value]];
  if (both) wanted.push([target.value, source.value]);

  let added = 0;
  for (const pair of wanted) {
    if (pairs.some(function (item) {
      return item[0] === pair[0] && item[1] === pair[1];
    })) {
      continue;
    }
    lines.push(pair[0] + " -> " + pair[1]);
    pairs.push(pair);
    added++;
  }
  if (!added) {
    relayMsg("这条规则已经存在了，没有重复添加", true);
    return;
  }
  await saveRelayRules(lines.join("\n"), "已添加 " + added + " 条规则");
}

async function removeRelayRule(line) {
  const raw = String(line || "").trim();
  if (!raw) {
    relayMsg("这条规则没有原始文本，删不掉 —— 请到上面的输入框里手工改", true);
    return;
  }
  if (!window.confirm("删除这条互转规则？\n\n" + raw)) return;
  const kept = splitRuleLines(el("relay_rules").value).filter(function (item) {
    return item.trim() !== raw;
  });
  await saveRelayRules(kept.join("\n"), "已删除 1 条规则");
}

/* 直接写**全局**，不跟右上角的「频道范围」走：
   互转规则是群维度的键，带 guild_id 保存会被后端的「群维度键忽略」逻辑丢掉，
   那样点了「添加」却什么都没发生，最难查。 */
async function saveRelayRules(value, okText) {
  const node = el("relay_rules");
  if (node) node.value = value;
  const res = await postJSON("/api/settings", {guild_id: "", values: {relay_rules: value}});
  if (res && res.ok === false) {
    relayMsg((res.msg || "保存失败"), true);
  } else {
    relayMsg((okText || "已保存") + "（互转规则是全局设置）", false);
  }
  await loadRelayRules();
}

async function loadRelayRules() {
  const box = el("relayRules");
  if (!box) return;
  const data = await getJSON("/api/relay/rules");
  if (!data) { box.innerHTML = '<div class="empty">读不到规则解析结果</div>'; return; }
  box.innerHTML = "";
  RELAY_NUMBER_MAP = data.number_map || {};

  const head = document.createElement("p");
  head.className = "hint";
  head.textContent = "生效规则 " + (data.routes || 0) + " 条　"
      + "已知群备注 " + Object.keys(data.number_map || {}).length + " 个";
  box.appendChild(head);

  const rows = data.rules || [];
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "note";
    empty.textContent = "还没有生效的规则";
    box.appendChild(empty);
  } else {
    const table = document.createElement("table");
    table.className = "list";
    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const title of ["来源群", "目标群", ""]) {
      const th = document.createElement("th");
      th.textContent = title;
      headRow.appendChild(th);
    }
    thead.appendChild(headRow);

    const tbody = document.createElement("tbody");
    for (const row of rows) {
      const tr = document.createElement("tr");
      const from = document.createElement("td");
      from.textContent = row.source_label || row.source;
      const to = document.createElement("td");
      to.textContent = row.target_label || row.target;
      const ops = document.createElement("td");
      // 删的是**规则原文**（可能是群号、也可能是一长串 openid）。
      // 原文由后端随解析结果一起给出，页面不用自己反推 —— 反推必然对不上群号写法
      ops.appendChild(buttonOf("删除", "ghost", function () {
        removeRelayRule(row.line);
      }));
      tr.append(from, to, ops);
      tbody.appendChild(tr);
    }
    table.append(thead, tbody);
    box.appendChild(table);
  }

  // 没生效的规则**必须**显示出来：静默跳过等于"配了没生效"，最难查
  for (const issue of data.issues || []) {
    const warn = document.createElement("p");
    warn.className = "note warn";
    warn.textContent = "没生效：" + issue.line + "　——　" + issue.reason;
    box.appendChild(warn);
  }

  const numbers = Object.keys(data.number_map || {});
  if (numbers.length) {
    const hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent = "已知群号：" + numbers.join("、")
        + "（这些群号也可以直接写在规则里）";
    box.appendChild(hint);
  }

  // 格式预览里用哪个群的群号：优先第一条规则的源群，没有规则就取一个已知群号
  const labelOf = data.label || {};
  RELAY_GROUP = rows.length ? (labelOf[rows[0].source] || "") : (numbers[0] || "");
  renderRelayPreview();
}

/* ---------- 成员自助绑定的 QQ 号 ---------- */
/* 只读展示 + 删除。**不做"手工新增"** —— 新增走群里那条自助指令；
   管理员要给某人手工指定标识，用「成员标识映射」（优先级更高、还能纠正填错的）。
   两套入口各管一件事，不用在一张表里塞两种来源。 */
async function loadRelayBindings() {
  const box = el("relayBindings");
  if (!box) return;
  const data = await getJSON("/api/relay/bindings");
  if (!data) { box.innerHTML = '<div class="empty">读不到绑定列表</div>'; return; }
  box.innerHTML = "";

  const head = document.createElement("p");
  head.className = "hint";
  head.textContent = "已绑定 " + (data.count || 0) + " 人"
      + (data.bound_only ? "　·　已开启「只转发已绑定成员」" : "");
  box.appendChild(head);

  const rows = data.bindings || [];
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "note";
    empty.textContent = "还没有人绑定 —— 由 OneBot 自动识别或手工指定后出现";
    box.appendChild(empty);
    return;
  }

  const table = document.createElement("table");
  table.className = "list";
  const tbody = document.createElement("tbody");
  for (const row of rows) {
    const tr = document.createElement("tr");

    const nick = document.createElement("td");
    nick.textContent = row.nick || "（无昵称）";

    const qq = document.createElement("td");
    qq.textContent = row.qq || "";
    qq.style.fontFamily = "ui-monospace, Consolas, monospace";
    qq.style.fontWeight = "600";

    // openid 也留着：relay_notes 要按它填，光看昵称对不上是哪个 openid
    const who = document.createElement("td");
    who.className = "hint";
    who.textContent = row.openid ? row.openid.slice(0, 10) + "…" : "";
    who.title = row.openid || "";

    const when = document.createElement("td");
    when.className = "hint";
    when.textContent = (row.at_text || "")
        + (row.source === "onebot" ? "　·　OneBot 自动" : "");

    const ops = document.createElement("td");
    ops.appendChild(buttonOf("删除", "ghost", async function () {
      const label = row.nick || row.qq;
      if (!window.confirm("删掉「" + label + "」的绑定？\n"
          + "他之后发的消息在转发里会显示成 openid 前 8 位。")) {
        return;
      }
      const res = await postJSON("/api/relay/bindings",
          {action: "remove", openid: row.openid});
      const msg = el("msg");
      msg.className = res && res.ok ? "status ok" : "status err";
      msg.textContent = res && res.ok ? "已删除「" + label + "」的绑定" : "删除失败";
      loadRelayBindings();
    }));
    tr.append(nick, qq, who, when, ops);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  box.appendChild(table);

  const foot = document.createElement("p");
  foot.className = "hint";
  foot.style.marginTop = "10px";
  foot.appendChild(buttonOf("清空全部绑定", "ghost", async function () {
    if (!window.confirm("清空全部 " + rows.length + " 条绑定？此操作不可撤销。")) return;
    const res = await postJSON("/api/relay/bindings", {action: "clear"});
    const msg = el("msg");
    msg.className = res && res.ok ? "status ok" : "status err";
    msg.textContent = res && res.ok ? "已清空全部绑定" : "清空失败";
    loadRelayBindings();
  }));
  box.appendChild(foot);
}

/* ---------- 成员黑名单 ---------- */
/* 按**人**封（上面那个名单按**内容**封）。这里只读 + 删除；
   新增走群里的「拉黑成员」指令，或者引用一条消息发「拉黑」。 */
async function loadMemberBan() {
  const table = el("memberBanTable");
  if (!table) return;
  const body = table.querySelector("tbody");
  const data = await getJSON("/api/member-ban");
  if (!data || !body) return;
  body.innerHTML = "";
  const foot = el("memberBanFoot");
  if (foot) foot.innerHTML = "";

  const rows = data.members || [];
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 5;
    td.className = "hint";
    td.textContent = "还没有人 —— 引用某人的消息发「拉黑」，或发「拉黑成员」";
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }

  for (const row of rows) {
    const tr = document.createElement("tr");
    const group = row.group_openid || "";
    tr.append(
      plainCell(row.username || "（无昵称）"),
      plainCell(row.openid || "", "hint"),
      plainCell(row.at || "", "hint"),
      plainCell(group ? group.slice(0, 10) + "…" : "", "hint")
    );
    const ops = document.createElement("td");
    ops.appendChild(buttonOf("解除", "ghost", async function () {
      const label = row.username || row.openid;
      if (!window.confirm("把「" + label + "」移出成员黑名单？\n"
          + "他之后发的消息就不撤了。")) {
        return;
      }
      const res = await postJSON("/api/member-ban",
          {action: "remove", openid: row.openid});
      const msg = el("msg");
      msg.className = res && res.ok ? "status ok" : "status err";
      msg.textContent = res && res.ok ? "已解除「" + label + "」" : "解除失败";
      loadMemberBan();
    }));
    tr.appendChild(ops);
    body.appendChild(tr);
  }

  if (foot) {
    foot.appendChild(buttonOf("清空成员黑名单", "ghost", async function () {
      if (!window.confirm("清空全部 " + rows.length + " 人？此操作不可撤销。")) return;
      const res = await postJSON("/api/member-ban", {action: "clear"});
      const msg = el("msg");
      msg.className = res && res.ok ? "status ok" : "status err";
      msg.textContent = res && res.ok ? "已清空成员黑名单" : "清空失败";
      loadMemberBan();
    }));
  }
}

/* ---------- AI 候选拉黑关键词 ---------- */
/* 群里拉黑消息后 AI 拆出来的短语堆在这里，勾选才真正进名单（见后端注释）。
   没开功能/没填 key 时给出明确的提示，别让人对着空列表猜原因。 */
async function loadAiSuggest() {
  const box = el("aiSuggestList");
  if (!box) return;
  const data = await getJSON("/api/ai-suggest");
  const items = (data && data.items) || [];
  box.innerHTML = "";
  const foot = el("aiSuggestFoot");
  if (foot) foot.innerHTML = "";

  if (data && !data.enabled) {
    box.innerHTML = '<p class="hint">未启用：到「AI 服务」页配好厂商和 Key，并打开上面的「拉黑消息时用 AI 拆分候选关键词」。</p>';
    return;
  }
  if (data && data.enabled && !data.key_set) {
    box.innerHTML = '<p class="hint">开关已打开，但还没填 API Key —— 到「AI 服务」页填好并保存。</p>';
    return;
  }
  if (!items.length) {
    box.innerHTML = '<p class="hint">暂时没有候选 —— 群里引用拉黑一条消息后，AI 拆出的短语会出现在这里。</p>';
    return;
  }

  for (const item of items) {
    const wrap = document.createElement("div");
    wrap.className = "card";
    wrap.style.cssText = "margin:10px 0;border:1px solid var(--line,#ddd);border-radius:8px;padding:10px 12px";

    const meta = document.createElement("p");
    meta.className = "hint";
    meta.textContent = [item.by_name, item.at].filter(Boolean).join(" / ") || "AI 拆分";
    wrap.appendChild(meta);

    const src = document.createElement("p");
    src.style.cssText = "margin:4px 0;word-break:break-all";
    src.textContent = "原消息：" + (item.source || "（无）");
    wrap.appendChild(src);

    const list = document.createElement("div");
    for (const phrase of item.phrases || []) {
      const label = document.createElement("label");
      label.style.cssText = "display:block;margin:3px 0";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.className = "ai-phrase";
      cb.value = phrase;
      label.append(cb, document.createTextNode(" " + phrase));
      list.appendChild(label);
    }
    wrap.appendChild(list);

    const acts = document.createElement("div");
    acts.style.marginTop = "8px";
    acts.appendChild(buttonOf("加入选中的", "ghost", async function () {
      const picked = Array.from(wrap.querySelectorAll(".ai-phrase:checked"))
          .map(function (node) { return node.value; });
      if (!picked.length) { window.alert("先勾选要加入的短语"); return; }
      const res = await postJSON("/api/ai-suggest",
          {action: "accept", id: item.id, phrases: picked});
      const msg = el("msg");
      msg.className = res && res.ok ? "status ok" : "status err";
      msg.textContent = (res && res.msg) || "操作失败";
      await Promise.all([loadBlacklist(), loadAiSuggest()]);
    }));
    acts.appendChild(buttonOf("忽略这批", "ghost", async function () {
      const res = await postJSON("/api/ai-suggest", {action: "dismiss", id: item.id});
      const msg = el("msg");
      msg.className = res && res.ok ? "status ok" : "status err";
      msg.textContent = (res && res.msg) || "操作失败";
      loadAiSuggest();
    }));
    wrap.appendChild(acts);
    box.appendChild(wrap);
  }

  if (foot) {
    foot.style.marginTop = "10px";
    foot.appendChild(buttonOf("清空全部候选", "ghost", async function () {
      if (!window.confirm("清空全部 " + items.length + " 批候选？")) return;
      const res = await postJSON("/api/ai-suggest", {action: "clear"});
      const msg = el("msg");
      msg.className = res && res.ok ? "status ok" : "status err";
      msg.textContent = (res && res.msg) || "操作失败";
      loadAiSuggest();
    }));
  }
}

/* 「测试 AI 连接」：拿**已保存**的设置真调一次拆分接口。
   测的是保存过的值 —— 刚改完 Key 没保存就点，测的还是旧配置。 */
async function testAI() {
  const btn = el("aiTestBtn");
  const out = el("aiTestMsg");
  if (!btn) return;
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "测试中…（最长 20 秒）";
  if (out) { out.textContent = ""; out.className = "hint"; }
  const res = await postJSON("/api/ai-test", {});
  if (out) {
    out.textContent = (res && res.msg) || "无响应（配置页可能没连上机器人进程）";
    out.className = res && res.ok ? "status ok" : "status err";
  }
  btn.disabled = false;
  btn.textContent = original;
}

/* ---------- OneBot QQ 号识别 ---------- */
function showOnebotMsg(ok, text) {
  const out = el("onebotMsg");
  if (out) {
    out.textContent = text;
    out.className = ok ? "status ok" : "status err";
  }
  const issues = el("onebotIssues");
  if (issues) {
    const list = (arguments[2] || []).filter(Boolean);
    issues.style.display = list.length ? "block" : "none";
    issues.textContent = list.join("\n");
  }
}

async function onebotProbe() {
  const btn = el("onebotProbeBtn");
  if (!btn) return;
  btn.disabled = true;
  showOnebotMsg(true, "测试中…");
  const res = await postJSON("/api/onebot/probe", {});
  showOnebotMsg(!!(res && res.ok), (res && res.msg) || "无响应");
  btn.disabled = false;
}

async function onebotSync() {
  const btn = el("onebotSyncBtn");
  if (!btn) return;
  if (!window.confirm("从 NapCat 拉取群成员并按昵称自动绑定？\n"
      + "匹配到的真实 QQ 号会覆盖用户自己绑的号。")) {
    return;
  }
  btn.disabled = true;
  showOnebotMsg(true, "拉取中…（群多人多时可能要十几秒）");
  const res = await postJSON("/api/onebot/sync", {});
  showOnebotMsg(!!(res && res.ok), (res && res.msg) || "无响应", res && res.issues);
  await loadRelayBindings();
  btn.disabled = false;
}

/* ---------- 转发格式实时预览 ---------- */
/* 样本只是拿来看效果的，不参与任何判断。
   qq 给个示例值，好让 {qq} 在预览里也有东西看 —— 真实情况下没绑定就是空串，
   下面 renderRelayPreview 会把这点写在提示里。 */
const RELAY_SAMPLE = {
  nick: "张三", uid: "10086", qq: "10086",
  content: "今晚八点开会", role: "普通成员"
};
let RELAY_GROUP = "";

function insertFormat(token) {
  const node = el("relay_format");
  if (!node) return;
  const start = node.selectionStart === null ? node.value.length : node.selectionStart;
  const end = node.selectionEnd === null ? node.value.length : node.selectionEnd;
  node.value = node.value.slice(0, start) + token + node.value.slice(end);
  node.selectionStart = node.selectionEnd = start + token.length;
  node.focus();
  renderRelayPreview();
}

/* 把 {key} 逐段替换成 map 里的值。
   这里**不用正则**：这段 JS 要穿过 Python 的字符串字面量，
   反斜杠转义容易被 Python 当成转义序列吃掉，出问题还很难看出来。 */
function fillTokens(tpl, map) {
  let out = "";
  const chunks = String(tpl).split("{");
  for (let i = 0; i < chunks.length; i += 1) {
    if (i === 0) { out += chunks[i]; continue; }
    const end = chunks[i].indexOf("}");
    if (end === -1) { out += "{" + chunks[i]; continue; }
    const key = chunks[i].slice(0, end).trim();
    out += (key in map ? map[key] : "{" + key + "}") + chunks[i].slice(end + 1);
  }
  return out;
}

function renderRelayPreview() {
  const box = el("relayPreview");
  if (!box) return;
  const node = el("relay_format");
  const tpl = String((node && node.value) || "{nick}({uid})：{content}").trim();

  const sample = {
    nick: RELAY_SAMPLE.nick,
    uid: RELAY_SAMPLE.uid,
    content: RELAY_SAMPLE.content,
    role: RELAY_SAMPLE.role,
    group: RELAY_GROUP || "（源群没填群备注）"
  };
  sample.group_no = sample.group;

  box.textContent = fillTokens(tpl, sample) || "（格式是空的 —— 保存后会退回默认格式）";

  const hint = el("relayPreviewHint");
  if (!hint) return;
  if (RELAY_GROUP) {
    hint.textContent = "样本里的群号 " + RELAY_GROUP + " 来自「群聊」页的群备注";
  } else if (tpl.indexOf("{group") !== -1) {
    hint.textContent = "提示：还没有群填过群备注，{group} 现在只能显示 openid —— "
        + "去「群聊」页给群填个备注就有群号了";
  } else {
    hint.textContent = "";
  }
}

/* ---------- CLI 扫码登录 ---------- */
/* 流程：点「扫码登录」-> 后端同步调 CLI 拿到授权码和二维码 -> 这里画出来 ->
   每 3 秒轮询一次登录结果 -> 成功后自动刷新状态。
   用轮询而不是长连接，理由和「运行日志」页一样：配置页是个极简的 http.server，
   加长连接要动整个服务端结构，而 3 秒一次的开销小得多。

   二维码只在**本次点击之后**才存在于内存里（刷新页面就没了），
   所以下面的状态渲染分两种情况处理。 */
let CLI_QR = null;      // {qr_code, verification_uri, expires_in_s}
let CLI_QR_AT = 0;      // 拿到二维码的时间，用来算倒计时
let CLI_TIMER = null;
// 防重入。`poll-token` 在"还没扫码"时会**阻塞等授权**，单次要 8 秒，
// 而轮询间隔是 3 秒 —— 不挡一下的话会有好几次调用叠在一起。
// 叠在一起不只是浪费：**一次成功的轮询会把授权状态文件消费掉**，
// 排队在后头那次就会读到"文件不存在"，页面上表现成
// "刚显示登录成功，紧接着冒出一串报错"。（线上就是这么炸的。）
let CLI_POLLING = false;
// 已经出结果了（显示过 ✅）。用来挡住迟到的响应把成功提示盖掉。
let CLI_DONE = false;

function cliBadge(box, label, value, kind) {
  const k = document.createElement("span");
  k.className = "k";
  k.textContent = label + "：";
  box.appendChild(k);
  const v = document.createElement("span");
  if (kind) { v.appendChild(badgeOf(value, kind)); } else { v.textContent = value; }
  box.appendChild(v);
}

async function loadCli(probe) {
  const box = el("cliStat");
  if (!box) return;
  const data = probe
      ? await postJSON("/api/cli/probe", {})
      : await getJSON("/api/cli/status");
  const st = data && data.status;
  box.innerHTML = "";
  if (!st) {
    box.innerHTML = '<span class="hint">读不到 CLI 状态</span>';
    return;
  }
  cliBadge(box, "命令", st.command || "(未配置)");
  cliBadge(box, "安装", st.installed ? "已安装" : "未找到", st.installed ? "on" : "warn");
  cliBadge(box, "登录", st.logged_in ? "已登录" : "未登录 / 已失效",
           st.logged_in ? "on" : "warn");
  cliBadge(box, "可用", st.available ? "可用" : "不可用", st.available ? "on" : "off");
  cliBadge(box, "短链缓存", String(st.cached_links || 0) + " 条");

  const note = document.createElement("div");
  note.className = "hint";
  note.style.flexBasis = "100%";
  note.textContent = st.note || "";
  box.appendChild(note);

  const btn = el("cliLoginBtn");
  if (btn) btn.textContent = st.logged_in ? "扫码登录 / 换个账号" : "扫码登录";

  if (st.login_pending) {
    if (CLI_QR) {
      showCliQr();
    } else {
      const box2 = el("cliLoginBox");
      if (box2) {
        box2.innerHTML = '<div class="note">上次发起的扫码还在等待授权，'
            + '但二维码已经不在页面上了 —— 重新点一次「扫码登录」拿张新的。</div>';
      }
    }
    startCliPoll();
  }
}

function showCliQr() {
  const box = el("cliLoginBox");
  if (!box || !CLI_QR) return;
  box.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "qrbox";
  const img = document.createElement("img");
  img.alt = "扫码登录二维码";
  img.src = "data:image/png;base64," + (CLI_QR.qr_code || "");
  wrap.appendChild(img);
  const tip = document.createElement("div");
  tip.className = "qr-tip";
  wrap.appendChild(tip);
  box.appendChild(wrap);
  updateCliCountdown();
  startCliTick();

  if (CLI_QR.verification_uri) {
    const line = document.createElement("p");
    line.className = "hint";
    line.style.marginTop = "10px";
    line.appendChild(document.createTextNode("手机不方便扫码？"));
    const a = document.createElement("a");
    a.href = CLI_QR.verification_uri;
    a.target = "_blank";
    a.rel = "noreferrer";
    a.textContent = "点这条链接直接授权";
    line.appendChild(a);
    line.appendChild(document.createTextNode("（用已登录 QQ 的手机打开）"));
    box.appendChild(line);
  }
}

/* 倒计时**必须有自己的定时器**。
   以前只在"轮询返回 pending"时顺手更新一次 —— 而轮询一旦因为任何原因停下
   （出错、idle、切页），倒计时就冻在那一格：页面上就是「剩余 599 秒」一动不动。 */
let CLI_TICK = null;

function stopCliTick() {
  if (CLI_TICK) { clearInterval(CLI_TICK); CLI_TICK = null; }
}

function startCliTick() {
  if (CLI_TICK) return;
  CLI_TICK = setInterval(updateCliCountdown, 1000);
}

function updateCliCountdown() {
  const tip = document.querySelector("#cliLoginBox .qr-tip");
  // 二维码或那一行字没了就自动停下 —— 不用在每个收尾分支里各写一遍 stopCliTick，
  // 漏一处就会留一个每秒空转的定时器
  if (!tip || !CLI_QR) { stopCliTick(); return; }
  const total = Number(CLI_QR.expires_in_s || 0);
  const left = Math.max(0, total - Math.floor((Date.now() - CLI_QR_AT) / 1000));
  tip.textContent = "用手机 QQ 扫码授权　剩余 " + left + " 秒";
  if (left <= 0) {
    tip.textContent = "二维码已过期，重新点「扫码登录」";
    stopCliTick();
  }
}

async function startCliLogin(force) {
  const box = el("cliLoginBox");
  if (box) box.innerHTML = '<div class="hint">正在向腾讯申请授权码…</div>';
  stopCliPoll();
  CLI_DONE = false;
  const res = await postJSON("/api/cli/login", {action: force ? "force" : "start"});
  if (!res || !res.ok) {
    if (box) {
      box.innerHTML = '<div class="note warn">' + ((res && res.msg) || "发起登录失败")
          + (force ? "" : "　（如果之前登录过，请用「强制重新登录」）") + "</div>";
    }
    await loadCli();
    return;
  }
  CLI_QR = res.login || {};
  CLI_QR_AT = Date.now();
  showCliQr();
  startCliPoll();
}

function startCliPoll() {
  if (CLI_TIMER) return;
  CLI_TIMER = setInterval(pollCli, 3000);
}

function stopCliPoll() {
  if (CLI_TIMER) { clearInterval(CLI_TIMER); CLI_TIMER = null; }
  CLI_POLLING = false;
}

async function pollCli() {
  // 上一次还没回来就别发新的（见 CLI_POLLING 的说明）。
  // 注意：stopCliPoll() 只能停掉**将来**的定时触发，
  // 已经在飞行中的那一次还是会跑完 —— 所以结果回来时还得再看一眼状态。
  if (CLI_POLLING) return;
  CLI_POLLING = true;
  try {
    const res = await postJSON("/api/cli/poll", {});
    if (!res) return;
    const box = el("cliLoginBox");
    const state = res.login_status;

    if (state === "authorized") {
      stopCliPoll();
      CLI_QR = null;
      CLI_DONE = true;
      if (box) {
        box.innerHTML = '<div class="note">✅ 登录成功，凭证已更新。'
            + '短链功能立刻恢复，<b>不用重启机器人</b>。</div>';
      }
      await loadCli();
      return;
    }

    if (state === "expired") {
      stopCliPoll();
      CLI_QR = null;
      if (box) {
        box.innerHTML = '<div class="note warn">二维码已过期，重新点一次「扫码登录」。</div>';
      }
      await loadCli();
      return;
    }

    if (state === "idle") {
      // **没有正在进行的登录流程**。这不是错误 —— 最常见的原因是上一次
      // 已经成功了（授权状态文件被消费掉），或者被取消了。
      // 以前这种情况会被当成 error 弹红字，用户刚看到 ✅ 就以为白扫了。
      stopCliPoll();
      await loadCli();
      if (!CLI_DONE && box) {
        box.innerHTML = '<div class="note">这次登录流程已经结束了 —— '
            + '如果还没登录上，重新点一次「扫码登录」。</div>';
      }
      return;
    }

    if (state === "error") {
      stopCliPoll();
      CLI_QR = null;
      if (box) {
        // **先清空**：不清的话每报一次就多堆一段，最后是一串一模一样的红块
        box.innerHTML = "";
        const hint = document.createElement("div");
        hint.className = "note warn";
        hint.textContent = "查询登录结果失败：" + (res.msg || "");
        const more = document.createElement("div");
        more.className = "hint";
        more.style.marginTop = "6px";
        more.textContent = "本次扫码已经作废（重新点「扫码登录」拿张新的）。"
            + "如果反复出现同一句话，去「运行日志」页搜这句 —— 那里有完整的原始输出。";
        box.append(hint, more);
      }
      await loadCli();
      return;
    }

    // pending：只更新倒计时，**不重画二维码**（重画会闪）
    updateCliCountdown();
  } finally {
    CLI_POLLING = false;
  }
}

async function cancelCliLogin() {
  stopCliPoll();
  stopCliTick();
  CLI_QR = null;
  CLI_DONE = false;
  await postJSON("/api/cli/login", {action: "cancel"});
  const box = el("cliLoginBox");
  if (box) box.innerHTML = "";
  await loadCli();
}

/* ---------- NapCat 小号扫码登录 ---------- */
/* 交互和 CLI 扫码一样：点按钮出二维码 -> 自动轮询 -> 成功提示。
   区别在判定方式：CLI 有自己的登录状态接口，NapCat 这里轮询的是
   OneBot HTTP 的 get_login_info —— 服务起来了就是登录完成了。
   二维码来自 NapCat WebUI 的非官方接口，失败时给 WebUI 跳转退路。 */
let NAPCAT_TIMER = null;
let NAPCAT_WAITED = 0;

function stopNapcatPoll() {
  if (NAPCAT_TIMER) { clearInterval(NAPCAT_TIMER); NAPCAT_TIMER = null; }
}

async function loadNapcat() {
  const box = el("napcatStat");
  if (!box) return false;
  const res = await postJSON("/api/onebot/probe", {});
  if (res && res.ok) {
    box.textContent = res.msg;
    box.className = "status ok";
    return true;
  }
  // 失败先别急着报 errno：让后端分诊一下（进程没运行 vs 只是掉线），
  // 掉线的直接引导扫码 —— token 在设置里存过就不用再去日志翻
  const dia = await postJSON("/api/napcat/diagnose", {});
  box.textContent = (dia && dia.msg) || (res && res.msg) || "未登录";
  box.className = "status err";
  return false;
}

async function startNapcatLogin() {
  const box = el("napcatLoginBox");
  const btn = el("napcatLoginBtn");
  if (!box) return;
  btn.disabled = true;
  stopNapcatPoll();
  box.innerHTML = '<p class="hint">拿二维码中…</p>';
  const res = await postJSON("/api/napcat/qrcode", {});
  if (!(res && res.ok)) {
    box.innerHTML = '<p class="note warn">' + ((res && res.msg) || "无响应")
        + '　—— 可以直接 <a href="#" onclick="napcatWebui();return false">打开 NapCat 管理页</a> 扫码</p>';
    btn.disabled = false;
    return;
  }
  // 二维码：旧版直接给 PNG；新版只给二维码内容 URL，用 qrserver 渲染成图
  // （里面只有一次性登录链接，没有账号信息）
  const img = res.kind === "url"
      ? '<img class="qr" alt="NapCat 登录二维码" src="https://api.qrserver.com/v1/create-qr-code/?size=208x208&data='
          + encodeURIComponent(res.qrcode_url) + '">'
      : '<img class="qr" alt="NapCat 登录二维码" src="' + res.image + '">';
  let html = '<div class="toolbar">' + img
      + '<span class="hint grow">用<b>小号</b>的手机 QQ 扫这个码。登录成功会自动提示；'
      + '二维码过期就点「重新获取」。</span></div>';
  // 新版支持快速登录：之前登录过的号掉线后直接拉起，不用扫码
  if (res.quick && res.quick.length) {
    html += '<div class="toolbar"><span class="hint grow">检测到之前登录过的账号，'
        + '可直接快速登录（不用扫码）：</span></div><div class="toolbar">';
    for (const acc of res.quick) {
      html += '<button class="ghost" onclick="napcatQuickLogin('
          + JSON.stringify(acc.uin) + ')">快速登录 '
          + (acc.nick || acc.uin) + '</button>';
    }
    html += '</div>';
  }
  html += '<div class="toolbar"><button class="ghost" onclick="startNapcatLogin()">重新获取</button></div>';
  box.innerHTML = html;
  NAPCAT_WAITED = 0;
  NAPCAT_TIMER = setInterval(async function () {
    NAPCAT_WAITED += 3;
    const ok = await loadNapcat();
    if (ok) {
      stopNapcatPoll();
      box.innerHTML = '<p class="status ok">✅ 小号登录成功！到「配置中心 → QQ 号识别」'
          + '配好群映射就能拉取成员了</p>';
      btn.disabled = false;
    } else if (NAPCAT_WAITED > 150) {
      // 二维码一般两分钟过期：超时停止轮询，让用户手动要一张新的
      stopNapcatPoll();
      box.innerHTML += '<p class="hint">等了很久还没登录上 —— 二维码可能过期了，点「重新获取」</p>';
      btn.disabled = false;
    }
  }, 3000);
}

async function napcatQuickLogin(uin) {
  const out = el("napcatStat");
  if (out) { out.textContent = "快速登录中…"; out.className = "hint"; }
  const res = await postJSON("/api/napcat/quick-login", {uin: String(uin)});
  if (!(res && res.ok)) {
    if (out) {
      out.textContent = (res && res.msg) || "快速登录失败";
      out.className = "status err";
    }
    return;
  }
  // 请求被接受了：轮询确认小号真正上线（OneBot HTTP 起来 = 登录完成）
  stopNapcatPoll();
  NAPCAT_WAITED = 0;
  NAPCAT_TIMER = setInterval(async function () {
    NAPCAT_WAITED += 3;
    const ok = await loadNapcat();
    if (ok || NAPCAT_WAITED > 60) stopNapcatPoll();
  }, 3000);
}

function napcatWebui() {
  const node = el("napcat_webui_url");
  const url = String((node && node.value) || "").trim();
  if (url) window.open(url, "_blank");
  else window.alert("先在「配置中心 → QQ 号识别」里填好 NapCat WebUI 地址");
}

/* 「自动获取 token」：后端从 docker exec / 启动日志 / 挂载文件三路找
   WebUI token，验证能用后直接写进设置 —— 不用去服务器翻日志 */
async function napcatFetchToken() {
  const btn = el("napcatTokenBtn");
  if (!btn) return;
  btn.disabled = true;
  const out = el("napcatStat");
  if (out) { out.textContent = "正在从容器配置/日志里找 token…"; out.className = "hint"; }
  const res = await postJSON("/api/napcat/fetch-token", {});
  if (out) {
    out.textContent = (res && res.msg) || "无响应";
    out.className = res && res.ok ? "status ok" : "status err";
  }
  if (res && res.ok) await loadNapcat();
  btn.disabled = false;
}

/* ---------- 群映射选择器 ---------- */
/* 左右两列点选 + 添加映射，替代手抄 openid。
   映射数据的唯一真相是 onebot_group_map 设置（下面的文本框），
   选择器只是它的图形化编辑器 —— 手改和点选随时可以混用。 */
let OB_QQ = null;    // 选中的 QQ 群号
let OB_BOT = null;   // 选中的机器人群 openid
let OB_DATA = null;  // 上次拉取的完整数据（加/删映射后本地重渲染用）

function obgPick(side, value) {
  if (side === "qq") OB_QQ = value; else OB_BOT = value;
  // 重新渲染选中态
  const qqList = el("obgQQList"), botList = el("obgBotList");
  if (qqList) qqList.querySelectorAll(".obg-item").forEach(function (n) {
    n.classList.toggle("sel", n.dataset.qq === OB_QQ);
  });
  if (botList) botList.querySelectorAll(".obg-item").forEach(function (n) {
    n.classList.toggle("sel", n.dataset.openid === OB_BOT);
  });
  const pick = el("obgPickHint"), add = el("obgAddBtn");
  const ready = OB_QQ && OB_BOT;
  if (pick) pick.textContent = ready
      ? "将添加：" + OB_QQ + " ↔ " + OB_BOT.slice(0, 10) + "…"
      : "两边各点一个";
  if (add) add.disabled = !ready;
}

async function loadOnebotGroups() {
  const btn = el("obgBtn"), hint = el("obgHint");
  if (btn) btn.disabled = true;
  if (hint) hint.textContent = "拉取中…";
  const data = await getJSON("/api/onebot/groups");
  if (btn) btn.disabled = false;
  if (!data) { if (hint) hint.textContent = "读取失败"; return; }

  OB_DATA = data;
  OB_QQ = OB_BOT = null;
  if (hint) hint.textContent = data.error ? ("NapCat 拉取失败：" + data.error) : "";
  const qqList = el("obgQQList"), botList = el("obgBotList");
  if (!qqList || !botList) return;

  qqList.innerHTML = "";
  if (!(data.qq_groups || []).length) {
    qqList.innerHTML = '<div class="empty">小号还没有加入任何群</div>';
  } else {
    for (const g of data.qq_groups || []) {
      const item = document.createElement("div");
      item.className = "obg-item";
      item.dataset.qq = g.qq;
      item.textContent = g.qq + (g.name ? "　" + g.name : "");
      item.onclick = function () { obgPick("qq", g.qq); };
      qqList.appendChild(item);
    }
  }

  botList.innerHTML = "";
  const bots = data.bot_groups || [];
  if (!bots.length) {
    botList.innerHTML = '<div class="empty">机器人还没见过任何群（群里发条消息后再来）</div>';
  } else {
    for (const g of bots) {
      const item = document.createElement("div");
      item.className = "obg-item";
      item.dataset.openid = g.openid;
      const label = document.createElement("span");
      label.textContent = (g.number ? g.number + "　" : "")
          + (g.name || g.note || "（未命名）");
      item.appendChild(label);
      if (g.mapped) {
        const badge = document.createElement("span");
        badge.className = "hint";
        badge.textContent = "　已映射";
        item.appendChild(badge);
      } else if (g.suggest_qq) {
        // 备注里的群号恰好在小号的群列表里：高置信建议，自动预选
        const badge = document.createElement("span");
        badge.className = "hint";
        badge.textContent = "　建议 ↔ " + g.suggest_qq;
        item.appendChild(badge);
        if (!OB_BOT) OB_BOT = g.openid;
        item.classList.add("sel");
        const qqItem = qqList.querySelector('.obg-item[data-qq="' + g.suggest_qq + '"]');
        if (qqItem && !OB_QQ) { OB_QQ = g.suggest_qq; qqItem.classList.add("sel"); }
      }
      item.onclick = function () { obgPick("bot", g.openid); };
      botList.appendChild(item);
    }
  }
  obgPick("qq", OB_QQ);  // 刷新提示行
  renderObgMaps(data.map || [], data.map_issues || []);
}

function renderObgMaps(map, issues) {
  const box = el("obgMaps");
  if (!box) return;
  box.innerHTML = "";
  if (!map.length) {
    box.innerHTML = '<div class="empty">还没有映射 —— 用上面两列点选添加</div>';
    return;
  }
  const table = document.createElement("table");
  table.className = "list";
  const tbody = document.createElement("tbody");
  for (const pair of map) {
    const tr = document.createElement("tr");
    const left = document.createElement("td");
    left.textContent = "QQ 群 " + pair[0];
    const arrow = document.createElement("td");
    arrow.className = "hint";
    arrow.textContent = "→";
    const right = document.createElement("td");
    right.textContent = pair[1].slice(0, 10) + "…";
    right.title = pair[1];
    const ops = document.createElement("td");
    ops.appendChild(buttonOf("删除", "ghost", async function () {
      const res = await postJSON("/api/onebot/map",
          {action: "remove", qq: pair[0], openid: pair[1]});
      const msg = el("msg");
      msg.className = res && res.ok ? "status ok" : "status err";
      msg.textContent = (res && res.msg) || "操作失败";
      renderObgMaps(map.filter(function (p) { return p !== pair; }), issues);
    }));
    tr.append(left, arrow, right, ops);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  box.appendChild(table);
  if ((issues || []).length) {
    const warn = document.createElement("p");
    warn.className = "note warn";
    warn.style.whiteSpace = "pre-wrap";
    warn.textContent = issues.join("\n");
    box.appendChild(warn);
  }
}

async function addOnebotMap() {
  if (!OB_QQ || !OB_BOT) return;
  const btn = el("obgAddBtn");
  if (btn) btn.disabled = true;
  const res = await postJSON("/api/onebot/map",
      {action: "add", qq: OB_QQ, openid: OB_BOT});
  const msg = el("msg");
  msg.className = res && res.ok ? "status ok" : "status err";
  msg.textContent = (res && res.msg) || "操作失败";
  if (btn) btn.disabled = false;
  if (res && res.ok) {
    // 本地同步：映射表打标 + 选择器保持选中
    if (OB_DATA) {
      OB_DATA.map = (OB_DATA.map || []).filter(function (p) {
        return p[1] !== OB_BOT && p[0] !== OB_QQ;
      });
      OB_DATA.map.push([OB_QQ, OB_BOT]);
      for (const g of OB_DATA.bot_groups || []) {
        if (g.openid === OB_BOT) { g.mapped = true; g.number = OB_QQ; }
      }
      renderObgMaps(OB_DATA.map, OB_DATA.map_issues || []);
      obgPick("qq", OB_QQ);
      obgPick("bot", OB_BOT);
    }
    // 同步下面那个手填文本框的内容，两个入口不打架
    const ta = el("onebot_group_map");
    if (ta) {
      ta.value = (OB_DATA ? (OB_DATA.map || []) : [])
          .map(function (p) { return p[0] + "=" + p[1]; }).join("\n");
    }
  }
}

/* ---------- 共同所在 ---------- */
/* **只在这个按钮上拉**：它要真调一次命令行（一两秒），要是切页就自动拉，
   每次点进这一页都得干等。 */
async function loadPlaces() {
  const box = el("placesBox");
  if (!box) return;
  const btn = el("placesBtn");
  if (btn) { btn.disabled = true; btn.textContent = "获取中…"; }
  box.innerHTML = '<div class="empty">正在拉取…</div>';
  try {
    renderPlaces(await getJSON("/api/places"));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "获取"; }
  }
}

function placesTable(rows, cells) {
  const table = document.createElement("table");
  table.className = "list";
  const tbody = document.createElement("tbody");
  for (const row of rows) {
    const tr = document.createElement("tr");
    for (const cell of cells(row)) tr.appendChild(cell);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  return table;
}

function plainCell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

function renderPlaces(data) {
  const box = el("placesBox");
  if (!box) return;
  box.innerHTML = "";
  const hint = el("placesHint");

  if (!data || !data.ok) {
    box.innerHTML = '<div class="note warn">没拿到数据</div>';
    return;
  }
  if (data.error) {
    const warn = document.createElement("div");
    warn.className = "note warn";
    warn.textContent = "拉取失败：" + data.error;
    box.appendChild(warn);
    return;
  }
  // 返回体拿到了，但认不出结构 —— 把**实际字段名**摆出来，
  // 照着补 parse_guild_list 就行，不用再去服务器上手动跑一遍
  if (!data.user_total && (data.raw_keys || []).length) {
    const warn = document.createElement("div");
    warn.className = "note warn";
    warn.textContent = "返回体拿到了，但认不出「频道列表」在哪一层。实际字段名："
        + data.raw_keys.join("、");
    box.appendChild(warn);
    return;
  }

  if (hint) {
    hint.textContent = "这个 QQ 号在 " + data.user_total + " 个频道，机器人同步到 "
        + data.bot_total + " 个";
  }

  // 对账：命令行自己报的总数和我们解析出来的对不上。
  //   故意**不用警告色**：total_count 到底算不算"又创建又加入"的重复，
  //   文档里没写、我也验不了 —— 假警报每来一次，用户就少信页面一分。
  //   所以摆成中性提示，把判断的依据交给用户（他认识自己有哪些频道）。
  //   真漏了一类是可能的，所以这话还是要说，只是别喊。
  if (data.cli_total && data.cli_total !== data.user_total) {
    const mismatch = document.createElement("div");
    mismatch.className = "note";
    mismatch.textContent = "命令行报的总数是 " + data.cli_total
        + " 个，这里解析出 " + data.user_total + " 个。"
        + "如果列表里少了你认识的频道，把提示里的字段名发给开发者。";
    box.appendChild(mismatch);
  }

  // ---- 频道：真交集 ----
  const gHead = document.createElement("div");
  gHead.className = "hint";
  gHead.textContent = "频道　共同 " + data.guilds.length + " 个";
  box.appendChild(gHead);
  if (!data.guilds.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "没有共同所在的频道";
    box.appendChild(empty);
  } else {
    box.appendChild(placesTable(data.guilds, function (row) {
      // 身份会同时出现好几个（自己创建的频道当然也加入、也管理），
      // 都列出来 —— 出问题时这是第一眼要的信息
      return [
        plainCell(row.name || "（无名）"),
        plainCell(row.channel_number || row.guild_id, "hint"),
        plainCell((row.relations || []).join(" / "), "hint"),
      ];
    }));
  }

  // ---- QQ 群：**没有交集**，为什么写在页面上 ----
  const qHead = document.createElement("div");
  qHead.className = "hint";
  qHead.style.marginTop = "16px";
  qHead.textContent = "QQ 群　机器人在的 " + data.groups.length + " 个";
  box.appendChild(qHead);

  const why = document.createElement("div");
  why.className = "note";
  // 这里是 HTML 不是 markdown，别用 ** —— 会原样显示出来
  why.textContent = "QQ 群没有交集可用：平台没有「某个 QQ 号加入了哪些群」这种接口，"
      + "机器人只能知道自己被拉进了哪些群。所以这里列的是机器人在的群，"
      + "不是两边共同的。";
  box.appendChild(why);

  if (!data.groups.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "还没有记录到任何群";
    box.appendChild(empty);
  } else {
    box.appendChild(placesTable(data.groups, function (row) {
      return [
        plainCell(row.number || row.label || "（没填备注）"),
        plainCell(row.name || "", "hint"),
      ];
    }));
  }
}

/* ---------- 初始化 ---------- */
(function init() {
  el("menu").addEventListener("click", function (event) {
    const item = event.target.closest(".menu-item");
    if (item) show(item.dataset.key);
  });
  document.addEventListener("input", renderPreview);
  document.addEventListener("change", renderPreview);
  // 转发格式要边打字边看效果 —— 看的是**还没保存**的值，所以只能在前端算
  document.addEventListener("input", renderRelayPreview);
  document.addEventListener("change", renderRelayPreview);

  let last = "";
  try { last = localStorage.getItem("forum_bot_section") || ""; } catch (e) { /* 忽略 */ }
  if (!last || !show(last, false)) show("overview", false);

  // 恢复上次选的「生效范围」
  try { SCOPE = localStorage.getItem("forum_bot_scope") || ""; } catch (e) { SCOPE = ""; }

  load();
})();
</script>
</body>
</html>
"""


# 这几页的内容由 JS 动态渲染，不走 FIELD_META 的字段网格
_DYNAMIC_PANELS = {
    "overview": """
      <div class="bot-card" id="overviewBot"></div>
      <div class="stats" id="overviewStats"></div>
      <div class="card">
        <h2>功能模块</h2>
        <p class="lead">点任意一项可直接跳到对应设置</p>
        <div class="chips" id="overviewModules"></div>
      </div>""",
    "groups": """
      <div id="groupListView">
        <div class="card">
          <h2>群聊</h2>
          <p class="lead" id="groupsNote"></p>
          <div class="toolbar">
            <span class="hint grow" id="groupsSummary"></span>
            <button class="ghost" onclick="syncAllMembers()">同步全部成员</button>
            <button class="ghost" onclick="loadGroups()">刷新</button>
          </div>
          <div id="groupsApiHint"></div>
          <table class="list gtable" id="groupTable">
            <thead><tr>
              <th style="width:32%">群</th>
              <th>机器人身份</th>
              <th>成员同步时间</th>
              <th>最近发现</th>
              <th class="r">操作</th>
            </tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>
      <div id="groupDetailView" style="display:none"></div>""",
    "guilds": """
      <div class="card">
        <h2>频道与板块</h2>
        <p class="lead">频道和板块是机器人启动时向平台真查的；点「同步频道」会让机器人立刻重拉一次。</p>
        <div class="toolbar">
          <span class="hint grow" id="guildsSync"></span>
          <button id="syncBtn" onclick="syncGuilds()">同步频道</button>
        </div>
        <div id="guildList"></div>
      </div>""",
    "preview": """
      <div class="card">
        <h2>效果预览</h2>
        <p class="lead">下面是用当前设置渲染出来的样子（示例数据），发到群里就是这个效果。</p>
        <pre id="preview"></pre>
      </div>""",
    "logs": """
      <div class="card">
        <h2>运行日志</h2>
        <p class="lead">机器人在后台的输出，和 <code>journalctl -u forum-bot</code> 看到的是同一份。
          只保留最近若干条，页面每 2 秒自动拉一次新增内容。
          <b>页面上一闪而过的报错，这里能翻回去看。</b></p>
        <div class="toolbar">
          <label class="scope">级别
            <select id="logLevel" onchange="resetLogs()">
              <option value="DEBUG">DEBUG 及以上</option>
              <option value="INFO" selected>INFO 及以上</option>
              <option value="WARNING">WARNING 及以上</option>
              <option value="ERROR">ERROR 及以上</option>
            </select>
          </label>
          <label class="scope"><input type="checkbox" id="logFollow" checked> 自动跟随最新</label>
          <span class="hint grow" id="logStats"></span>
          <button class="ghost" onclick="clearLogs()">清空缓冲</button>
          <button class="ghost" onclick="resetLogs()">重新加载</button>
        </div>
        <div class="logbox" id="logBox"><div class="empty">读取中…</div></div>
      </div>""",
    "cli": """
      <div class="card">
        <div class="toolbar" style="margin:0 0 4px">
          <h2 style="margin:0">频道凭证（官方 CLI）</h2>
        </div>
        <p class="lead">个人号在<b>频道侧</b>的登录凭证：帖子短链（pd.qq.com/s）、
        共同所在的频道都靠它。凭证存在 CLI 自己的目录，和下面的 NapCat 小号是两回事。</p>
        <div class="clistat" id="cliStat"><span class="hint">读取中…</span></div>
        <div class="toolbar" style="margin-top:12px">
          <button class="ghost" onclick="loadCli(true)">检测</button>
          <button id="cliLoginBtn" onclick="startCliLogin(false)">扫码登录</button>
          <button class="ghost" onclick="startCliLogin(true)">强制重新登录</button>
          <button class="ghost" onclick="cancelCliLogin()">取消本次</button>
        </div>
        <div id="cliLoginBox" style="margin-top:14px"></div>
      </div>

      <div class="card" style="margin-top:14px">
        <div class="toolbar" style="margin:0 0 4px">
          <h2 style="margin:0">群成员识别（NapCat 小号）</h2>
        </div>
        <p class="lead"><b>非官方协议端</b>，登录小号后才能在「配置中心 → QQ 号识别」里
        自动拉群成员。⚠ <b>务必用小号</b> —— 非官方框架有封号风险，别把主功能凭证搭进去。
        小号掉线不用进 NapCat 管理页：WebUI 地址和 token 存在
        「配置中心 → QQ 号识别」里，点下面「扫码登录」会自动取二维码。</p>
        <div class="toolbar">
          <span class="hint grow" id="napcatStat">读取中…</span>
          <button class="ghost" onclick="loadNapcat()">检测</button>
          <button class="ghost" id="napcatTokenBtn" onclick="napcatFetchToken()">自动获取 token</button>
          <button id="napcatLoginBtn" onclick="startNapcatLogin()">扫码登录</button>
          <button class="ghost" onclick="napcatWebui()">打开 NapCat 管理页</button>
        </div>
        <div id="napcatLoginBox" style="margin-top:14px"></div>
      </div>

      <div class="card" style="margin-top:14px">
        <div class="toolbar">
          <h2 style="margin:0">共同所在</h2>
          <span class="sp" style="flex:1"></span>
          <span class="hint" id="placesHint"></span>
          <button class="ghost" id="placesBtn" onclick="loadPlaces(true)">获取</button>
        </div>
        <div id="placesBox">
          <div class="empty">点「获取」拉一次（会真调命令行，一两秒）</div>
        </div>
      </div>""",
}

# 「配置中心」页里 Collapse 的分组顺序（key 对应 FIELD_META 的分组名）
SETTINGS_SECTIONS = (
    "strategy",
    "content",
    "card",
    "recall",
    "block",
    "ai",
    "relay",
    "relay_filter",
    "relay_bind",
    "onebot",
    "admin",
    "access",
)

# 折叠面板头部的箭头（展开时由 CSS 旋转 180°）
_ICON_ARROW = (
    '<svg class="carrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M6 9l6 6 6-6"/></svg>'
)


def _section_note(key):
    """每个配置分组在折叠面板里的说明/附属表格（返回 HTML，可空）。

    之前这段写死在面板生成循环里；配置中心把所有分组收进一个面板后，
    它成了唯一的"每组额外内容"出口，单独成函数才好维护。
    """
    if key == "recall":
        return (
            '<p class="note">官方硬性限制：只能撤回发送不超过 <b>2 分钟</b> 的消息；'
            "撤回普通群成员的消息要求机器人是<b>群管理员</b>；"
            "想对群里任意一条消息生效，需要在开放平台开启「接收所有消息」（群消息全量模式）。</p>"
        )
    if key == "block":
        return (
            '<p class="note">群内用法：引用某条消息后发「拉黑」即可加进上面这份名单；'
            "之后任何一条群消息只要包含其中的内容就会被直接撤回。"
            "也可发「拉黑 &lt;内容&gt;」、「取消拉黑」、「拉黑列表」。<br>"
            "名单会保存到本地（forum_forward_settings.json），下面是每一条的来源记录。</p>"
            + _BLOCK_TABLE
            + _AI_SUGGEST_CARD
            + '<div class="card" style="margin-top:16px">'
            "<h2>成员黑名单</h2>"
            '<p class="lead">上面那份是按<b>内容</b>封；这份是按<b>人</b>封 —— '
            "在名单里的人，发消息就会被撤回，不看内容。"
            "上面的「引用拉黑时连发送者也一起拉黑」打开时，"
            "引用某人的消息发「拉黑」会同时把他加进来。</p>"
            '<p class="note warn">这不是踢人。平台的 QQ群接口'
            "<b>没有移除成员的能力</b>（唯一那个 "
            "<code>DELETE /guilds/{id}/members/{uid}</code> 是 QQ<b>频道</b>的），"
            "所以他还在群里，只是说不了话。<br>"
            "而且撤不撤得掉，取决于机器人在<b>那个群</b>是不是群管理员 —— "
            "不是管理员的群会暂停尝试 30 分钟（设为管理员后自动恢复），"
            "日志里会点名是哪个群。</p>"
            '<div id="memberBanFoot"></div>'
            + _MEMBER_BAN_TABLE
            + "</div>"
        )
    if key == "ai":
        return (
            '<p class="note">选一个厂商（接口都是 OpenAI 兼容的，换厂商只改这个下拉框），'
            "填好 API Key 并<b>保存</b>，然后点下面的按钮真调一次验证。"
            "模型名 / 接口地址<b>留空 = 用该厂商默认</b>，换型号或自建服务才需要改。<br>"
            "<b>这里不决定任何功能开不开 AI</b> —— 要不要用 AI 在各功能模块单独开，"
            "比如「拉黑名单」组的「拉黑消息时用 AI 拆分候选关键词」。</p>"
            '<p style="margin:8px 0">'
            '<button type="button" class="ghost" id="aiTestBtn" onclick="testAI()">测试 AI 连接</button>'
            '<span id="aiTestMsg" class="hint" style="margin-left:8px"></span></p>'
        )
    if key == "onebot":
        return (
            '<p class="note">原理：用一个登录了 <b>NapCat</b>（非官方框架）的小号拉进群，'
            "拉取全量群成员的真实 QQ 号，和机器人这边的成员名册按<b>昵称</b>匹配后自动绑定。"
            "需要先把小号拉进群、并用下面的<b>群映射选择器</b>把两边对上。<br>"
            "<b>⚠ 非官方框架违反腾讯用户协议，有封号风险 —— 务必用小号</b>；"
            "自动匹配已刻意放低到 6 小时一次。部署步骤见 README「QQ 号自动识别」一节。</p>"
            '<p style="margin:8px 0">'
            '<button type="button" class="ghost" id="onebotProbeBtn" onclick="onebotProbe()">测试 OneBot 连接</button>'
            '<button type="button" class="ghost" id="onebotSyncBtn" onclick="onebotSync()" style="margin-left:8px">拉取并绑定</button>'
            '<span id="onebotMsg" class="hint" style="margin-left:8px"></span></p>'
            '<div id="onebotIssues" class="note" style="display:none;white-space:pre-wrap"></div>'
            '<div class="card" style="margin-top:16px">'
            "<h2>群映射选择器</h2>"
            '<p class="lead">不用手抄 openid：左边拉取<b>小号所在的 QQ 群</b>，'
            "右边列出<b>机器人在的群</b>（备注里填过群号的会自动标出建议配对）。"
            "两边各点一个 → 「添加映射」。映射结果写在下面的文本框里，手改也认。</p>"
            '<div class="toolbar">'
            '<button type="button" class="ghost" id="obgBtn" onclick="loadOnebotGroups()">拉取 QQ 群列表</button>'
            '<span class="hint grow" id="obgHint"></span>'
            "</div>"
            '<div class="obg-cols">'
            '<div><p class="hint">QQ 群（小号所在）</p><div class="obg-list" id="obgQQList"><div class="empty">点「拉取 QQ 群列表」</div></div></div>'
            '<div><p class="hint">机器人的群</p><div class="obg-list" id="obgBotList"><div class="empty">读取中…</div></div></div>'
            "</div>"
            '<div class="toolbar">'
            '<button type="button" id="obgAddBtn" onclick="addOnebotMap()" disabled>添加映射</button>'
            '<span class="hint" id="obgPickHint"></span>'
            "</div>"
            '<div id="obgMaps"><div class="empty">读取中…</div></div>'
            "</div>"
        )
    if key == "relay":
        return (
            '<div class="card" style="margin-top:16px">'
            "<h2>选群添加规则（不用抄 openid）</h2>"
            '<p class="lead">选好「来源群」和「目标群」，点「添加」即可 —— '
            "规则会写进上面的输入框并<b>立即保存</b>，不用再点底部保存。"
            "要双向互转就点「添加双向」，等于一次写两条规则。</p>"
            '<p class="note warn" id="relayPickerWarn" style="display:none"></p>'
            '<div class="toolbar">'
            '<select id="relaySource" style="flex:1;min-width:200px;width:auto"></select>'
            '<span class="hint">→</span>'
            '<select id="relayTarget" style="flex:1;min-width:200px;width:auto"></select>'
            '<button onclick="addRelayRule(false)">添加</button>'
            '<button class="ghost" onclick="addRelayRule(true)">添加双向</button>'
            "</div>"
            '<p class="hint" id="relayPickerHint">读取中…</p>'
            "</div>"
            '<div class="card" style="margin-top:16px">'
            "<h2>规则解析结果</h2>"
            '<p class="lead">下面显示的是<b>已保存</b>的规则解析成了什么，'
            "每条右边的「删除」会精确删掉对应的那一行。"
            "规则里可以直接写群号（靠「群聊」页的群备注翻译成 openid），"
            "用上面那个选群按钮添加时写的是群 openid —— 那种写法"
            "<b>不依赖群备注</b>，备注没填也能用。写错的规则会在这里被点出来，"
            "<b>不会静默失效</b>。</p>"
            '<div id="relayRules"><div class="empty">读取中…</div></div>'
            "</div>"
            '<div class="card" style="margin-top:16px">'
            "<h2>转发格式预览</h2>"
            '<p class="lead">用上面「转发格式」输入框里的内容<b>实时</b>渲染'
            "（示例数据），不用保存就能看效果。</p>"
            '<div class="toolbar">'
            '<span class="hint grow">点一下插到格式末尾：</span>'
            '<button class="ghost" onclick="insertFormat(\'{group}\')">{group} 群号</button>'
            '<button class="ghost" onclick="insertFormat(\'{nick}\')">{nick} 昵称</button>'
            '<button class="ghost" onclick="insertFormat(\'{uid}\')">{uid} 标识</button>'
            '<button class="ghost" onclick="insertFormat(\'{qq}\')">{qq} QQ号</button>'
            '<button class="ghost" onclick="insertFormat(\'{content}\')">{content} 正文</button>'
            "</div>"
            '<pre id="relayPreview"></pre>'
            '<p class="hint" id="relayPreviewHint"></p>'
            "</div>"
        )
    if key == "relay_filter":
        return (
            '<p class="note">这里的每一项都只决定<b>要不要转过去</b>，'
            "不影响原群里的消息 —— 消息该在还在。<br>"
            "想让它从原群里也消失（撤回），那是另一回事，用「拉黑名单」组。</p>"
        )
    if key == "relay_bind":
        return (
            '<div class="card" style="margin-top:16px">'
            "<h2>已识别到 QQ 号的成员</h2>"
            '<p class="lead">平台<b>不提供 QQ 号</b>（官方原文：「与真实的 ID 无关」），'
            "号是「<b>QQ 号识别</b>」组的 OneBot 旁路自动匹配的（也可以在上面"
            "「成员标识映射」里手工指定）。"
            "识别到之后，转发里括号中显示的就是这个号；"
            "没识别到的显示 openid 前 8 位。上面那个"
            "「<b>只转发已识别到 QQ 号的成员</b>」开关打开时，"
            "<b>这张表里没有的人发的消息一条都不会被转发</b>。</p>"
            '<p class="note warn">号码只是<b>显示用的标签</b>：判权限请继续用'
            "「权限管理」组的管理员名单（按 openid），别按 QQ 号。</p>"
            '<div id="relayBindings"><div class="empty">读取中…</div></div>'
            "</div>"
        )
    if key == "admin":
        return (
            '<p class="note">只有管理员能在群里使用「订阅 / 取消订阅 / 立即推送 / 刷新设置 / '
            "撤回 / 拉黑」这些指令；「状态」「模式」任何人都能用。<br>"
            "QQ 群消息不携带 QQ 号，只能按 <code>member_openid</code> 授权 —— "
            "让要当管理员的人在群里发一条消息，他就会出现在下面的列表里。<br>"
            "「频道管理员」是在<b>频道内</b>执行管理指令用的，"
            "在频道里 @机器人 发「我的ID」可以查到自己的 user_id。</p>"
            + _MEMBER_TABLE
        )
    if key == "access":
        return (
            '<p class="note">流程：用户 <b>@机器人（或私信机器人）发「领资料」</b> 拿验证码'
            "（6 位数字）→ "
            "加入指定 QQ 群 → 在群里 @机器人 发送验证码 → 机器人自动给他开该板块的"
            "「可查看」权限。<br>"
            "验证码在<b>频道侧</b>生成并绑定发起人的频道账号，所以就算被人看到也用不了。<br>"
            "<b>入口限制</b>：只有<b>文字子频道</b>里 @机器人 才会收到事件；"
            "论坛子频道里发帖 / 评论不产生频道消息事件，这种频道请让用户走<b>私信</b>。<br>"
            "三个前提：① 该子频道要设为「指定成员可见」（现在是公开的话授权没有意义）；"
            "② 机器人在该子频道要有「管理子频道」权限；"
            "③ 开放平台要申请 <code>GROUP_MEMBER_EVENT</code>（群成员加入/退出）"
            "与私信权限，这样有人退群时才能自动收回权限。<br>"
            "<b>多个频道各配一套</b>：资料板块 ID、解锁群这些都是按频道独立的 —— "
            "用右上角「生效范围」切到某个频道（或在「频道」页点「编辑本频道设置」）"
            "再改，改的只覆盖那个频道，其余字段继续跟随全局；"
            "在全局范围改的是所有频道的默认值。</p>"
            + _ACCESS_TABLE
            + """
        <div class="card" style="margin-top:14px" id="accessGroupPickerCard">
          <h2>兑换群选择器</h2>
          <p class="lead">勾选允许兑换验证码的群（<b>可多选</b>，任选一个群都能兑换），
          点「保存兑换群」写入配置 —— 不用手填 openid。写到哪个频道看上面的「生效范围」，
          当前写入目标：<b id="accessPickerScope">全局</b>。</p>
          <div id="accessGroupPicker" class="hint">读取中…</div>
          <div class="toolbar" style="margin-top:8px">
            <button class="ghost" id="accessGroupSaveBtn" onclick="saveAccessGroups()">保存兑换群</button>
            <button class="ghost" onclick="loadAccessGroupPicker()">重新读取</button>
          </div>
        </div>"""
        )
    return ""


def build_page():
    """把菜单、面板和设置字段渲染进页面模板。"""
    grouped = {}
    for key, label, hint, kind, options, section in FIELD_META:
        grouped.setdefault(section, []).append(
            _render_field(key, label, hint, kind, options, DEFAULT_SETTINGS[key])
        )

    # 自检：SECTIONS 控制「面板」，MENU_GROUPS 控制「侧栏菜单」—— 两处都要登记。
    # 漏一个的后果很隐蔽：面板明明渲染出来了，但侧栏没有入口，页面上**点不到**。
    # （加「群消息互转」时就只改了一处，查了半天。）留一条日志免得再踩。
    menu_keys = {key for _, keys in MENU_GROUPS for key in keys}
    # 只有真正生成面板的 key 才需要挂到菜单（设置分组的设置项都收进了「配置中心」）
    panel_keys = {key for key, _, _ in SECTIONS
                  if key in _DYNAMIC_PANELS or key == "config"}
    unreachable = sorted(panel_keys - menu_keys)
    if unreachable:
        _log.warning(
            f"[botpy] 配置页这些分组没挂到侧栏菜单，页面上点不到："
            f"{'、'.join(unreachable)}"
        )
    section_keys = {key for key, _, _ in SECTIONS}
    orphan = sorted({section for *_, section in FIELD_META if section not in section_keys})
    if orphan:
        _log.warning(f"[botpy] 这些设置分组没有对应的页面：{'、'.join(orphan)}")

    menu_parts = []
    for group_label, keys in MENU_GROUPS:
        items = []
        for key in keys:
            label = next((item[1] for item in SECTIONS if item[0] == key), key)
            items.append(
                '<div class="menu-item" data-key="%s">%s<span>%s</span></div>'
                % (key, _icon(key), label)
            )
        menu_parts.append(
            '      <div class="menu-group">%s</div>\n%s'
            % (group_label, "\n".join("      " + line for line in items))
        )

    panels = []
    # ── 配置中心：所有设置分组收进一个面板，用 Collapse 折叠，**默认全部收起** ──
    # 好处：不用在十几个侧栏页之间跳；每组折叠互不影响；改完统一在底部保存。
    collapse_items = []
    for key in SETTINGS_SECTIONS:
        label, desc = next(
            ((l, d) for k, l, d in SECTIONS if k == key), (key, "")
        )
        body = '<div class="grid">\n          %s\n        </div>' % "\n          ".join(
            grouped.get(key, [])
        )
        note = _section_note(key)
        inner = body + (("\n      " + note + "\n") if note else "")
        collapse_items.append(
            '<div class="citem" data-group="%s">\n'
            '      <button type="button" class="chead" onclick="toggleGroup(this)">\n'
            '        <span class="ct">%s</span>\n'
            '        <span class="cd">%s</span>\n'
            "        %s\n"
            "      </button>\n"
            '      <div class="cbody"><div class="cinner">\n%s\n      </div></div>\n'
            "    </div>"
            % (
                key,
                _escape(label),
                _escape(desc),
                _ICON_ARROW,
                inner,
            )
        )
    panels.append(
        '<section class="panel" data-key="config">\n'
        '      <div class="card cfg-head">\n'
        "        <h2>配置中心</h2>\n"
        '        <p class="lead">配置按业务模块折叠分组 —— 点开需要调整的模块，'
        "改完点页面底部「<b>保存配置</b>」。带 <span class=\"tip\">?</span> "
        "的标签悬停可看详细说明。</p>\n"
        "      </div>\n"
        '      <div class="collapse">\n        '
        + "\n        ".join(collapse_items)
        + "\n      </div>\n"
        '      <div class="cfg-footer">\n'
        '        <span class="hint grow" id="footNote">设置保存在 forum_forward_settings.json，重启不丢</span>\n'
        '        <button type="button" class="ghost" onclick="load()">重置</button>\n'
        '        <button type="button" id="cfgSaveBtn" onclick="save()">保存配置</button>\n'
        "      </div>\n"
        "    </section>"
    )
    for key, label, desc in SECTIONS:
        if key not in _DYNAMIC_PANELS:
            continue
        # 动态面板整块替换了默认的字段网格，所以**字段要自己补回去** ——
        # 不补的话，挂在这个分组下的设置项在页面上根本不存在：
        # 读表单时会 `console.warn("找不到控件")`，保存时也带不上它，
        # **那个开关会静默消失**（改完看着好好的，实际点了没反应）。
        extra = ""
        if grouped.get(key):
            extra = (
                '\n      <div class="card" style="margin-top:14px">\n'
                "        <h2>设置</h2>\n"
                '        <div class="grid">\n          %s\n        </div>\n'
                "      </div>"
                % "\n          ".join(grouped[key])
            )
        panels.append(
            '<section class="panel" data-key="%s">\n%s%s\n    </section>'
            % (key, _DYNAMIC_PANELS[key], extra)
        )

    fields_js = json.dumps(
        [
            {
                "key": key,
                "kind": kind,
                "label": label,
                # 仅全局有效的字段：频道范围下要禁用，避免用户填了被静默丢弃
                "global_only": key in _GLOBAL_ONLY_KEYS,
            }
            for key, label, _, kind, _, _ in FIELD_META
        ],
        ensure_ascii=False,
    )
    sections_js = json.dumps(
        [{"key": key, "label": label, "desc": desc} for key, label, desc in SECTIONS],
        ensure_ascii=False,
    )
    appid = config.get_appid()
    return (
        _PAGE
        .replace("__MENU__", "\n".join(menu_parts))
        .replace("__PANELS__", "\n\n    ".join(panels))
        .replace("__SECTIONS__", sections_js)
        .replace("__FIELDS__", fields_js)
        .replace("__APPID_LABEL__", "appid %s" % _escape(appid or "未配置"))
        .replace("__FOOT_NOTE__", "设置保存在 forum_forward_settings.json")
    )


def start_settings_web(
    settings, host="127.0.0.1", port=8765,
    members=None, inventory=None, dashboard=None, group_info=None,
    groups=None, channel_cli=None,
    username="", password="", cert="", key="",
):
    """在后台线程启动配置页面，返回 server 对象（可 .shutdown()）。

    可选参数（都是只读用，例外是 group_info 的「同步成员」请求标志
    和 channel_cli 的扫码登录）：
      members      SeenMembers，页面「权限管理」据此列出最近见过的群成员
      inventory    Inventory，「频道」页读它的缓存、「同步频道」按钮向它发请求
      dashboard    Dashboard，「概况 / 群聊 / 频道」页的数据聚合
      group_info   GroupDirectory，「群聊」页的「同步成员」按钮向它发请求
      groups       KnownGroups，已知群列表（「QQ 号识别」的群映射选择器右列用）
      channel_cli  ChannelCli，「CLI 登录」页在这里**同步**调它扫码登录。
                   它能被网页线程直接用，是因为 ChannelCli 的内核是同步的
                   （见 channel_cli._execute 的说明）—— 这也是这个页面能做出来的前提
      username/password  访问口令（HTTP Basic）
      cert/key           证书路径，配了就走 HTTPS

    安全（两条硬要求，不是建议）
    --------------------------
    这个面板能改设置、能改管理员名单、能收回别人的板块权限 —— 等于机器人的总控台，
    而 do_GET / do_POST 里原本**一行身份校验都没有**。所以：

      1. 绑非本机地址（0.0.0.0 / 公网 IP）时**必须**设 password，否则直接拒绝启动。
         宁可起不来，也不能裸奔在网上 —— 对外公开意味着几小时内就会被扫到。
      2. 公网访问**必须**同时给 cert + key 走 HTTPS。
         Basic Auth 的密码只是 base64 编码，不是加密；不走 TLS 等于明文过网。

    默认只绑 127.0.0.1 时不强制口令：本机访问本来就有 SSH 隧道这层壳。
    """
    username = str(username or "").strip() or "admin"
    password = str(password or "")
    cert = str(cert or "").strip()
    key = str(key or "").strip()
    use_tls = bool(cert and key)
    # 口令是 Basic Auth 的开关：没设就代表"只给本机用"
    require_auth = bool(password)

    if host not in ("127.0.0.1", "localhost", "::1"):
        if not require_auth:
            raise RuntimeError(
                "拒绝启动配置页：host=%s 是对外地址，但没有设置访问口令。"
                "这个面板能改设置和管理员名单，没有口令等于把机器人控制权公开。"
                "请在 config.yaml 的 web 段补上 password（公网访问还要补 cert/key 走 HTTPS）。"
                % host
            )
        if not use_tls:
            _log.warning(
                "[botpy] 配置页对外监听但没配 HTTPS（web.cert / web.key）："
                "Basic Auth 的密码是明文传输的，强烈建议配上证书"
            )

    page = build_page().encode("utf-8")

    # 暴力破解减速：同一 IP 连续猜错若干次后，每次多等一会儿。
    # 不做完美防护（那要靠 fail2ban），但让在线猜口令变得不划算。
    failures = {}
    failures_lock = threading.Lock()
    FAIL_FREE = 5            # 前 5 次失败正常响应，不惩罚（手滑输错很正常）
    FAIL_DELAY_STEP = 0.5    # 之后每多失败一次多等 0.5 秒
    FAIL_DELAY_MAX = 5.0     # 最多等 5 秒，别把自己拖死

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # ---------------- 身份校验 ----------------
        def _authorized(self):
            """HTTP Basic 校验。没设口令时一律放行（那种情况只会绑在本机）。"""
            if not require_auth:
                return True
            header = self.headers.get("Authorization") or ""
            if not header.startswith("Basic "):
                return False
            try:
                raw = base64.b64decode(header[6:].strip()).decode("utf-8")
            except Exception:
                return False
            user, _, given = raw.partition(":")
            # compare_digest 是定时安全比较，避免按字符提前返回泄漏口令前缀
            if hmac.compare_digest(user, username) and hmac.compare_digest(given, password):
                with failures_lock:
                    failures.pop(self.client_address[0], None)
                return True
            self._penalize()
            return False

        def _penalize(self):
            """记一次失败；超过免罚次数就拖慢这个 IP 的后续请求。"""
            with failures_lock:
                count = failures.get(self.client_address[0], 0) + 1
                failures[self.client_address[0]] = count
            if count > FAIL_FREE:
                time.sleep(min((count - FAIL_FREE) * FAIL_DELAY_STEP, FAIL_DELAY_MAX))

        def _challenge(self):
            """401 + WWW-Authenticate —— 浏览器会弹出原生登录框。"""
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="forum-bot"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send(self, body: bytes, content_type: str):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, payload, status=200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            """读 JSON body。**一个请求只读一次**，重复调用返回同一份。

            必须保证**每个 POST 都把 body 读掉**。HTTP/1.1 是长连接，没读干净的
            字节会留在这个连接上，被当成**下一个**请求的请求行 ->
            `Bad request syntax`（400，而且返回的是 HTML 不是 JSON，
            前端 `res.json()` 直接抛）。

            这个坑很难查，因为它炸的不是当前请求、而是同一连接上的下一个：
            表现出来是"点了某个按钮之后，页面后面所有请求都失败"。
            线上就是这么炸的 —— `/api/cli/poll` 每 3 秒发一次、而且带了 body，
            几下就能把连接搞坏。
            """
            if self._body is _UNREAD:
                length = int(self.headers.get("Content-Length") or 0)
                raw = b""
                if length > 0:
                    try:
                        raw = self.rfile.read(length)
                    except Exception:
                        raw = b""
                try:
                    data = json.loads(raw or b"{}")
                except Exception:
                    data = {}
                self._body = data if isinstance(data, dict) else {}
            return self._body

        def do_GET(self):
            # 每个请求都要过这一关。页面能改设置，前端做的登录挡不住直接打接口。
            if not self._authorized():
                self._challenge()
                return
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path.startswith("/api/settings"):
                self._send_json(_settings_view(settings, (query.get("guild_id") or [""])[0]))
            # 「运行日志」页：增量拉取。since 是上次拿到的游标，
            # 页面只取新增的部分，不用每次传整份缓冲。
            elif path.startswith("/api/logs"):
                # install() 幂等：万一 handlers 被第三方库重置过，顺手补挂一次就自愈
                buf = logbuf.install()
                try:
                    since = int((query.get("since") or ["0"])[0] or 0)
                except (TypeError, ValueError):
                    since = 0
                level = (query.get("level") or [""])[0]
                entries, cursor, kept = buf.since(since, level)
                stats = buf.stats()
                stats["kept"] = kept   # 按级别过滤后，以实际保留条数为准
                self._send_json({
                    "entries": entries,
                    "cursor": cursor,
                    "stats": stats,
                })
            elif path.startswith("/api/relay/rules"):
                self._send_json(_relay_rules_view(settings))
            elif path.startswith("/api/relay/bindings"):
                self._send_json(_relay_bindings_view(settings))
            elif path.startswith("/api/member-ban"):
                self._send_json(_member_ban_view())
            # 「QQ 登录」页的「共同所在」。**会真调 CLI**（一两秒），
            # 所以只在用户点「获取」时才走 —— 切页不会自动触发
            elif path.startswith("/api/places"):
                self._send_json(_common_places_view(settings, dashboard, channel_cli))
            # 「CLI 登录」页的状态。**只读本地状态，不调 CLI** ——
            # 页面每 3 秒轮一次，不能每次都去跑一个子进程（要真跑就用 /api/cli/probe）
            elif path.startswith("/api/cli/status"):
                self._send_json({
                    "ok": True,
                    "status": (
                        channel_cli.status() if channel_cli is not None
                        else {
                            "command": "",
                            "installed": False,
                            "logged_in": False,
                            "available": False,
                            "cached_links": 0,
                            "login_pending": False,
                            "login_expires_in": 0,
                            "note": "这个进程没挂上 CLI 桥（配置页可能是单独启动的）",
                        }
                    ),
                })
            elif path.startswith("/api/members"):
                self._send_json(members.all() if members is not None else [])
            elif path.startswith("/api/blacklist"):
                # 每次现读，手工改了 json 也能立刻反映到页面上
                self._send_json(Blacklist(settings).entries())
            elif path.startswith("/api/ai-suggest"):
                # AI 拆分的候选关键词。enabled/key_set 一起带回去，
                # 页面据此提示"为什么列表一直是空的"（没开 / 没填 key）
                data = settings.snapshot()
                self._send_json(
                    {
                        "enabled": ai_split.ai_enabled(data),
                        "key_set": bool(str(data.get("ai_api_key") or "").strip()),
                        "items": ai_split.SuggestStore().entries(),
                    }
                )
            elif path.startswith("/api/onebot/groups"):
                # 群映射选择器的数据：左列 = 小号所在的 QQ 群（NapCat 拉的），
                # 右列 = 机器人已知的群（KnownGroups + 备注推出过的 + 已映射的）。
                # 备注里填过群号、且那个号在小号的群里 -> 标成"建议配对"
                data = settings.snapshot()
                qq_groups, error = onebot.fetch_group_list(
                    str(data.get("onebot_http_url") or ""),
                    str(data.get("onebot_token") or ""),
                )
                qq_ids = {item["qq"] for item in qq_groups}
                number_map = group_number_map(data)
                current, map_issues = onebot.parse_group_map(
                    data.get("onebot_group_map")
                )
                # 已映射的对子也是群号来源之一：右列要能显示"已映射"
                for qq_num, oid in current:
                    number_map.setdefault(qq_num, oid)
                notes = read_notes()

                openids, seen = [], set()
                if groups is not None:
                    for item in groups.all():
                        oid = str(item.get("group_openid") or "")
                        if oid and oid not in seen:
                            seen.add(oid)
                            openids.append(oid)
                for oid in number_map.values():
                    oid = str(oid)
                    if oid and oid not in seen:
                        seen.add(oid)
                        openids.append(oid)

                mapped_pairs = {(oid, qq_num) for qq_num, oid in current}
                bot_groups = []
                for oid in openids:
                    number = next(
                        (num for num, mapped in number_map.items() if str(mapped) == oid),
                        "",
                    )
                    bot_groups.append(
                        {
                            "openid": oid,
                            "name": group_info.name_of(oid) if group_info else "",
                            "note": notes.get(oid, ""),
                            "number": number,
                            # 备注/映射里写的群号恰好在小号的群列表里 -> 高置信建议
                            "suggest_qq": number if number in qq_ids else "",
                            "mapped": (oid, number) in mapped_pairs if number else False,
                        }
                    )
                self._send_json(
                    {
                        "ok": not error,
                        "error": error,
                        "qq_groups": qq_groups,
                        "bot_groups": bot_groups,
                        "map": current,
                        "map_issues": map_issues,
                    }
                )
            elif path.startswith("/api/access"):
                state = read_state()
                # 绑定表按频道拆开显示：给每个频道一个能认出来的名字
                # （频道独立配置的 label -> 频道名 -> id 前缀）
                name_by_id = {}
                if inventory:
                    for guild in (inventory.snapshot().get("guilds") or []):
                        name_by_id[str(guild.get("id") or "")] = str(guild.get("name") or "")
                labels = {}
                for binding in state.get("bindings") or []:
                    gid = str(binding.get("guild_id") or "")
                    if not gid or gid in labels:
                        continue
                    labels[gid] = (
                        str(settings.profile(gid).get("label") or "")
                        or name_by_id.get(gid, "")
                        or ("频道 " + gid[:10] + "…")
                    )
                state["guild_labels"] = labels
                self._send_json(state)
            elif path.startswith("/api/overview"):
                self._send_json(dashboard.overview() if dashboard else {})
            # 注意顺序：/api/groups/detail 必须先于 /api/groups 判断
            elif path.startswith("/api/groups/detail"):
                group_openid = (query.get("group_openid") or [""])[0]
                self._send_json(
                    dashboard.group_detail(group_openid)
                    if dashboard
                    else {"found": False, "members": []}
                )
            elif path.startswith("/api/groups"):
                self._send_json(dashboard.groups() if dashboard else {"rows": []})
            elif path.startswith("/api/guilds"):
                if dashboard:
                    self._send_json(dashboard.guilds())
                else:
                    self._send_json(
                        inventory.snapshot() if inventory else {"guilds": [], "has_data": False}
                    )
            elif self.path in ("/", "/index.html"):
                self._send(page, "text/html; charset=utf-8")
            else:
                self._send(b"not found", "text/plain; charset=utf-8")

        def do_POST(self):
            # POST 全是写操作（改设置、加管理员、收回权限），更不能漏
            if not self._authorized():
                self._challenge()
                return
            path = urlparse(self.path).path

            # **不管走哪个分支，先把 body 读掉。**
            # 有些接口（清空日志、CLI 检测/轮询）根本不看 body，但前端照样会带
            # 一个 `{}` 过来 —— 不读就会留在这个长连接里，把**下一个**请求
            # 搞成 400。详见 _read_json 的说明。
            self._body = _UNREAD
            self._read_json()

            # 「运行日志」页的「清空缓冲」。只清内存里这份，**不动 botpy.log 文件** ——
            # 文件里的历史比内存里的长得多的多，清掉内存是为了看新的、不是销毁证据。
            if path.startswith("/api/logs/clear"):
                cursor = logbuf.install().clear()
                self._send_json({"ok": True, "cursor": cursor})
                return

            # 成员黑名单：同样**只能删、不能加**（加走群里的「拉黑成员」指令）。
            # 手工填 32 位 openid 填错一位的后果是"封错人"或者"以为封了其实没有"，
            # 两种都不是页面上能看出来的。
            if path.startswith("/api/member-ban"):
                payload = self._read_json()
                action = str(payload.get("action") or "remove")
                store = MemberBlacklist()
                if action == "clear":
                    removed = store.clear()
                    self._send_json({
                        "ok": True, "msg": f"已清空 {removed} 人", "count": 0,
                    })
                    return
                openid = str(payload.get("openid") or "")
                if not openid:
                    self._send_json({"ok": False, "msg": "缺少 openid"}, 400)
                    return
                removed, _old = store.remove(openid)
                self._send_json({
                    "ok": True,
                    "removed": removed,
                    "msg": "已解除" if removed else "这个人已经不在名单里了",
                })
                return

            # AI 候选关键词：勾选 -> 真正加进拉黑名单；忽略/清空 -> 丢弃。
            # 「加入」在这做而不是让管理员把词抄回上面的文本框 —— 抄一遍
            # 等于白拆。加进名单的来源标成「AI 拆分」，和手工添加区分开
            if path.startswith("/api/ai-suggest"):
                payload = self._read_json()
                action = str(payload.get("action") or "")
                store = ai_split.SuggestStore()
                if action == "clear":
                    removed = store.clear()
                    self._send_json({"ok": True, "msg": f"已清空 {removed} 批候选"})
                    return
                item_id = str(payload.get("id") or "")
                if not item_id:
                    self._send_json({"ok": False, "msg": "缺少 id"}, 400)
                    return
                if action == "dismiss":
                    store.resolve(item_id, "dismiss")
                    self._send_json({"ok": True, "msg": "已忽略这批候选"})
                    return
                if action == "accept":
                    found = store.resolve(item_id, "accept")
                    if not found:
                        self._send_json({
                            "ok": False, "msg": "这批候选已经不存在了（可能已被处理）",
                        }, 404)
                        return
                    # 勾了哪些就用哪些；一个没勾（理论上前端拦住了）就全加
                    picked = [
                        str(item).strip()
                        for item in (payload.get("phrases") or [])
                        if str(item or "").strip()
                    ]
                    phrases = picked or [str(i) for i in (found.get("phrases") or [])]
                    black = Blacklist(settings)
                    added = 0
                    for phrase in phrases:
                        ok, _ = black.add(
                            phrase,
                            by_name="AI 拆分",
                            group_openid=str(found.get("group_openid") or ""),
                        )
                        added += 1 if ok else 0
                    self._send_json({
                        "ok": True,
                        "msg": f"已加入 {added} 条拉黑（提交 {len(phrases)} 条）",
                        "added": added,
                    })
                    return
                self._send_json({"ok": False, "msg": "未知操作"}, 400)
                return

            # 「测试 AI 连接」：用**已保存**的设置真调一次拆分接口，
            # 让管理员不用去群里拉黑一条消息来验证 key 配没配对。
            # 网页线程里没有事件循环，asyncio.run 现开一个即可；
            # 一条测试请求 20 秒封顶（ai_split.REQUEST_TIMEOUT），阻塞得起
            if path.startswith("/api/ai-test"):
                data = settings.snapshot()
                # 只测「服务」本身通不通，不看各功能的开关 —— 那些开关在各自
                # 功能页上，和服务配置是两回事
                if not str(data.get("ai_api_key") or "").strip():
                    self._send_json({
                        "ok": False, "msg": "先填好 API Key 并点右上角保存，再测试",
                    })
                    return
                started = time.time()
                base_url, model = ai_split.resolve_config(data)
                phrases, error = asyncio.run(
                    ai_split.split_keywords(
                        "【连接测试】限时福利！加微信 qq123456 免费领取",
                        api_key=str(data.get("ai_api_key") or ""),
                        base_url=base_url,
                        model=model,
                    )
                )
                if error:
                    self._send_json({"ok": False, "msg": f"连接失败：{error}"})
                    return
                self._send_json({
                    "ok": True,
                    "msg": "连接正常，{} 秒拆出 {} 个短语：{}".format(
                        int(time.time() - started),
                        len(phrases),
                        "、".join(phrases[:5]) + ("…" if len(phrases) > 5 else ""),
                    ),
                })
                return

            # OneBot 旁路：测连接（拿小号登录信息）/ 拉成员并绑定。
            # sync 是逐群 HTTP 拉取，可能要几秒到十几秒 —— ThreadingHTTPServer
            # 每请求一线程，阻塞得起
            if path.startswith("/api/onebot/probe"):
                data = settings.snapshot()
                if not str(data.get("onebot_http_url") or "").strip():
                    self._send_json({"ok": False, "msg": "先填 OneBot 接口地址并保存"})
                    return
                info, error = onebot.probe(
                    str(data.get("onebot_http_url") or ""),
                    str(data.get("onebot_token") or ""),
                )
                if error:
                    self._send_json({"ok": False, "msg": f"连接失败：{error}"})
                    return
                self._send_json({
                    "ok": True,
                    "msg": "连接正常，小号：{}（QQ {}）".format(
                        info.get("nickname") or "?", info.get("user_id") or "?"),
                })
                return
            if path.startswith("/api/onebot/sync"):
                data = settings.snapshot()
                if not data.get("onebot_enabled"):
                    self._send_json({
                        "ok": False,
                        "msg": "先打开上面的「启用 OneBot 连接」并保存",
                    })
                    return
                if members is None:
                    self._send_json({
                        "ok": False, "msg": "配置页是单独启动的，拿不到成员名册",
                    })
                    return
                result = onebot.match_and_bind(
                    members,
                    str(data.get("onebot_http_url") or ""),
                    str(data.get("onebot_token") or ""),
                    str(data.get("onebot_group_map") or ""),
                )
                self._send_json(result)
                return

            # 群映射选择器的增删：改的是 onebot_group_map 这份设置本身
            # （手填文本框和选择器是同一份数据的两个入口）
            if path.startswith("/api/onebot/map"):
                payload = self._read_json()
                action = str(payload.get("action") or "")
                qq = str(payload.get("qq") or "").strip()
                openid = str(payload.get("openid") or "").strip()
                if action not in ("add", "remove") or not qq or not openid:
                    self._send_json({"ok": False, "msg": "参数不全"}, 400)
                    return
                data = settings.snapshot()
                lines = split_lines(data.get("onebot_group_map"))
                parsed_lines = []
                for line in lines:
                    parsed, _issues = onebot.parse_group_map(line)
                    pair = parsed[0] if parsed else ("", "")
                    parsed_lines.append((line, pair))  # (原文, (群号, openid))
                if action == "add":
                    # 一个群只允许一条映射：同 openid 或同群号的旧线先移除，
                    # 不然匹配逻辑撞上双映射时行为说不清
                    lines = [
                        line for line, (p_qq, p_oid) in parsed_lines
                        if p_oid != openid and p_qq != qq
                    ]
                    lines.append(f"{qq}={openid}")
                    msg = "已添加映射"
                else:
                    lines = [
                        line for line, (p_qq, p_oid) in parsed_lines
                        if not (p_oid == openid and p_qq == qq)
                    ]
                    msg = "已删除映射"
                settings.update({"onebot_group_map": "\n".join(lines)})
                self._send_json({"ok": True, "msg": msg})
                return

            # NapCat 小号扫码登录：代理 WebUI 的二维码。
            # 登录成功与否由前端轮询 /api/onebot/probe 判断（OneBot HTTP 服务
            # 起来了 = 登录完成），不依赖 WebUI 自己的状态接口
            # NapCat 掉线诊断：「检测」失败时前端调这个，自动分辨
            # 「NapCat 进程没运行」（要 docker restart）和「只是小号掉线」
            # （服务还在，直接点扫码登录，token 用设置里保存的那份，
            # 不用去日志里翻、也不用进 WebUI 手动登录）
            if path.startswith("/api/napcat/diagnose"):
                data = settings.snapshot()
                _info, http_error = onebot.probe(
                    str(data.get("onebot_http_url") or ""),
                    str(data.get("onebot_token") or ""),
                )
                webui_state, webui_msg = onebot.webui_probe(
                    str(data.get("napcat_webui_url") or ""),
                    str(data.get("napcat_webui_token") or ""),
                )
                if not http_error:
                    self._send_json({"ok": True, "msg": "小号在线，一切正常"})
                    return
                if webui_state == "ok":
                    self._send_json({
                        "ok": False,
                        "msg": "小号掉线了，但 NapCat 服务还在 —— 不用进 WebUI，"
                               "直接点上面「扫码登录」，配置页会用保存的 token "
                               "自动取二维码，扫一下就行",
                    })
                    return
                if webui_state == "auth":
                    # WebUI 页面能打开 ≠ token 对：登录接口验的是 token 本身
                    self._send_json({
                        "ok": False,
                        "msg": "NapCat 在运行，但 WebUI token 不对 —— 到服务器上"
                               "执行 docker logs napcat 2>&1 | grep -i token 找到"
                               " token，填到「QQ 号识别」页的「WebUI token」保存，"
                               "然后回来点「扫码登录」（全程不用打开 WebUI）",
                    })
                    return
                self._send_json({
                    "ok": False,
                    "msg": "NapCat 服务没在运行（WebUI 也连不上：" + webui_msg + "）"
                           "—— 先到服务器上执行 docker restart napcat，"
                           "起来后再回来点「扫码登录」",
                })
                return

            # 自动获取 WebUI token：docker exec / 启动日志 / 挂载文件三路找，
            # 找到先验证能用再落盘 —— 用户全程不用碰服务器日志
            if path.startswith("/api/napcat/fetch-token"):
                token, source, error = onebot.autofetch_token()
                if error:
                    self._send_json({"ok": False, "msg": error})
                    return
                state, msg = onebot.webui_probe(
                    str(settings.snapshot().get("napcat_webui_url") or ""), token
                )
                if state != "ok":
                    self._send_json({
                        "ok": False,
                        "msg": f"找到了 token（来自{source}），但登录 WebUI 失败：{msg}",
                    })
                    return
                settings.update({"napcat_webui_token": token})
                self._send_json({
                    "ok": True,
                    "msg": f"已自动获取并保存 WebUI token（来自{source}），"
                           "现在可以直接点「扫码登录」",
                })
                return

            if path.startswith("/api/napcat/qrcode"):
                data = settings.snapshot()
                webui_url = str(data.get("napcat_webui_url") or "")
                token = str(data.get("napcat_webui_token") or "")
                kind, payload, error = onebot.webui_qrcode(webui_url, token)
                if error:
                    self._send_json({"ok": False, "msg": error})
                    return
                result = {"ok": True, "kind": kind}
                if kind == "png":
                    result["image"] = "data:image/png;base64," + base64.b64encode(
                        payload
                    ).decode("ascii")
                else:
                    # 新版给的是二维码内容 URL，前端自己渲染成图
                    result["qrcode_url"] = payload
                # 顺手带上"以前登录过的账号"：掉线重连可以直接快速登录，免扫码
                accounts, _cred, _err = onebot.webui_quick_accounts(webui_url, token)
                if accounts:
                    result["quick"] = accounts
                self._send_json(result)
                return

            # 新版 NapCat 的快速登录：小号掉线后不用扫码，把之前登录过的
            # 号直接拉起来（登录成功与否由前端轮询 /api/onebot/probe 确认）
            if path.startswith("/api/napcat/quick-login"):
                data = settings.snapshot()
                error = onebot.webui_quick_login(
                    str(data.get("napcat_webui_url") or ""),
                    str(data.get("napcat_webui_token") or ""),
                    str(data.get("uin") or ""),
                )
                if error:
                    self._send_json({"ok": False, "msg": error})
                    return
                self._send_json({
                    "ok": True,
                    "msg": "快速登录请求已发送，等几秒小号就会上线",
                })
                return

            # 「群消息互转」页的绑定列表：**只能删，不能加**。
            # 加走群里的自助指令（成员自己绑自己那条），管理员要手工指定
            # 某人就用 relay_notes —— 两边各管一件事，别混在一起。
            if path.startswith("/api/relay/bindings"):
                payload = self._read_json()
                action = str(payload.get("action") or "remove")
                if action == "clear":
                    removed = clear_bindings()
                    self._send_json({
                        "ok": True, "msg": f"已清空 {removed} 条绑定", "count": 0,
                    })
                    return
                openid = str(payload.get("openid") or "")
                if not openid:
                    self._send_json({"ok": False, "msg": "缺少 openid"}, 400)
                    return
                removed, _ = unbind_qq(openid)
                self._send_json({
                    "ok": True,
                    "removed": removed,
                    "msg": "已删除" if removed else "这条已经不在绑定表里了",
                })
                return

            # 「CLI 登录」页的三个动作。**都是同步调** ——
            # 配置页是 ThreadingHTTPServer，每个请求一个线程，而 CLI 一次
            # 只花一秒左右，阻塞得起（这也是 ChannelCli 内核做成同步的原因）。
            if path.startswith("/api/cli/probe"):
                if channel_cli is None:
                    self._send_json({"ok": False, "msg": "这个进程没挂上 CLI 桥"})
                    return
                self._send_json({"ok": True, "status": channel_cli.probe_sync()})
                return

            if path.startswith("/api/cli/login"):
                if channel_cli is None:
                    self._send_json({"ok": False, "msg": "这个进程没挂上 CLI 桥"})
                    return
                payload = self._read_json()
                action = str(payload.get("action") or "start")
                if action == "cancel":
                    channel_cli.cancel_login()
                    self._send_json({"ok": True, "status": channel_cli.status()})
                    return
                data, error = channel_cli.login_start(force=(action == "force"))
                if error:
                    # 已经登录过时 CLI 会拒绝普通 login —— 页面据此提示
                    # 改用「强制重新登录」，所以要把原因原样带回去
                    self._send_json({
                        "ok": False,
                        "msg": error,
                        "status": channel_cli.status(),
                    })
                    return
                self._send_json({
                    "ok": True,
                    "login": {
                        "qr_code": str(data.get("qr_code") or ""),
                        "verification_uri": str(data.get("verification_uri") or ""),
                        "expires_in_s": int(data.get("expires_in_s") or 0),
                    },
                    "status": channel_cli.status(),
                })
                return

            if path.startswith("/api/cli/poll"):
                if channel_cli is None:
                    self._send_json({"ok": False, "msg": "这个进程没挂上 CLI 桥"})
                    return
                state, message = channel_cli.login_poll()
                self._send_json({
                    "ok": True,
                    "login_status": state,
                    "msg": message,
                    "status": channel_cli.status(),
                })
                return

            # 「同步频道」：只置一个请求标志，真正的拉取发生在机器人的事件循环里
            # （异步接口没法在网页线程里 await）
            if path.startswith("/api/sync"):
                if inventory is None:
                    self._send_json({"ok": False, "msg": "配置页是单独启动的，没有机器人可同步"})
                else:
                    inventory.request_sync()
                    self._send_json({"ok": True, "msg": "已请求同步"})
                return

            # 「群聊」页的「同步成员」：同样是置标志，由机器人循环去拉
            if path.startswith("/api/groups/members"):
                payload = self._read_json()
                if group_info is None:
                    self._send_json({"ok": False, "msg": "配置页是单独启动的，没有机器人可同步"})
                else:
                    group_info.request_members(payload.get("group_openid"))
                    self._send_json({"ok": True, "msg": "已请求同步，几秒后自动刷新"})
                return

            # 「群备注」：配置页直接写文件，不经过机器人 —— 所以是即时生效的。
            # 备注文件只有网页线程写、机器人只读，不存在两个线程互相覆盖的问题。
            if path.startswith("/api/groups/note"):
                payload = self._read_json()
                group_openid = str(payload.get("group_openid") or "")
                if not group_openid:
                    self._send_json({"ok": False, "msg": "缺少 group_openid"}, 400)
                    return
                notes = write_note(group_openid, payload.get("note"))
                self._send_json({"ok": True, "msg": "备注已保存", "notes": notes})
                return

            # 频道配置：启用 / 清除覆盖 / 删除 / 设置转发目标群
            if path.startswith("/api/guilds/profile"):
                payload = self._read_json()
                guild_id = str(payload.get("guild_id") or "")
                if not guild_id:
                    self._send_json({"ok": False, "msg": "缺少 guild_id"}, 400)
                    return
                action = str(payload.get("action") or "set")
                if action == "drop":
                    settings.drop_profile(guild_id)
                elif action == "clear":
                    settings.clear_overrides(guild_id)
                else:
                    patch = {}
                    for key in ("label", "channel_number", "subscribers", "channel_routes"):
                        if key in payload:
                            patch[key] = payload[key]
                    # 只给了 label 也算"启用独立配置"（set_profile 会建出这条记录）
                    settings.set_profile(guild_id, patch)
                self._send_json({
                    "ok": True,
                    "profile": settings.profile(guild_id),
                    "overridden": settings.overridden_keys(guild_id),
                })
                return

            if not path.startswith("/api/settings"):
                self._send_json({"ok": False, "msg": "not found"}, 404)
                return
            try:
                payload = self._read_json()
                # 新格式 {guild_id, values}；老格式直接把表单当 body，两种都认
                raw_values = payload.get("values")
                values = raw_values if isinstance(raw_values, dict) else payload
                guild_id = str(payload.get("guild_id") or "")
                settings.update(values, guild_id=guild_id or None)
                self._send_json(_settings_view(settings, guild_id))
            except Exception as error:
                self._send_json({"ok": False, "msg": str(error)}, 400)

        def log_message(self, *args):
            pass  # 静音，避免刷屏

    server = ThreadingHTTPServer((host, port), _Handler)
    if use_tls:
        # 自签证书也行 —— 重点是把内容加密，口令不会明文过网
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=cert, keyfile=key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, name="settings-web", daemon=True)
    thread.start()
    return server


if __name__ == "__main__":
    # 单独启动配置页（不跑机器人）：python -m forum_bot.config_web
    web = config.get_web_config()
    _server = start_settings_web(
        Settings(config.SETTINGS_FILE),
        host=web["host"],
        port=web["port"],
        username=web["username"],
        password=web["password"],
        cert=web["cert"],
        key=web["key"],
    )
    print(
        "配置页面: %s://%s:%d  (Ctrl+C 退出)"
        % ("https" if (web["cert"] and web["key"]) else "http", web["host"], web["port"])
    )
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        _server.shutdown()
