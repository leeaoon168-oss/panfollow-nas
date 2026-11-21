#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行期可改的敏感配置（存 NAS 本地，不进 .env、不进聊天记录）。"""

from __future__ import annotations

import json
import os
from pathlib import Path

PATH = Path(os.environ.get(
    "SETTINGS_PATH",
    str(Path(os.environ.get("STATE_DIR", "/state")) / "settings.json")))

ALLOWED = ("alist_url", "alist_user", "alist_pass", "bduss", "stoken",
           "notify_type", "notify_key", "min_free_gb", "download_root",
           "ai_enabled", "ai_key", "ai_base", "ai_model", "auto_pick")

_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(PATH.read_text(encoding="utf-8"))
        except Exception:
            _cache = {}
    return _cache


def get(key: str, default: str = "") -> str:
    v = _load().get(key)
    return v if v else default


def update(data: dict) -> dict:
    """空值表示"不修改"，绝不删除——避免用户空着点保存把已存的密钥清掉。"""
    cur = dict(_load())
    for k in ALLOWED:
        if k in data:
            val = (data.get(k) or "").strip()
            if val:                      # 只有非空才写入
                cur[k] = val
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    global _cache
    _cache = cur
    return {k: ("***" if k in ("alist_pass", "bduss") else v) for k, v in cur.items()}


def clear(key: str) -> None:
    """显式清除某个字段（目前没有 UI 入口，留给以后）。"""
    cur = dict(_load())
    cur.pop(key, None)
    PATH.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    global _cache
    _cache = cur


def masked() -> dict:
    """只返回是否已配置，不回传明文；但给出长度和末尾几位，方便对照是否复制完整。"""
    d = _load()
    bd = d.get("bduss", "") or ""
    return {
        "alist_url": d.get("alist_url", ""),
        "alist_user": d.get("alist_user", ""),
        "has_pass": bool(d.get("alist_pass")),
        "has_bduss": bool(bd),
        "bduss_len": len(bd),
        "bduss_tail": bd[-6:] if bd else "",
        "has_stoken": bool(d.get("stoken")),
        "stoken_len": len(d.get("stoken", "") or ""),
        "notify_type": d.get("notify_type", ""),
        "has_notify": bool(d.get("notify_key")),
        "min_free_gb": d.get("min_free_gb", ""),
        "download_root": d.get("download_root", ""),
        "ai_enabled": d.get("ai_enabled", ""),
        "has_ai_key": bool(d.get("ai_key")),
        "ai_base": d.get("ai_base", ""),
        "ai_model": d.get("ai_model", ""),
        "auto_pick": d.get("auto_pick", "1"),
    }
