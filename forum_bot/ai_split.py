# -*- coding: utf-8 -*-
"""拉黑消息时用 AI 把内容**拆成候选关键词**（接国产大模型，默认 DeepSeek）。

为什么要有这个模块
==================
群里的「拉黑」是把**整条消息正文**原样塞进拉黑名单 —— 那条内容确实撤了，
但发送者换个说法（改个标点、换个词）就绕过去了。真正有用的是从正文里
**提炼出几条可复用的关键词**（广告话术、谐音、固定句式），挑着拉黑。

提炼这件事机器做不好（不懂"哪些部分是废话、哪些是刷屏的核心"），
人做又懒得逐条手工挑词 —— 所以交给 AI：拉黑一条消息时自动调
DeepSeek 把内容拆成几个候选短语，放到配置页「拉黑名单」页，
管理员**勾选**后真正进名单。人只做选择，不做提取。

API Key 在配置页「AI 服务」页（forum_forward_settings.json 的 ai_api_key）。
这些厂商的接口**都是 OpenAI 兼容的 /chat/completions**，所以换厂商只改
base_url / model 两个值 —— 厂商预设表在 PROVIDERS，配置页选一下就切换，
留空的模型名/地址自动用该厂商的默认值。

为什么后台跑
============
拆分要等几秒钟，而「拉黑」指令的回复（含撤回被引用消息）不该被它卡住。
所以群指令那边只**排队**（asyncio.create_task），结果写进候选文件，
群里不等、失败只记日志。

candidates 存哪
===============
forum_ai_suggest.json，结构：

    {"items": [{"id", "phrases": [...], "source", "by_name", "group_openid", "at"}]}

和拉黑名单分开：候选词**不是**拉黑词（AI 给的东西必须人看过才能生效），
混进 forum_forward_settings.json 会让人分不清哪些真的在撤回。
文件小、写得不频繁，每次现读现写（和 bindings 一个做法），
配置页线程和机器人线程两边都安全。
"""
import asyncio
import json
import os
import re
import threading
import time

import aiohttp

from botpy import logging

from . import config

__all__ = ["SuggestStore", "split_keywords", "ai_enabled"]

_log = logging.get_logger()

# 单次拆分的超时。AI 慢一点没关系（后台跑），但别让任务挂死占着事件循环
REQUEST_TIMEOUT = 20
# 最多让 AI 给几个短语、每个最长多少字 —— 防止它把整条消息原文又吐回来
MAX_PHRASES = 8
MAX_PHRASE_LEN = 60
# 候选最多攒多少批。超了丢最旧的：这是"待挑选"的收件箱，不是档案
_MAX_ITEMS = 30

_PROMPT = (
    "你在一个 QQ 群管理助手里。下面是一条被管理员拉黑的群消息正文。"
    "请把它拆分成最多 %d 个、适合做「消息包含即撤回」的关键词短语。"
    "要求：\n"
    "1. 只提取刷屏/广告/违规内容的核心片段（固定话术、专有名词、联系方式、谐音写法），"
    "不要选日常用语；\n"
    "2. 每个短语不超过 %d 个字，互相不包含；\n"
    "3. 只输出一个 JSON 字符串数组，不要解释、不要 Markdown 代码块。"
) % (MAX_PHRASES, MAX_PHRASE_LEN)


def ai_enabled(data):
    """开关开了、且 apikey 填了才算启用（缺一个都安静地不启用）。"""
    return bool(data.get("blacklist_ai_split")) and bool(
        str(data.get("ai_api_key") or "").strip()
    )


