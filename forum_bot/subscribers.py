# -*- coding: utf-8 -*-
"""订阅群管理：哪些群要接收帖子转发。

注意：QQ 开放平台没有「查询机器人在哪些群」的接口，
group_openid 只能从事件里拿 —— 所以这里的名单靠「被拉进群自动订阅」
和群内发「订阅 / 取消订阅」来维护，另外也能在 config.yaml 里写死几个。
"""
import json

from botpy import logging

from . import config

__all__ = ["Subscribers"]

_log = logging.get_logger()


class Subscribers:
    def __init__(self, path=None, seed=()):
        self._path = path or config.SUBSCRIBER_FILE
        self._groups = self._load()
        for group_openid in seed:
            self._groups.add(str(group_openid))
        self._save()

    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as file:
                return {str(item) for item in json.load(file)}
        except Exception:
            return set()

    def _save(self):
        try:
            with open(self._path, "w", encoding="utf-8") as file:
                json.dump(sorted(self._groups), file, ensure_ascii=False, indent=2)
        except Exception as error:
            _log.warning(f"[botpy] 订阅表写入失败: {error}")

    def all(self):
        return set(self._groups)

    def add(self, group_openid):
        self._groups.add(str(group_openid))
        self._save()

    def discard(self, group_openid):
        self._groups.discard(str(group_openid))
        self._save()

    def __len__(self):
        return len(self._groups)

    def __contains__(self, group_openid):
        return str(group_openid) in self._groups
