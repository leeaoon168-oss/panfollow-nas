#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 DeepSeek 在「规则分不出高下」时仲裁选哪个资源来源。

设计原则：
- AI 只返回候选的 id，不执行任何动作；
- 调用失败/超时/返回不合法，一律回退到规则结果；
- 同一组候选 6 小时内不重复问（缓存）。
"""

from __future__ import annotations

import hashlib
import json
import time

import httpx

import settings
import store

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


def configured() -> bool:
    return bool((settings.get("ai_key", "") or "").strip())


def enabled() -> bool:
    return configured() and (settings.get("ai_enabled", "0") in ("1", "on", "true", "yes"))


def _base() -> str:
    return (settings.get("ai_base", "") or DEFAULT_BASE).rstrip("/")


def _model() -> str:
    return (settings.get("ai_model", "") or DEFAULT_MODEL).strip()


SYSTEM = """你是影视资源选片助手。给你一部剧的若干候选资源（来自不同来源的网盘分享），
请选出最适合下载的那一个。

判断依据按优先级：
1. 集数完整性（缺集会直接影响追更，缺 5 集以上应让位给集数更全的来源）
2. 分辨率（4K > 2K > 1080P > 720P）
3. HDR / 杜比视界 > 普通 SDR
4. 码率标记（高码率/臻彩/蓝光 > 普通）
5. 单集体积是否与标注画质相符（标 4K 却只有 300MB/集说明码率很低或有诈）

只输出 JSON：{"pick": <候选id>, "reason": "<20字以内中文理由>"}
不要输出任何其他内容。"""


def _fingerprint(show: str, pref: str, cands: list[dict]) -> str:
    blob = json.dumps({"s": show, "p": pref, "c": sorted(
        [c.get("id"), c.get("eps"), c.get("max_ep"), c.get("quality"), c.get("avg_mb")]
        for c in cands)}, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()[:16]


def list_models() -> dict:
    """拉取服务端可用模型（OpenAI 兼容的 /models）。"""
    if not configured():
        return {"ok": False, "error": "还没填 API Key", "models": []}
    try:
        r = httpx.get(f"{_base()}/models", timeout=20,
                      headers={"Authorization": f"Bearer {settings.get('ai_key', '').strip()}"})
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:120]}", "models": []}
        data = (r.json() or {}).get("data") or []
        models = sorted({str(x.get("id")) for x in data if x.get("id")})
        return {"ok": True, "models": models}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "models": []}


def ping() -> dict:
    """连通性测试：发一句最短的请求，验证 key / base / model。"""
    if not configured():
        return {"ok": False, "error": "还没填 API Key"}
    body = {"model": _model(),
            "messages": [{"role": "user", "content": "回复两个字：可用"}],
            "max_tokens": 8, "temperature": 0}
    try:
        r = httpx.post(f"{_base()}/chat/completions", json=body, timeout=20,
                       headers={"Authorization": f"Bearer {settings.get('ai_key', '').strip()}",
                                "Content-Type": "application/json"})
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:120]}"}
        txt = r.json()["choices"][0]["message"]["content"].strip()
        return {"ok": True, "reply": txt[:40], "model": _model(), "base": _base()}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def judge(show_title: str, pref: str, cands: list[dict],
          timeout: int = 20) -> dict | None:
    """返回 {"pick": id, "reason": str, "cached": bool} 或 None。"""
    if not enabled() or len(cands) < 2:
        return None

    key = f"ai:{_fingerprint(show_title, pref, cands)}"
    try:
        cached = store.get_meta(key, "")
        if cached:
            d = json.loads(cached)
            if time.time() - d.get("ts", 0) < 6 * 3600:
                return {"pick": d["pick"], "reason": d.get("reason", ""), "cached": True}
    except Exception:
        pass

    payload = {
        "show": show_title,
        "prefer": pref or "最高画质",
        "candidates": [{
            "id": c.get("id"),
            "source": c.get("label") or c.get("provider") or "",
            "eps": c.get("eps"),
            "max_ep": c.get("max_ep"),
            "quality": c.get("quality") or "未知",
            "avg_mb": c.get("avg_mb"),
            "note": c.get("note") or "",
        } for c in cands],
    }
    body = {
        "model": _model(),
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "temperature": 0,
        "max_tokens": 200,
        "response_format": {"type": "json_object"},
    }
    try:
        r = httpx.post(f"{_base()}/chat/completions", json=body, timeout=timeout,
                       headers={"Authorization": f"Bearer {settings.get('ai_key', '').strip()}",
                                "Content-Type": "application/json"})
        if r.status_code != 200:
            print(f"[ai] HTTP {r.status_code}: {r.text[:160]}", flush=True)
            return None
        txt = r.json()["choices"][0]["message"]["content"]
        d = json.loads(txt)
        pick = int(d.get("pick"))
        ids = {int(c.get("id")) for c in cands}
        if pick not in ids:
            print(f"[ai] 返回的 id {pick} 不在候选里，忽略", flush=True)
            return None
        reason = str(d.get("reason", ""))[:60]
        store.set_meta(key, json.dumps({"pick": pick, "reason": reason,
                                        "ts": int(time.time())}, ensure_ascii=False))
        print(f"[ai] 选中 {pick}：{reason}", flush=True)
        return {"pick": pick, "reason": reason, "cached": False}
    except Exception as e:
        print(f"[ai] 调用失败：{type(e).__name__}: {e}", flush=True)
        return None
