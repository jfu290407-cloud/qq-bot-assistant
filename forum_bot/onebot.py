# -*- coding: utf-8 -*-
"""QQ 号自动识别：通过 OneBot（NapCat 等非官方框架）旁路获取真实 QQ 号。

为什么要有这个模块
==================
官方平台只给 member_openid（按 appid 隔离、与真实 QQ 无关，见 bindings 模块
说明），自助绑定全靠用户自己报号。但任何一个**普通 QQ 账号**在群里本来就
能看到全群成员的真实 QQ 号 —— 所以用一个登录了 OneBot 框架（NapCat）的
小号拉进同一个群，拉取全量群成员列表，再和机器人这边「见过的群成员」
名册按**昵称**对上，QQ 号就自动补全了。

为什么用 HTTP 而不是 WebSocket
==============================
只需要「拉成员列表」这一个动作，不需要实时事件 —— HTTP 请求-响应就够。
好处是：配置页线程可以直接同步调用（不用现开事件循环）、机器人侧的
自动匹配也只是隔几小时发一次请求，没有长连接要维护、断了就下次再拉。

匹配规则（按优先级）
====================
  · 群名片（card）优先，没有群名片用昵称（nickname），**精确相等**才算匹配；
  · OneBot 侧同一个群里**同名多人** -> 冲突，跳过（宁缺勿错）；
  · 机器人侧多个 openid 同名 -> 同样冲突，跳过；
  · 只匹配「在官方侧发过消息」的人（SeenMembers 名册），不做全群导出。

来源与覆盖
==========
绑定的 source 标成 "onebot"。按用户的决定：**OneBot 匹配到的真实号
优先于用户自绑**（自绑可能填错）—— bind_qq 本来就支持覆盖更新。
管理员手工 relay_notes 是另一套数据，优先级仍然最高，不受影响。

风险（务必读完）
================
NapCat 这类非官方框架**违反腾讯用户协议**，小号有被冻结的风险：
  · 只用小号，绝不要把官方机器人或主人自己的号搭进来；
  · 控制频率：自动匹配默认 6 小时一次，成员列表不做轮询。
获取到的 QQ 号**只能用于转发展示**，绝不能拿去做权限判定
（和自助绑定同一个红线，见 bindings 模块开头）。
"""
import collections
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request

from botpy import logging

from .bindings import bind_qq

__all__ = [
    "OneBotError",
    "probe",
    "fetch_group_members",
    "parse_group_map",
    "match_and_bind",
]

_log = logging.get_logger()

# 探活/拉列表的超时。成员列表接口在千人群里可能要一两秒，给足余量
PROBE_TIMEOUT = 8
FETCH_TIMEOUT = 20

# 一次拉取的群数量上限：防手滑在映射里填几十个群把小号打爆
MAX_GROUPS_PER_SYNC = 20

_LOCK = threading.Lock()


class OneBotError(Exception):
    """OneBot 调用失败（网络/鉴权/业务错误），message 是给人看的一句话。"""


def _friendly_conn_error(error):
    """把底层连接类错误翻译成「下一步做什么」。

    错误文案跨平台差异很大（Linux 是英文 errno，Windows 是中文
    "WinError 10061 由于目标计算机积极拒绝"），所以两边的特征都要认。
    """
    detail = str(getattr(error, "reason", None) or error)
    if ("reset" in detail.lower() or "Errno 104" in detail
            or "10054" in detail or "重置" in detail or "强迫关闭" in detail):
        return (
            "连接被重置 —— NapCat 进程多半没在运行，"
            "先到服务器上执行 docker restart napcat，起来后再回来重试"
        )
    if ("refused" in detail.lower() or "Errno 111" in detail
            or "10061" in detail or "拒绝" in detail):
        return (
            "连接被拒绝 —— 端口上没有服务"
            "（检查 NapCat 是否启动、端口映射对不对）"
        )
    if "timed out" in detail.lower() or "10060" in detail or "超时" in detail:
        return "连接超时 —— NapCat 没响应，或地址/端口不通"
    return ""


