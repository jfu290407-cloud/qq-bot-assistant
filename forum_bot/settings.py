# -*- coding: utf-8 -*-
"""转发 / 撤回设置模型：默认值、读写、校验，以及免打扰时段判断。

设置持久化在 forum_forward_settings.json，配置页（config_web）编辑的就是它。

设置项说明
----------
mode                        推送模式：
                              instant 收到即推；batch 攒够 N 条推一次；
                              digest 每个周期汇总推一次
batch_count                 batch 模式：攒够多少条立即推送
batch_max_wait_seconds      batch 模式：攒不够也最多等多少秒就推
digest_interval_minutes     digest 模式：汇总周期（分钟）
quiet_hours_enabled         是否开启免打扰时段（时段内只攒不发）
quiet_start / quiet_end     免打扰起止时间，24 小时制 HH:MM，支持跨天
include_keywords            标题包含这些词才转发，英文逗号分隔，留空=不过滤
exclude_keywords            标题包含这些词就不转发，英文逗号分隔
single_header               instant 模式的标题头
batch_header                多条合并时的标题头，{count} 是条数
title_template              单条帖子的文案，占位符见 TITLE_PLACEHOLDERS
max_chars_per_message       单条群消息最大字符数，超出会自动拆成多条

卡片样式相关
------------
style                       消息形式：text 纯文本 / markdown 卡片 /
                            markdown_button 卡片 + 「查看帖子」跳转按钮
card_title                  markdown 卡片标题，多条汇总时自动追加（共 N 条）
card_item                   markdown 里每条帖子的样式，占位符 {index} + 帖子字段
                            （链接由跳转按钮承载，正文里通常不用写 {link}；
                              若选了不带按钮的 markdown，会自动补上链接）
button_label                跳转按钮文字
max_buttons                 最多给前几条帖子挂按钮（1-25）

群消息撤回相关
--------------
recall_enabled              是否开启「命中关键词自动撤回」
recall_keywords             命中这些词就撤回该消息，英文逗号分隔
                            （留空时不做自动撤回，避免误伤整群消息）
recall_notify               撤回后在群里回一句提示（会占用主动消息频次）
blacklist_enabled           是否开启「消息包含拉黑内容就撤回」
blacklist_keywords          拉黑的内容，一行一个（可以含逗号）
                            群内发「拉黑」并引用某条消息，会自动把它加到这里
blacklist_members           引用拉黑时，**连发送者也一起拉进成员黑名单**（默认开）

内容拉黑 vs 成员拉黑
--------------------
这是两件事，别混：

  · **内容**黑名单（blacklist_keywords）—— 这条**内容**不许再说，
    别人说同样的也会被撤。存 settings。
  · **成员**黑名单（forum_member_blacklist.json）—— 这个**人**说的话都撤，
    不看内容。开这个开关后，「拉黑」会把两者一起做。

**成员黑名单不是踢人。** 平台的 QQ群接口里没有任何"移除成员"的能力
（唯一那个 DELETE /guilds/{id}/members/{uid} 是 QQ**频道**的），所以只能
"他还在群里，但一说话就被撤"。两个前提：
  1. member_openid 得跨群稳定（同一个人在不同群是同一个 openid）；
  2. 机器人在那些群得是**群管理员**，否则撤普通成员的消息报 40062003。
     不是管理员的群会被记下来、暂停尝试 30 分钟（设为管理员后自动恢复）。

按频道覆盖（guild_profiles）
---------------------------
同一个机器人可以加多个频道，每个频道可以有自己的推送策略、文案、资料板块。
都存在同一个文件的 guild_profiles 里：

    "guild_profiles": {
        "3500634359415041451": {
            "label": "中国矿业大学",
            "channel_number": "1234567890123456789",
            "subscribers": ["群openid", ...],          // 这个频道的帖子推给哪些群
            "channel_routes": {"子频道id": ["群openid"]},  // 可选：按子频道细分
            "access_channel_id": "742830645",          // 覆盖全局的同名键
            ...
        }
    }

两条规则：
  1. **只存差异**。某个键的值和全局相同时会被删掉，等于"跟随全局"。
     所以以后改全局默认，没覆盖的频道会跟着变 —— 这是符合直觉的。
  2. 名字不在字段表里的键（label / subscribers / channel_routes /
     channel_number）是频道配置独有的，不参与「和全局比差异」。

群维度的那几个键（拉黑、撤回关键词、管理员名单、打印群消息）**不能**按频道覆盖，
见 _GLOBAL_ONLY_KEYS 的说明。

权限
----
admin_openids               管理员名单，一行一个 member_openid
                            （QQ群消息不携带 QQ 号，只能按 openid 授权；
                              配置页会列出「最近见过的群成员」，点一下即填入）
admin_allow_group_owner     群主 / 群管理员是否自动算管理员（默认开）
channel_admin_ids           频道管理员 user_id，一行一个；用于在频道内执行管理指令

加群解锁板块（access）
--------------------
access_enabled              是否开启「加入 QQ 群才能看资料板块」
access_channel_id           要解锁的「资料」子频道 ID
access_group_openid         要求加入的群 openid（兑换验证码时校验）
access_group_label          展示给用户的群号 / 邀请链接，只影响文案
access_code_ttl_minutes     验证码有效期（分钟）
access_days                 授权有效期（天）；0 = 永久，只靠退群事件回收
access_reconcile            是否定期拿「群成员列表」对账，收回已退群者的权限
                            （依赖平台白名单接口，没权限时会自动跳过，不影响别的回收）

群消息互转（relay）
------------------
把一个群的消息转发到另一个群。**四条平台限制绕不过去**，用之前先看清楚：

    1. 需要开「接收所有消息」全量模式 —— 否则机器人只看得到 @它 的消息；
    2. 转过去的一定是「主动消息」（目标群里没有对应事件，拿不到 msg_id），
       吃的是严格配额：每个群**每天 1000 条**、20 qpm，超了报 40034100；
    3. 消息里**不能有 URL**（40054010 直接拒），所以含链接的默认跳过；
    4. **拿不到 QQ 号** —— 事件和成员接口都只给 openid / 昵称（官方原文：
       「与真实的 ID 无关」）。想让转发里出现 QQ 号，只有两条路：
       OneBot 旁路**自动识别**（见 onebot 模块），
       或管理员在 relay_notes 里手工填。
       QQ 号在这里只是显示标签，别拿它当权限。

relay_enabled               是否开启互转
relay_rules                 互转规则，一行一条：`源 -> 目标`
                            **两边都可以填群号**（推荐）或群 openid：
                                123456789 -> 123456789
                            群号靠「群聊」页的群备注或 relay_group_ids 翻译成 openid。
                            双向互转就写两行；写错的会被跳过，并在配置页和日志里点名
relay_group_ids             群号 → openid 的显式映射，一行一条：`群号=openid`
                            一般不用填 —— 「群聊」页填过群备注就够用了。
                            给"没填备注、或备注里写的不是群号"的群兜底
relay_format                转发文本的格式。占位符：
                                {nick}      昵称
                                {uid}       成员标识 —— **不是 QQ 号**（平台不提供）
                                            取值顺序：relay_notes > 自动识别的 QQ 号 > openid 前 8 位
                                            它**永远有值**，所以适合当兜底的那一个
                                {qq}        明确的 QQ 号；**没识别到就是空串**
                                            想让"没识别到的人"和"识别到的人"看起来不一样，
                                            用它排版（{uid} 做不到，它总会退到 openid 前缀）
                                {content}   正文
                                {role}      群内身份（群主 / 群管理员 / 普通成员）
                                {group}     源群的 QQ 群号（{group_no} 是同义词）
                            默认 "{nick}({uid})：{content}" -> 「张三(10086)：你好」
                            想在转发里标出**消息来自哪个群**，就用 {group}：
                                "[{group}] {nick}：{content}" -> 「[123456789] 张三：你好」
                            群号来自「群聊」页填的群备注；没填备注的群会显示 openid 前 10 位
relay_notes                 成员标识映射，一行一条：`member_openid=备注`
                            管理员手工维护，**优先于** OneBot 自动识别 ——
                            得能纠正识别错的、也得给识别不上的人补上
relay_bound_only            只转发已识别到 QQ 号的成员的消息（默认关）
                            QQ 号由「QQ 号识别」页的 OneBot 旁路自动匹配；
                            管理员在 relay_notes 里填过的也算已识别。
                            开了之后，没识别到的人发的消息**一条都不转**。
                            **它管的不是权限** —— 是"要不要把这条话转出去"。
                            判断谁能用机器人仍然只看 openid（admin_openids）
relay_media                 是否连图片一起转（默认开）。一条图片消息 = 一条主动消息配额
relay_media_limit           每条消息最多转几张图（默认 1）。0 = 不转图。
                            多张图是**分多条发**的，每张各占 1 份配额，
                            所以别调太大 —— 配额撞上去是硬失败
relay_daily_limit           每群每日上限（默认 900，平台硬限 1000）
relay_skip_url              跳过含链接的消息（平台不允许，默认开）
relay_skip_commands         跳过指令消息（默认开）
relay_block_keywords        互转过滤词，一行一个：正文**包含**其中任意一条就**不转发**
                            想挡住的多半是广告词、推广话术之类。和「拉黑」的区别是：
                            拉黑会**撤回**消息，这里只是不往对面群转，原群照常说话

环路防护：机器人自己发的消息在事件里带 author.bot=true，会被直接跳过 ——
双向互转时正是靠这条断开环路，否则 A->B 的消息会被再转回 A，几分钟烧光配额。

调试
----
log_group_messages          是否把每条群消息（含原始下发数据）打印到控制台
"""
import json
import threading
from datetime import datetime, timedelta

