# -*- coding: utf-8 -*-
"""文案资源层：把「发给用户看的中文」抽到 text.yaml，改文案不用改代码。

只抽该抽的
----------
分成两类，边界很明确：

    抽        发到 QQ 的消息、指令词、枚举标签 —— 用户能看到的、你大概率想改的
    不抽      日志：它出现在 journalctl 里，抽进资源文件只会让你在两个文件之间
                    来回跳，排错时更慢
              注释 / 文档字符串：它们属于代码，不该和"文案"混在一起
              内部 key、文件路径、正则：不是文案

三层兜底（任何一层坏掉都不会让机器人崩）
----------------------------------------
    1. text.yaml    仓库根目录，**进 git**，随便改；改完发「刷新设置」或重启即可生效
    2. DEFAULTS     本文件里的内置默认值，yaml 里缺哪项就用它
    3. key 本身     连内置默认都没有时，原样返回 key 并记一条 warning

第 3 层是**故意的**：宁可界面上冒出一个奇怪的 key，也绝不能因为少配一行就让机器人
崩在"给用户发消息"的路上 —— 那是最不该失败的地方。同理，占位符填不上时也只记
warning 并返回原文，不抛异常。
"""
import copy
import os

from botpy import logging

from . import config

__all__ = [
    "get",
    "text",
    "lines",
    "words",
    "word",
    "reload",
    "source",
    "generation",
    "unknown_keys",
]

_log = logging.get_logger()

TEXT_FILE = os.path.join(config.BASE_DIR, "text.yaml")