def _call(base_url, action, token="", params=None, timeout=PROBE_TIMEOUT):
    """调一个 OneBot HTTP API，返回 data 段；失败抛 OneBotError。

    OneBot 11 的 HTTP 接口是 POST {url}/{action}，鉴权用 Bearer token。
    用标准库 urllib 同步调 —— 配置页线程和机器人的后台线程都能直接用。
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise OneBotError("没有填 OneBot 接口地址")
    url = f"{base}/{action}"
    body = json.dumps(params or {}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = str(token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise OneBotError(f"{action} HTTP {error.code}（地址/端口对吗？）") from error
    except urllib.error.URLError as error:
        hint = _friendly_conn_error(error)
        if hint:
            raise OneBotError(f"{action} {hint}") from error
        raise OneBotError(f"{action} 连不上：{error.reason}（NapCat 没启动？）") from error
    except Exception as error:
        hint = _friendly_conn_error(error)
        if hint:
            raise OneBotError(f"{action} {hint}") from error
        raise OneBotError(f"{action} 请求失败：{error}") from error

    if not isinstance(payload, dict):
        raise OneBotError(f"{action} 返回格式不对")
    retcode = payload.get("retcode")
    if retcode != 0:
        # 常见：token 错（鉴权失败）、群号不在小号所在群
        hint = str(payload.get("wording") or payload.get("status") or "")
        raise OneBotError(f"{action} 失败：retcode={retcode} {hint}".strip())
    return payload.get("data")


def probe(base_url, token=""):
    """测连接：拿小号自己的登录信息。返回 ({"user_id","nickname"}, 错误信息)。"""
    try:
        data = _call(base_url, "get_login_info", token)
    except OneBotError as error:
        return {}, str(error)
    if not isinstance(data, dict) or not data.get("user_id"):
        return {}, "返回里没有登录信息"
    return data, ""


# ----------------------------------------------------------------------
# NapCat WebUI：登录二维码代理（新旧两版接口都兼容）
# ----------------------------------------------------------------------
# ⚠ 这些是 WebUI 的**非官方**接口，而且 4.x 起整个换过一轮（对照官方源码
#   napcat-webui-backend 确认过）：
#   旧版：POST /api/auth/login {"token": 原文}          -> data.Authorization
#         GET  /api/QrCode                              -> PNG 图片
#   新版：POST /api/auth/login {"hash": SHA256(token+".napcat")} -> data.Credential
#         POST /api/QQLogin/GetQQLoginQrcode            -> data.qrcode（URL！
#            前端要自己把 URL 渲染成二维码图）
#         还有快速登录：GetQuickLoginListNew / SetQuickLogin ——
#         掉线重连根本不用扫码，直接把之前登录过的号拉起来。
# 登录成功与否**不靠 WebUI 判断**：配置页轮询 probe()，OneBot HTTP 服务
# 一旦起来了就说明小号登录完成。


class _AuthRejected(Exception):
    """本次登录请求被服务端明确拒绝（token 不对），不是网络/版本问题。"""


def _webui_post_json(base, path, body, timeout, credential=""):
    """POST 一个 WebUI 接口并解析 JSON。网络错误抛 OneBotError，
    401/403 抛 _AuthRejected（交给上层换下一种登录格式重试）。"""
    headers = {"Content-Type": "application/json"}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body or {}).encode("utf-8"),
        headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise _AuthRejected() from error
        raise OneBotError(f"WebUI {path} HTTP {error.code}（地址对吗？）") from error
    except urllib.error.URLError as error:
        hint = _friendly_conn_error(error)
        if hint:
            raise OneBotError(f"WebUI {hint}") from error
        raise OneBotError(f"WebUI 连不上：{error.reason}（NapCat 没启动？）") from error
    except OneBotError:
        raise
    except Exception as error:
        hint = _friendly_conn_error(error)
        if hint:
            raise OneBotError(f"WebUI {hint}") from error
        raise OneBotError(f"WebUI 请求失败：{error}") from error
    try:
        payload = json.loads(raw)
    except Exception:
        raise OneBotError(f"WebUI {path} 返回的不是 JSON（NapCat 版本变了？）")
    return payload if isinstance(payload, dict) else {}


def _data_of(payload):
    """取响应的 data 段；不是 dict 就回退到顶层（字段位置随版本变过）。"""
    data = (payload or {}).get("data")
    return data if isinstance(data, dict) else (payload or {})


def _pick_ci(data, name):
    """大小写不敏感地取一个字段。"""
    for key, value in (data or {}).items():
        if str(key).lower() == name and str(value or "").strip():
            return str(value).strip()
    return ""


def _webui_credential(base, token, timeout):
    """登录 WebUI 拿凭证。返回 (credential, 版本)。两种格式依次试。"""
    # 新版（4.x+）：hash = SHA256(token + ".napcat")，成功返回 data.Credential
    try:
        payload = _webui_post_json(
            base, "/api/auth/login",
            {"hash": hashlib.sha256((token + ".napcat").encode("utf-8")).hexdigest()},
            timeout,
        )
    except _AuthRejected:
        payload = {}
    credential = _pick_ci(_data_of(payload), "credential")
    if credential:
        return credential, "new"

    # 旧版：原文 token，成功返回 data.Authorization
    try:
        payload = _webui_post_json(
            base, "/api/auth/login", {"token": token}, timeout
        )
    except _AuthRejected:
        payload = {}
    credential = _pick_ci(_data_of(payload), "authorization")
    if credential:
        return credential, "old"

    raise OneBotError(
        "WebUI 登录失败：token 不对（正确 token 在 NapCat 容器日志里，"
        "docker logs napcat 2>&1 | grep \"WebUi Token\" 能找到；"
        "填到「QQ 号识别」页保存后再试。"
        "注意：连续失败会触发 NapCat 的登录频率限制，等几分钟再试）"
    )


def webui_ticket(base_url, token="", timeout=PROBE_TIMEOUT):
    """用 WebUI token 换会话凭证（Authorization 头的值）。"""
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise OneBotError("没有填 NapCat WebUI 地址")
    token = str(token or "").strip()
    if not token:
        raise OneBotError("没有填 WebUI token（NapCat 容器日志里有）")
    credential, _version = _webui_credential(base, token, timeout)
    return credential


def webui_probe(base_url, token=""):
    """探 WebUI。返回 (状态, 说明)：状态是 ok / auth（token 不对）/ down。"""
    try:
        webui_ticket(base_url, token)
    except OneBotError as error:
        message = str(error)
        state = "auth" if ("token" in message) else "down"
        return state, message
    return "ok", "WebUI 正常"


def webui_quick_accounts(base_url, token="", timeout=PROBE_TIMEOUT):
    """新版：之前在这台 NapCat 上登录过的 QQ 列表（可一键快速重登）。

    返回 (账号列表, 凭证, 错误)。旧版没有这套接口，返回空列表不算错。
    """
    base = str(base_url or "").strip().rstrip("/")
    try:
        credential, version = _webui_credential(base, str(token or "").strip(), timeout)
    except OneBotError as error:
        return [], "", str(error)
    if version != "new":
        return [], credential, ""
    try:
        payload = _webui_post_json(
            base, "/api/QQLogin/GetQuickLoginListNew", {}, timeout, credential
        )
    except OneBotError as error:
        return [], credential, str(error)
    data = _data_of(payload)
    items = data.get("list") if isinstance(data.get("list"), list) else (
        data if isinstance(data, list) else []
    )
    accounts = []
    for item in items or []:
        if isinstance(item, dict) and str(item.get("uin") or ""):
            accounts.append({
                "uin": str(item.get("uin")),
                "nick": str(item.get("nick") or ""),
            })
    return accounts, credential, ""


def webui_quick_login(base_url, token, uin, timeout=PROBE_TIMEOUT):
    """新版：快速登录之前用过的 QQ（掉线重连不用扫码）。"""
    base = str(base_url or "").strip().rstrip("/")
    credential, version = _webui_credential(base, str(token or "").strip(), timeout)
    if version != "new":
        return "当前 NapCat 版本没有快速登录接口，请用扫码登录"
    payload = _webui_post_json(
        base, "/api/QQLogin/SetQuickLogin",
        # 字段名没有文档，两个常见的都带上，服务端各取所需
        {"uin": str(uin), "qq": str(uin)}, timeout, credential
    )
    message = str(payload.get("message") or "")
    if str(payload.get("code")) not in ("0", "None"):
        return f"快速登录请求被拒：{message or '未知原因'}"
    return ""


def webui_qrcode(base_url, token="", timeout=PROBE_TIMEOUT):
    """拿登录二维码。返回 (kind, 数据, 错误)。

    kind="png"：数据是 PNG 字节（旧版，前端直接 <img>）
    kind="url"：数据是二维码内容 URL（新版，前端渲染成二维码图）
    """
    base = str(base_url or "").strip().rstrip("/")
    try:
        credential, version = _webui_credential(
            base, str(token or "").strip(), timeout
        )
    except OneBotError as error:
        return "", "", str(error)

    if version == "new":
        # NapCat 运行时里存的可能是**上一次登录时**的旧二维码（扫了必过期），
        # 所以先强制刷新拿一张新的；刷新失败（比如正在重连）再退回直接取
        try:
            fresh = _webui_post_json(
                base, "/api/QQLogin/RefreshQRcode", {}, timeout, credential
            )
            fresh_data = _data_of(fresh)
            # restarting=true 表示 NapCat 正在重连，此刻还没有新码可拿
            if (not fresh_data.get("restarting")
                    and str(fresh_data.get("qrcodeurl") or "")):
                return "url", str(fresh_data["qrcodeurl"]), ""
        except OneBotError:
            pass
        try:
            payload = _webui_post_json(
                base, "/api/QQLogin/GetQQLoginQrcode", {}, timeout, credential
            )
        except OneBotError as error:
            return "", "", str(error)
        message = str(payload.get("message") or "")
        if "logined" in message.lower():
            return "", "", "小号已经登录了，点「检测」确认即可"
        url = str(_data_of(payload).get("qrcode") or "")
        if url:
            return "url", url, ""
        return "", "", f"拿二维码失败：{message or '响应里没有 qrcode'}"

    # 旧版：GET /api/QrCode -> PNG
    request = urllib.request.Request(
        base + "/api/QrCode", headers={"Authorization": credential}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = resp.read()
    except urllib.error.HTTPError as error:
        return "", "", f"拿二维码 HTTP {error.code}（小号可能已经登录了，点「检测」看看）"
    except Exception as error:
        hint = _friendly_conn_error(error)
        return "", "", f"拿二维码失败：{hint or error}"
    if not data.startswith(b"\x89PNG"):
        return "", "", "返回的不是二维码图片（NapCat 版本变了？去 WebUI 手动扫码）"
    return "png", data, ""


# ---------------- 自动获取 WebUI token ----------------
# token 藏在三处，都能**不进 WebUI、不翻服务器**拿到（机器人和 NapCat
# 部署在同一台机器上，直接用 docker 命令 / 读文件）：
#   1. 容器里的 config/webui.json —— token 持久化在这，重启也不变（最可靠）
#   2. 容器启动日志里的「WebUi Token: xxx」
#   3. 宿主机上挂载出来的配置文件（README 部署方式挂在 /opt/napcat/config）
# 配置页一键把三路都试一遍，拿到就自动填进设置。

_WEBUI_TOKEN_RE = re.compile(r"WebUi Token:\s*([0-9a-fA-F\-]{6,})")
_WEBUI_URL_TOKEN_RE = re.compile(r"webui\?token=([0-9a-fA-F\-]{6,})")


def _run_cmd(args, timeout=12):
    """跑一条 shell 命令。docker 不存在/没权限时安静地返回失败。"""
    try:
        import subprocess

        result = subprocess.run(
            args, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        return result.returncode == 0, result.stdout or ""
    except Exception:
        return False, ""


def _napcat_container_name():
    """从 docker ps 里认出 NapCat 容器名（认不认得出都退回 napcat）。"""
    ok, out = _run_cmd(["docker", "ps", "--format", "{{.Names}}"])
    if ok:
        for line in out.splitlines():
            name = line.strip()
            if name and "napcat" in name.lower():
                return name
    return "napcat"


def _token_from_webui_json(text):
    try:
        data = json.loads(text)
    except Exception:
        return ""
    return str((data or {}).get("token") or "").strip()


def autofetch_token():
    """自动找 WebUI token。返回 (token, 来源说明, 错误信息)。"""
    name = _napcat_container_name()

    # 1) 容器里的 webui.json
    ok, out = _run_cmd(
        ["docker", "exec", name, "cat", "/app/napcat/config/webui.json"]
    )
    if ok:
        token = _token_from_webui_json(out)
        if token:
            return token, f"容器 {name} 的 config/webui.json", ""

    # 2) 启动日志。**必须用带 [WebUi] 前缀的精确模式** ——
    #    日志里转发群聊也全是 "token" 字样，宽松匹配会抄到聊天内容
    ok, out = _run_cmd(["docker", "logs", "--tail", "500", name])
    if ok:
        matches = _WEBUI_TOKEN_RE.findall(out) or _WEBUI_URL_TOKEN_RE.findall(out)
        if matches:
            # 取最后一次出现的（最接近当前状态）
            return matches[-1], f"容器 {name} 的启动日志", ""

    # 3) 宿主机挂载文件
    for path in ("/opt/napcat/config/webui.json",
                 "/root/napcat/config/webui.json",
                 "napcat/config/webui.json"):
        try:
            with open(path, "r", encoding="utf-8") as file:
                token = _token_from_webui_json(file.read())
        except Exception:
            continue
        if token:
            return token, f"宿主机文件 {path}", ""

    return "", "", (
        "三处都没找到 token（docker exec / 启动日志 / 宿主机配置文件）。"
        "常见原因：运行机器人的账号没有 docker 权限，或容器名不含 napcat。"
        "手动兜底：docker logs napcat 2>&1 | grep \"WebUi Token\""
    )


def fetch_group_members(base_url, token, group_id):
    """拉一个 QQ 群的全量成员。返回 (成员列表, 错误信息)。

    每个成员：{"user_id": 真实QQ号, "nickname": 昵称, "card": 群名片}
    """
    try:
        data = _call(
            base_url,
            "get_group_member_list",
            token,
            params={"group_id": int(str(group_id).strip())},
            timeout=FETCH_TIMEOUT,
        )
    except (ValueError, TypeError):
        return [], f"QQ 群号不合法：{group_id!r}"
    except OneBotError as error:
        return [], str(error)
    if not isinstance(data, list):
        return [], "返回的成员列表格式不对"
    result = []
    for item in data:
        if not isinstance(item, dict) or not item.get("user_id"):
            continue
        result.append(
            {
                "qq": str(item["user_id"]),
                "nickname": str(item.get("nickname") or "").strip(),
                "card": str(item.get("card") or "").strip(),
            }
        )
    return result, ""


def fetch_group_list(base_url, token):
    """拉小号所在的**全部 QQ 群**。返回 (群列表, 错误信息)。

    每个群：{"qq": 群号, "name": 群名}。给「群映射选择器」的左列用 ——
    不用管理员手抄群号，点一下就知道小号在哪些群里。
    """
    try:
        data = _call(base_url, "get_group_list", token, timeout=FETCH_TIMEOUT)
    except OneBotError as error:
        return [], str(error)
    if not isinstance(data, list):
        return [], "返回的群列表格式不对"
    result = []
    for item in data:
        if not isinstance(item, dict) or not item.get("group_id"):
            continue
        result.append(
            {
                "qq": str(item["group_id"]),
                "name": str(item.get("group_name") or "").strip(),
            }
        )
    result.sort(key=lambda item: item["qq"])
    return result, ""


def fetch_history(base_url, token, group_id, count=50):
    """拉一个 QQ 群的**最近消息历史**。返回 (消息列表, 错误信息)。

    每条：{"message_id", "time": 发送时刻(秒), "qq": 发送者真实QQ号, "text": 归一化正文}。
    给「实时识别」用：官方机器人收到消息时，到这里把同一条消息找出来，
    发送者是谁就确定了 —— 昵称同名、改名片都拦不住它。
    """
    try:
        data = _call(
            base_url,
            "get_group_msg_history",
            token,
            params={"group_id": int(str(group_id).strip()), "count": int(count)},
            timeout=FETCH_TIMEOUT,
        )
    except (ValueError, TypeError):
        return [], f"QQ 群号不合法：{group_id!r}"
    except OneBotError as error:
        return [], str(error)
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list):
        return [], "返回的消息列表格式不对"

    result = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        sender = item.get("sender") or {}
        qq = str((sender or {}).get("user_id") or "")
        if not qq:
            continue
        raw = str(item.get("raw_message") or "")
        result.append(
            {
                "message_id": str(item.get("message_id") or ""),
                "time": float(item.get("time") or 0),
                "qq": qq,
                "text": normalize_content(raw),
            }
        )
    result.sort(key=lambda item: item["time"])  # 统一成时间升序，FIFO 配对才稳定
    return result, ""


_CQ_RE = re.compile(r"\[CQ:[^\]]*\]")            # OneBot 的 CQ 码（[CQ:face,id=..] 等）
_OFFICIAL_MARK_RE = re.compile(r"<faceType=[^>]*/?>|<img[^>]*/?>")  # 官方侧的表情/图片标记


def normalize_content(text):
    """把两边的消息正文归一化成"可比较的纯文本"。

    官方事件里表情是 <faceType=...>、图片在 attachments 里（content 常为空）；
    OneBot 里是 [CQ:face,id=..]、[CQ:image,...]。直接比字符串永远对不上。
    统一删掉这些标记、压掉空白后比剩下的纯文字 —— 纯图/纯表情消息归一化后
    是空串，匹配时跳过（这类消息认不出发送者，宁缺勿错）。
    """
    text = _CQ_RE.sub("", str(text or ""))
    text = _OFFICIAL_MARK_RE.sub("", text)
    return " ".join(text.split()).strip()


class LiveMatcher:
    """消息级实时匹配器：缓存 NapCat 消息流，按内容+时间把官方消息配对到真实 QQ。

    工作方式
    --------
    1. refresh()：按群拉最近一页消息历史，去重后进缓冲（每个映射的群独立）；
    2. resolve()：官方侧来一条消息 -> 归一化正文 -> 到缓冲里找
       「同群 + 内容一致 + 时间相近 + 未被消费」的最早一条 -> 返回发送者 QQ 并标记消费。

    为什么标记"消费"：群里短时间多条相同内容（刷"哈哈哈"）时，
    不消费的话永远配到同一条上；FIFO 消费让配对在统计上是对的。

    为什么用轮询不用 WebSocket：OneBot HTTP 已经打通了，
    每个映射的群每 15 秒拉一次历史，断线自愈、不需要维护长连接。
    """

    # 缓冲里消息的保留时长（秒）。超过就丢 —— 匹配窗口远小于这个值
    _TTL = 600
    # 官方消息和 NapCat 消息的时间差容忍。两边时钟/推送延迟，给到 120 秒
    _WINDOW = 120
    # 每个群最少隔多久刷新一次（秒）。调用方传多密都会被压到这个间隔之上
    _REFRESH_MIN_INTERVAL = 15
    # 每群缓冲最多存多少条
    _MAX_BUFFER = 200

    def __init__(self):
        self._buffers = {}       # group_openid -> {"entries": deque, "last_refresh": ts}
        self._group_locks = {}   # group_openid -> 拉取锁（同群串行，防重复拉）
        self._lock = threading.Lock()

    def _lock_for(self, group_openid):
        with self._lock:
            return self._group_locks.setdefault(str(group_openid), threading.Lock())

    def stale(self, group_openid, now=None):
        """这个群的缓冲是不是该刷新了（首次必刷，之后按最小间隔节流）。"""
        now = now if now is not None else time.time()
        state = self._buffers.get(str(group_openid))
        if not state:
            return True
        return now - state["last_refresh"] >= self._REFRESH_MIN_INTERVAL

    def refresh(self, group_openid, base_url, token, qq_group_id, now=None):
        """拉一页最新消息并入缓冲。返回 (新入缓冲条数, 错误信息)。

        **同群串行**（群锁）：同一群的两条消息接连到达时，第二条会等
        第一条拉完再拉自己的 —— 第二次拉取的历史里包含第二条消息，
        这是配对能成立的关键。不同群互不阻塞。
        """
        with self._lock_for(group_openid):
            entries, error = fetch_history(base_url, token, qq_group_id)
            if error:
                return 0, error
            now = now if now is not None else time.time()
            with self._lock:
                state = self._buffers.setdefault(
                    str(group_openid),
                    {"entries": collections.deque(), "last_refresh": 0.0},
                )
                state["last_refresh"] = now
                known = {item["message_id"] for item in state["entries"]}
                fresh = 0
                for item in entries:
                    if not item["message_id"] or item["message_id"] in known:
                        continue
                    if not item["text"]:
                        continue  # 纯图/纯表情：配不了对，不进缓冲
                    item["consumed"] = False
                    item["at"] = now
                    state["entries"].append(item)
                    known.add(item["message_id"])
                    fresh += 1
                # 修剪：超量的丢最旧的
                while len(state["entries"]) > self._MAX_BUFFER:
                    state["entries"].popleft()
                # 过期的丢掉
                self._expire(state, now)
            return fresh, ""

    @staticmethod
    def _expire(state, now):
        entries = state["entries"]
        while entries and now - float(entries[0]["time"]) > LiveMatcher._TTL:
            entries.popleft()

    def resolve(self, group_openid, content, now=None):
        """给官方侧的一条消息找发送者的真实 QQ。返回 (QQ 或 "", 是否找到)。

        content 传**归一化后**的正文。找不到（没进过缓冲/已消费/内容为空）返回 ("", False)。
        """
        text = str(content or "").strip()
        if not text:
            return "", False
        now = now if now is not None else time.time()
        with self._lock:
            state = self._buffers.get(str(group_openid))
            if not state:
                return "", False
            self._expire(state, now)
            for item in state["entries"]:
                if item.get("consumed"):
                    continue
                if item["text"] != text:
                    continue
                if abs(now - float(item["time"])) > self._WINDOW:
                    continue
                item["consumed"] = True
                return item["qq"], True
        return "", False


def parse_group_map(raw):
    """解析「QQ群号=group_openid」映射，返回 ([(群号, openid)], 问题列表)。

    两种顺序都认 —— 等号哪边是 32 位 openid 一眼就能看出来，
    不逼用户记住固定写法。
    """
    groups = []
    issues = []
    for line_no, line in enumerate(str(raw or "").splitlines(), start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        left, sep, right = text.partition("=")
        if not sep:
            issues.append(f"第 {line_no} 行缺等号：{text[:40]}")
            continue
        left, right = left.strip(), right.strip()
        openid = qq_id = ""
        for a, b in ((left, right), (right, left)):
            if len(a) == 32 and all(c in "0123456789ABCDEFabcdef" for c in a):
                openid = a
                if b.isdigit() and 5 <= len(b) <= 11:
                    qq_id = b
                    break
        if not openid or not qq_id:
            issues.append(f"第 {line_no} 行认不出来（要 openid=QQ群号）：{text[:40]}")
            continue
        groups.append((qq_id, openid))
    # 同一个 openid 配了多个 QQ 群？取第一个，多的当问题报出来
    seen_openids = set()
    deduped = []
    for qq_id, openid in groups:
        if openid in seen_openids:
            issues.append(f"群 {openid[:8]}… 配了多个 QQ 群号，只用第一个（{qq_id}）")
            continue
        seen_openids.add(openid)
        deduped.append((qq_id, openid))
    return deduped[:MAX_GROUPS_PER_SYNC], issues


def match_and_bind(members, base_url, token="", group_map_raw="", path=None):
    """拉全量群成员 -> 按昵称匹配 -> 自动绑定。返回给配置页看的统计 dict。

    members 是 SeenMembers 实例（机器人侧的名册：openid -> username + 所在群）。
    全程同步阻塞（每个群一次 HTTP），调用方自己决定放哪个线程。
    """
    with _LOCK:
        # 串行化：自动匹配和页面按钮同时触发也没关系，排队跑
        return _match_and_bind_locked(members, base_url, token, group_map_raw, path)


def _match_and_bind_locked(members, base_url, token, group_map_raw, path):
    groups, issues = parse_group_map(group_map_raw)
    if not groups:
        return {
            "ok": False,
            "msg": "没有可用的群映射：在下面按「QQ群号=group_openid」一行一条填",
            "issues": issues,
            "bound": 0,
        }
    if not str(base_url or "").strip():
        return {"ok": False, "msg": "没有填 OneBot 接口地址", "issues": issues, "bound": 0}

    # 机器人侧名册：openid -> (username, 所在群集合)
    roster = {}
    for entry in members.all():
        username = str(entry.get("username") or "").strip()
        if username:
            roster[str(entry["openid"])] = (
                username,
                set(entry.get("groups") or []),
            )
    if not roster:
        return {
            "ok": False,
            "msg": "机器人还没见过任何群成员（群里有人发过消息后再来）",
            "issues": issues,
            "bound": 0,
        }

    # 同名 openid 检测：机器人侧两个人同名时，匹配到这个名字没法确定给谁
    name_to_openids = {}
    for openid, (username, _) in roster.items():
        name_to_openids.setdefault(username, []).append(openid)

    bound = updated = same = unmatched = conflicts = 0
    for qq_group_id, group_openid in groups:
        onebot_members, error = fetch_group_members(base_url, token, qq_group_id)
        if error:
            issues.append(f"群 {qq_group_id}: {error}")
            continue

        # OneBot 侧：显示名（群名片优先）-> QQ 号集合
        name_to_qqs = {}
        for item in onebot_members:
            display = item["card"] or item["nickname"]
            if display:
                name_to_qqs.setdefault(display, set()).add(item["qq"])

        for openid, (username, seen_groups) in roster.items():
            # 只匹配他在**这个映射的群**里出现过的情况，
            # 避免 A 群的聊天记录给 B 群的人配上号
            if group_openid not in seen_groups:
                continue
            qqs = name_to_qqs.get(username)
            if not qqs:
                unmatched += 1
                continue
            if len(qqs) > 1 or len(name_to_openids.get(username) or []) > 1:
                conflicts += 1
                issues.append(
                    f"群 {qq_group_id}: 「{username}」同名多人，跳过（需要手工绑）"
                )
                continue
            qq = next(iter(qqs))
            state, _record = bind_qq(
                openid,
                qq,
                nick=username,
                group_openid=group_openid,
                path=path,
                source="onebot",
            )
            if state == "ok":
                bound += 1
            elif state == "update":
                updated += 1
            elif state == "same":
                same += 1

    _log.info(
        f"[botpy] OneBot 匹配完成：新绑 {bound}、纠正 {updated}、"
        f"无变化 {same}、未匹配 {unmatched}、冲突 {conflicts}"
    )
    parts = []
    if bound:
        parts.append(f"新绑定 {bound} 人")
    if updated:
        parts.append(f"纠正 {updated} 人（以 OneBot 真实号为准）")
    if same:
        parts.append(f"{same} 人无变化")
    if unmatched:
        parts.append(f"{unmatched} 人次没匹配上（改了群名片？下次说话会再试）")
    if conflicts:
        parts.append(f"{conflicts} 人次同名冲突已跳过")
    msg = "；".join(parts) if parts else "没有任何可匹配的成员"
    return {
        "ok": True,
        "msg": msg,
        "issues": issues,
        "bound": bound + updated,
    }