from .ai_split import PROVIDERS as _AI_PROVIDERS

TITLE_PLACEHOLDERS = ("title", "link", "author_id", "channel_id", "thread_id", "date_time")

DEFAULT_SETTINGS = {
    "mode": "instant",
    "batch_count": 5,
    "batch_max_wait_seconds": 60,
    "digest_interval_minutes": 30,
    "quiet_hours_enabled": False,
    "quiet_start": "23:00",
    "quiet_end": "08:00",
    "include_keywords": "",
    "exclude_keywords": "",
    "single_header": "【频道新帖】",
    "batch_header": "【频道新帖汇总】本时段新增 {count} 条",
    "title_template": "{title}\n{link}",
    "max_chars_per_message": 400,
    # 用官方 CLI 把 {link} 换成帖子短链（pd.qq.com/s/xxx）。
    # 这条链路依赖服务器上装好的 tencent-channel-cli + 用户扫码登录，
    # 拿不到就退回 config.yaml 的长链模板 —— 详细取舍见 channel_cli.py 的模块说明。
    "cli_enabled": True,
    "style": "text",
    "card_title": "## 频道新帖",
    "card_item": "{index}. {title}",
    "button_label": "查看帖子",
    "max_buttons": 5,
    "recall_enabled": False,
    "recall_keywords": "",
    "recall_notify": False,
    "blacklist_enabled": True,
    "blacklist_keywords": "",
    # 引用拉黑时连发送者也一起拉进**成员**黑名单（见文件开头那段说明）
    "blacklist_members": True,
    # 拉黑时用 AI 把整条消息拆成候选关键词。候选**不自动进名单**，
    # 要管理员在配置页勾选 —— 见 ai_split 模块说明。
    # 各功能是否用 AI **在各自的功能页开**；厂商/Key/模型集中在「AI 服务」页
    "blacklist_ai_split": False,
    # AI 服务配置（配置页「AI 服务」页）。厂商预设见 ai_split.PROVIDERS：
    # 换厂商只动 ai_provider 一个下拉框，模型/地址留空就用该厂商默认
    "ai_provider": "deepseek",
    "ai_api_key": "",
    "ai_model": "",
    "ai_base_url": "",
    "admin_openids": "",
    "admin_allow_group_owner": True,
    "channel_admin_ids": "",
    "access_enabled": False,
    # 「领资料 / 我的资料」的指令词覆盖（一行一个，留空用 text.yaml 默认）。
    # 只能全局：指令表是全局一份的
    "access_cmd_grant": "",
    "access_cmd_status": "",
    "access_channel_id": "",
    "access_group_openid": "",
    "access_group_label": "",
    "access_code_ttl_minutes": 5,
    "access_days": 0,
    "access_reconcile": True,
    # 验证码对话（群里 @机器人发码 + 机器人的回复）几秒后自动撤回。
    # 0 = 不撤回。**只能全局**：群消息事件里没有 guild_id，按频道分没有意义
    "access_recall_seconds": 15,
    # 群消息互转：把一个群的消息转发到另一个群（需要「接收所有消息」全量模式）
    "relay_enabled": False,
    "relay_rules": "",
    "relay_group_ids": "",
    "relay_format": "{nick}({uid})：{content}",
    # 按来源群覆盖转发格式（一行一条：群号或openid=格式）。没配的群用 relay_format
    "relay_format_by_group": "",
    # 转发带引用的消息时，先转一条被引用的原文，回复用 message_reference 挂引用。
    # 目标群看不到源群的引用上下文，不带的话"不知道他在回复什么"
    "relay_quote": True,
    "relay_notes": "",
    "relay_media": True,
    "relay_media_limit": 1,
    "relay_bound_only": False,
    # 互转过滤词：正文命中就不转发（只是不转，不撤回原消息）
    "relay_block_keywords": "",
    "relay_daily_limit": 900,
    # QQ 号自动识别（OneBot 旁路，见 onebot 模块说明）。
    # 各自独立于「AI 服务」—— 这是另一个外部服务
    "onebot_enabled": False,
    "onebot_http_url": "http://127.0.0.1:3000",
    "onebot_token": "",
    "onebot_group_map": "",
    "onebot_auto": False,
    # 实时识别：收到消息时从 NapCat 消息历史里即时配对出发送者的真实 QQ 号。
    # 和上面的批量匹配互补 —— 能破解同名冲突和改名片的情况
    "onebot_live": False,
    # NapCat WebUI（登录小号用的管理页）。「QQ 登录」页的扫码代理走它
    "napcat_webui_url": "http://127.0.0.1:6099",
    "napcat_webui_token": "",
    "relay_skip_url": True,
    "relay_skip_commands": True,
    "log_group_messages": True,
}