# ----------------------------------------------------------------------
# 内置默认值。text.yaml 里的同名项会覆盖它；没有的项就落回这里。
# 改文案优先改 text.yaml（更好读、能写注释），这里只当兜底。
# ----------------------------------------------------------------------
DEFAULTS = {
    "commands": {
        # ── 群里任何人都能用的 ──
        "status": "状态",
        "mode": "模式",
        # ── 仅管理员 ──
        "subscribe": "订阅",
        "unsubscribe": "取消订阅",
        "flush": "立即推送",
        "reload_settings": "刷新设置",
        "recall": "撤回",
        "recall_last": "撤回上一条",
        "blacklist_add": "拉黑",
        "blacklist_remove": "取消拉黑",
        "blacklist_show": "拉黑列表",
        # 成员黑名单（按人封）。和上面三个「拉黑…」是分开的：
        # 那几个管**内容**，这三个管**人**
        "blacklist_member": "拉黑成员",
        "unblacklist_member": "解除成员",
        "blacklist_members": "成员黑名单",
        "bindings": "绑定列表",
        "revoke": "撤销",
        "group_info": "群信息",
        "group_info_refresh": "刷新群信息",
        "group_members": "群成员",
        "reconcile": "对账",
        "relay_status": "转发状态",
        # ── 领资料的各种叫法（精确匹配，任一即可） ──
        "access_grant": ["领资料", "领取资料", "拿资料", "资料"],
        "access_status": ["我的资料", "资料状态", "查询资料"],
        # 帖子正文里出现这些就当成领资料请求。
        # **故意不放「资料」这个泛词** —— 这本来就是个「资料」板块，
        # 正文里出现"资料"太常见，会被大量误判。
        "access_post_hints": ["领资料", "领取资料", "拿资料"],
        # ── 频道里的指令 ──
        "channel_my_id": ["我的ID", "ID"],
        "channel_set_access": ["设为资料板块", "设置资料板块", "设为资料频道"],
    },
    "labels": {
        "group_role": {
            "owner": "群主",
            "admin": "群管理员",
            "member": "普通成员",
        },
        # 「机器人在群内的接收消息设置」（bot_state 接口的 recv_msg_setting）。
        # 只有 all 才收得到群里每一条消息 —— 群消息互转依赖它。
        "recv_msg_setting": {
            "all": "接收全部消息",
            "only_mention": "仅被 @ 时",
            "mention_and_context": "被 @ 及其上下文",
        },
        # 子频道类型（配置页里「文字」「语音」这类标签）
        "channel_type": {
            "text": "文字",
            "voice": "语音",
            "group": "分组",
            "live": "直播",
            "app": "应用",
            "forum": "论坛",
        },
        # 子频道的可见范围
        "private_type": {
            "public": "公开",
            "admin": "管理员可见",
            "specified_user": "指定成员可见",
        },
        "fallback_group": "指定的 QQ 群",
        "fallback_guild": "(未知频道)",
        "default_channel": "资料",
        "not_configured": "未配置",
        "long_term": "长期有效",
        "no_snapshot": "还没有快照",
        "unknown": "未知",
    },
    "messages": {
        # ── 验证码与领资料 ──
        "access": {
            "code": (
                "验证码：{code}（{minutes} 分钟内有效）\n"
                "加入 QQ 群「{group}」，在群里 @我 发送这个验证码，"
                "即可开通「{channel}」板块权限"
            ),
            "channel_mismatch": (
                "提示：开通的是「{channel}」板块（{expected}），"
                "不是你当前所在的子频道（{channel_id}）"
            ),
            "intro": (
                "想拿资料？两步：\n"
                "1. 在本板块 @我 发送「领资料」，拿到一个验证码\n"
                "2. 加入 QQ 群「{group}」，在群里 @我 发送这个验证码"
            ),
            "feature_off": "这个功能还没开启",
            "not_configured": "管理员还没配置好：{items}",
            "no_user_id": "没拿到你的用户 ID，请稍后再试",
            "missing_channel": "资料板块 ID",
            "missing_group": "指定群的 openid",
            "redeem_bad_code": "验证码不对，或者已经用过了（每个码只能用一次）",
            "redeem_used": "这个验证码已经用过了",
            "redeem_expired": "验证码过期了，回频道里再发一次「领资料」吧",
            "redeem_wrong_group": "这个验证码要到指定的 QQ 群里兑换哦",
            "redeem_member_taken": "这个群账号已经绑定过另一个频道账号了，请先让管理员解绑",
            "redeem_user_taken": "这个频道账号已经绑定过别的群账号了，请先让管理员解绑",
            "redeem_ok": "验证通过，已给你开通「{channel}」板块权限，回频道里就能看到了",
            "redeem_grant_failed": (
                "验证通过了，但没授权成功：{hint}\n"
                "（这次没有写进绑定表，处理好之后重新发一次「领资料」拿新码即可）"
            ),
            "status_none": (
                "你还没有「{channel}」板块的权限。\n"
                "加入 QQ 群「{group}」后，在本板块 @我 发「领资料」按提示操作即可。"
            ),
            "status_ok_header": "你已经有「{channel}」板块的权限了。",
            "status_granted": "授权时间：{time}",
            "status_expire": "有效期：{value}",
            "status_left": "（检测到你已退群，权限正在回收中）",
            "expire_at": "{time} 到期",
            # 接口报错翻译
            "api_301003": (
                "平台拒绝了这次授权（301003）。两个前提要同时满足：\n"
                "① 「资料」子频道要设为「指定成员可见」—— 公开子频道没法给单个成员授权；\n"
                "② 机器人要是频道管理员（或至少在该子频道勾选了「管理子频道」）。\n"
                "改完不用重启，重新领一次码再试"
            ),
            "api_403": "机器人没有这个板块的管理权限 —— 请把机器人设为频道管理员",
            "api_404": "没找到这个子频道，请检查配置页里的「资料板块 ID」",
            "api_429": "接口调用太频繁了，稍后再试",
            "api_other": "接口报错：{error}",
        },
        # ── 群指令 ──
        "group": {
            "no_permission": (
                "没有权限：只有机器人管理员才能使用「{command}」\n"
                "你当前的身份：{identity}\n"
                "可找群主 / 群管理员操作，或在机器人配置页「权限管理」里把管理员加进名单"
            ),
            "status_line": "你的身份：{identity}{suffix}",
            "status_no_admin": "（无权使用管理指令）",
            "status_admin": "（管理员）",
            "status_targets": "转发目标群：{count} 个",
            "status_strategy": "推送策略：{strategy}",
            "status_buffer": "缓冲区待推送：{count} 条",
            "mode_now": "当前推送策略：{strategy}",
            "flush_ok": "已推送 {count} 条",
            "flush_empty": "缓冲区是空的",
            "reload_ok": "已重新读取设置：{strategy}",
            "bindings_title": "资料板块授权（共 {count} 人）：",
            "bindings_empty": "还没有人绑定",
            "bindings_more": "…… 其余 {count} 条未显示",
            "binding_line": "{prefix}{user}…　{granted} 授权，{expire}{flag}",
            "binding_left": "（已退群，待收回）",
            "revoke_not_found": "没找到这个用户的授权记录：{user_id}",
            "revoke_done": "已收回 {user_id} 在 {count} 个频道的授权：{labels}",
            "revoke_failed_header": "以下没收回成功：",
            "revoke_nothing": "没有可收回的授权",
            "help_any_user": "可用指令（任何人都能用）：{names}",
            "help_identity": "你的身份：{identity}{suffix}",
            "help_admin_lines": [
                "管理指令：订阅 / 取消订阅 / 立即推送 / 刷新设置",
                "订阅：多频道下要指明频道，例如「订阅 1234567890123456789」；",
                "　　　只配了一个频道时直接发「订阅」即可",
                "撤回：引用某条消息后发「撤回」（不用 @）；或「撤回 <message_id>」；或「撤回上一条」",
                "拉黑：引用某条消息后发「拉黑」，之后包含这段内容的消息会自动撤回；",
                "　　　「取消拉黑」「拉黑列表」用于管理名单（拉黑内容会保存到本地）",
                "资料权限：群里发验证码即可为对方开通；「绑定列表」查看，",
                "　　　　　「撤销 <频道用户id>」手动收回",
                "群信息：「群信息」查看、「刷新群信息」拉最新、「群成员」列名单、",
                "　　　　「对账」立刻比对群成员并收回已退群者的权限",
                "转发：QQ 号由 OneBot 旁路自动识别（见配置页「QQ 号识别」），",
                "　　　没识别到则显示 openid 前 8 位。「转发状态」看名单",
                "拉黑：「拉黑」引用某条消息 = 屏蔽这条内容，同时把发送者一起拉进成员",
                "　　　黑名单（「拉黑成员」「解除成员」「成员黑名单」单独管理）。",
                "　　　成员黑名单 = 他以后发的消息都会被撤回，不是踢人（平台没这接口）",
            ],
            "help_member_line": (
                "管理指令（订阅 / 撤回 / 拉黑 等）只有管理员可用，可找群主或配置页授权"
            ),
        },
        # ── 订阅 ──
        "subscribe": {
            "plain_ok": "已订阅频道新帖转发",
            "plain_cancel": "已取消频道新帖转发",
            "need_guild": "请指明频道：{action} <频道号>",
            "not_found": "没找到「{argument}」对应的频道",
            "choices": "可选频道：",
            "example": "例：{action} 1234567890123456789",
            "done": "已订阅「{label}」的新帖转发",
            "done_cancel": "已取消「{label}」的新帖转发",
            "now": "本群现在订阅了 {count} 个频道：{labels}",
            "none": "本群当前没有订阅任何频道",
        },
        # ── 群信息 / 群成员 ──
        "group_info": {
            "notice": "（群名 / 群人数 / 群成员列表是平台白名单能力，未开通会提示 11253）",
            "denied": (
                "{hint}\n「获取群成员列表」是平台白名单能力，需要在 QQ 开放平台申请开通"
            ),
            "members_title": "群成员（{snapshot}）：",
            "unavailable": "群信息接口不可用",
            "no_group": "没给群 openid",
        },
        # ── 对账 ──
        "reconcile": {
            "done": "对账完成",
            "removed_header": "已收回 {count} 个授权：",
            "removed_item": "　{user}…　{reason}",
            "reason_left": "对账发现已不在群",
            "total": "当前授权人数：{count}",
            "feature_off": "功能未开启",
            "no_registry": "没有接入群信息模块",
            "unavailable": "群信息接口不可用（未获白名单权限）",
            "incomplete": "群成员没拉全，本轮跳过对账（不会动任何人的权限）",
            "summary": "比对 {count} 名群成员，收回 {removed} 个授权",
            "hint_unavailable": "提示：群信息接口不可用，对账没做成 —— 现在是只靠退群事件回收。",
            # 多频道：一次对一遍，逐频道汇总
            "no_guild_enabled": "配置过的频道都没开启加群解锁",
            "no_guild_configured": "还没配置任何频道的加群解锁",
            "guild_disabled": "{guild}：已关闭对账",
            "guild_note": "{guild}：{hint}",
            "no_group_for_guild": "频道 {guild} 还没配置「指定群的 openid」",
        },
        # ── 群消息互转 ──
        "relay": {
            "state_on": "已开启",
            "state_off": "已关闭",
            "summary": "群消息互转：{sources} 个源群、{routes} 条规则",
            "status_title": "群消息互转：{state}",
            "status_limit": "每群每日上限：{limit} 条（平台硬限 1000）",
            "no_rules": "还没有配置互转规则（一行一条「源 -> 目标」，两边填群号即可）",
            "rules_header": "互转规则（{count} 个源群）：",
            "rule_line": "{source}  ->  {target}　今日 {used}/{limit}",
            # 规则解析失败的原因。**必须说出来** —— 静默跳过等于
            # 「配了却没生效」，是最难查的一类问题。
            "bad_rule": "格式不对，应该是「源 -> 目标」",
            "same_group": "源群和目标群是同一个群",
            "unknown_number": (
                "群号 {number} 认不出来 —— 先到配置页「群聊」页给它填个群备注，"
                "或者填到「群号 → openid 映射」里"
            ),
            "empty_token": "箭头有一边是空的",
            "issues_header": "有 {count} 条规则没生效：",
            "issue_line": "{line}　{reason}",
            "issues_more": "　…… 其余 {count} 条见配置页「群消息互转」",
            "media_state": "图片转发：每条消息最多 {count} 张（每张各占 1 条主动消息）",
            # 自动识别到的 QQ 号（OneBot 旁路 + 管理员手工，见 bindings/onebot 模块）。
            # 「转发状态」是管理员指令，所以这里可以列名字 —— 管理员本来就要核对
            "bound_count": "已识别 QQ 号：{count} 人（这里只列最近 5 个，完整名单在配置页）",
            "no_bindings": "已识别 QQ 号：还没有人（OneBot 匹配或手工指定后出现）",
            "bound_line": "{nick}　QQ {qq}",
            "bound_more": "　…… 其余 {count} 人在配置页「群消息互转」页查看",
            "bound_only_on": "已开启「只转发已绑定成员」：目前 {count} 人会被转发",
            # 群消息是纯文本，这里不能用 markdown 的 ** —— 会原样显示出来
            "bound_only_empty": ("已开启「只转发已绑定成员」，但还没有人绑定 —— "
                                 "现在一条都不会转发！"),
            # 群没把消息推给机器人时最该说的一句话：用户往往会以为是规则写错了
            "recv_warning": (
                "注意：这些群的「消息接收设置」不是「接收全部消息」，"
                "互转对它们不会生效：{groups} —— "
                "需要群管理员在群设置里把机器人的消息接收改成「接收全部消息」"
            ),
            # 纯图片消息没有正文，用它当 {content} 好让转发里能看出是谁发的
            "media_only": "[图片]",
            "notes_count": "成员标识映射：已填 {count} 个（转发里会显示成你填的备注）",
            "no_notes": (
                "成员标识映射：未填 —— 转发里只显示 openid 前 8 位"
                "（不是 QQ 号，平台不提供）"
            ),
            "no_nick": "（无昵称）",
            "unknown_member": "未知成员",
        },
        # ── 频道内的回复 ──
        "channel": {
            "my_id": "你的 user_id：{user_id}\n当前子频道 ID：{channel_id}",
            "set_access_denied": (
                "只有频道管理员能设置。\n"
                "把你的 user_id（{user_id}）加到配置页「权限管理 → 频道管理员 user_id」后重试，"
                "或者直接在配置页里填「资料板块 ID」。"
            ),
            "set_access_done": "已把当前子频道（{channel_id}）设为「{guild}」的「资料」板块。",
            "help_title": "可用指令：",
            "help_lines": [
                "领资料 —— 拿验证码，去指定 QQ 群兑换后开通本板块权限",
                "我的资料 —— 查看自己的授权状态",
                "我的ID —— 查看自己的 user_id 和当前子频道 ID",
                "（在文字子频道里 @我，或者直接私信我，都可以）",
            ],
        },
        # ── 撤回与拉黑 ──
        "recall": {
            "no_message_id": "没拿到消息 ID",
            "too_late": "撤不回来了：消息发送已超过 2 分钟",
            "not_admin": "没有权限：撤回他人的消息需要机器人是群管理员",
            "bad_id": "消息 ID 无效",
            "failed": "撤回失败：{error}",
            "ok": "已撤回",
            "notify": "已撤回一条消息（{reason}）",
            "reason_blacklist": "命中拉黑内容「{phrase}」",
            "reason_member": "{who} 在成员黑名单里",
            "reason_keyword": "命中撤回关键词「{word}」",
            "need_quote": "没有识别到引用：请先引用要撤回的那条消息，再发「撤回」",
            "quoted_content": "{reason}\n被引用的内容是：{preview}",
        },
        # ── 成员黑名单（按**人**封，不是按内容）──
        # **它不是踢人**：平台的 QQ群接口没有移除成员的能力。文案里必须写明，
        # 否则用户以为人已经被踢走了，结果人家还在群里说话（只是被撤）。
        "member_ban": {
            "need_target": (
                "没找到要拉黑的人：引用他的一条消息再发「拉黑成员」，"
                "或者发「拉黑成员 <openid / 昵称>」"
            ),
            "added": "已把这个人加进成员黑名单，他以后发消息都会被撤回",
            "already": "这个人已经在成员黑名单里了",
            "target": "对象：{who}（{openid}）",
            "total": "成员黑名单：{total} 人（存本地，重启不丢）",
            "quoted": "被引用的消息：{content}",
            "added_inline": "同时把发送者也加进了成员黑名单：{who}",
            # 群消息是纯文本，这里不能有 markdown 的 ** —— 会原样显示出来。
            # 也不能用英文双引号（在 Python 的 "..." 里会把字符串截断）
            "not_kick": (
                "注意：这「不是」把人踢出群 —— 平台没有移除群成员的接口。"
                "他还在群里，只是发消息会被撤回（撤不撤得掉取决于机器人是不是群管理员）"
            ),
            "no_permission": (
                "这个群（{group}…）撤不了别人的消息：机器人不是群管理员。"
                "已暂停在该群尝试 30 分钟，把它设成群管理员后会自动恢复"
            ),
            "removed": "已把 {who} 移出成员黑名单（还剩 {left} 人）",
            "not_found": "{who} 不在成员黑名单里",
            "empty": "成员黑名单是空的",
            "title": "成员黑名单（{count} 人）：",
            "more": "…… 其余 {count} 人在配置页查看",
            "hint": "（这些人发消息会被撤回；撤不掉时是因为机器人不是那个群的管理员）",
        },
        "blacklist": {
            "need_quote_add": "请引用要拉黑的消息再发「拉黑」，或者发「拉黑 <内容>」",
            "need_quote_remove": "请引用要解除的消息再发「取消拉黑」，或者发「取消拉黑 <内容>」",
            "already": "这条已经在拉黑名单里了",
            "added": "已拉黑，之后包含这段内容的消息会自动撤回",
            "content": "拉黑内容：{content}",
            "total": "当前名单：{total} 条（已保存到本地）",
            "quoted": "被引用的消息：{content}",
            "not_found": "拉黑名单里没有找到：{content}",
            "removed": "已解除拉黑 {count} 条，剩余 {left} 条",
            "empty": "拉黑名单是空的",
            "title": "拉黑名单（共 {count} 条）：",
        },
    },
}