# 厂商预设。**接口全部 OpenAI 兼容**（/chat/completions），所以切换厂商
# 只是换 base_url + model 两个字符串，请求代码一行不用改。
# base_url/model 填 None 表示"没有默认值，用户必须自己填"（豆包的 model
# 实际是接入点 ID，每家账号不一样；custom 更是全靠手填）。
PROVIDERS = {
    "deepseek": {
        "label": "DeepSeek（深度求索）",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "key_site": "platform.deepseek.com",
    },
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "key_site": "open.bigmodel.cn",
    },
    "qwen": {
        "label": "通义千问（DashScope）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-turbo",
        "key_site": "bailian.console.aliyun.com",
    },
    "moonshot": {
        "label": "月之暗面 Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "key_site": "platform.moonshot.cn",
    },
    "qianfan": {
        "label": "百度千帆",
        "base_url": "https://qianfan.baidubce.com/v2",
        "model": "ernie-speed-8k",
        "key_site": "qianfan.baidubce.com",
    },
    "doubao": {
        "label": "豆包（火山方舟）",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "",
        "key_site": "console.volcengine.com/ark",
    },
    "minimax": {
        "label": "MiniMax",
        "base_url": "https://api.minimax.chat/v1",
        "model": "abab6.5s-chat",
        "key_site": "platform.minimaxi.com",
    },
    "custom": {
        "label": "自定义（OpenAI 兼容接口）",
        "base_url": "",
        "model": "",
        "key_site": "",
    },
}

DEFAULT_PROVIDER = "deepseek"


def resolve_config(data):
    """按设置算出实际要用的 (base_url, model)。

    用户填了就**以填的为准**（方便换型号 / 用中转地址）；
    留空回落到所选厂商的预设 —— 所以日常换厂商只需要动一个下拉框。
    """
    provider = str(data.get("ai_provider") or "").strip().lower()
    preset = PROVIDERS.get(provider) or PROVIDERS[DEFAULT_PROVIDER]
    base_url = str(data.get("ai_base_url") or "").strip() or preset["base_url"]
    model = str(data.get("ai_model") or "").strip() or preset["model"]
    return base_url, model


def _endpoint(base_url):
    """拼出 chat/completions 的完整地址，容忍几种常见填法。

    用户可能填 https://api.deepseek.com、带 /v1 的、甚至直接填到
    /chat/completions 的 —— 都归一成同一种。
    """
    url = str(base_url or "").strip() or "https://api.deepseek.com"
    url = url.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    return url + "/chat/completions"


def _extract_phrases(payload):
    """从接口返回里抠出短语数组。返回 (短语列表, 错误信息)。"""
    try:
        choice = (payload.get("choices") or [{}])[0]
        raw = str((choice.get("message") or {}).get("content") or "")
    except AttributeError:
        return [], "接口返回结构不对"
    if not raw.strip():
        return [], "接口返回了空内容"

    # 模型偶尔会拿 ```json ... ``` 包着 —— 剥掉再看
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
    except ValueError:
        return [], "返回的不是 JSON"
    # 允许 {"keywords": [...]} 和裸数组两种
    if isinstance(data, dict):
        data = data.get("keywords") or data.get("phrases") or []
    if not isinstance(data, list):
        return [], "返回的不是短语数组"

    phrases = []
    for item in data:
        if not isinstance(item, str):
            continue
        phrase = item.strip()
        # 太长的大概率是整句原文，不是关键词 —— 丢掉比截断好（和 normalize_qq 一个思路）
        if not phrase or len(phrase) > MAX_PHRASE_LEN:
            continue
        if phrase not in phrases:
            phrases.append(phrase)
        if len(phrases) >= MAX_PHRASES:
            break
    if not phrases:
        return [], "没拆出可用的短语"
    return phrases, ""