# 这些数字项允许填 0（0 是有意义的取值，不能被抬成 1）
# access_days：0 = 永久有效
# access_recall_seconds：0 = 不自动撤回验证码对话
_ZERO_ALLOWED = {
    "access_days",
    "access_recall_seconds",
}

# 旧默认值 -> 新默认值的兼容映射
_LEGACY_DEFAULTS = {
    "card_item": "{index}. {title}\n{link}",
}

# 配置页里每个字段的展示信息（顺序即页面顺序）
# (key, 名称, 提示, 控件类型, 下拉选项, 所属分组)

# 「AI 服务」页的厂商下拉框，选项从 ai_split.PROVIDERS 生成 ——
# 两边必须是同一份，加厂商时只改 ai_split 一处
_AI_PROVIDER_OPTIONS = tuple(
    (key, meta["label"]) for key, meta in _AI_PROVIDERS.items()
)

FIELD_META = (
    ("mode", "推送模式", "instant=收到即推 / batch=攒够N条 / digest=周期汇总", "select",
     (("instant", "即时推送"), ("batch", "合并N条推送"), ("digest", "按周期汇总")), "strategy"),
    ("batch_count", "批量条数", "batch 模式：攒够这么多条立即推送", "number", (), "strategy"),
    ("batch_max_wait_seconds", "批量最长等待(秒)", "batch 模式：攒不够也最多等这么久", "number", (), "strategy"),
    ("digest_interval_minutes", "汇总周期(分钟)", "digest 模式：每隔多久汇总推一次", "number", (), "strategy"),
    ("quiet_hours_enabled", "开启免打扰", "时段内只攒不发，时段结束后一并推送", "checkbox", (), "strategy"),
    ("quiet_start", "免打扰开始", "24 小时制，如 23:00", "text", (), "strategy"),
    ("quiet_end", "免打扰结束", "24 小时制，如 08:00，支持跨天", "text", (), "strategy"),
    ("include_keywords", "标题必须包含", "英文逗号分隔，留空表示不限制", "text", (), "content"),
    ("exclude_keywords", "标题不能包含", "英文逗号分隔，命中则不转发", "text", (), "content"),
    ("single_header", "即时推送标题头", "纯文本 · instant 模式的文案头", "text", (), "content"),
    ("batch_header", "汇总标题头", "纯文本 · 多条合并时的文案头，{count} 表示条数", "text", (), "content"),
    ("title_template", "单条文案模板", "纯文本 · 占位符：" + " ".join("{%s}" % item for item in TITLE_PLACEHOLDERS),
     "text", (), "content"),
    ("max_chars_per_message", "单条消息最大字数", "超出自动拆分成多条消息发送", "text", (), "content"),
    # 这一项挂在「QQ 登录」页（section="cli"）—— 它和扫码登录是同一件事的两半，
    # 分在两个页面只会让人来回找：登录了却不知道开关在哪、或者开了开关却不知道要登录
    ("cli_enabled", "把 {link} 换成帖子短链",
     "短链形如 pd.qq.com/s/xxx。**需要上面那个扫码登录有效** —— "
     "机器人自己的凭证拿不到短链（平台没给机器人开这个接口）。"
     "拿不到就自动退回 config.yaml 里的长链模板，不影响转发",
     "checkbox", (), "cli"),
    ("style", "消息形式", "text=纯文本 / markdown=卡片 / markdown_button=卡片+跳转按钮", "select",
     (("text", "纯文本"), ("markdown", "Markdown 卡片"), ("markdown_button", "Markdown 卡片 + 按钮")), "card"),
    ("card_title", "卡片标题", "markdown 卡片标题，多条汇总时自动追加（共 N 条）", "text", (), "card"),
    ("card_item", "卡片内单条样式", "markdown · 占位符 {index} 加帖子字段；链接由按钮承载，正文里不用写 {link}",
     "text", (), "card"),
    ("button_label", "按钮文字", "markdown_button 模式的跳转按钮文字", "text", (), "card"),
    ("max_buttons", "最多挂几个按钮", "官方限制：最多 5 行、每行 5 个，即最多 25 个", "number", (), "card"),
    ("recall_enabled", "开启关键词自动撤回", "群消息命中下面的关键词就自动撤回（需要机器人为群管理员）",
     "checkbox", (), "recall"),
    ("recall_keywords", "撤回关键词", "英文逗号分隔；留空时不做自动撤回，避免误伤整群消息", "text", (), "recall"),
    ("recall_notify", "撤回后群里提示", "会占用主动消息频次，默认关闭", "checkbox", (), "recall"),
    ("blacklist_enabled", "开启拉黑撤回", "群消息只要包含下面任一条拉黑内容，就直接撤回", "checkbox", (), "block"),
    ("blacklist_keywords", "拉黑内容（一行一个）", "可以含逗号；群内发「拉黑」并引用某条消息会自动加进来",
     "textarea", (), "block"),
    ("blacklist_members", "引用拉黑时连发送者也一起拉黑",
     "开了之后，「拉黑」不只是屏蔽那条内容，还会把**发那条消息的人**加进成员黑名单 —— "
     "他之后在**任何群**发消息都会被撤回（不看内容）。"
     "**这不是踢人**：平台没有移除群成员的接口，他还在群里，只是说不了话。"
     "两个前提：① 同一个人的 member_openid 在各群一致；"
     "② 机器人在那些群是**群管理员**，否则撤不了别人的消息（会暂停尝试并在日志里说）",
     "checkbox", (), "block"),
    ("blacklist_ai_split", "拉黑消息时用 AI 拆分候选关键词",
     "引用拉黑一条消息后，机器人把正文交给 AI 拆成几个**候选关键词**，"
     "放到下面「AI 候选关键词」卡片里 —— 你勾选后才真正进名单，"
     "<b>AI 给的东西不会自动生效</b>。"
     "用哪家 AI、API Key 是什么，到<b>「AI 服务」页</b>配置；"
     "这里只管「拉黑拆分这件事要不要用 AI」。"
     "拉黑本身照常生效，AI 拆分失败只记日志、不打扰群里",
     "checkbox", (), "block"),
    # ---- AI 服务（独立一页）：厂商预设 + Key。将来别的功能要用 AI，
    # ---- 只要在各自功能页加开关，服务配置共用这一份
    ("ai_provider", "AI 厂商",
     "接口都是 OpenAI 兼容的，换厂商只改这一处，模型/地址留空即用该厂商默认。"
     "豆包的「模型名」实际填接入点 ID（ep- 开头）；自建/中转服务选自定义",
     "select", _AI_PROVIDER_OPTIONS, "ai"),
    ("ai_api_key", "API Key",
     "到所选厂商的控制台创建（厂商名后面是各家控制台地址）。"
     "保存在本机 forum_forward_settings.json，<b>不要</b>把这份文件提交进仓库或发给别人",
     "text", (), "ai"),
    ("ai_model", "模型名（留空用厂商默认）",
     "留空 = 用所选厂商的默认模型。豆包必须填（接入点 ID，ep- 开头）；"
     "选了自定义也必须填",
     "text", (), "ai"),
    ("ai_base_url", "接口地址（留空用厂商默认）",
     "一般不用填。自建/中转服务才填，填到域名或带 /v1 都认",
     "text", (), "ai"),
    ("onebot_enabled", "启用 OneBot 连接",
     "打开后「拉取并绑定」按钮和自动匹配才会工作。"
     "NapCat 的部署说明见 README「QQ 号自动识别」一节",
     "checkbox", (), "onebot"),
    ("onebot_http_url", "OneBot HTTP 接口地址",
     "默认 http://127.0.0.1:3000（NapCat 和机器人在同一台机器时）。"
     "填到端口号为止，不带最后的斜杠",
     "text", (), "onebot"),
    ("onebot_token", "OneBot access token（可空）",
     "NapCat 的 network 配置里设了 token 就填同一个，没设留空",
     "text", (), "onebot"),
    ("onebot_group_map", "群映射（QQ群号=group_openid）",
     "一行一条，把小号所在的 QQ 群和官方机器人所在的群对上 —— "
     "两套体系没有公共 ID，只能人工配一次。<code>openid=群号</code> 或 "
     "<code>群号=openid</code> 都认。只对配了映射的群做匹配",
     "textarea", (), "onebot"),
    ("onebot_auto", "自动匹配（每 6 小时）",
     "打开后机器人每 6 小时自动拉一次成员列表并匹配绑定；"
     "不开就只用下面「拉取并绑定」按钮手动触发。"
     "⚠ 非官方框架有封号风险（用小号！），频率已刻意放低，别改得更频繁",
     "checkbox", (), "onebot"),
    ("napcat_webui_url", "NapCat WebUI 地址",
     "默认 http://127.0.0.1:6099。「QQ 登录」页给小号扫码用的管理页地址"
     "（和上面的 HTTP 接口端口不是同一个）",
     "text", (), "onebot"),
    ("napcat_webui_token", "NapCat WebUI token",
     "NapCat 的 WebUI 有自己的 token（启动日志里会打印，或在 webui 配置文件里）。"
     "WebUI 没设 token 就留空",
     "text", (), "onebot"),
    ("onebot_live", "实时识别（收到消息即时配对）",
     "打开后每条群消息都会到 NapCat 的消息历史里即时配对出发送者的真实 QQ 号并写入绑定表 —— "
     "<b>转发不再依赖预先绑定</b>，同名冲突、改名片都拦不住它。"
     "识别延迟约 20 秒（轮询间隔）；纯图片/纯表情消息配不了。"
     "配合上面的批量匹配使用：实时负责新消息，批量负责存量",
     "checkbox", (), "onebot"),
    ("admin_openids", "管理员 openid 名单",
     "一行一个；QQ群消息不携带 QQ 号，只能按 member_openid 授权 —— 用下面「最近见过的群成员」里的「加入名单」一键填入",
     "textarea", (), "admin"),
    ("admin_allow_group_owner", "群主/群管理员自动放行",
     "勾选后，群里身份是群主或群管理员的人不用加名单也能使用管理指令", "checkbox", (), "admin"),
    ("channel_admin_ids", "频道管理员 user_id",
     "一行一个；在频道里 @机器人 发「我的ID」可看到自己的 user_id，用于频道侧管理指令",
     "textarea", (), "admin"),
    ("access_enabled", "开启「加群解锁板块」",
     "开启后，用户在资料板块 @机器人 发「领资料」拿验证码，去指定群兑换后自动开通板块权限",
     "checkbox", (), "access"),
    ("access_cmd_grant", "「领资料」指令词",
     "一行一个。用户在资料板块/私信里发这些词就会领验证码。"
     "<b>留空 = 用默认（领资料 / 领取资料 / 拿资料）</b>。"
     "改完立刻生效，不用重启",
     "textarea", (), "access"),
    ("access_cmd_status", "「我的资料」指令词",
     "一行一个，查自己的授权状态用。"
     "<b>留空 = 用默认（我的资料 / 资料状态）</b>。改完立刻生效",
     "textarea", (), "access"),
    ("access_channel_id", "资料板块 ID",
     "要解锁的子频道 ID；在该板块里 @机器人 发「我的ID」就能看到当前子频道 ID", "text", (), "access"),
    ("access_group_openid", "兑换群（可配多个）",
     "验证码允许在哪些群里兑换，<b>一行一个</b>，任配一个群都能兑换。"
     "推荐用下面的「兑换群选择器」直接勾选，不用手填 openid。"
     "留空 = 不限制群（不推荐，验证码会到处乱兑）",
     "textarea", (), "access"),
    ("access_group_label", "展示给用户的群号/链接",
     "只影响给用户看的文案，比如 123456789 或群邀请链接", "text", (), "access"),
    ("access_code_ttl_minutes", "验证码有效期(分钟)", "超时后需要回频道重新领取", "number", (), "access"),
    ("access_days", "授权有效期(天)",
     "0 = 永久（靠退群事件 + 成员对账回收）；大于 0 则到期自动收回", "number", (), "access"),
    ("access_reconcile", "用群成员列表对账",
     "定期拉群成员名单，把已退群者的权限收回 —— 补上「退群事件漏推」的那部分。"
     "依赖平台白名单接口（11253），没权限时会自动跳过，不影响别的回收",
     "checkbox", (), "access"),
    ("access_recall_seconds", "验证码对话自动撤回(秒)",
     "群里 @机器人发验证码兑换时，<b>用户那条消息和机器人的回复</b>都会在"
     "这么多秒后自动撤回 —— 验证码和兑换记录没必要长期挂在聊天记录里。"
     "撤用户的消息需要机器人在该群是<b>管理员</b>，不是就只撤机器人自己的回复。"
     "填 <b>0</b> = 不自动撤回",
     "number", (), "access"),
    ("log_group_messages", "把群消息打印到控制台", "含完整原始下发数据，排查引用/成员字段时很有用；稳定后可关掉",
     "checkbox", (), "debug"),
    # ── 互转规则（页面：互转规则） ──
    ("relay_enabled", "开启群消息互转",
     "把一个群的消息转发到另一个群。**需要开放平台的「接收所有消息」全量模式**，"
     "否则机器人只看得到 @它 的消息，谈不上同步。"
     "注意：转过去的一定是「主动消息」，平台限制每个群每天最多 1000 条",
     "checkbox", (), "relay"),
    ("relay_rules", "互转规则（一行一条）",
     "格式：源 -> 目标（也认 →）。**两边直接填群号就行**，"
     "比如「123456789 -> 123456789」—— 群号靠「群聊」页的群备注翻译成 openid，"
     "所以先把每个群的备注填上；也可以直接填群 openid（老写法继续支持）。"
     "双向互转就写两行。下面会实时显示每条规则解析到了哪个群，写错的当场点出来",
     "textarea", (), "relay"),
    ("relay_group_ids", "群号 → openid 映射（一般不用填）",
     "一行一条：群号=openid。只有在「群聊」页没填群备注、"
     "或者备注里写的不是群号时才需要 —— 正常情况下群备注就够了。"
     "这里填的会覆盖从备注推出来的结果",
     "textarea", (), "relay"),
    ("relay_quote", "转发时带上被引用的消息",
     "源群里引用了某条消息再回复的，转发到目标群时会带上下文 —— "
     "目标群里呈现为正常的「引用回复」样式（message_reference）。"
     "<b>被引用的消息以前转发过</b>的话直接挂到那条转发上，不多发；"
     "只有从没转发过的（老消息/当时没监听）才补发一条原文。"
     "被引用的原文若本身还是引用消息，只取最外层正文，不会把嵌套渲染块发出来。"
     "引用目标已失效（被撤回）时自动去掉引用重发，消息不会丢。"
     "补发的原文压成单行、最多 200 字，里面的链接会一并剥掉（平台不允许），"
     "并<b>额外占用 1 条</b>每日配额。引用发送失败时自动降级为拼接文本",
     "checkbox", (), "relay"),
    ("relay_format_by_group", "按群转发格式（可选）",
     "一行一条：<code>QQ群号=格式</code>（填群 openid 也认）。"
     "命中的群用<b>自己的格式</b>转发，没配的群用上面的全局「转发格式」—— "
     "两层是覆盖关系，不是要你把全局那份抄一遍。"
     "占位符和全局格式完全相同（{nick}/{uid}/{qq}/{content}/{role}/{group}）。"
     "想换行：直接回车，或写 \\n（两种都认）。"
     "下面的实时预览只预览全局格式；按群格式保存后发条消息即可验证",
     "textarea", (), "relay"),
    ("relay_format", "转发格式",
     "占位符：{nick} 昵称　{uid} 成员标识　{qq} QQ 号　{content} 正文　"
     "{role} 群内身份　{group} 源群 QQ 群号（{group_no} 同义）。"
     "要在转发里标出消息来自哪个群，就加 {group} —— "
     "「[{group}] {nick}：{content}」会发成「[123456789] 张三：你好」。"
     "群号取自「群聊」页的群备注，所以先把每个群的备注填上。"
     "注意 {uid} **不是 QQ 号**（平台不提供）：它的取值顺序是 "
     "「成员标识映射」> 成员自助绑定 > openid 前 8 位，**永远有值**；"
     "而 {qq} 只认前两者，没绑定就是**空串** —— "
     "想换行：直接回车，或写 \\n（两种都认）。"
     "想让「没绑定」看起来不一样，就用 {qq} 排版",
     "text", (), "relay"),
    ("relay_daily_limit", "每群每日上限",
     "平台硬限是每个群每天 1000 条主动消息，撞上去报 40034100。默认 900，留一点余量。"
     "**「加群提醒」也吃这份额度**（它同样是主动消息）—— 两边共用一本账，"
     "所以这里要留够余量",
     "number", (), "relay"),

    # ── 互转过滤（页面：互转过滤） ──
    ("relay_block_keywords", "互转过滤词（命中就不转发，一行一个）",
     "正文**包含**其中任意一条，这条消息就**不往对面群转**。"
     "和「拉黑名单」不是一回事：拉黑是**撤回**消息（原群也看不到了），"
     "这里只是不转过去 —— 原群里照常说，只是对面收不到。"
     "一行一个，可以带逗号；留空表示不过滤",
     "textarea", (), "relay_filter"),
    ("relay_skip_url", "跳过含链接的消息",
     "平台不允许主动消息里出现 URL（40054010 直接拒），所以默认跳过",
     "checkbox", (), "relay_filter"),
    ("relay_skip_commands", "跳过指令消息",
     "「订阅」「撤回」这类指令不往对面群转，免得刷屏",
     "checkbox", (), "relay_filter"),
    ("relay_media", "连图片一起转",
     "把群消息里的图片也转过去。注意**每条图片消息各占 1 条主动消息配额**，"
     "而且图片要分两步（先让平台去抓源地址换 file_info，再发富媒体消息），"
     "比转文字慢、也更容易失败。源图地址带签名会过期，"
     "所以只适合「实时互转」—— 补发历史消息基本转不动",
     "checkbox", (), "relay_filter"),
    ("relay_media_limit", "每条消息最多转几张图",
     "多张图是分**多条消息**发出去的，每张各占 1 份配额。默认 1，别调太大",
     "number", (), "relay_filter"),

    # ── 成员标识与绑定（页面：成员绑定） ──
    # 自助绑定（「绑定QQ」指令）已废弃 —— QQ 号现在由 OneBot 旁路自动识别
    # （见 onebot 模块），绑定表只剩展示用途，配置页管理
    ("relay_notes", "成员标识映射（一行一条）",
     "格式：member_openid=备注。管理员手工维护，<b>优先于</b>自动识别的 QQ 号 —— "
     "用来纠正识别错的、或给识别不上的人补上。"
     "没填也没识别到的会显示 openid 前 8 位。"
     "openid 到「权限管理」页的成员列表里复制",
     "textarea", (), "relay_bind"),
    ("relay_bound_only", "只转发已识别到 QQ 号的成员",
     "开了之后，**还没识别到 QQ 号的成员发的消息一条都不转**。"
     "QQ 号由「QQ 号识别」页的 OneBot 旁路自动匹配，"
     "管理员在下面「成员标识映射」里填过的也算。"
     "这是**宁可少转**的选择 —— 能避免把不想被转的人的话转到别的群，"
     "但如果群里大部分人还没识别到，互转会看起来像坏了"
     "（日志里会点名是谁被跳过，不会静默失效）",
     "checkbox", (), "relay_bind"),
)

