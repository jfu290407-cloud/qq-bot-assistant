# QQ 机器人助手

一个 QQ 官方机器人的自托管方案：**频道论坛新帖自动转发到 QQ 群**、
**群消息互转**、**「加群解锁板块」权限系统**、**关键词撤回/拉黑**，
外加一个可视化的 Web 配置台。

基于腾讯官方 [botpy](https://github.com/tencent-connect/botpy) SDK（仓库内 `botpy/`
是打过补丁的源码副本：群成员事件解析、错误码细节、补充群信息接口）。

## 功能

**内容搬运**
- 频道论坛新帖自动推送到 QQ 群，支持多频道、按子频道细分目标群
- 群消息互转：把一个群的消息转发到另一个群，支持引用转发、图文、每日配额
- 帖子短链（pd.qq.com/s）自动生成

**加群解锁板块（核心）**
- 用户在频道 @机器人「领资料」拿验证码 → 加入指定 QQ 群兑换 → 自动开通子频道权限
- 三重权限回收：退群事件实时回收 + 群成员名单对账 + 有效期兜底
- 多频道各配一套板块和兑换群，兑换群可配多个
- 验证码对话不进互转、自动撤回

**群管理**
- 关键词撤回 / 拉黑名单 / 成员黑名单
- 拉黑消息时可调用 AI（DeepSeek / 智谱 / 通义千问 / Kimi 等）拆分出候选关键词
- 群指令：订阅、状态、撤回、拉黑、绑定列表等

**可选扩展**
- **QQ 号识别**：外挂一个 NapCat 小号，自动获取群成员真实 QQ 号用于转发展示
- **帖子短链**：官方 CLI 扫码登录后生成 `pd.qq.com/s` 短链
- **Web 配置台**：全部设置可视化修改，支持按频道独立配置、一键测试

## 快速开始

### 1. 准备

- 在 [QQ 开放平台](https://q.qq.com) 创建机器人，拿到 `appid` / `secret`
- 机器人需要开通：群消息、频道论坛事件、私信等权限（缺什么启动日志会提示）
- 一台能访问外网的服务器（Python 3.10+）

### 2. 安装

```bash
git clone https://github.com/<你>/channel-butler.git /opt/channel-butler
cd /opt/channel-butler
python3 -m venv .venv
.venv/bin/pip install -r requirements-runtime.txt
```

### 3. 配置

```bash
cp config.yaml.example config.yaml
vim config.yaml    # 填 appid / secret，按注释说明按需调整
```

必须配置的只有 `appid` / `secret` 两项，其余全部有默认值或可在 Web 配置台改。

### 4. 运行

```bash
python -m forum_bot    # 前台试跑，确认日志无报错
```

生产环境用 systemd（模板在 `deploy/forum-bot.service`，按注释改路径后）：

```bash
sudo cp deploy/forum-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now forum-bot
journalctl -u forum-bot -n 50 -f    # 看启动日志
```

### 5. Web 配置台

启动后按日志提示打开配置页（默认 `http://127.0.0.1:8765`，仅本机）。
要公网访问：`host` 改 `0.0.0.0` 并**必须**设置访问口令（详见 `config.yaml.example`
内的安全说明）。

配置页里可以完成几乎所有日常运维：板块/兑换群/指令词、互转规则、拉黑词、
AI 服务、QQ 号识别、运行日志，全部可视化，支持按频道独立配置。

## 目录结构

```
├── bot.py                 主程序（事件分发与组装）
├── forwarder.py           帖子转发策略
├── relay.py               群消息互转
├── access.py              加群解锁：验证码 + 权限下发/回收
├── recall.py / blacklist.py  撤回与拉黑
├── onebot.py              QQ 号自动识别（NapCat 旁路，可选）
├── ai_split.py            拉黑时 AI 拆分候选关键词（可选）
├── config_web.py          Web 配置台
├── settings.py            设置模型（持久化 forum_forward_settings.json）
├── text.yaml / text.py    所有发出去的文案（可改）
└── botpy/                 botpy 源码副本（打了补丁）
```

运行时数据都在仓库根目录的 `forum_*.json`（已 gitignore）：设置、绑定表、
配额账本等，备份这些文件即备份全部状态。

## 常见问题

- **启动后收不到群消息**：开放平台需要开通「接收所有消息」全量模式，且申请对应权限
- **领码后兑换提示群不对**：兑换群的 openid 在群里 @机器人 发「我的ID」可查到；
  配置页有选择器直接勾选
- **板块授权失败（301003）**：板块要设为「指定成员可见」，且机器人要有管理权限
- **每小时一次的 4009 断连**：QQ 网关的会话有效期就是 1 小时，自动重连，不用管

更多细节见 `config.yaml.example` 的逐项注释和配置页内提示。

## 安全须知

- `config.yaml`（含 appid/secret）与 `forum_*.json`（运行数据）都已 gitignore，不要提交
- 配置页暴露公网时务必设置强口令；更好的做法是只监听本机 + SSH 隧道访问
- 使用 NapCat 等非官方框架有违反腾讯用户协议的风险，只用小号，自行评估

## License

[MIT](LICENSE)
