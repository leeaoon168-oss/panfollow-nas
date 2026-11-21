#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kdocs 扫码登录会话。

原理：容器里跑一个 headless Chromium 打开金山文档登录页，把页面周期性截图，
网页那边轮询这张图给人扫；一旦拿到登录 Cookie，立刻存盘并验证能否打开文档。

局限：全程盲操作（不点按钮、不切 tab），所以只能扫默认展示的登录方式。
若页面默认是账号密码登录，请退回「手动粘贴 Cookie」。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

STATE_DIR = Path(os.environ.get("STATE_DIR", "/state"))
LOGIN_TIMEOUT = int(os.environ.get("LOGIN_TIMEOUT", "300"))
CHROMIUM_PATH = os.environ.get("CHROMIUM_PATH", "").strip()
# account.kdocs.cn 现在只返回 {"error_msg":"failed to match any routes"}
LOGIN_URL = os.environ.get("KDOCS_LOGIN_URL", "https://passport.kdocs.cn/")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

_s = {
    "running": False,
    "logged_in": False,
    "error": "",
    "hint": "",
    "started_at": 0.0,
    "expires_in": 0,
}
_lock = threading.Lock()


# ---------- 路径（按 STATE_DIR 现算，便于测试与迁移）----------

def shot_path() -> Path:
    return STATE_DIR / "login.png"


def cookie_path() -> Path:
    return STATE_DIR / "doc_cookie.txt"


def storage_path() -> Path:
    p = STATE_DIR / "doc_state.json"
    legacy = STATE_DIR / "kdocs_state.json"
    if not p.exists() and legacy.exists():
        try:
            legacy.rename(p)          # 老版本的文件名，自动迁移
        except OSError:
            return legacy
    return p


# ---------- 对外 ----------

def status() -> dict:
    with _lock:
        s = dict(_s)
    p = shot_path()
    s["shot"] = p.exists()
    s["shot_mtime"] = int(p.stat().st_mtime) if p.exists() else 0
    c = cookie_path()
    s["has_cookie"] = c.exists() and bool(c.read_text("utf-8").strip())
    s["has_state"] = storage_path().exists()
    return s


def cookie_for_scrape() -> tuple[str, str]:
    """返回 (storage_state 路径, cookie 字符串)，两者可能为空。"""
    st = storage_path()
    ck = cookie_path()
    return (str(st) if st.exists() else "",
            ck.read_text("utf-8").strip() if ck.exists() else "")


def save_cookie(raw: str) -> None:
    raw = (raw or "").strip()
    if "=" not in raw:
        raise ValueError("Cookie 格式不对，应形如 name=value; name2=value2")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cookie_path().write_text(raw, encoding="utf-8")
    with _lock:
        _s["hint"] = "已保存手动 Cookie"
        _s["error"] = ""


def start(doc_url: str) -> dict:
    with _lock:
        if _s["running"]:
            return {"ok": True, "note": "会话已在进行"}
        _s.update(running=True, logged_in=False, error="", hint="正在打开登录页…",
                  started_at=time.time(), expires_in=LOGIN_TIMEOUT)
    threading.Thread(target=_run, args=(doc_url,), daemon=True).start()
    return {"ok": True}


def stop() -> None:
    with _lock:
        _s["logged_in"] = True          # 让循环退出
        _s["running"] = False


# ---------- 内部 ----------

def _cookie_header(cookies: list[dict]) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies
                     if c.get("name") and c.get("value"))


def _run(doc_url: str) -> None:
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
                **({"executable_path": CHROMIUM_PATH} if CHROMIUM_PATH else {}),
            )
            ctx = browser.new_context(locale="zh-CN", user_agent=UA,
                                      viewport={"width": 460, "height": 720})
            page = ctx.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(4000)
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
                if names & {"wps_sid", "wps_sid_new"}:
                    # 用这个会话去打开文档，确认真的能用
                    try:
                        page.goto(doc_url, wait_until="domcontentloaded",
                                  timeout=60_000)
                        page.wait_for_timeout(3000)
                        ok_url = page.url
                    except Exception as e:
                        ok_url = f"err:{e}"

                    if "account.kdocs.cn" in ok_url or "passport" in ok_url:
                        with _lock:
                            _s["hint"] = "有登录态但打不开文档，可能卡在二次验证"
                    else:
                        cookie_path().write_text(_cookie_header(cookies), encoding="utf-8")
                        try:
                            ctx.storage_state(path=str(storage_path()))
                        except Exception:
                            pass
                        with _lock:
                            _s["logged_in"] = True
                            _s["hint"] = "登录成功，Cookie 已保存"
                        break

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