# ---------------------------------------------------------------------------
# 按频道（guild）覆盖
# ---------------------------------------------------------------------------
# 这些键**只属于全局**。它们都是用在「群消息」上的，而群消息事件里没有 guild_id
# —— 机器人在哪个群不知道"这个群属于哪个频道"，所以按频道分开没有意义。
_GLOBAL_ONLY_KEYS = (
    "recall_enabled",
    "recall_keywords",
    "recall_notify",
    "blacklist_enabled",
    "blacklist_keywords",
    "blacklist_members",
    # AI 拆分候选关键词：key 是全局一份（配置页填的），按频道分开只会让人
    # 搞不清"这条到底用的哪个 key"
    "blacklist_ai_split",
    # AI 服务配置（厂商/Key/模型/地址）：key 是全局一份，按频道分开只会让人
    # 搞不清"这条到底用的哪个 key / 哪家服务"
    "ai_provider",
    "ai_api_key",
    # OneBot 旁路同理：小号、NapCat 都是全局一份
    "onebot_enabled",
    "onebot_http_url",
    "onebot_token",
    "onebot_group_map",
    "onebot_auto",
    "onebot_live",
    # NapCat WebUI 同理是全局一份（登录小号用的，跟频道无关）
    "napcat_webui_url",
    "napcat_webui_token",
    "ai_model",
    "ai_base_url",
    "admin_openids",
    "admin_allow_group_owner",
    "log_group_messages",
    # 互转的源/目标都是「群」，而群消息里没有 guild_id，
    # 按频道分开放没意义 —— 同上面几个，只做全局
    "relay_enabled",
    "relay_rules",
    "relay_group_ids",
    "relay_format",
    "relay_format_by_group",
    "relay_quote",
    "relay_notes",
    "relay_media",
    "relay_media_limit",
    "relay_bound_only",
    "relay_block_keywords",
    "relay_daily_limit",
    "relay_skip_url",
    "relay_skip_commands",
    # 短链走的是同一条全局链路（CLI 命令 + 一份用户登录凭证），
    # 而且链接模板本身也是全局的（config.yaml 的 link_template）——
    # 按频道分开配只会让人困惑：为什么 A 频道短、B 频道长
    "cli_enabled",
    # 撤回发生在「群消息」上，事件里没有 guild_id（同互转，只做全局）
    "access_recall_seconds",
    # 指令表全局一份，按频道分开会让 A 频道认的词 B 频道不认
    "access_cmd_grant",
    "access_cmd_status",
)

