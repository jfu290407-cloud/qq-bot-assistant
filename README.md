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

---

## 可选扩展：配置与使用

### 一、QQ 号自动识别（NapCat 旁路）

官方机器人拿不到群成员的真实 QQ 号（只有按机器人隔离的 `member_openid`）。
这个扩展用一个登录了 **NapCat**（非官方框架）的**小号**拉进群，拉取全量群成员的
真实 QQ 号，与机器人记录的成员名册**按昵称匹配**后自动写进绑定表，
转发消息时就能显示真实 QQ 号。

> ⚠ 非官方框架违反腾讯用户协议，小号有被冻结的风险——务必用小号；
> 自动匹配已刻意放低到 6 小时一次。拿到的 QQ 号只能用于展示，不能做权限判定。

**1. 部署 NapCat（服务器上，Docker）：**

```bash
mkdir -p /opt/napcat/config /opt/napcat/data
docker run -d --name napcat \
  -e ACCOUNT=小号QQ号 \
  -e WEBUI_TOKEN=一个长随机串 \
  -v /opt/napcat/config:/app/napcat/config \
  -v /opt/napcat/data:/app/napcat/data \
  -p 6099:6099 -p 3000:3000 \
  mlikiowa/napcat-docker:latest
```

浏览器打开 `http://服务器IP:6099`（WebUI，用 WEBUI_TOKEN 登录），
在「网络配置」里添加一个 **HTTP 服务器**：端口 `3000`，enable 开启。

**2. 机器人侧配置**（配置页「QQ 登录」页 + 「QQ 号识别」页）：

- 「QQ 登录」页点**「自动获取 token」**——会自动从容器配置/日志里找到
  WebUI token 并填好，不用翻日志
- 点**「扫码登录」**：如果小号之前登录过，直接点「快速登录」按钮（免扫码）；
  第一次用就扫二维码
- 「QQ 号识别」页：打开「启用 OneBot 连接」，地址填 `http://127.0.0.1:3000`
  （NapCat 同机部署时）
- **群映射**：一行一条把 QQ 群号和机器人群的 openid 对上——用页面上的
  **群映射选择器**左右点选即可，不用手抄 openid
- 点「测试 OneBot 连接」确认，再点「拉取并绑定」

匹配规则：群名片/昵称与机器人侧记录的昵称**精确相等**才算匹配，
同名多人会跳过并提示；匹配到的 QQ 号以 OneBot 为准。
打开「自动匹配」后每 6 小时自动跑一次。

掉线恢复：小号掉线不影响机器人主功能；到「QQ 登录」页点「检测」按提示
快速登录/扫码即可。

### 二、帖子短链（官方 CLI）

帖子转发到群里时，链接默认是长链模板。装了 **tencent-channel-cli** 并用
个人 QQ 扫码登录后，会自动生成 `pd.qq.com/s/xxx` 短链（好看也不易被截断）；
拿不到短链时自动退回长链，不影响转发。

**1. 服务器上安装 CLI**（参考 [tencent-channel-cli](https://github.com/tencent-connect/channel-cli) 官方仓库）：

```bash
npm install -g @tencent-connect/channel-cli
```

**2. 登录**：配置页「QQ 登录」页 →「频道凭证（官方 CLI）」→ 点「扫码登录」，
用**个人 QQ**（不是机器人）扫码。状态显示"已登录"即可。

**3. 开关**：配置页「过滤与文案」页，打开「把 {link} 换成帖子短链」。

失败排查：链接突然变长 = 凭证过期，重新扫码；systemd 部署报 HOME 错误的
情况见 `config.yaml.example` 里 `cli_home` 的注释。

### 三、Web 配置台

启动后自动运行，几乎所有日常运维都在这里改，改完即时生效。

**访问方式**（三选一）：

| 方式 | 配置 | 安全性 |
|---|---|---|
| 本机访问 | 默认 `127.0.0.1:8765`，无需口令 | 最高 |
| SSH 隧道 | 服务器保持默认，本机执行 `ssh -N -L 8765:127.0.0.1:8765 root@服务器IP`，浏览器开 `http://127.0.0.1:8765` | 高（推荐远程管理用） |
| 公网直接访问 | `host: "0.0.0.0"` + **必须设 password**（不设拒绝启动） | 一般，口令明文过网，务必用强口令 |

**按频道独立配置**：页面右上角「生效范围」切到某个频道（或「频道」页点
「编辑本频道设置」），此时改的字段只覆盖该频道，其余跟随全局；
标了"仅全局有效"的字段（互转、AI Key、指令词等）只在全局范围可改。

**主要页面**：

- **加群解锁**：板块 ID、兑换群（选择器勾选）、指令词、绑定表（按频道分组）
- **互转规则 / 过滤 / 成员绑定**：转发规则、过滤词、QQ 号绑定管理
- **拉黑名单**：拉黑词管理 + AI 拆分候选关键词勾选
- **AI 服务**：选厂商、填 Key，带「测试 AI 连接」按钮
- **QQ 登录 / QQ 号识别**：CLI 与 NapCat 的登录、测试、群映射
- **运行日志**：实时日志，排错首选

## License

[MIT](LICENSE)
