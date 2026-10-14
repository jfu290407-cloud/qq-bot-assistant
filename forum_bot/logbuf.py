# -*- coding: utf-8 -*-
"""内存日志环形缓冲：把机器人跑起来之后的输出留一份，供配置页查看。

为什么需要它
============
日志目前只去两个地方：控制台（journalctl）和 botpy.log 文件。想在浏览器里看，
只能 SSH 上去 tail —— 而这套东西的主要用户是在群里管机器人的群主，
不会天天开终端。留一份在内存里，配置页轮询一下就能看。

设计取舍
========
· **只留最近 N 条**（默认 800）。日志是无界流，留全量等于把内存跑爆；
  排查现场要的是"刚刚发生了什么"，不是三个月前的。
· **写用锁、读用锁拿快照**。写来自机器人事件循环和其它线程，读来自配置页线程，
  两边同时在跑，不加锁会在 deque 扩容时读到半截。
· **异常也记**。`exc_info` 会被格式化成文本附在正文后面 —— 抓 botpy 里的
  Traceback 正是这个页面最主要的用途。
· **handler 挂一次**。同时挂到 root 和 `botpy` 两个 logger 上，靠给 record
  打标记去重：`botpy` 的 record 会 propagate 到 root，不标记就会记两遍。
· **install() 幂等且可重复调用**。万一哪次被第三方库重置了 handlers，
  配置页每次拉日志时顺手补挂一次就自愈了。

线程安全
========
`deque` 的 append 本身是原子的，但"读快照"要跨多条记录保持一致，
所以读写都在同一把锁里 —— 这个量级（每秒几条到几十条）完全不是瓶颈。
"""
import collections
import logging
import threading
import time
import traceback

__all__ = ["LogBuffer", "buffer", "install", "LEVELS"]

# 默认保留条数。800 条大约能覆盖"最近几十分钟"的正常运行，
# 出问题时的爆发期也够回溯到起因。
DEFAULT_CAPACITY = 800

# 单次最多返回多少条给页面。轮询间隔 2 秒，正常远到不了这个数；
# 设上限是防止"页面刚打开 + 日志正好爆发"时一次塞几 MB 进浏览器。
MAX_PAGE = 300

LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# 给 record 打标记用的属性名。`botpy` 的 record 会 propagate 到 root，
# 两个 logger 上各挂了一个 handler，不标记就会重复记一条。
_SEEN = "_forum_bot_logbuf_seen"

_INSTALL_LOCK = threading.Lock()
_BUFFER = None


def _format_exception(exc_info):
    """把 exc_info 格式化成文本（含 Traceback）。

    **不能用 `self.formatException()`** —— `logging.Handler` 上的这个方法
    在 Python **3.14** 被移除了（3.14 上实测 AttributeError，而 `logging.Formatter`
    上的同名方法还在）。服务器是 3.11、开发机是 3.14，两边都得能跑，
    所以直接用 `traceback.format_exception` —— 它从 3.6 到 3.14 签名都兼容。
    """
    if not exc_info:
        return ""
    try:
        chunk = "".join(traceback.format_exception(*exc_info))
    except Exception:
        return "<异常信息格式化失败>"
    return chunk.rstrip("\n")


def _timestamp(created):
    """把 epoch 秒格式化成「本地时间」。存文本而不是让页面自己算 ——
    服务器的时区由服务器决定，浏览器在另一个时区，让浏览器算会串。"""
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(created))
    except Exception:
        return ""