# 可以被某个频道单独覆盖的键（字段网格里的键，去掉上面那些）
_GUILD_KEYS = tuple(key for key, *_ in FIELD_META if key not in _GLOBAL_ONLY_KEYS)

# 频道配置里这些键的结构是list/dict，不走 _coerce 那套按默认值推类型
_GUILD_LIST_KEYS = ("subscribers",)
_GUILD_DICT_KEYS = ("channel_routes",)
_GUILD_TEXT_KEYS = ("label", "channel_number")

_LOCK = threading.RLock()


def split_lines(raw):
    """把多行文本拆成短语列表（一行一个，去空去重保序）。

    拉黑内容用换行分隔而不是逗号 —— 被拉黑的消息正文里经常带逗号。
    """
    items = []
    for line in str(raw or "").splitlines():
        text = line.strip()
        if text and text not in items:
            items.append(text)
    return items


def _coerce(key, value):
    """按默认值的类型把配置页传来的字符串转成正确的类型。"""
    default = DEFAULT_SETTINGS[key]
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "on", "yes")
        return bool(value)
    if isinstance(default, int):
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            return default
        if key in _ZERO_ALLOWED:
            return max(0, number)
        return max(1, number)
    return default if value is None else str(value)


def _clean_list(value):
    """把订阅名单整理成一串去空去重的字符串。"""
    if isinstance(value, str):
        items = value.replace(",", "\n").splitlines()
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        return []
    result = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _clean_routes(value):
    """整理「子频道 -> 群名单」映射。"""
    if not isinstance(value, dict):
        return {}
    routes = {}
    for channel_id, groups in value.items():
        channel_id = str(channel_id or "").strip()
        items = _clean_list(groups)
        if channel_id and items:
            routes[channel_id] = items
    return routes