_DATA = None
_SOURCE = "内置默认值"
# 每次 reload 都 +1。别的模块（如 bot.py 的指令表）靠它判断"要不要重建缓存"，
# 这样改完 text.yaml 不用重启就能生效。
_GENERATION = 0


def generation():
    """文案版本号。reload 时 +1，缓存方据此判断要不要重建。"""
    return _GENERATION


def _read_file():
    """读 text.yaml。读不到/读坏了都返回 (None, 原因)，由调用方落回默认值。"""
    if not os.path.exists(TEXT_FILE):
        return None, "没找到 text.yaml，全部用内置默认值"
    try:
        from botpy.ext.cog_yaml import read

        data = read(TEXT_FILE)
    except Exception as error:
        return None, f"text.yaml 解析失败（{error}），全部用内置默认值"
    if not isinstance(data, dict):
        return None, "text.yaml 顶层不是键值对，全部用内置默认值"
    return data, "text.yaml"


def _merge(base, override):
    """把 override 深合并进 base 的副本（只覆盖写了的那几项）。"""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def reload():
    """重新加载 text.yaml。「刷新设置」指令和启动时都会调它。"""
    global _DATA, _SOURCE, _GENERATION
    data, source = _read_file()
    _DATA = _merge(DEFAULTS, data)
    _SOURCE = source
    _GENERATION += 1
    return _SOURCE