class LogBuffer(logging.Handler):
    """把日志记录留在内存里的 handler。"""

    def __init__(self, capacity=DEFAULT_CAPACITY):
        logging.Handler.__init__(self)
        # 自己不设门槛：logger 已经按 level 过滤过一遍了，
        # 这里再拦一次只会让"把 logger 调到 DEBUG"变得没意义。
        self.setLevel(logging.DEBUG)
        self._items = collections.deque(maxlen=max(50, int(capacity)))
        self._lock = threading.Lock()
        self._seq = 0
        self._dropped = 0

    # ---------------- handler 接口 ----------------
    def emit(self, record):
        if getattr(record, _SEEN, False):
            return
        setattr(record, _SEEN, True)

        try:
            message = record.getMessage()
        except Exception:
            message = ""
        if record.exc_info:
            # 异常要连堆栈一起留下：抓 botpy 里的 Traceback 正是这个页面最主要的用途
            stack = _format_exception(record.exc_info)
            if stack:
                message += "\n" + stack

        entry = {
            "seq": 0,
            "ts": float(getattr(record, "created", 0) or 0),
            "time": _timestamp(getattr(record, "created", 0) or 0),
            "level": str(record.levelname or ""),
            "logger": str(record.name or ""),
            "source": "%s:%s" % (record.filename or "?", record.lineno or 0),
            "func": str(record.funcName or ""),
            "text": message,
        }

        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            # deque 满了会自动丢最老的；记一下丢了多少，页面上好解释
            # "为什么看不到更早的"
            if self._items.maxlen and len(self._items) >= self._items.maxlen:
                self._dropped += 1
            self._items.append(entry)

    # ---------------- 给配置页读 ----------------
    def since(self, seq=0, level="", limit=MAX_PAGE):
        """取 seq 之后的条目，返回 (entries, cursor, total)。

        seq <= 0 表示"从头开始"，这时只返回**最后 limit 条** ——
        页面第一次打开不该把 800 条全拉过去。
        level 是「最低级别」，空字符串表示不过滤。
        """
        minimum = LEVELS.get(str(level or "").upper(), 0)
        try:
            limit = max(1, min(int(limit or MAX_PAGE), self._items.maxlen or MAX_PAGE))
        except (TypeError, ValueError):
            limit = MAX_PAGE

        with self._lock:
            total = self._seq
            items = list(self._items)

        if seq and seq > 0:
            items = [item for item in items if item["seq"] > seq]
            # 增量拉取时如果积压太多，只给最新的 limit 条（页面按 seq 续拉）
            if len(items) > limit:
                items = items[-limit:]
        else:
            items = items[-limit:]

        if minimum:
            items = [
                item for item in items
                if LEVELS.get(item["level"].upper(), 0) >= minimum
            ]
        return items, total, len(self._items)

    def tail(self, count=50, level=""):
        """只要最后几条（不关心 seq）。"""
        entries, _, _ = self.since(0, level, count)
        return entries

    def stats(self):
        with self._lock:
            return {
                "total": self._seq,
                "kept": len(self._items),
                "capacity": self._items.maxlen or 0,
                "dropped": self._dropped,
            }

    def clear(self):
        """清空并返回当前的 seq。

        返回 seq 是给页面用的：清空后把游标设成它，下一条新日志才会被当成"新"，
        否则页面会拿着 seq=0 反复拉全量。
        """
        with self._lock:
            self._items.clear()
            self._dropped = 0
            return self._seq


def buffer():
    """取全局缓冲（没有就建一个）。"""
    global _BUFFER
    if _BUFFER is None:
        with _INSTALL_LOCK:
            if _BUFFER is None:
                _BUFFER = LogBuffer()
    return _BUFFER


def install():
    """把缓冲挂到日志上（幂等，可重复调用）。返回缓冲对象。

    **同时挂 root 和 `botpy` 两个 logger**，因为两边都可能是"唯一"的出口：
      · 只挂 root：万一 botpy 把 `propagate` 关了，一条都收不到；
      · 只挂 botpy：我们自己的模块（都用 botpy 的 logger）能收到，
        但第三方库（aiohttp 等）走的是 root，会漏。
    两边都挂后用 record 标记去重，不会重复记。
    """
    buf = buffer()
    for name in ("", "botpy"):
        logger = logging.getLogger(name)
        if not any(handler is buf for handler in logger.handlers):
            logger.addHandler(buf)
    return buf
