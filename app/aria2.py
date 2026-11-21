#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""aria2 JSON-RPC 极简封装。"""

from __future__ import annotations

import httpx


class Aria2:
    def __init__(self, rpc: str, secret: str = "", timeout: float = 20.0):
        self.rpc = rpc
        self.secret = secret
        self.http = httpx.Client(timeout=timeout)

    def _call(self, method: str, params: list) -> dict:
        body = {"jsonrpc": "2.0", "id": "kdocs", "method": method,
                "params": (["token:" + self.secret] if self.secret else []) + params}
        r = self.http.post(self.rpc, json=body)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"aria2 {method}: {data['error']}")
        return data.get("result")

    def ping(self) -> bool:
        try:
            return self._call("aria2.getVersion", []) is not None
        except Exception:
            return False

    def add(self, url: str, directory: str, out: str = "") -> str:
        opts = {"dir": directory, "split": "8", "max-connection-per-server": "8",
                "min-split-size": "8M", "continue": "true",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        if out:
            opts["out"] = out
        return self._call("aria2.addUri", [[url], opts])

    def queue_names(self) -> set[str]:
        """返回队列里所有任务的目标文件名，用于去重。"""
        names: set[str] = set()
        try:
            for meth in ("aria2.tellActive", "aria2.tellWaiting", "aria2.tellStopped"):
                if meth == "aria2.tellWaiting":
                    items = self._call(meth, [0, 1000]) or []
                elif meth == "aria2.tellStopped":
                    items = self._call(meth, [0, 1000]) or []
                else:
                    items = self._call(meth, []) or []
                for it in items:
                    p = (it.get("files") or [{}])[0].get("path", "")
                    if p:
                        names.add(p.rsplit("/", 1)[-1])
        except Exception:
            pass
        return names

    def stats(self) -> dict:
        try:
            g = self._call("aria2.getGlobalStat", []) or {}
            return {
                "ok": True,
                "speed": int(g.get("downloadSpeed", 0)),
                "active": int(g.get("numActive", 0)),
                "waiting": int(g.get("numWaiting", 0)),
                "stopped": int(g.get("numStopped", 0)),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}
