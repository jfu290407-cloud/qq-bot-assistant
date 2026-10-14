# -*- coding: utf-8 -*-
"""QQ 机器人：频道帖子转发到 QQ 群 + 群消息撤回/拉黑。

模块划分（一个功能一个文件）：
    config          路径常量 + config.yaml 读取
    text            文案资源（text.yaml 的读取、占位符填充、三层兜底）
    logbuf          日志内存环形缓冲（配置页「运行日志」页的数据源）
    bindings        群成员自助绑定的 QQ 号（平台不提供 QQ 号，只能让人自己说）
    settings        转发/撤回设置模型（默认值、读写、校验、免打扰判断）
    config_web      Web 配置界面（纯标准库）
    admins          管理员判定（按 member_openid + 群内身份）
    members         见过的群成员名册（配置页据此列出可授权的 openid）
    groups          已知的群（平台没有"机器人在哪些群"的接口，只能本地记录）
    channel_message 频道消息正文：内嵌格式、转义、@机器人 识别
    inventory       所在频道 / 子频道 / 接口权限缓存（异步接口只能在机器人循环里调）
    dashboard       配置页「概况 / 群聊 / 频道」的数据聚合（只读）
    access          「加群解锁板块」验证码流程 + 子频道权限下发/回收（含成员对账）
    group_info      群信息 / 群成员查询（平台白名单接口，拿不到权限会降级）
    group_message   群消息字段解析（引用、@、msg_idx）+ 控制台打印
    message_index   msg_idx <-> message_id 映射表
    subscribers     订阅群管理
    channel_forum   频道帖子反查 + 帖子链接生成
    channel_cli     tencent-channel-cli 的桥（取帖子短链；评论区以后也走它）
    forwarder       转发策略（即时/合并N条/周期汇总/免打扰/文案/卡片）
    recall          群消息撤回
    blacklist       拉黑名单（内容存 settings，来源存本地文件）
    relay           群消息互转（一个群 -> 另一个群，含每日配额守卫与环路防护）
    bot             事件回调与指令分发（组装上面这些）
"""
__all__ = [
    "config",
    "text",
    "logbuf",
    "settings",
    "config_web",
    "admins",
    "members",
    "groups",
    "channel_message",
    "inventory",
    "dashboard",
    "access",
    "group_info",
    "group_message",
    "message_index",
    "subscribers",
    "channel_forum",
    "forwarder",
    "recall",
    "blacklist",
    "relay",
    "bot",
]
