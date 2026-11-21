#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
夸克网盘（pan.quark.cn）能力封装。

三块：
1) 扫码登录：容器内 headless Chromium 打开夸克网盘，周期性截图给人扫；
   检测到 __puus 后落盘 cookie + storage_state，并调 file/sort 验证真能用。
2) 分享解析（不需要账号）：token → detail 递归，拿到文件名/大小/fid/fid_token。
3) 转存与下载（需要账号）：mkdir / save / file/sort / file/download / file/delete。

注意：夸克免费空间只有 10GB 左右，整季 4K 塞不下，下载要"转存单集→下载→删除"滚动。
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

import httpx

STATE_DIR = Path(os.environ.get("STATE_DIR", "/state"))
CHROMIUM_PATH = os.environ.get("CHROMIUM_PATH", "").strip()
LOGIN_URL = os.environ.get("QUARK_LOGIN_URL", "https://pan.quark.cn/")
LOGIN_TIMEOUT = int(os.environ.get("QUARK_LOGIN_TIMEOUT", "300"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
BASE = "https://drive-pc.quark.cn/1/clouddrive"
HDR_BASE = {
    "User-Agent": UA,
    "Referer": "https://pan.quark.cn/",
    "Origin": "https://pan.quark.cn",
    "Accept": "application/json, text/plain, */*",
}

_s = {"running": False, "logged_in": False, "error": "", "hint": "",
      "started_at": 0.0, "expires_in": 0}
_lock = threading.Lock()


# ---------------- 路径 ----------------

def shot_path() -> Path:
    return STATE_DIR / "quark_login.png"


def cookie_path() -> Path:
    return STATE_DIR / "quark_cookie.txt"


def storage_path() -> Path:
    return STATE_DIR / "quark_state.json"


# ---------------- 登录状态 ----------------

def cookie_header() -> str:
    p = cookie_path()
    return p.read_text("utf-8").strip() if p.exists() else ""


def has_cookie() -> bool:
    return "__puus" in cookie_header()


def status() -> dict:
    with _lock:
        s = dict(_s)
    p = shot_path()
    s["shot"] = p.exists()
    s["shot_mtime"] = int(p.stat().st_mtime) if p.exists() else 0
    s["has_cookie"] = has_cookie()
    return s


def start() -> dict:
    with _lock:
        if _s["running"]:
            return {"ok": True, "note": "登录会话已在进行"}
        _s.update(running=True, logged_in=False, error="",
                  hint="正在打开夸克登录页…", started_at=time.time(),
                  expires_in=LOGIN_TIMEOUT)
    threading.Thread(target=_run_login, daemon=True).start()
    return {"ok": True}


def stop() -> None:
    with _lock:
        _s["logged_in"] = True
        _s["running"] = False


def save_cookie(raw: str) -> None:
    raw = (raw or "").strip()
    if "__puus" not in raw:
        raise ValueError("Cookie 里没有 __puus，可能复制不全")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cookie_path().write_text(raw, encoding="utf-8")
    with _lock:
        _s["hint"] = "已保存手动 Cookie"
        _s["error"] = ""


def _cookie_dict() -> dict:
    out = {}
    for part in cookie_header().split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


# ---------------- 扫码登录 ----------------

def _run_login() -> None:
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            kw = {"headless": True,
                  "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]}
            if CHROMIUM_PATH:
                kw["executable_path"] = CHROMIUM_PATH
            browser = pw.chromium.launch(**kw)
            ctx = browser.new_context(locale="zh-CN", user_agent=UA,
                                      viewport={"width": 430, "height": 780})
            page = ctx.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(4000)

            # 尽量切到「扫码登录」页签（找不到就算了，默认通常就是二维码）
            for txt in ("扫码登录", "二维码登录", "扫码"):
                try:
                    el = page.query_selector(f"text={txt}")
                    if el:
                        el.click()
                        page.wait_for_timeout(1200)
                        break
                except Exception:
                    pass

            STATE_DIR.mkdir(parents=True, exist_ok=True)
            deadline = time.time() + LOGIN_TIMEOUT
            while time.time() < deadline:
                with _lock:
                    if _s["logged_in"]:
                        break
                    _s["expires_in"] = int(deadline - time.time())

                try:
                    page.screenshot(path=str(shot_path()), full_page=False)
                except Exception:
                    pass

                cookies = ctx.cookies()
                names = {c.get("name") for c in cookies}
                if "__puus" in names:
                    hdr = "; ".join(f"{c['name']}={c['value']}" for c in cookies
                                    if c.get("name") and c.get("value"))
                    # 验证真能用：列一次自己的根目录
                    ok = False
                    try:
                        c = httpx.Client(timeout=15, headers={**HDR_BASE, "Cookie": hdr})
                        r = c.get(f"{BASE}/file/sort", params={"pr": "ucpro", "fr": "pc",
                                                               "pdir_fid": "0",
                                                               "force": "0", "_page": "1",
                                                               "_size": "1",
                                                               "_fetch_total": "1"})
                        ok = r.status_code == 200 and (r.json() or {}).get("code") == 0
                    except Exception:
                        ok = False

                    if ok:
                        cookie_path().write_text(hdr, encoding="utf-8")
                        try:
                            ctx.storage_state(path=str(storage_path()))
                        except Exception:
                            pass
                        with _lock:
                            _s["logged_in"] = True
                            _s["hint"] = "登录成功，Cookie 已保存"
                        break
                    else:
                        with _lock:
                            _s["hint"] = "已拿到 __puus，但接口还没通，稍等…"

                time.sleep(1.5)
            else:
                with _lock:
                    _s["error"] = "扫码超时，请重新发起"
            ctx.close()
            browser.close()
    except Exception as e:
        with _lock:
            _s["error"] = f"{type(e).__name__}: {e}"
    finally:
        with _lock:
            _s["running"] = False


# ---------------- 分享解析（游客） ----------------

SHARE_RE = re.compile(r"(?:pan\.quark\.cn|drive\.quark\.cn)/s/([0-9a-zA-Z]+)")


def parse_share(url: str) -> tuple[str, str]:
    """从分享链接里取 (pwd_id, passcode)。"""
    url = (url or "").strip()
    m = SHARE_RE.search(url)
    pwd_id = m.group(1) if m else ""
    pm = re.search(r"[?&]pwd=([0-9a-zA-Z]{1,8})", url)
    return pwd_id, (pm.group(1) if pm else "")


def _c(cookie: bool = True, timeout: float = 20.0) -> httpx.Client:
    h = dict(HDR_BASE)
    if cookie and has_cookie():
        h["Cookie"] = cookie_header()
    return httpx.Client(timeout=timeout, headers=h, follow_redirects=True)


def share_token(pwd_id: str, passcode: str = "") -> dict:
    with _c(cookie=False) as c:
        r = c.post(f"{BASE}/share/sharepage/token", params={"pr": "ucpro", "fr": "pc"},
                   json={"pwd_id": pwd_id, "passcode": passcode or ""})
    j = r.json()
    if r.status_code != 200 or j.get("code") not in (0, None):
        raise RuntimeError(f"取分享凭证失败：{j.get('message') or r.status_code}")
    d = j.get("data") or {}
    if not d.get("stoken"):
        raise RuntimeError("分享需要提取码，或链接已失效")
    return d


def share_list(pwd_id: str, stoken: str, pdir_fid: str = "0",
               page_size: int = 100) -> list[dict]:
    out, page = [], 1
    while page <= 20:
        with _c(cookie=False) as c:
            r = c.get(f"{BASE}/share/sharepage/detail",
                      params={"pr": "ucpro", "fr": "pc", "pwd_id": pwd_id,
                              "stoken": stoken, "pdir_fid": pdir_fid, "force": "0",
                              "_page": str(page), "_size": str(page_size),
                              "_sort": "file_type:asc,updated_at:desc"})
        j = r.json()
        if j.get("code") not in (0, None):
            raise RuntimeError(f"列分享失败：{j.get('message')}")
        d = j.get("data") or {}
        batch = d.get("list") or []
        out.extend(batch)
        meta = d.get("metadata") or {}
        total = meta.get("_total")
        if len(batch) < page_size or (total and len(out) >= int(total)):
            break
        page += 1
    return out


def share_tree(url: str, max_dirs: int = 40) -> list[tuple[str, str, str]]:
    """递归列出分享里的文件，返回 [(相对路径, 文件名, 大小字符串)]，与 walk_pan 同形。"""
    pwd_id, passcode = parse_share(url)
    if not pwd_id:
        raise RuntimeError("不是夸克分享链接")
    st = share_token(pwd_id, passcode)["stoken"]

    files: list[tuple[str, str, str]] = []
    stack = [("", "0")]
    seen = 0
    while stack and seen < max_dirs:
        rel, fid = stack.pop()
        seen += 1
        for it in share_list(pwd_id, st, fid):
            name = it.get("file_name") or ""
            if not name or name.startswith("."):
                continue
            r = f"{rel}/{name}" if rel else name
            if it.get("dir"):
                stack.append((r, it.get("fid") or "0"))
            else:
                files.append((r, name, _human(it.get("size") or 0)))
    return files


def _human(n) -> str:
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "0"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f}{unit}"
        n /= 1024
    return f"{n:.2f}TB"


# ---------------- 自己的网盘（需要 cookie） ----------------

def _need_cookie() -> None:
    if not has_cookie():
        raise RuntimeError("夸克未登录：去设置页扫码")


def mkdir(name: str, parent_fid: str = "0") -> str:
    _need_cookie()
    with _c() as c:
        r = c.post(f"{BASE}/file", params={"pr": "ucpro", "fr": "pc"},
                   json={"pdir_fid": parent_fid, "file_name": name, "dir_path": "",
                         "dir_init_lock": False})
    j = r.json()
    if j.get("code") != 0:
        raise RuntimeError(f"建目录失败：{j.get('message')}")
    return (j.get("data") or {}).get("fid", "")


def list_dir(pdir_fid: str = "0", page_size: int = 200) -> list[dict]:
    _need_cookie()
    out, page = [], 1
    while page <= 20:
        with _c() as c:
            r = c.get(f"{BASE}/file/sort",
                      params={"pr": "ucpro", "fr": "pc", "pdir_fid": pdir_fid,
                              "force": "0", "_page": str(page), "_size": str(page_size),
                              "_fetch_total": "1", "_sort": "file_type:asc,updated_at:desc"})
        j = r.json()
        if j.get("code") != 0:
            raise RuntimeError(f"列目录失败：{j.get('message')}")
        d = j.get("data") or {}
        batch = d.get("list") or []
        out.extend(batch)
        meta = d.get("metadata") or {}
        if len(batch) < page_size or (meta.get("_total") and len(out) >= int(meta["_total"])):
            break
        page += 1
    return out


def transfer(url: str, to_pdir_fid: str = "0", pdir_fid: str = "0") -> dict:
    """把分享里的内容转存到自己的网盘目录。不指定文件时整层转存。"""
    _need_cookie()
    pwd_id, passcode = parse_share(url)
    if not pwd_id:
        raise RuntimeError("不是夸克分享链接")
    st = share_token(pwd_id, passcode)["stoken"]
    items = share_list(pwd_id, st, pdir_fid)
    fids = [x.get("fid") for x in items if x.get("fid")]
    toks = [x.get("share_fid_token") or "" for x in items if x.get("fid")]
    if not fids:
        raise RuntimeError("分享里没有可转存的内容")
    with _c() as c:
        r = c.post(f"{BASE}/share/sharepage/save", params={"pr": "ucpro", "fr": "pc"},
                   json={"fid_list": fids, "fid_token_list": toks,
                         "to_pdir_fid": to_pdir_fid, "pwd_id": pwd_id,
                         "stoken": st, "pdir_fid": pdir_fid, "scene": "link"})
    j = r.json()
    if r.status_code != 200 or j.get("code") != 0:
        raise RuntimeError(f"转存失败：{j.get('message') or r.status_code}")
    data = j.get("data") or {}
    wait_task(data.get("task_id", ""))
    return data


def ensure_folder(name: str, parent_fid: str = "0") -> str:
    """在网盘根（或指定目录）下找同名文件夹，没有就建一个。返回 fid。"""
    _need_cookie()
    for it in list_dir(parent_fid):
        if it.get("dir") and it.get("file_name") == name:
            return it.get("fid") or ""
    try:
        return mkdir(name, parent_fid)
    except Exception:
        # 并发/同名冲突时再查一次
        for it in list_dir(parent_fid):
            if it.get("dir") and it.get("file_name") == name:
                return it.get("fid") or ""
        raise


def wait_task(task_id: str, timeout: float = 90.0) -> bool:
    """等异步任务（转存/删除）完成。返回 True 表示成功。"""
    if not task_id:
        return True
    _need_cookie()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with _c() as c:
                r = c.get(f"{BASE}/task",
                          params={"pr": "ucpro", "fr": "pc", "task_id": task_id,
                                  "retry_index": "0"})
            j = r.json()
            d = j.get("data") or {}
            st = d.get("status")
            if st == 2:
                return True
            if st in (3, 4):          # 3=失败 4=取消
                return False
        except Exception:
            pass
        time.sleep(1.2)
    return False


def download_urls(fids: list[str]) -> list[dict]:
    _need_cookie()
    with _c() as c:
        r = c.post(f"{BASE}/file/download", params={"pr": "ucpro", "fr": "pc"},
                   json={"fids": fids})
    j = r.json()
    if r.status_code != 200 or j.get("code") != 0:
        raise RuntimeError(f"取直链失败：{j.get('message') or r.status_code}")
    return j.get("data") or []


def remove(fids: list[str]) -> bool:
    _need_cookie()
    if not fids:
        return True
    with _c() as c:
        r = c.post(f"{BASE}/file/delete", params={"pr": "ucpro", "fr": "pc"},
                   json={"fids": fids, "filelist": fids, "action_type": 2,
                         "exclude_fids": []})
    j = r.json()
    return r.status_code == 200 and j.get("code") == 0


def verify() -> dict:
    """验证 cookie 是否还能用，返回账号信息。"""
    if not has_cookie():
        return {"ok": False, "error": "未登录"}
    try:
        with _c() as c:
            r = c.get(f"{BASE}/file/sort", params={"pr": "ucpro", "fr": "pc",
                                                   "pdir_fid": "0", "force": "0",
                                                   "_page": "1", "_size": "1",
                                                   "_fetch_total": "1"})
        j = r.json()
        if r.status_code == 200 and j.get("code") == 0:
            d = j.get("data") or {}
            meta = d.get("metadata") or {}
            return {"ok": True, "total": meta.get("_total")}
        return {"ok": False, "error": j.get("message") or f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