def _clean_profile(raw):
    """把磁盘上读到的频道配置规范化，顺便丢掉不认识的键。"""
    profile = {}
    for key, value in (raw or {}).items():
        if key in _GUILD_KEYS:
            profile[key] = _coerce(key, value)
        elif key in _GUILD_LIST_KEYS:
            items = _clean_list(value)
            if items:
                profile[key] = items
        elif key in _GUILD_DICT_KEYS:
            routes = _clean_routes(value)
            if routes:
                profile[key] = routes
        elif key in _GUILD_TEXT_KEYS:
            text = str(value or "").strip()[:64]
            if text:
                profile[key] = text
    return profile


def _parse_clock(text):
    try:
        hour, minute = str(text).strip().split(":")
        return int(hour) % 24, int(minute) % 60
    except (TypeError, ValueError):
        return None


class Settings:
    """线程安全的设置容器：bot 侧读取、Web 线程写入。"""

    def __init__(self, path):
        self._path = path
        self._data, self._guilds = self._load()

    def _load(self):
        data = dict(DEFAULT_SETTINGS)
        guilds = {}
        try:
            with open(self._path, "r", encoding="utf-8") as file:
                saved = json.load(file)
            if isinstance(saved, dict):
                for key, value in saved.items():
                    if key in DEFAULT_SETTINGS:
                        data[key] = _coerce(key, value)
                raw = saved.get("guild_profiles")
                if isinstance(raw, dict):
                    for guild_id, profile in raw.items():
                        if isinstance(profile, dict):
                            guilds[str(guild_id)] = _clean_profile(profile)
            for key, legacy in _LEGACY_DEFAULTS.items():
                if data.get(key) == legacy:
                    data[key] = DEFAULT_SETTINGS[key]
        except Exception:
            pass
        return data, guilds

    # ---------------- 读取 ----------------
    def snapshot(self, guild_id=None):
        """取生效中的设置。

        guild_id 非空时，用该频道的覆盖盖在全局之上；没配过就等价于全局。
        """
        with _LOCK:
            data = dict(self._data)
            profile = self._guilds.get(str(guild_id or "")) if guild_id else None
            if profile:
                for key, value in profile.items():
                    if key in _GUILD_KEYS:
                        data[key] = value
            return data

    def guild_ids(self):
        """配过覆盖的频道 id。"""
        with _LOCK:
            return sorted(self._guilds)

    def profile(self, guild_id):
        """某个频道的原始配置（含 label / subscribers / channel_routes）。"""
        with _LOCK:
            profile = self._guilds.get(str(guild_id or ""))
            return dict(profile) if profile else {}

    def overridden_keys(self, guild_id):
        """这个频道覆盖了哪些字段（配置页用来标「已覆盖」）。"""
        with _LOCK:
            profile = self._guilds.get(str(guild_id or "")) or {}
            return sorted(key for key in profile if key in _GUILD_KEYS)

    def subscribers_of(self, guild_id):
        return list(self.profile(guild_id).get("subscribers") or [])

    def channel_routes(self, guild_id):
        routes = self.profile(guild_id).get("channel_routes")
        return dict(routes) if isinstance(routes, dict) else {}

    def targets(self, guild_id, channel_ids=(), fallback=()):
        """算出「这个频道的帖子该推给哪些群」。

        频道级默认名单 + 可选的按子频道细分：
          guild_profiles[gid].subscribers         这个频道默认推给哪些群
          guild_profiles[gid].channel_routes      某个子频道单独推给哪些群（优先）

        **没配过 subscribers / channel_routes 的频道回退到全局订阅列表**，
        这样从单频道升级上来时不会突然一条都不推。
        """
        profile = self.profile(guild_id)
        default = profile.get("subscribers")
        routes = self.channel_routes(guild_id)
        if not default and not routes:
            return set(fallback or ())

        targets = set()
        for channel_id in (channel_ids or (None,)):
            specific = routes.get(str(channel_id)) if channel_id is not None else None
            if isinstance(specific, list):
                targets.update(str(item) for item in specific)
            else:
                targets.update(str(item) for item in (default or []))
        return targets

    # ---------------- 写入 ----------------
    def update(self, values, guild_id=None):
        """写设置。guild_id 为空写全局，否则只写该频道的**差异**。

        「只存差异」很关键：和全局相同的值会被删掉，等于"跟随全局"。
        这样以后改全局默认，没覆盖的频道会跟着变。
        """
        with _LOCK:
            guild_id = str(guild_id or "")
            if not guild_id:
                for key, value in (values or {}).items():
                    if key in DEFAULT_SETTINGS:
                        self._data[key] = _coerce(key, value)
            else:
                profile = self._guilds.setdefault(guild_id, {})
                for key, value in (values or {}).items():
                    if key not in _GUILD_KEYS:
                        continue          # 群维度的键忽略，别在频道层留下影子
                    value = _coerce(key, value)
                    if value == self._data.get(key):
                        profile.pop(key, None)
                    else:
                        profile[key] = value
                if not profile:
                    self._guilds.pop(guild_id, None)
            self._dump()
            return self.snapshot(guild_id or None)

    def set_profile(self, guild_id, patch):
        """直接设置频道配置里那些「不在字段表里」的键。

        label / subscribers / channel_routes / channel_number 都是频道配置独有的，
        没有"全局值"可比，所以直接覆盖。
        """
        guild_id = str(guild_id or "")
        if not guild_id:
            return {}
        with _LOCK:
            profile = self._guilds.setdefault(guild_id, {})
            for key, value in (patch or {}).items():
                if key in _GUILD_LIST_KEYS:
                    items = _clean_list(value)
                    if items:
                        profile[key] = items
                    else:
                        profile.pop(key, None)
                elif key in _GUILD_DICT_KEYS:
                    mapping = _clean_routes(value)
                    if mapping:
                        profile[key] = mapping
                    else:
                        profile.pop(key, None)
                elif key in _GUILD_TEXT_KEYS:
                    text = str(value or "").strip()[:64]
                    if text:
                        profile[key] = text
                    else:
                        profile.pop(key, None)
            if not profile:
                self._guilds.pop(guild_id, None)
            self._dump()
            return self.profile(guild_id)

    def add_subscriber(self, guild_id, group_openid):
        """把某个群加进这个频道的转发目标（群内发「订阅 <频道>」走这里）。"""
        guild_id = str(guild_id or "")
        group_openid = str(group_openid or "")
        if not guild_id or not group_openid:
            return []
        current = self.subscribers_of(guild_id)
        if group_openid not in current:
            current.append(group_openid)
        return self.set_profile(guild_id, {"subscribers": current}).get("subscribers") or []

    def remove_subscriber(self, guild_id, group_openid):
        guild_id = str(guild_id or "")
        group_openid = str(group_openid or "")
        current = [item for item in self.subscribers_of(guild_id) if item != group_openid]
        return self.set_profile(guild_id, {"subscribers": current}).get("subscribers") or []

    def clear_overrides(self, guild_id):
        """清掉某个频道的字段覆盖（保留 label / 订阅 / 频道号）。"""
        guild_id = str(guild_id or "")
        with _LOCK:
            profile = self._guilds.get(guild_id)
            if not profile:
                return {}
            for key in list(profile):
                if key in _GUILD_KEYS:
                    profile.pop(key, None)
            if not profile:
                self._guilds.pop(guild_id, None)
            self._dump()
            return self.profile(guild_id)

    def drop_profile(self, guild_id):
        """整个删掉某个频道的配置。"""
        guild_id = str(guild_id or "")
        with _LOCK:
            removed = self._guilds.pop(guild_id, None)
            if removed:
                self._dump()
            return removed or {}

    def reload(self):
        """重新从磁盘读取（手动改过 json 文件时用）。"""
        with _LOCK:
            self._data, self._guilds = self._load()
            return self.snapshot()

    def _dump(self):
        try:
            payload = dict(self._data)
            if self._guilds:
                payload["guild_profiles"] = self._guilds
            with open(self._path, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------------- 业务判断 ----------------

    def is_quiet_now(self, guild_id=None):
        """当前是否处于免打扰时段（支持跨天，如 23:00 - 08:00）。"""
        data = self.snapshot(guild_id)
        if not data.get("quiet_hours_enabled"):
            return False
        start = _parse_clock(data.get("quiet_start"))
        end = _parse_clock(data.get("quiet_end"))
        if not start or not end or start == end:
            return False

        now = datetime.now()
        current = now.hour * 60 + now.minute
        begin = start[0] * 60 + start[1]
        finish = end[0] * 60 + end[1]
        if begin < finish:
            return begin <= current < finish
        return current >= begin or current < finish

    def quiet_ends_at(self, guild_id=None):
        """免打扰结束时间点，用于算「还要等多久」。"""
        data = self.snapshot(guild_id)
        start = _parse_clock(data.get("quiet_start"))
        end = _parse_clock(data.get("quiet_end"))
        if not start or not end:
            return None
        now = datetime.now()
        target = now.replace(hour=end[0], minute=end[1], second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    def filter_reason(self, title, guild_id=None):
        """返回 None 表示放行，否则返回被过滤的原因。"""
        data = self.snapshot(guild_id)
        text = title or ""

        includes = [item.strip() for item in str(data.get("include_keywords") or "").split(",") if item.strip()]
        if includes and not any(item in text for item in includes):
            return "未命中必须包含的关键词"

        excludes = [item.strip() for item in str(data.get("exclude_keywords") or "").split(",") if item.strip()]
        if excludes and any(item in text for item in excludes):
            return "命中排除关键词"
        return None

    def describe(self, guild_id=None):
        data = self.snapshot(guild_id)
        mode = data["mode"]
        if mode == "batch":
            summary = f"合并推送（满 {data['batch_count']} 条或 {data['batch_max_wait_seconds']} 秒后推送）"
        elif mode == "digest":
            summary = f"周期汇总（每 {data['digest_interval_minutes']} 分钟一次）"
        else:
            summary = "即时推送"
        if data["quiet_hours_enabled"]:
            summary += f"；免打扰 {data['quiet_start']}-{data['quiet_end']}"
        overridden = len(self.overridden_keys(guild_id)) if guild_id else 0
        if guild_id and overridden:
            summary += f"（本频道覆盖了 {overridden} 项）"
        return summary