async def split_keywords(content, *, api_key, base_url="", model=""):
    """调 DeepSeek 把 content 拆成候选短语。返回 (短语列表, 错误信息)。

    错误信息是给人看的一句话（记日志/日志排查用），不出错就是空串。
    **任何失败都返回空列表** —— 拆分失败不能影响拉黑本身，拉黑已经生效了。
    """
    body = str(content or "").strip()
    if not body:
        return [], "消息内容是空的"
    if not str(api_key or "").strip():
        return [], "没有填 API Key"

    model_name = str(model or "").strip()
    if not model_name:
        # 走到这说明选了"没有默认模型"的厂商（豆包/自定义）却没填模型名。
        # 报错而不是悄悄换成别家的模型 —— 那种"以为在用 A 其实调的是 B"最难查
        return [], "没有指定模型名（选豆包/自定义时要在「AI 服务」页手工填）"
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": _PROMPT},
            {"role": "user", "content": body[:2000]},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    headers = {
        "Authorization": "Bearer " + str(api_key).strip(),
        "Content-Type": "application/json",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                _endpoint(base_url), json=payload, headers=headers
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    message = ""
                    if isinstance(data, dict):
                        message = str((data.get("error") or {}).get("message") or "")
                    return [], f"接口返回 {resp.status}：{message or '未知错误'}"
    except asyncio.TimeoutError:
        return [], f"接口超时（{REQUEST_TIMEOUT} 秒）"
    except Exception as error:
        return [], f"请求失败：{error}"
    return _extract_phrases(data if isinstance(data, dict) else {})


class SuggestStore:
    """AI 拆分出来的候选关键词，落盘在 forum_ai_suggest.json。

    读写两端：机器人线程（拉黑时写入）+ 配置页线程（读取/挑选/忽略）。
    每次操作现读现写 —— 文件很小，而"配置页刚清完、机器人又写回旧数据"
    是最难查的一类问题，不值得为它做缓存。
    """

    def __init__(self, path=None):
        self._path = path or getattr(config, "AI_SUGGEST_FILE", "") or ""
        self._lock = threading.Lock()

    # ---------------- 读写 ----------------
    def _read(self):
        try:
            with open(self._path, "r", encoding="utf-8") as file:
                data = json.load(file)
        except Exception:
            return []
        items = data.get("items") if isinstance(data, dict) else None
        return [item for item in items or [] if isinstance(item, dict) and item.get("phrases")]

    def _write(self, items):
        if not self._path:
            return
        try:
            directory = os.path.dirname(self._path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            temp = self._path + ".tmp"
            with open(temp, "w", encoding="utf-8") as file:
                json.dump({"items": items}, file, ensure_ascii=False, indent=2)
            os.replace(temp, self._path)
        except Exception as error:
            _log.warning(f"[botpy] AI 候选关键词写入失败: {error}")

    # ---------------- 写 ----------------
    def add(self, phrases, source="", by_name="", group_openid=""):
        """存一批候选，返回实际新加的短语数（和已有候选重复的不算）。"""
        phrases = [str(item or "").strip() for item in phrases or []]
        phrases = [item for item in phrases if item]
        if not phrases:
            return 0
        with self._lock:
            items = self._read()
            existing = {phrase for item in items for phrase in item.get("phrases") or []}
            fresh = []
            for phrase in phrases:
                if phrase in existing or phrase in fresh:
                    continue
                fresh.append(phrase)
            if not fresh:
                return 0
            items.insert(
                0,
                {
                    "id": time.strftime("%Y%m%d%H%M%S") + f"-{time.time_ns() % 10000:04d}",
                    "phrases": fresh,
                    "source": str(source or "")[:500],
                    "by_name": str(by_name or ""),
                    "group_openid": str(group_openid or ""),
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
            self._write(items[:_MAX_ITEMS])
        return len(fresh)

    def resolve(self, item_id, action="accept"):
        """处理一批候选：accept / dismiss 都把它从待挑选里拿掉。

        返回拿掉的那批（accept 方要用它的 phrases）。找不到返回 {}。
        """
        with self._lock:
            items = self._read()
            found = None
            rest = []
            for item in items:
                if found is None and str(item.get("id") or "") == str(item_id or ""):
                    found = item
                else:
                    rest.append(item)
            if found is None:
                return {}
            self._write(rest)
            return found

    def clear(self):
        """清空全部候选，返回清掉的批数。"""
        with self._lock:
            items = self._read()
            if items:
                self._write([])
            return len(items)

    # ---------------- 读 ----------------
    def entries(self):
        """待挑选的候选批（新的在前）。"""
        return self._read()