def _data():
    if _DATA is None:
        reload()
    return _DATA


def source():
    """当前文案来自哪里（启动日志/配置页展示用）。"""
    _data()
    return _SOURCE


def get(path, default=None):
    """按点分路径取值：get("commands.subscribe") -> "订阅"。"""
    node = _data()
    for part in str(path).split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def text(path, **kwargs):
    """取一段文案并填占位符。

    **不抛异常**：取不到就返回 path 本身，占位符填不上就返回原文 ——
    只记一条 warning。发消息的路上不能因为文案配置出错而断掉。
    """
    template = get(path)
    if template is None:
        _log.warning(f"[botpy] 文案 {path} 不存在（内置默认值里也没有），原样显示")
        return path
    if not isinstance(template, str):
        return str(template)
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError) as error:
        _log.warning(f"[botpy] 文案 {path} 的占位符填不上（{error}），原样显示")
        return template


def lines(path, fallback=()):
    """取一组文本行（帮助文案那种列表）。yaml 里写列表或一个多行字符串都认。"""
    value = get(path)
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if isinstance(value, str) and value.strip():
        return [item for item in value.splitlines() if item.strip()]
    return list(fallback)


def words(path, fallback=()):
    """取一组叫法（指令词、别名）。yaml 里写列表或逗号分隔的字符串都认。"""
    value = get(path)
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value]
    elif isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    else:
        items = [str(item).strip() for item in fallback]
    return tuple(item for item in items if item)


def word(path, fallback=""):
    """取单个指令词。"""
    value = get(path)
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (list, tuple)) and value:
        return str(value[0]).strip()
    return str(fallback)


def _flatten(node, prefix=""):
    keys = []
    for key, value in (node or {}).items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            keys.extend(_flatten(value, path))
        else:
            keys.append(path)
    return keys


def unknown_keys():
    """text.yaml 里「内置默认值没有」的 key —— 基本就是拼错了。

    这类错误不会报错（多层兜底让它静默地用默认值），所以启动时打一条日志点名，
    否则你改了文案却没生效会完全摸不着头脑。
    """
    data, source = _read_file()
    if not data:
        return []
    known = set(_flatten(DEFAULTS))
    return sorted(key for key in _flatten(data) if key not in known)
