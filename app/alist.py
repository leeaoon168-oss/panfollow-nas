#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Alist 管理 API 的极简客户端：登录 / 建存储 / 列目录 / 取直链。"""

from __future__ import annotations

import json
import time
from typing import Iterable

import httpx


class AlistError(RuntimeError):
    pass


class Alist:
    def __init__(self, base: str, username: str, password: str, bduss: str,
                 timeout: float = 30.0):
        self.base = base.rstrip("/")
        self.username = username
        self.password = password
        self.bduss = bduss
        self.token = ""
        self.http = httpx.Client(timeout=timeout, follow_redirects=True)

    # ---------- 基础设施 ----------

    def _call(self, method: str, path: str, payload: dict | None = None,
              retry_auth: bool = True) -> dict:
        headers = {"Authorization": self.token} if self.token else {}
        url = f"{self.base}{path}"
        r = self.http.request(method, url, json=payload, headers=headers)
        try:
            data = r.json()
        except Exception:
            raise AlistError(f"{path} 返回非 JSON（HTTP {r.status_code}）: {r.text[:200]}")
        if data.get("code") != 200:
            msg = data.get("message") or data
            if data.get("code") in (401,) and retry_auth:
                self.login()
                return self._call(method, path, payload, retry_auth=False)
            raise AlistError(f"{path} 失败: {msg}")
        return data.get("data") or {}

    def login(self) -> None:
        r = self.http.post(f"{self.base}/api/auth/login",
                           json={"username": self.username, "password": self.password})
        try:
            data = r.json()
        except Exception:
            raise AlistError(f"登录返回非 JSON（HTTP {r.status_code}）")
        if data.get("code") != 200:
            raise AlistError(f"Alist 登录失败: {data.get('message')}")
        self.token = (data.get("data") or {}).get("token", "")
        if not self.token:
            raise AlistError("登录成功但没拿到 token")

    def ping(self) -> bool:
        try:
            r = self.http.get(f"{self.base}/ping", timeout=8)
            return r.text.strip() == "pong"
        except Exception:
            return False

    # ---------- 存储 ----------

    def storages(self) -> list[dict]:
        return self._call("GET", "/api/admin/storage/list?page=1&per_page=500").get("content", [])

    def ensure_share_storage(self, mount_path: str, surl: str, pwd: str,
                             web_proxy: bool = True) -> dict:
        """确保该挂载点存在且参数是最新的 BaiduShare 存储。

        注意：早期版本只创建不更新，导致后来补填的 BDUSS 永远不生效。
        """
        want = {"surl": surl, "pwd": pwd, "BDUSS": self.bduss, "root_folder_id": "/"}
        want_json = json.dumps(want, ensure_ascii=False)

        for s in self.storages():
            if s.get("mount_path") != mount_path:
                continue
            try:
                cur = json.loads(s.get("addition") or "{}")
            except Exception:
                cur = {}
            same = (cur.get("surl") == surl and cur.get("pwd") == pwd
                    and (cur.get("BDUSS") or "") == self.bduss)
            if same:
                return s
            payload = {
                "id": s.get("id"),
                "mount_path": mount_path,
                "order": s.get("order", 0),
                "driver": "BaiduShare",
                "cache_expiration": s.get("cache_expiration", 30),
                "status": "work",
                "addition": want_json,
                "order_by": s.get("order_by", "name"),
                "order_direction": s.get("order_direction", "asc"),
                "extract_folder": s.get("extract_folder", ""),
                "web_proxy": web_proxy,
                "webdav_policy": s.get("webdav_policy", "native_proxy"),
                "down_proxy_url": s.get("down_proxy_url", ""),
            }
            self._call("POST", "/api/admin/storage/update", payload)
            time.sleep(1.2)
            return {"id": s.get("id"), "mount_path": mount_path, "updated": True}

        payload = {
            "mount_path": mount_path,
            "order": 0,
            "driver": "BaiduShare",
            "cache_expiration": 30,
            "status": "work",
            "addition": want_json,
            "order_by": "name",
            "order_direction": "asc",
            "extract_folder": "",
            "web_proxy": web_proxy,
            "webdav_policy": "native_proxy",
            "down_proxy_url": "",
        }
        data = self._call("POST", "/api/admin/storage/create", payload)
        time.sleep(0.8)                     # 等它异步初始化
        return {"id": data.get("id"), "mount_path": mount_path}

    def delete_storage(self, storage_id: int) -> None:
        self._call("POST", f"/api/admin/storage/delete?id={storage_id}")

    # ---------- 文件 ----------

    def list_dir(self, path: str) -> list[dict]:
        data = self._call("POST", "/api/fs/list",
                          {"path": path, "password": "", "page": 1,
                           "per_page": -1, "refresh": False})
        return data.get("content", []) or []

    def walk(self, path: str, depth: int = 0) -> Iterable[tuple[str, dict]]:
        """递归列出所有文件，产出 (相对路径, 对象)。"""
        if depth > 4:
            return
        for obj in self.list_dir(path):
            name = obj.get("name", "")
            child = f"{path.rstrip('/')}/{name}"
            if obj.get("is_dir"):
                yield from self.walk(child, depth + 1)
            else:
                yield child, obj

    def link(self, path: str) -> str:
        data = self._call("POST", "/api/fs/get", {"path": path, "password": ""})
        url = data.get("raw_url") or ""
        if not url:
            raise AlistError(f"没拿到直链: {path}")
        return url
