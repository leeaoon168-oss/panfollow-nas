#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kdocs 追更服务：手机网页勾选订阅 -> 每天比对分享目录 -> 只下新集数。

环境变量见 .env.example。所有写操作串行（一把锁），避免抓取和同步互相打架。
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import traceback
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import ai
import kdocs
import login
import notify
import quark
import settings
import store
from alist import Alist, AlistError
from aria2 import Aria2

# ---------- 配置 ----------

DOC_URL = (os.environ.get("DOC_URL") or os.environ.get("KDOCS_URL") or "").strip()
COOKIE = (os.environ.get("DOC_COOKIE") or os.environ.get("KDOCS_COOKIE") or "").strip()
ALIST_URL = os.environ.get("ALIST_URL", "http://127.0.0.1:5244").strip()
ALIST_USER = os.environ.get("ALIST_USER", "admin").strip()
ALIST_PASS = os.environ.get("ALIST_PASS", "").strip()
ALIST_BDUSS = os.environ.get("ALIST_BDUSS", "").strip()
ARIA2_RPC = os.environ.get("ARIA2_RPC", "http://aria2:6800/jsonrpc").strip()
ARIA2_SECRET = os.environ.get("ARIA2_SECRET", "").strip()
DL_ROOT = Path(os.environ.get("DOWNLOAD_DIR", "/downloads"))
DATA_ROOT = Path(os.environ.get("DATA_DIR", "/mnt/data"))

# 下载目录可用性缓存（/api/state 会被频繁轮询，不每次都摸磁盘）
_DL_CACHE: dict = {"v": None, "t": 0}


def dl_root() -> Path:
    """当前下载根目录（可在设置里改）"""
    r = (settings.get("download_root", "") or "").strip()
    p = Path(r) if r else DL_ROOT
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        return DL_ROOT
    return p if p.exists() else DL_ROOT


def dl_status(force: bool = False) -> dict:
    """下载目录能不能写 —— 新用户挂载配错时，这里能第一时间看出来。

    结果缓存 30 秒：/api/state 被前端频繁轮询，不能每次都去摸磁盘。
    """
    now = time.time()
    cached = _DL_CACHE.get("v")
    if not force and cached and now - _DL_CACHE.get("t", 0) < 30:
        return cached
    p = dl_root()
    out = {"path": str(p), "ok": True, "error": ""}
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write_test"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as e:
        out["ok"] = False
        out["error"] = f"{type(e).__name__}: {e}"
    _DL_CACHE["v"] = out
    _DL_CACHE["t"] = now
    return out


SYNC_HOURS = float(os.environ.get("SYNC_HOURS", "24"))
REFRESH_HOURS = float(os.environ.get("REFRESH_HOURS", "6"))
AUTO_SYNC = os.environ.get("AUTO_SYNC", "0").lower() in ("1", "true", "yes", "on")
WEB_PROXY = os.environ.get("ALIST_WEB_PROXY", "1") == "1"
VIDEO_EXT = {".mp4", ".mkv", ".ts", ".avi", ".mov", ".rmvb", ".flv", ".m4v", ".wmv"}

# BaiduPCS-Go（转存 + 下载），二进制随镜像打包
PCS_BIN = os.environ.get("PCS_BIN", "/app/bin/BaiduPCS-Go")
PCS_CFG = os.environ.get("PCS_CONFIG_DIR", "/state/pcs")
PAN_TMP = os.environ.get("PAN_TMP_DIR", "/kdocs")
PAN_KEEP = os.environ.get("PAN_KEEP", "0") == "1"
PCS_PARALLEL = os.environ.get("PCS_PARALLEL", "8")
MAX_SCROLL = int(os.environ.get("MAX_SCROLL", "160"))

# 分享目录里常混着花絮/封面，这些不下
SKIP_WORDS = ("陪看", "花絮", "预告", "彩蛋", "主题曲", "OST", "海报", "封面",
              "样片")

# 这些词出现在「文件名」里才算跳过；出现在目录名里不能算
# （例：目录叫「[简繁内封字幕]」，里面是正片，不能当字幕跳过）
SKIP_NAME_WORDS = ("字幕", "subtitle", "subs")
EP_PATTERNS = (r"^0*(\d{1,3})[\.\s_\-]", r"第\s*0*(\d{1,3})\s*[集话]", r"[Ee][Pp]?[\.\-\s]?0*(\d{1,3})")
LS_ROW = re.compile(r"^\s*\d+\s+(\S+)\s+\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s{2,}(.+?)\s*$")


def parse_ep(name: str) -> int | None:
    base = Path(name).stem
    for pat in EP_PATTERNS:
        m = re.search(pat, base)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
    return None


# ---------- 画质打分 ----------
# 分辨率是硬指标（权重 1000 级），HDR/码率/帧率只做同级内排序
Q_RES = (
    (r"2160p|4k|uhd", 4000, "4K"),
    (r"1440p|2k", 1440, "2K"),
    (r"1080p|fhd|全高清", 1080, "1080P"),
    (r"720p", 720, "720P"),
    (r"480p", 480, "480P"),
)


def quality_info(text: str) -> tuple[int, int, str]:
    """返回 (分辨率基准, 总分, 简称)"""
    t = re.sub(r"\s+", "", (text or "").lower())
    base, label = 100, ""
    for pat, val, name in Q_RES:
        if re.search(pat, t):
            base, label = val, name
            break
    score = base
    if re.search(r"杜比视界|dolbyvision|dovi|dv", t):
        score += 300
        label += "·杜比视界"
    elif "hdr" in t:
        score += 200
        label += "·HDR"
    if re.search(r"高码率|臻彩|蓝光|remux|hq", t):
        score += 50
        label += "·高码率"
    if re.search(r"60fps|60帧", t):
        score += 20
    return base, score, (label or "未知画质")


def quality_score(text: str) -> int:
    return quality_info(text)[1]


def pcs_list(path: str) -> list[tuple[str, bool, str]]:
    _, out = pcs("ls", path, timeout=180)
    items = []
    for line in out.splitlines():
        m = LS_ROW.match(line)
        if not m:
            continue
        name = m.group(2).strip()
        is_dir = name.endswith("/")
        name = name.rstrip("/").strip()
        if not name or name.startswith("总"):
            continue
        items.append((name, is_dir, m.group(1)))
    return items


def walk_pan(root: str, max_dirs: int = 40) -> list[tuple[str, str, str]]:
    """递归列出网盘目录，返回 [(相对路径, 文件名, 大小)]"""
    out: list[tuple[str, str, str]] = []
    stack = [("", root)]
    seen = 0
    while stack and seen < max_dirs:
        rel, cur = stack.pop()
        seen += 1
        for name, is_dir, size in pcs_list(cur):
            r = f"{rel}/{name}" if rel else name
            if is_dir:
                stack.append((r, f"{cur}/{name}"))
            else:
                out.append((r, name, size))
    return out

_LOCK = threading.Lock()
# BaiduPCS-Go 的「当前目录」是全局配置，检查和下载同时跑会互相串目录，
# 所以整条百度通道用一把锁串起来：下载时检查让路。
_PCS_LOCK = threading.Lock()
_STATE = {"refreshing": False, "syncing": False}

app = FastAPI(title="PanFollow")


# ---------- 工具 ----------

_ALIST = {"client": None, "sig": None, "tried": False}


def alist(force: bool = False) -> Alist:
    """复用已登录的客户端：Alist 对失败登录有频率限制，不能每次请求都 login。"""
    url = settings.get("alist_url", ALIST_URL)
    user = settings.get("alist_user", ALIST_USER)
    pwd = settings.get("alist_pass", ALIST_PASS)
    bduss = settings.get("bduss", ALIST_BDUSS)
    sig = (url, user, pwd, bduss)
    c = _ALIST["client"]
    if c is None or _ALIST["sig"] != sig or force:
        c = Alist(url, user, pwd, bduss)
        c.login()
        _ALIST["client"] = c
        _ALIST["sig"] = sig
    return c


def aria() -> Aria2:
    return Aria2(ARIA2_RPC, ARIA2_SECRET)


def _is_error_stub(p: Path) -> bool:
    """Alist 取链失败时会返回 JSON，aria2 仍当成下载成功写成小文件。"""
    try:
        if p.stat().st_size > 4096:
            return False
        with open(p, "rb") as f:
            head = f.read(64).lstrip()
        return head.startswith(b'{"code"')
    except OSError:
        return False


def show_root(slug: str) -> Path:
    """某部剧的落盘目录。

    指定了目录 -> 直接用它本身（不再套剧名，否则会出现「仙逆/仙逆（2026）」）；
    没指定 -> 用全局目录/剧名。
    """
    d = (store.get_dest(slug) or "").strip()
    if d:
        p = Path(d)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            return dl_root() / slug
        return p
    return dl_root() / slug


def flatten_dir(root: Path) -> int:
    """把子目录里的文件挪到根目录，并清掉同名同大小的重复副本。

    只会删除「名字相同且字节数相同」的文件（几乎必然是同一集下重了），
    名字相同但大小不同的一律保留。
    """
    if not root.exists():
        return 0
    try:
        files = [p for p in root.rglob("*") if p.is_file() and p.parent != root]
    except OSError:
        return 0
    moved = 0
    for p in files:
        if p.name.endswith(".BaiduPCS-Go-downloading"):
            continue
        dest = root / p.name
        if dest.exists():
            try:
                if dest.stat().st_size == p.stat().st_size:
                    p.unlink()          # 重复副本
            except OSError:
                pass
            continue
        try:
            p.rename(dest)
            moved += 1
        except OSError:
            pass
    try:
        for d in sorted([x for x in root.rglob("*") if x.is_dir()], reverse=True):
            try:
                if not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass
    except OSError:
        pass
    return moved


def newest_ts(slug: str) -> int:
    """目录里最新一个文件的修改时间，用来回填「上次完成」。"""
    root = show_root(slug)
    if not root.exists():
        return 0
    mx = 0
    for p in root.rglob("*"):
        try:
            if p.is_file():
                m = int(p.stat().st_mtime)
                if m > mx:
                    mx = m
        except OSError:
            pass
    return mx


def analyze_files(files, start_ep: int = 1, pref: str = "best") -> dict:
    """从分享文件列表里挑出该下载的目标（选组 + 同集去重 + 起始集过滤）。

    download_show 和「定时检查」共用这一套逻辑，保证"检查看到的"和"实际下的"一致。
    返回 targets=[(rel,name,ep,score)]、best={ep: 最高画质分}、label、groups 等。
    """
    # 1) 粗筛
    cand = []
    for rel, name, size in files:
        if Path(name).suffix.lower() not in VIDEO_EXT:
            continue
        if any(w in rel for w in SKIP_WORDS):
            continue
        if any(w in name.lower() for w in SKIP_NAME_WORDS):
            continue
        cand.append((str(Path(rel).parent), rel, name, parse_ep(name)))

    # 2) 按目录分组，排除剧场版/片头尾这类附带目录
    groups = {}
    for parent, rel, name, ep in cand:
        groups.setdefault(parent, []).append((rel, name, ep))
    counts = {g: sum(1 for _, _, ep in v if ep is not None) for g, v in groups.items()}
    top = max(counts.values()) if counts else 0
    if top > 0:
        pool = [g for g, c in counts.items() if c >= max(2, top * 0.5)]
    else:
        pool = list(groups.keys())
    if not pool:
        return {"targets": [], "best": {}, "label": "未知画质", "score": 0,
                "groups": groups, "counts": counts, "keep": set()}

    def gscore(g):
        return max([quality_score(g)] + [quality_score(r) for r, _, _ in groups[g]])

    def gres(g):
        return max([quality_info(g)[0]] + [quality_info(r)[0] for r, _, _ in groups[g]])

    # 3) 按画质挑整组
    scored = {g: gscore(g) for g in pool}
    p = (pref or "best").lower()
    if p == "smallest":
        t = min(scored.values())
        keep = {g for g in pool if scored[g] == t}
    elif p in ("1080", "720"):
        want = 1080 if p == "1080" else 720
        hit = [g for g in pool if gres(g) == want]
        if hit:
            m = max(scored[g] for g in hit)
            keep = {g for g in hit if scored[g] == m}
        else:
            m = max(scored.values())
            keep = {g for g in pool if scored[g] == m}
    else:
        m = max(scored.values())
        keep = {g for g in pool if scored[g] == m}

    # 4) 同集去重，记录每集最高画质分
    best_by_ep = {}
    extras = []
    for g in keep:
        numbered = counts.get(g, 0) > 0
        for rel, name, ep in groups[g]:
            if numbered and ep is None:
                continue
            sc = quality_score(rel)
            if ep is None:
                extras.append((rel, name, ep, sc))
            else:
                cur = best_by_ep.get(ep)
                if cur is None or sc > cur[3]:
                    best_by_ep[ep] = (rel, name, ep, sc)

    all_targets = sorted(list(best_by_ep.values()) + extras,
                         key=lambda x: (x[2] if x[2] is not None else 99999))
    targets = [t for t in all_targets if t[2] is None or t[2] >= start_ep]
    chosen = sorted(keep)[0] if keep else ""
    label = quality_info(" ".join([chosen] + [r for g in keep for r, _, _ in groups[g]]))[2]
    return {"targets": targets, "best": {e: v[3] for e, v in best_by_ep.items()},
            "label": label, "score": m, "groups": groups, "counts": counts,
            "keep": keep, "dropped": sorted(set(groups.keys()) - keep)}


_SIZE_RE = re.compile(r"([\d.]+)\s*([KMGTP]?)i?B", re.I)


def parse_size(s) -> int:
    """把 BaiduPCS-Go 的 "994.83MB" 之类转成字节数。"""
    if isinstance(s, (int, float)):
        return int(s)
    m = _SIZE_RE.search(str(s or ""))
    if not m:
        return 0
    try:
        n = float(m.group(1))
    except ValueError:
        return 0
    u = (m.group(2) or "").upper()
    mul = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
           "T": 1024 ** 4, "P": 1024 ** 5}.get(u, 1)
    return int(n * mul)


def size_tolerance(s) -> int:
    """按显示精度算容差："1.31GB" 只有两位小数，误差可能到 5MB。"""
    m = _SIZE_RE.search(str(s or ""))
    if not m:
        return 1048576
    num, unit = m.group(1), (m.group(2) or "").upper()
    dec = len(num.split(".")[1]) if "." in num else 0
    mul = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
           "T": 1024 ** 4, "P": 1024 ** 5}.get(unit, 1)
    return max(int(0.5 * (10 ** -dec) * mul), 1024)


def link_hash(url: str) -> str:
    import hashlib
    return hashlib.md5((url or "").encode("utf-8")).hexdigest()[:16]


def is_dead_link(slug: str, url: str) -> bool:
    """文档里的这个链接之前确认过失效，且还没换新链接。"""
    got = store.get_meta(f"dead:{slug}", "")
    return bool(got) and got == link_hash(url)


def _looks_dead(out: str) -> bool:
    """BaiduPCS-Go 的转存失败输出里，哪些意味着分享本身没了。"""
    t = out or ""
    return any(k in t for k in ("错误码-12", "错误码-9", "不存在", "已失效", "已取消", "违规"))


def confirm_share_alive(url: str) -> bool:
    """用 HTTP 看一眼分享页还在不在（-12 时用来区分"分享死了"和"网络抖动"）。"""
    try:
        surl, pwd = kdocs.baidu_surl(url)
        if not surl:
            return True
        st, ck = login.cookie_for_scrape()
        h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"}
        if ck:
            h["Cookie"] = ck
        r = httpx.get(f"https://pan.baidu.com/s/{surl}", params={"pwd": pwd or ""},
                      headers=h, follow_redirects=True, timeout=20)
        return not any(k in r.text for k in ("链接不存在", "分享已失效", "已取消分享"))
    except Exception:
        return True


def mark_dead_link(slug: str, url: str, why: str) -> None:
    store.set_meta(f"dead:{slug}", link_hash(url))
    store.set_meta(f"deadwhy:{slug}", why[:160])


PARTIAL_SUFFIX = ".BaiduPCS-Go-downloading"

# 常见容器格式的文件头特征（用于识别"预分配但没写数据"的空壳文件）
MEDIA_MAGIC = (
    (4, b"ftyp"),          # MP4 / MOV
    (0, b"\x1a\x45\xdf\xa3"),  # MKV / WebM
    (0, b"RIFF"),          # AVI
    (0, b"FLV"),           # FLV
    (0, b"\x00\x00\x01\xba"),  # MPEG-PS
)


def is_valid_media(p: Path) -> bool:
    """快速判断文件是不是真的视频（只读 16 字节）。

    BaiduPCS-Go 会先按大小创建文件再写数据，进程被杀就会留下一个大小正确、
    内容全零的空壳，而且不带 .downloading 标记——这种必须识别出来重下。
    """
    try:
        with p.open("rb") as f:
            head = f.read(16)
    except OSError:
        return True                     # 读不到就别动它
    if not head or head == b"\0" * len(head):
        return False
    for off, magic in MEDIA_MAGIC:
        if head[off:off + len(magic)] == magic:
            return True
    if head[0:1] == b"\x47":            # MPEG-TS
        return True
    return True                          # 认得别的格式就放过，避免误杀


def is_partial(p: Path) -> bool:
    """下载中/中断的文件：同目录存在 <name>.BaiduPCS-Go-downloading 标记。"""
    try:
        return Path(str(p) + PARTIAL_SUFFIX).exists()
    except OSError:
        return False


def drop_partial(p: Path) -> None:
    """删掉残缺文件和它的标记，让下次重新下。"""
    for q in (p, Path(str(p) + PARTIAL_SUFFIX)):
        try:
            if q.exists():
                q.unlink()
        except OSError:
            pass


def local_ep_scores(slug: str) -> dict[int, int]:
    """本地每一集的最高画质分（残缺/空壳文件不算）。"""
    root = show_root(slug)
    m = {}
    if not root.exists():
        return m
    for p in root.rglob("*"):
        if not p.is_file() or p.name.endswith(PARTIAL_SUFFIX):
            continue
        if is_partial(p) or not is_valid_media(p):
            continue
        e = parse_ep(p.name)
        if not e:
            continue
        sc = quality_score(str(p.relative_to(root)))
        if sc > m.get(e, 0):
            m[e] = sc
    return m


def local_stat(slug: str) -> tuple[int, int]:
    """返回 (文件数, 总字节)。残缺/空壳/错误页都不算。"""
    d = show_root(slug)
    if not d.exists():
        return 0, 0
    n = 0
    size = 0
    for p in d.rglob("*"):
        if not p.is_file() or p.name.endswith(PARTIAL_SUFFIX):
            continue
        if is_partial(p) or not is_valid_media(p) or _is_error_stub(p):
            continue
        n += 1
        try:
            size += p.stat().st_size
        except OSError:
            pass
    return n, size


def local_files(slug: str) -> set[str]:
    d = show_root(slug)
    if not d.exists():
        return set()
    good: set[str] = set()
    removed = 0
    now = time.time()
    for p in d.rglob("*"):
        if not p.is_file() or p.name.endswith(".aria2") or p.name.endswith(PARTIAL_SUFFIX):
            continue
        bad = _is_error_stub(p) or is_partial(p) or not is_valid_media(p)
        if not bad:
            good.add(p.name)
            continue
        # 判为"坏"的先别急着删：aria2 会先按总大小预分配（内容全零），
        # 正在下或刚写进来的文件很容易被误杀。只删「没在下载 + 放了一会儿」的。
        if Path(str(p) + ".aria2").exists():
            continue
        try:
            if now - p.stat().st_mtime < 600:
                continue
        except OSError:
            continue
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    if removed:
        print(f"[clean] {slug}: 清掉 {removed} 个损坏/无效文件", flush=True)
    return good


def stub_message(p: Path) -> str:
    try:
        return p.read_text("utf-8", errors="replace")[:200]
    except OSError:
        return ""


def scrape(doc_url: str = "") -> list[dict]:
    """kdocs 的文档编辑器是虚拟渲染的：DOM 里只保留当前可视区，
    滚过去的内容会被移出。所以必须边滚边抓、按标题合并。"""
    from playwright.sync_api import sync_playwright

    st_path, ck = login.cookie_for_scrape()
    if not st_path and not ck:
        ck = COOKIE                      # 退回 .env 里手填的

    with sync_playwright() as pw:
        launch_kw = {"headless": True,
                     "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]}
        chromium = os.environ.get("CHROMIUM_PATH", "").strip()
        if chromium:
            launch_kw["executable_path"] = chromium
        browser = pw.chromium.launch(**launch_kw)
        kwargs = {"storage_state": st_path} if st_path else {}
        ctx = browser.new_context(
            locale="zh-CN",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/128.0.0.0 Safari/537.36"),
            viewport={"width": 1500, "height": 1100},
            **kwargs,
        )
        if ck:
            jar = []
            for part in ck.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    jar.append({"name": k.strip(), "value": v.strip(),
                                "domain": ".kdocs.cn", "path": "/"})
            if jar:
                ctx.add_cookies(jar)

        page = ctx.new_page()
        page.goto(doc_url or DOC_URL, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(9000)
        if "account.kdocs.cn" in page.url or "passport" in page.url:
            raise RuntimeError("被弹到登录页：DOC_COOKIE 缺失或已过期")

        info = page.evaluate("""() => {
            const c = [...document.querySelectorAll('*')].filter(el =>
                el.scrollHeight > el.clientHeight + 200 && el.clientHeight > 200);
            c.sort((a, b) => b.scrollHeight - a.scrollHeight);
            window.__sc = c[0] || document.scrollingElement;
            return {sh: window.__sc.scrollHeight, ch: window.__sc.clientHeight};
        }""")
        print(f"[scrape] 容器高度 {info.get('sh')} 可视 {info.get('ch')}", flush=True)

        merged: dict[str, dict] = {}
        first_text = ""
        stuck = 0
        for step in range(MAX_SCROLL):
            text = page.evaluate("document.body ? document.body.innerText : ''") or ""
            if step == 0:
                first_text = text
            for it in kdocs.parse(text):
                cur = merged.get(it["title"])
                if cur is None:
                    merged[it["title"]] = it
                else:                      # 同一部剧在不同帧出现，补齐链接
                    for prov, v in it["links"].items():
                        cur["links"].setdefault(prov, v)
            before = page.evaluate("() => window.__sc ? window.__sc.scrollTop : window.scrollY")
            page.evaluate("""() => {
                const s = window.__sc;
                if (s) s.scrollTop = Math.min(s.scrollHeight,
                    s.scrollTop + Math.round(s.clientHeight * 0.6));
            }""")
            page.wait_for_timeout(420)
            after = page.evaluate("() => window.__sc ? window.__sc.scrollTop : window.scrollY")
            if after == before:
                stuck += 1
                if stuck >= 2:
                    break
                page.wait_for_timeout(700)
            else:
                stuck = 0

        ctx.close()
        browser.close()

    if not merged and "扫码登录" in first_text:
        raise RuntimeError("文档要求登录：请打开 /login 扫码，或在 .env 填 DOC_COOKIE")
    if not merged:
        raise RuntimeError("解析到 0 条，可能文档不可访问或结构变了")
    print(f"[scrape] 合并后 {len(merged)} 条", flush=True)
    return list(merged.values())


# ---------- 业务 ----------


def _norm_title(t: str) -> str:
    t = re.sub(r"^[A-Za-z0-9★⭐🔴🟡🟢\s]{0,3}", "", (t or "").strip()).strip(" ◆♦*")
    t = re.sub(r"\s*[(（]\s*(\d{4})\s*[)）]\s*", lambda m: "（%s）" % m.group(1), t)
    return re.sub(r"\s+", "", t)


def scrape_sheet(doc_url: str, doc_name: str = "") -> list[dict]:
    """kdocs 表格：用 WPSOpenApi 按 Range 读单元格与超链接（能拿到真实分享地址）。"""
    from playwright.sync_api import sync_playwright
    import sheetlinks

    st_path, ck = login.cookie_for_scrape()
    if not st_path and not ck:
        ck = COOKIE
    with sync_playwright() as pw:
        launch_kw = {"headless": True,
                     "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]}
        chromium = os.environ.get("CHROMIUM_PATH", "").strip()
        if chromium:
            launch_kw["executable_path"] = chromium
        browser = pw.chromium.launch(**launch_kw)
        kwargs = {"storage_state": st_path} if st_path else {}
        ctx = browser.new_context(
            locale="zh-CN",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
            viewport={"width": 1600, "height": 1000}, **kwargs)
        page = ctx.new_page()
        page.goto(doc_url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(12000)
        res = sheetlinks.extract_sheet_rows(page, 400, log=lambda *a: None)
        browser.close()

    rows = res.get("rows") or []
    merged: dict[str, dict] = {}
    for r in rows:
        title = _norm_title(r.get("title"))
        if not title or len(title) < 2 or "防失联" in title:
            continue
        cur = merged.setdefault(title, {"title": title, "eps": "", "quality": "",
                                        "cat": r.get("cat") or "", "links": {},
                                        "finished": False, "total_eps": 0})
        raw_eps = r.get("eps") or ""
        nums = re.findall(r"(\d{1,4})", raw_eps)
        # 只有两种写法能确定「总集数」：
        #   "04/30集"  → 已更 4，总 30
        #   "12集完结" → 完结，总 12
        # 其它（"184集"、"更新02集"）只是当前集数，总集未知记 0
        if "/" in raw_eps and len(nums) >= 2:
            total = int(nums[1])
            cur["total_eps"] = max(cur.get("total_eps") or 0, total)
            if total > 0 and int(nums[0]) >= total:
                cur["finished"] = True
        elif nums and int(nums[-1]) > int(cur["eps"] or 0):
            cur["eps"] = nums[-1]
        if "完结" in raw_eps:
            cur["finished"] = True
            if nums:
                cur["total_eps"] = max(cur.get("total_eps") or 0, int(nums[-1]))
        cur["quality"] = cur["quality"] or (r.get("quality") or "")
        for prov, key in (("百度", "baidu"), ("夸克", "quark")):
            u = (r.get(key) or "").strip()
            if u and prov not in cur["links"]:
                surl_pwd = re.search(r"[?&]pwd=([^&\s]+)", u)
                cur["links"][prov] = {"url": u,
                                      "pwd": surl_pwd.group(1) if surl_pwd else ""}

    items = []
    for v in merged.values():
        m = re.search(r"[（(](\d{4})[)）]", v["title"])
        items.append({
            "title": v["title"],
            "slug": kdocs.slugify(v["title"]),
            "year": m.group(1) if m else "",
            "episodes": v["eps"],
            "quality": v["quality"],
            "category": v["cat"],
            "links": v["links"],
            "finished": bool(v.get("finished")),
            "total_eps": int(v.get("total_eps") or 0),
        })
    # 只保留有链接、标题里有中文的行（没有链接的进来只会变成空条目）
    items = [i for i in items if i["links"] and re.search(r"[\u4e00-\u9fa5]", i["title"])]
    with_link = sum(1 for i in items if i["links"])
    print(f"[sheet] {doc_name} 解析 {len(items)} 部，其中 {with_link} 部有链接", flush=True)
    return items


def _old_scrape_sheet(doc_url: str) -> list[dict]:
    """kdocs 表格（WPS 表格）：内容画在 canvas 上，DOM 里没有文字。
    用「全选 + 复制」拦剪贴板拿 TSV，逐个 sheet 页签抓，再解析成剧集清单。
    注意：表格里的网盘列是超链接，复制出来只有显示文字，没有 URL。
    """
    from playwright.sync_api import sync_playwright
    import sheet as sheet_mod

    st_path, ck = login.cookie_for_scrape()
    if not st_path and not ck:
        ck = COOKIE

    HOOK = """
    window.__copied = '';
    try {
      const o = navigator.clipboard.writeText.bind(navigator.clipboard);
      navigator.clipboard.writeText = function(t){ window.__copied = t; return o(t); };
    } catch(e) {}
    try {
      const oe = document.execCommand.bind(document);
      document.execCommand = function(cmd, ...r){
        if (String(cmd).toLowerCase() === 'copy'){
          const ae = document.activeElement;
          let t = (ae && ae.value) ? ae.value : ((ae && ae.textContent) || '');
          if (!t && window.getSelection) t = window.getSelection().toString();
          if (t) window.__copied = t;
        }
        return oe(cmd, ...r);
      };
    } catch(e) {}
    """

    def grab(pg) -> str:
        pg.evaluate(HOOK)
        for pos in ((600, 700), (800, 700), (500, 400)):
            try:
                pg.mouse.click(*pos)
                pg.wait_for_timeout(400)
            except Exception:
                pass
        pg.keyboard.press("Control+A")
        pg.wait_for_timeout(500)
        pg.keyboard.press("Control+C")
        pg.wait_for_timeout(1200)
        t = pg.evaluate("window.__copied || ''")
        if not t:
            try:
                t = pg.evaluate("navigator.clipboard.readText()")
            except Exception:
                t = ""
        return t or ""

    with sync_playwright() as pw:
        launch_kw = {"headless": True,
                     "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]}
        chromium = os.environ.get("CHROMIUM_PATH", "").strip()
        if chromium:
            launch_kw["executable_path"] = chromium
        browser = pw.chromium.launch(**launch_kw)
        kwargs = {"storage_state": st_path} if st_path else {}
        ctx = browser.new_context(
            locale="zh-CN",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
            viewport={"width": 1600, "height": 1000}, **kwargs)
        ctx.grant_permissions(["clipboard-read", "clipboard-write"])
        page = ctx.new_page()
        page.goto(doc_url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(9000)

        texts = [grab(page)]
        tabs = page.evaluate("""() => {
          const out = [];
          document.querySelectorAll('*').forEach(el => {
            const t = (el.textContent || '').trim();
            if (t && t.length <= 10 && el.children.length <= 2 &&
                /完结|更新列表|电影合集|动漫|剧集/.test(t)) out.push(t);
          });
          return [...new Set(out)].slice(0, 12);
        }""")
        for tab in tabs:
            try:
                el = page.query_selector("text=" + tab)
                if not el:
                    continue
                el.click()
                page.wait_for_timeout(2500)
                texts.append(grab(page))
            except Exception:
                continue
        browser.close()

    rows = []
    for t in texts:
        rows.extend(sheet_mod.parse_sheet_rows(t))

    merged: dict[str, dict] = {}
    for r in rows:
        k = r["title"]
        cur = merged.get(k)
        if not cur:
            merged[k] = dict(r)
        else:
            try:
                if int(r["episodes"] or 0) > int(cur["episodes"] or 0):
                    cur["episodes"] = r["episodes"]
            except ValueError:
                pass
            cur["quality"] = cur.get("quality") or r.get("quality")

    items = []
    for r in merged.values():
        title = r["title"]
        m = re.search(r"[（(](\d{4})[)）]", title)
        items.append({
            "title": title,
            "slug": kdocs.slugify(title),
            "year": m.group(1) if m else "",
            "episodes": r.get("episodes", ""),
            "quality": r.get("quality", ""),
            "category": r.get("country", ""),
            "links": {},
        })
    print(f"[sheet] 解析到 {len(items)} 部（{len(texts)} 个页签）", flush=True)
    return items


def do_refresh() -> dict:
    if _STATE["refreshing"]:
        return {"ok": False, "error": "正在刷新"}
    _STATE["refreshing"] = True
    t0 = time.time()
    try:
        docs = [d for d in store.list_documents() if d.get("enabled")]
        if not docs and DOC_URL:
            docs = [{"id": 0, "url": DOC_URL, "name": "主文档"}]

        total, failed, ms = 0, [], 0
        for d in docs:
            name = d.get("name") or d["url"][-12:]
            try:
                items = []
                is_sheet = False
                try:
                    items = scrape(d["url"])
                except Exception as e:
                    print(f"[refresh] {name}: 按文档抓失败（{e}），改按表格抓", flush=True)
                if not items:
                    items = scrape_sheet(d["url"])
                    is_sheet = True
                if not items:
                    raise RuntimeError("解析到 0 条（文档/表格都没内容）")
                if is_sheet:
                    ns, n = store.import_sheet_items(items, name)
                    print(f"[refresh] {name}: 新增 {ns} 部、来源 {n} 个", flush=True)
                else:
                    store.upsert_shows(items)
                    n = store.seed_sources_from_links(doc_name=name)
                if d.get("id"):
                    store.update_document(d["id"], last_refresh=int(time.time()),
                                          last_count=len(items), last_error="")
                total += len(items)
                print(f"[refresh] {name}: {len(items)} 条{'(表格)' if is_sheet else ''}，"
                      f"新增来源 {n}", flush=True)
            except Exception as e:
                failed.append(name)
                if d.get("id"):
                    store.update_document(d["id"], last_error=str(e)[:160])
                print(f"[refresh] {name} 失败: {e}", flush=True)

        ms = int((time.time() - t0) * 1000)
        if not total:
            store.set_meta("last_error", "所有文档都解析失败")
            store.set_meta("last_fail", str(int(time.time())))
            return {"ok": False, "error": "解析到 0 条"}

        store.set_meta("last_refresh", str(int(time.time())))
        store.set_meta("last_error", f"{len(failed)} 个文档失败" if failed else "")
        store.set_meta("last_count", str(total))
        store.set_meta("last_ms", str(ms))
        out = {"ok": True, "count": total, "ms": ms, "docs": len(docs)}
        if failed:
            out["failed"] = failed
        return out
    except Exception as e:
        store.set_meta("last_error", f"抓取失败: {e}")
        store.set_meta("last_fail", str(int(time.time())))
        return {"ok": False, "error": str(e)}
    finally:
        _STATE["refreshing"] = False
        try:
            _STATUS_CACHE["ts"] = 0.0      # 抓完立刻重算健康状态
        except NameError:
            pass


def sync_one(slug: str) -> dict:
    sh = store.show(slug)
    if not sh:
        return {"slug": slug, "error": "未找到该剧"}
    bd = (sh["links"] or {}).get("百度")
    if not bd:
        return {"slug": slug, "title": sh["title"], "error": "没有百度链接"}
    surl, pwd = kdocs.baidu_surl(bd.get("url", ""))
    if not surl:
        return {"slug": slug, "title": sh["title"], "error": "解析 surl 失败"}

    a = alist()
    mount = f"/sub/{slug}"
    a.ensure_share_storage(mount, surl, pwd, web_proxy=WEB_PROXY)

    files = [(p, o) for p, o in a.walk(mount)
             if Path(p).suffix.lower() in VIDEO_EXT]
    local = local_files(slug)

    # 先用第一个文件试链：BDUSS 无效时 Alist 会返回 JSON 错误，
    # 否则 aria2 会把 27 个错误响应当成"下载完成"写满磁盘
    if files:
        probe_path = files[0][0]
        try:
            probe_url = a.link(probe_path)
            r = httpx.get(probe_url, timeout=45, follow_redirects=True,
                          headers={"Range": "bytes=0-320"})
            head = r.content[:320].lstrip()
            if head.startswith(b'{"code"'):
                raise RuntimeError("百度拒绝取链：" + r.text[:150])
        except httpx.HTTPError as e:
            raise RuntimeError(f"试链失败：{type(e).__name__}: {e}")

    queued = aria().queue_names()
    out_dir = show_root(slug)
    out_dir.mkdir(parents=True, exist_ok=True)

    added, failed = 0, 0
    for path, obj in files:
        name = obj.get("name") or Path(path).name
        if name in local:
            store.mark_file(slug, name, "done")
            continue
        if name in queued:
            store.mark_file(slug, name, "queued")
            continue
        try:
            url = a.link(path)
            aria().add(url, str(out_dir), out=name)
            store.mark_file(slug, name, "queued")
            added += 1
        except Exception as e:
            store.mark_file(slug, name, "error", str(e))
            failed += 1

    # 对账：标记为已入队但既不在本地、也不在 aria2 队列里的条目视为失败
    try:
        now_q = aria().queue_names()
        for name, st in store.file_status(slug).items():
            if st == "queued" and name not in local and name not in now_q:
                store.mark_file(slug, name, "error", "未落盘")
    except Exception:
        pass

    return {"slug": slug, "title": sh["title"], "total": len(files),
            "local": len(local), "added": added, "failed": failed}


# ---------- 百度转存下载（BaiduPCS-Go）----------
# 百度的「分享直下」接口会对第三方客户端返回 9019 needverify（新旧 Alist 都一样），
# 但「转存」接口是好的，所以主流程走：转存到网盘临时目录 -> 下载 -> 删掉副本。

_PHASES: dict[str, dict] = {}
_QUEUE: "queue.Queue[str]" = queue.Queue()
BACKOFF = {"delay": 0.0}
_PROC: dict[str, subprocess.Popen] = {}
_CANCEL: set[str] = set()
_PROG: dict[str, dict] = {}
_PAUSED = {"on": False}
_NEXT_SYNC = {"ts": 0.0}
_QUEUED = set()

PROG_RE = re.compile(r"↓\s*([\d.]+\s*[KMGT]?i?B)/([\d.]+\s*[KMGT]?i?B)\s+([\d.]+\s*[KMGT]?i?B)/s")
UNIT = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4,
        "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4}


def to_bytes(s: str) -> float:
    m = re.match(r"([\d.]+)\s*([KMGT]?i?B)", s.strip().upper())
    if not m:
        return 0.0
    return float(m.group(1)) * UNIT.get(m.group(2), 1)


def read_progress(log_path: Path) -> dict | None:
    """从 BaiduPCS-Go 日志尾部抓「↓ 1.27GB/1.32GB 11.5MB/s」。"""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 3000))
            tail = f.read().decode("utf-8", "replace").replace("\r", "\n")
    except OSError:
        return None
    last = None
    for line in tail.splitlines():
        m = PROG_RE.search(line)
        if m:
            last = m
    if not last:
        return None
    cur, tot, spd = last.group(1), last.group(2), last.group(3)
    cb, tb = to_bytes(cur), to_bytes(tot)
    if tb <= 0:
        return None
    return {"pct": max(0, min(100, round(cb * 100 / tb))),
            "speed": spd.replace(" ", "") + "/s", "done": int(cb), "total": int(tb)}


def pcs_download(slug: str, src: str, dst: str, timeout: int = 6 * 3600) -> tuple[int, str]:
    """可中断的下载：把子进程登记下来，取消接口能立刻 kill 掉。"""
    log_path = Path(PCS_CFG) / f"dl-{abs(hash(slug)) % 10000}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "BAIDUPCS_GO_CONFIG_DIR": PCS_CFG}
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        p = subprocess.Popen(
            [PCS_BIN, "download", src, "-p", PCS_PARALLEL, "--saveto", dst],
            stdout=log, stderr=subprocess.STDOUT, env=env, text=True)
        _PROC[slug] = p
        end = time.time() + timeout
        try:
            while p.poll() is None:
                if slug in _CANCEL:
                    p.terminate()
                    for _ in range(10):
                        if p.poll() is not None:
                            break
                        time.sleep(0.3)
                    if p.poll() is None:
                        p.kill()
                    break
                if time.time() > end:
                    p.kill()
                    break
                pr = read_progress(log_path)
                if pr:
                    _PROG[slug] = pr
                time.sleep(1.2)
        finally:
            _PROC.pop(slug, None)
            _PROG.pop(slug, None)
            code = p.poll() if p.poll() is not None else -1
    try:
        out = log_path.read_text("utf-8", errors="replace")[-1200:]
    except Exception:
        out = ""
    return code, out


def set_phase(slug: str, phase: str, msg: str = "") -> None:
    _PHASES[slug] = {"phase": phase, "msg": msg[:300], "ts": int(time.time())}
    print(f"[{slug[:20]}] {phase} {msg[:100]}", flush=True)


def pcs(*args, timeout: int = 600) -> tuple[int, str]:
    Path(PCS_CFG).mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "BAIDUPCS_GO_CONFIG_DIR": PCS_CFG}
    try:
        p = subprocess.run([PCS_BIN, *args], capture_output=True, text=True,
                           env=env, timeout=timeout, errors="replace")
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, f"超时 {timeout}s"
    except FileNotFoundError:
        return 127, f"缺少可执行文件 {PCS_BIN}"


def pcs_login() -> None:
    bd = settings.get("bduss", ALIST_BDUSS)
    st = settings.get("stoken", "")
    if not bd:
        raise RuntimeError("未配置 BDUSS")
    if not st:
        raise RuntimeError("未配置 STOKEN（转存必须要它，见设置页）")
    _, out = pcs("login", f"-cookies=BDUSS={bd}; STOKEN={st}")
    if "登录成功" in out:
        return
    # 百度的「校验 BDUSS 有效性」接口会被风控/需要验证，返回 HTML 导致解析失败；
    # 但本地会话往往还是好的（pcs who 能报出账号）。这种情况继续用现有会话，别让全站瘫掉。
    code2, out2 = pcs("who")
    if code2 == 0 and ("用户名" in (out2 or "") or "uid" in (out2 or "")):
        print("[pcs] login 校验失败，但本地会话仍有效，继续用现有登录", flush=True)
        return
    raise RuntimeError(f"百度登录失败：{out[-200:]}")


QUARK_MOUNT = "/quark"


def _size_txt(n) -> str:
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "0"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f}{unit}"
        n /= 1024
    return f"{n:.2f}TB"


def ensure_quark_mount() -> str:
    """确保 Alist 里挂着夸克网盘（取直链靠它，因为夸克对大文件不直接给 URL）。"""
    if not quark.has_cookie():
        raise RuntimeError("夸克未登录：去设置页「连接与账号 → 夸克网盘」扫码")
    a = alist()
    try:
        a.ensure_quark_storage(QUARK_MOUNT, quark.cookie_header())
    except Exception as e:
        raise RuntimeError(f"Alist 挂载夸克失败：{e}")
    return QUARK_MOUNT


def download_show_quark(slug: str, sh: dict, src: dict) -> None:
    """夸克下载：转存到自己网盘 → 用 Alist 取直链 → aria2 落盘。"""
    mount = ensure_quark_mount()
    safe = re.sub(r"[^\w\u4e00-\u9fa5-]", "", slug)[:24] or "show"
    folder = f"PanFollow-{safe}"

    set_phase(slug, "转存中", f"夸克：转存到「{folder}」")
    fid = quark.ensure_folder(folder)
    quark.transfer(src["url"], to_pdir_fid=fid)

    set_phase(slug, "列目录", "等 Alist 看到转存内容…")
    a = alist()
    files = []
    for _ in range(6):
        time.sleep(2.0)
        try:
            # Alist 会缓存目录树，新建的文件夹要先刷一次挂载根才能看到
            a.list_dir(mount, refresh=True)
            files = [(p, o) for p, o in a.walk(f"{mount}/{folder}", refresh=True)
                     if Path(p).suffix.lower() in VIDEO_EXT | {".zip"}]
        except Exception:
            files = []
        if files:
            break
    if not files:
        raise RuntimeError("转存后在 Alist 里看不到文件（可能任务还没跑完，或分享为空）")

    start_ep = store.sub_map().get(slug, 1)
    pref = store.get_pref(slug) or "best"
    zips = [(p, o) for p, o in files if Path(p).suffix.lower() == ".zip"]
    vids = [(p, o) for p, o in files if Path(p).suffix.lower() in VIDEO_EXT]
    flat = [(p, o.get("name") or Path(p).name, _size_txt(o.get("size")))
            for p, o in vids]
    an = analyze_files(flat, start_ep, pref) if flat else {"targets": []}
    targets = an["targets"]
    if not targets and not zips:
        raise RuntimeError("分享里没有符合条件的视频文件")

    out_dir = show_root(slug)
    out_dir.mkdir(parents=True, exist_ok=True)
    local = local_files(slug)
    queued = aria().queue_names()

    # 任务清单：视频按集挑，zip 整包下完再解压（按文件名去重，分享里常有重复条目）
    jobs, _seen = [], set()
    for t in targets:
        name = t[1]
        path = next((p for p, o in files
                     if (o.get("name") or Path(p).name) == name), "")
        if path and name not in _seen:
            _seen.add(name)
            jobs.append((name, path, False))
    for p, o in zips:
        zname = o.get("name") or Path(p).name
        if zname not in _seen:
            _seen.add(zname)
            jobs.append((zname, p, True))

    done = 0
    for i, (name, path, is_zip) in enumerate(jobs, 1):
        if name in local or name in queued:
            continue
        set_phase(slug, "下载中", f"夸克 [{i}/{len(jobs)}] {name}")
        try:
            url = a.link(path)
            # 试链：直链过期时 Alist 会回 JSON/HTML 错误，aria2 会当成文件存下来
            r = httpx.get(url, timeout=60, follow_redirects=True,
                          headers={"Range": "bytes=0-320"})
            head = (r.content or b"")[:320].lstrip()
            if r.status_code >= 400 or head.startswith(b'{"code"') or head.startswith(b"<"):
                raise RuntimeError(f"直链无效（HTTP {r.status_code}）")
        except Exception as e:
            print(f"[quark] {slug}: 取链失败 {name}: {e}", flush=True)
            continue
        aria().add(url, str(out_dir), out=name)
        store.mark_file(slug, name, "queued")
        done += 1

    if not done:
        print(f"[quark] {slug}: 没有新增需要下载的文件", flush=True)
        return

    # 等这一批落盘
    deadline = time.time() + 8 * 3600
    last = 0
    while time.time() < deadline:
        have = sum(1 for n, _, _ in jobs if (out_dir / n).exists())
        if have != last:
            last = have
            set_phase(slug, "下载中", f"夸克 {have}/{len(jobs)} 已落盘")
        if have >= len(jobs):
            break
        if not busy_aria():
            time.sleep(3)
            if sum(1 for n, _, _ in jobs if (out_dir / n).exists()) >= len(jobs):
                break
        time.sleep(5)

    # 大小对账：明显小于源文件就是下坏了（典型是错误页被当文件）
    want = {}
    for p, o in files:
        nm = o.get("name") or Path(p).name
        try:
            want[nm] = int(o.get("size") or 0)
        except Exception:
            want[nm] = 0
    for name, path, is_zip in jobs:
        f2 = out_dir / name
        w = want.get(name) or 0
        if f2.exists() and w and f2.stat().st_size < w * 0.9 and not (is_zip and f2.stat().st_size > 1024):
            try:
                print(f"[quark] {slug}: {name} 只有 {f2.stat().st_size} / {w}，删掉重试", flush=True)
                f2.unlink()
            except OSError:
                pass

    # 解压 zip（夸克很多分享是分卷 zip，不是单集视频）
    for name, path, is_zip in jobs:
        z = out_dir / name
        if not is_zip or not z.exists():
            continue
        try:
            set_phase(slug, "解压中", f"解压 {name}")
            n = unzip_into(z, out_dir)
            z.unlink(missing_ok=True)
            print(f"[quark] {slug}: 解压 {name} → {n} 个文件", flush=True)
        except Exception as e:
            print(f"[quark] {slug}: 解压失败 {name}: {e}", flush=True)

    have_video = sum(1 for f in local_files(slug))
    print(f"[quark] {slug}: 完成，落盘 {last}/{len(jobs)}，当前视频 {have_video} 个", flush=True)

    # 下完就把网盘里的转存清掉，别占空间（下次要下会重新转存）
    if last >= len(jobs):
        try:
            ids = [o.get("fid") for _, o in files if o.get("fid")]
            quark.remove(ids)
            quark.remove([fid])
            print(f"[quark] {slug}: 已清理网盘暂存", flush=True)
        except Exception as e:
            print(f"[quark] {slug}: 清理暂存失败 {e}", flush=True)


def unzip_into(zpath: Path, out_dir: Path) -> int:
    """解压到目标目录，文件名优先 UTF-8，失败退回 GBK（国产压缩包常见）。"""
    import zipfile
    n = 0
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if not (info.flag_bits & 0x800):        # 非 UTF-8 标记
                try:
                    name = name.encode("cp437").decode("gbk")
                except Exception:
                    pass
            name = name.replace("\\", "/").split("/")[-1]
            if not name or name.startswith("."):
                continue
            dst = out_dir / name
            if dst.exists() and dst.stat().st_size == info.file_size:
                continue
            with z.open(info) as src, dst.open("wb") as fh:
                while True:
                    buf = src.read(1 << 20)
                    if not buf:
                        break
                    fh.write(buf)
            n += 1
    return n


def busy_aria() -> bool:
    try:
        st = aria().stats()
        return bool(st.get("ok") and (st.get("active") or st.get("waiting")))
    except Exception:
        return False


def download_show(slug: str) -> None:
    sh = store.show(slug)
    if not sh:
        raise RuntimeError("未找到该剧")
    # 优先用选好的来源；没有来源记录就退回文档里的百度链接
    src = store.active_source(slug)
    if src and src.get("dead"):
        print(f"[pick] {slug}: 当前来源已标失效，重新选优", flush=True)
        src = pick_source(slug) or store.active_source(slug)
    # 来源还活着但集数不够（作者停更 / 那条链接不再更新）→ 换一个能补齐的
    if src:
        _lb = local_ep_scores(slug)
        _local_max = max(_lb) if _lb else 0
        _doc_eps = max(int(sh.get("episodes") or 0), int(sh.get("total_eps") or 0))
        if _doc_eps > _local_max:
            _eps = int(src.get("probe_eps") or 0)
            if _eps < _doc_eps:
                print(f"[pick] {slug}: 当前来源 {_eps} 集、还差到 {_doc_eps} 集，找更全的来源",
                      flush=True)
                nxt = pick_source(slug, need=_doc_eps)
                if nxt and int(nxt.get("probe_eps") or 0) > _eps:
                    src = nxt
                    set_phase(slug, "换源补全",
                              f"{src.get('label')} 有 {src.get('probe_eps')} 集")
    if src and (src.get("provider") or "百度") == "夸克":
        return download_show_quark(slug, sh, src)
    if src and (src.get("provider") or "百度") == "百度":
        url, pwd0 = src["url"], src.get("pwd") or ""
        src_label = src.get("label") or "百度"
    else:
        bd = (sh["links"] or {}).get("百度")
        if not bd:
            raise RuntimeError("这条没有百度链接")
        url, pwd0 = bd.get("url", ""), bd.get("pwd") or ""
        src_label = "文档自带"
    surl, pwd = kdocs.baidu_surl(url)
    pwd = pwd0 or pwd or ""
    if not surl:
        raise RuntimeError("解析分享链接失败")

    pan = f"{PAN_TMP}/{slug}"
    set_phase(slug, "清理网盘暂存")
    pcs("rm", pan)
    pcs("mkdir", pan)
    code, out = pcs("cd", pan)
    if code != 0:
        raise RuntimeError(f"切换网盘目录失败：{out[-160:]}")

    set_phase(slug, "转存中")
    code, out = pcs("transfer", f"https://pan.baidu.com/s/{surl}", pwd or "1")
    if "成功" not in out:
        if "文件重复" in out:
            # 上一次转存的内容还在，直接复用，不算失败
            print(f"[transfer] {slug}: 目录里已有同一份内容，直接复用", flush=True)
        elif _looks_dead(out):
            mark_dead_link(slug, url, out[-120:])
            if src and src.get("id"):
                store.mark_source_dead(src["id"], out[-120:])
                print(f"[dead] {slug}: 来源 {src['id']} 已标记失效", flush=True)
            # 立刻换下一条可用来源重试（死链已排除，不会死循环）
            nxt = pick_source(slug)
            if nxt and (nxt.get("id") != (src or {}).get("id")):
                set_phase(slug, "换源重试",
                          f"上一条链接失效，改用「{nxt.get('label') or nxt.get('provider')}」")
                print(f"[retry] {slug}: 换到来源 {nxt.get('id')} 重试", flush=True)
                return download_show(slug)
            raise RuntimeError("分享已失效（作者删除或替换了链接），等文档更新出新的再试")
        else:
            raise RuntimeError(f"转存失败：{out[-240:]}")

    start_ep = store.sub_map().get(slug, 1)

    # 磁盘保护：剩余空间低于阈值就不下，避免把 NAS 撑爆
    try:
        min_free = float(settings.get("min_free_gb", "") or os.environ.get("MIN_FREE_GB", "20"))
    except ValueError:
        min_free = 20.0
    free_gb = shutil.disk_usage(dl_root()).free / 1024**3
    if min_free > 0 and free_gb < min_free:
        notify.notify("磁盘空间不足，已暂停下载",
                      f"《{sh['title']}》未开始：剩余 {free_gb:.1f}GB < 阈值 {min_free:.0f}GB")
        raise RuntimeError(f"磁盘剩余 {free_gb:.1f}GB，低于阈值 {min_free:.0f}GB，已跳过")

    set_phase(slug, "列目录", "正在读取分享里的文件…")
    all_files = walk_pan(pan)

    pref = (store.get_pref(slug) or "best").lower()
    an = analyze_files(all_files, start_ep, pref)
    targets = an["targets"]
    best_label = an["label"]
    if an.get("dropped"):
        print(f"[skip-dirs] {slug}: 跳过 {an['dropped']}", flush=True)
    print(f"[quality] {slug}: 偏好={pref} 选中={sorted(an['keep'])} "
          f"画质={best_label}({an['score']})", flush=True)

    if not targets:
        pcs("rm", pan)
        raise RuntimeError(f"没有符合条件的文件（起始集 {start_ep}，共扫到 {len(all_files)} 个）")

    before = len(local_files(slug))
    dst_root = show_root(slug)
    dst_root.mkdir(parents=True, exist_ok=True)
    moved = flatten_dir(dst_root)
    if moved:
        print(f"[flatten] {slug}: 把 {moved} 个文件挪到 {dst_root}", flush=True)

    # 本地每集现有画质（残缺/空壳文件不算，且顺手清掉）
    local_ep: dict[int, tuple[int, Path]] = {}
    junk = 0
    for p in list(dst_root.rglob("*")):
        if not p.is_file():
            continue
        if p.name.endswith(PARTIAL_SUFFIX):
            continue
        e = parse_ep(p.name)
        if e is None:
            continue
        if is_partial(p) or not is_valid_media(p):
            drop_partial(p)               # 断点残片 / 全零空壳：删掉重下
            junk += 1
            print(f"[junk] {slug}: 清理坏文件 {p.name}", flush=True)
            continue
        sc = quality_score(str(p.relative_to(dst_root)))
        if e not in local_ep or sc > local_ep[e][0]:
            local_ep[e] = (sc, p)
    if junk:
        print(f"[cleanup] {slug}: 清掉 {junk} 个残缺/空壳文件，重新下载", flush=True)

    size_map = {name: sz for _, name, sz in all_files}

    # 只处理「本地没有」或「本地画质更低」的集
    todo = [(rel, name, ep, sc) for rel, name, ep, sc in targets
            if ep is None or ep not in local_ep or local_ep[ep][0] < sc]
    skipped = len(targets) - len(todo)
    if not todo:
        if not PAN_KEEP:
            pcs("rm", pan)
        set_phase(slug, "完成", f"已是最佳画质 · {best_label} · {len(targets)} 集")
        store.touch_policy(slug, last_done=int(time.time()), new_eps=0, upgradable=0)
        return

    set_phase(slug, "下载中",
              f"{len(todo)} 集 · {best_label} · 从第 {start_ep} 集起 · 已有 {before} 个")

    errors = []
    replaced = 0
    for i, (rel, name, ep, sc) in enumerate(todo, 1):
        if slug in _CANCEL:
            break
        old = local_ep.get(ep) if ep is not None else None
        set_phase(slug, "下载中",
                  f"[{i}/{len(todo)}] 第 {ep if ep is not None else '?'} 集 {name}")
        target_file = dst_root / name
        if is_partial(target_file):       # 同一文件上次留了残片，先清掉
            drop_partial(target_file)
        code, out = pcs_download(slug, f"{pan}/{rel}", str(dst_root))
        if slug in _CANCEL:
            break
        if code != 0:
            errors.append(f"{name}: {out[-120:]}")
            drop_partial(target_file)     # 别让残片污染下一轮
            print(f"[fail] {slug}: 第 {ep} 集失败，已清理残片 {name}", flush=True)
            continue
        # 下完校验：文件头对不对、大小和分享里是否一致（容忍 1‰ 或 1MB 显示误差）
        got = target_file.stat().st_size if target_file.exists() else 0
        want = parse_size(size_map.get(name, 0))
        tol = size_tolerance(size_map.get(name, 0))
        off_by = abs(got - want) if want else 0
        if not is_valid_media(target_file) or (want and off_by > tol):
            drop_partial(target_file)
            errors.append(f"{name}: 校验失败 {got}/{want} 字节")
            print(f"[bad] {slug}: 第 {ep} 集校验失败（{got}/{want}），已删除", flush=True)
            continue
        if old and old[1].exists():       # 自动替换低画质旧文件
            try:
                old[1].unlink()
                replaced += 1
                print(f"[replace] {slug}: 第 {ep} 集 {old[1].name} → {name}", flush=True)
            except OSError:
                pass

    after = len(local_files(slug))
    if not PAN_KEEP:
        pcs("rm", pan)

    if slug in _CANCEL:
        _CANCEL.discard(slug)
        set_phase(slug, "已取消", f"停在 {after} 个文件，下次同步会从断点继续")
        return

    if errors and after <= before:
        raise RuntimeError(f"下载失败：{errors[0]}")

    msg = f"新增 {after - before} 个，共 {after} 个文件 · {best_label}"
    if skipped:
        msg += f"（{skipped} 集已是同画质跳过）"
    if replaced:
        msg += f" · 升级替换 {replaced} 集"
    if errors:
        msg += f"（{len(errors)} 个失败）"
    store.touch_policy(slug, last_done=int(time.time()), new_eps=0, upgradable=0)
    set_phase(slug, "完成", msg)
    notify.notify(f"《{sh['title']}》下载完成" if not errors else f"《{sh['title']}》部分完成",
                  f"{msg}\n目录：{show_root(slug)}")


def _worker() -> None:
    while True:
        while _PAUSED["on"]:
            time.sleep(2)
        slug = _QUEUE.get()
        _QUEUED.discard(slug)
        sh = store.show(slug) or {}
        try:
            if slug in _CANCEL:
                _CANCEL.discard(slug)
                set_phase(slug, "已取消", "已从队列移除")
                continue
            # 占用百度通道，期间检查线程不会插进来改工作目录
            _PCS_LOCK.acquire()
            try:
                pcs_login()
                download_show(slug)
            finally:
                _PCS_LOCK.release()
        except Exception as e:
            set_phase(slug, "失败", f"{type(e).__name__}: {e}")
            print(traceback.format_exc(), flush=True)
            try:
                notify.notify(f"《{sh.get('title', slug)}》下载失败",
                              f"{type(e).__name__}: {e}"[:300])
            except Exception:
                pass
        finally:
            _QUEUE.task_done()
        time.sleep(1.0)


def enqueue(slug: str) -> bool:
    """入队（同一部剧排队期间不重复入队）。"""
    with _LOCK:
        if slug in _QUEUED:
            return False
        _QUEUED.add(slug)
    _QUEUE.put(slug)
    return True


def do_sync() -> dict:
    slugs = store.subscribed()
    if not slugs:
        return {"ok": True, "queued": 0, "note": "还没有订阅"}
    busy = {s for s, v in _PHASES.items()
            if v.get("phase") in ("排队中", "清理网盘暂存", "转存中", "下载中")}
    n = 0
    dead = []
    nolink = []
    for s in slugs:
        if s in busy:
            continue
        sh = store.show(s) or {}
        url = ((sh.get("links") or {}).get("百度") or {}).get("url", "")
        srcs = store.list_sources(s)
        has_baidu = bool(url) or any((x.get("provider") or "百度") == "百度" for x in srcs)
        if not has_baidu:
            nolink.append(sh.get("title") or s)
            set_phase(s, "缺链接", "只有清单信息、没有分享链接，补一个来源才能下")
            continue
        if url and is_dead_link(s, url):
            dead.append(sh.get("title") or s)
            set_phase(s, "链接失效", "文档里的分享链接已失效，等更新出新链接")
            continue
        set_phase(s, "排队中")
        if enqueue(s):
            n += 1
    store.set_meta("last_sync", str(int(time.time())))
    out = {"ok": True, "queued": n, "busy": len(busy), "subscribed": len(slugs),
           "already": max(0, len(slugs) - len(busy) - n)}
    notes = []
    if dead:
        out["dead"] = dead
        notes.append(f"{len(dead)} 部链接失效")
    if nolink:
        out["nolink"] = nolink
        notes.append(f"{len(nolink)} 部缺分享链接")
    if notes:
        out["note"] = "、".join(notes) + "，已跳过"
    return out


def busy_downloading() -> bool:
    return any(v.get("phase") in ("排队中", "清理网盘暂存", "转存中", "列目录", "下载中")
               for v in _PHASES.values())


def scheduler() -> None:
    interval = max(0.25, REFRESH_HOURS) * 3600
    time.sleep(25)
    while True:
        try:
            # 抓文档要开 Chromium，和下载抢 fds，下载中就先等一会儿
            waited = 0
            while busy_downloading() and waited < 1800:
                time.sleep(60)
                waited += 60
            if DOC_URL:
                print("[scheduler] refresh", do_refresh(), flush=True)
            if AUTO_SYNC:
                print("[scheduler] sync", do_sync(), flush=True)
            else:
                print(f"[scheduler] 自动下载已关闭，只抓文档（每 {REFRESH_HOURS:g}h）", flush=True)
        except Exception:
            print(traceback.format_exc(), flush=True)
        _NEXT_SYNC["ts"] = time.time() + interval
        time.sleep(interval)


# ---------- 每部剧的定时检测 ----------
CHECK_ALLOWED = (3, 6, 24, 168)


def local_max_ep(slug: str) -> int:
    root = show_root(slug)
    mx = 0
    if not root.exists():
        return 0
    for p in root.rglob("*"):
        if p.is_file():
            e = parse_ep(p.name)
            if e and e > mx:
                mx = e
    return mx


# ---------- 多资源来源：探测 / 打分 / 选优 ----------
SOURCE_TTL = 3600
_PROBE_BEST: dict[int, dict] = {}


def probe_source(src: dict, force: bool = False) -> dict:
    """探测一个来源（转存 + 列目录），把元数据写回 sources 表。"""
    sid = int(src["id"])
    now = int(time.time())
    if (not force and src.get("last_probe")
            and now - int(src["last_probe"]) < SOURCE_TTL and src.get("probe_eps")):
        return src

    slug = src["slug"]
    prov = src.get("provider") or "百度"
    note = ""
    data = {"probe_eps": 0, "probe_max": 0, "probe_quality": "",
            "probe_score": 0, "probe_bytes": 0}
    pan = f"/probe/{slug[:24]}-{sid}-{now}"
    try:
        if prov == "夸克":
            # 夸克分享游客即可读：数集数、算体积、判画质，不需要登录
            files = quark.share_tree(src["url"])
            an = analyze_files(files, 1, (store.get_pref(slug) or "best"))
            best = an["best"]
            ep = max(best) if best else 0
            # 电影这类单文件没有集号，当作 1 集，否则永远排不进候选
            eps_n = len(best) if best else (1 if any(
                Path(n).suffix.lower() in VIDEO_EXT for _, n, _ in files) else 0)
            if not best and eps_n:
                ep = 1
            tot = sum(parse_size(sz) for _, _, sz in files)
            data = {"probe_eps": eps_n, "probe_max": ep,
                    "probe_quality": an["label"], "probe_score": an.get("score") or 0,
                    "probe_bytes": tot}
            store.update_source(sid, last_probe=now, probe_note="夸克·免登录探测",
                                **data)
            print(f"[probe] {slug}: 夸克 {eps_n} 集 · {an['label']}", flush=True)
            return store.get_source(sid) or src
        if prov != "百度":
            raise RuntimeError(f"{prov} 暂不支持探测（目前只做百度）")
        surl, pwd2 = kdocs.baidu_surl(src["url"])
        if not surl:
            raise RuntimeError("解析分享链接失败")
        pcs_login()
        pcs("mkdir", pan)
        code, out = pcs("cd", pan)
        if code != 0:
            raise RuntimeError(f"切换目录失败：{out[-80:]}")
        code, out = pcs("transfer", f"https://pan.baidu.com/s/{surl}",
                        (src.get("pwd") or pwd2 or "1"), timeout=600)
        if "成功" not in out and "文件重复" not in out:
            if _looks_dead(out):
                mark_dead_link(slug, src["url"], out[-100:])
                raise RuntimeError("分享链接已失效")
            raise RuntimeError(f"转存失败：{out[-120:]}")
        files = walk_pan(pan)
        pcs("cd", "/")
        pcs("rm", pan)

        pref = (store.get_pref(slug) or "best").lower()
        an = analyze_files(files, 1, pref)
        total = 0
        for rel, _name, sz in files:
            if Path(rel).suffix.lower() in VIDEO_EXT:
                total += parse_size(sz)
        _PROBE_BEST[sid] = {"best": an["best"], "ts": now}
        data = {"probe_eps": len(an["best"]),
                "probe_max": max(an["best"]) if an["best"] else 0,
                "probe_quality": an["label"],
                "probe_score": an["score"],
                "probe_bytes": total}
    except Exception as e:
        note = f"{type(e).__name__}: {e}"[:160]
        try:
            pcs("cd", "/")
            pcs("rm", pan)
        except Exception:
            pass
    store.update_source(sid, last_probe=now, probe_note=note, **data)
    return store.get_source(sid) or src


def rank_sources(cands: list[dict], need: int = 0) -> list[dict]:
    """规则排序。

    给了 need（这部剧还需要多少集才算齐）时，先看谁能覆盖 need：
      · 有能覆盖的 → 在能覆盖的里面挑画质最好的（画质优先只在"都能下完"时才有意义）
      · 一个都覆盖不了 → 挑集数最全的（谁下得多先用谁），画质做次级
    没给 need 时沿用原规则：画质优先，集数差 2 集以内视为并列。
    """
    good = [s for s in cands if (s.get("probe_eps") or 0) > 0]
    if not good:
        return []
    q = lambda s: s.get("probe_score") or 0

    if need > 0:
        cover = [s for s in good if s["probe_eps"] >= need]
        if cover:
            cover.sort(key=lambda s: (-q(s), -s["probe_eps"], -(s.get("priority") or 0)))
            return cover
        most = max(s["probe_eps"] for s in good)
        keep = [s for s in good if s["probe_eps"] >= most - 1]
        keep.sort(key=lambda s: (-s["probe_eps"], -q(s), -(s.get("priority") or 0)))
        return keep

    best_q = max(q(s) for s in good)
    tier = [s for s in good if q(s) >= best_q * 0.92]
    max_eps = max(s["probe_eps"] for s in tier)
    keep = [s for s in tier if s["probe_eps"] >= max_eps - 2]
    if not keep:
        max_eps_all = max(s["probe_eps"] for s in good)
        keep = [s for s in good if s["probe_eps"] >= max_eps_all - 2]
    keep.sort(key=lambda s: (-q(s), -s["probe_eps"], -(s.get("priority") or 0)))
    return keep


def pick_source(slug: str, force: bool = False, allow_ai: bool = True,
                prefer_complete: bool = False, need: int = 0) -> dict | None:
    """探测启用的来源，选出最好的并标为 active。

    prefer_complete=True（手动「换源补全」）会忽略锁定、强制重探、按集数覆盖优先。
    """
    cur = store.active_source(slug)
    auto = settings.get("auto_pick", "1") not in ("0", "", "false", "off")
    # 手动锁定 或 关闭了自动选优 → 直接用当前来源，不重新排
    if cur and (cur.get("locked") or not auto) and not prefer_complete:
        out = dict(cur)
        out["used_ai"] = False
        out["pinned"] = True
        return out

    srcs = store.revivable_sources(slug)  # 已排除死链，新链接优先
    if not srcs:
        return None
    if prefer_complete:
        force = True
        pick_n = 8
    else:
        pick_n = 6
    probed = [probe_source(s, force) for s in srcs[:pick_n]]
    ranked = rank_sources(probed, need=need)
    if not ranked:
        return None
    chosen = ranked[0]
    used_ai = False

    if allow_ai and ai.enabled() and len(ranked) >= 2 and not (prefer_complete and need > 0):

        q = lambda s: s.get("probe_score") or 0
        a, b = ranked[0], ranked[1]
        close = (q(a) - q(b)) <= 0.05 * max(1, q(a))
        unknown = ("未知" in (a.get("probe_quality") or "")
                   or "未知" in (b.get("probe_quality") or ""))
        if close or unknown:
            cands = [{
                "id": s["id"], "provider": s.get("provider"),
                "label": s.get("label") or s.get("provider") or "",
                "eps": s.get("probe_eps"), "max_ep": s.get("probe_max"),
                "quality": s.get("probe_quality"),
                "avg_mb": round((s.get("probe_bytes") or 0)
                                / max(1, s.get("probe_eps") or 1) / 1048576),
            } for s in ranked[:3]]
            title = (store.show(slug) or {}).get("title", slug)
            r = ai.judge(title, store.get_pref(slug), cands)
            if r:
                used_ai = True
                chosen = next((s for s in ranked if int(s["id"]) == int(r["pick"])), chosen)
                store.set_meta(f"aireason:{slug}", r.get("reason", ""))

    prev = store.active_source(slug)
    store.set_active(slug, chosen["id"])
    if prev and int(prev["id"]) != int(chosen["id"]):
        print(f"[switch] {slug}: {prev.get('label')}({prev.get('probe_quality')}) "
              f"→ {chosen.get('label')}({chosen.get('probe_quality')})", flush=True)
        try:
            t = (store.show(slug) or {}).get("title", slug)
            notify.notify(f"《{t}》切换了资源来源",
                          f"{prev.get('label')} → {chosen.get('label')}"
                          f"（{chosen.get('probe_quality')}，{chosen.get('probe_eps')} 集）")
        except Exception:
            pass
    out = dict(chosen)
    out["used_ai"] = used_ai
    return out


def probe_show(slug: str) -> dict:
    """转存 + 列目录，只看有多少集，不下载（看完立刻删暂存）。"""
    sh = store.show(slug)
    if not sh:
        raise RuntimeError("剧不存在")
    bd = (sh["links"] or {}).get("百度")
    if not bd:
        raise RuntimeError("没有百度链接")
    surl, pwd = kdocs.baidu_surl(bd.get("url", ""))
    if not surl:
        raise RuntimeError("解析分享链接失败")

    pcs_login()
    # 带时间戳，永远不会撞上残留目录的「文件重复」
    pan = f"/probe/{slug[:30]}-{int(time.time())}"
    pcs("mkdir", pan)
    code, out = pcs("cd", pan)
    if code != 0:
        raise RuntimeError(f"切换目录失败：{out[-100:]}")
    code, out = pcs("transfer", f"https://pan.baidu.com/s/{surl}", pwd or "1", timeout=600)
    if "成功" not in out:
        if "文件重复" in out:
            print(f"[probe] {slug}: 目录里已有同一份内容，直接复用", flush=True)
        elif _looks_dead(out):
            mark_dead_link(slug, bd.get("url", ""), out[-120:])
            pcs("rm", pan)
            raise RuntimeError("分享已失效（作者删除或替换了链接），等文档更新出新的再试")
        else:
            pcs("rm", pan)
            raise RuntimeError(f"转存失败：{out[-160:]}")
    try:
        files = walk_pan(pan)
    finally:
        pcs("rm", pan)
    pref = (store.get_pref(slug) or "best").lower()
    an = analyze_files(files, 1, pref)
    best = an["best"]
    return {"count": len(files), "max_ep": max(best) if best else 0,
            "best": best, "label": an["label"]}


def do_check(slug: str) -> dict:
    """检查分享是否有新集 / 更高画质：记数、通知，按策略决定是否自动入队下载。"""
    sh = store.show(slug) or {}
    url = ((sh.get("links") or {}).get("百度") or {}).get("url", "")
    if url and is_dead_link(slug, url):
        return {"ok": False, "dead": True, "error": "分享链接已失效，等文档更新出新链接"}

    # 下载占着百度通道时不插队（BaiduPCS-Go 的工作目录是全局的）
    if not _PCS_LOCK.acquire(blocking=False):
        print(f"[check] {slug}: 正在下载，本轮跳过", flush=True)
        return {"ok": False, "busy": True, "error": "正在下载，稍后再检查"}
    try:
        # 还差多少集才算齐 —— 缺集时选源要以"能补齐"为先
        _lb = local_ep_scores(slug)
        _local_max0 = max(_lb) if _lb else 0
        _doc_eps = max(int(sh.get("episodes") or 0), int(sh.get("total_eps") or 0))
        _need = _doc_eps if _doc_eps > _local_max0 else 0
        chosen = pick_source(slug, need=_need)
        if chosen:
            info = {"best": (_PROBE_BEST.get(int(chosen["id"])) or {}).get("best", {}),
                    "max_ep": chosen.get("probe_max") or 0,
                    "label": chosen.get("probe_quality") or ""}
            if chosen.get("used_ai"):
                print(f"[check] {slug}: AI 仲裁 → {chosen.get('label')}", flush=True)
        else:
            info = probe_show(slug)      # 没有来源记录时退回文档链接
    except Exception as e:
        store.touch_policy(slug, last_check=int(time.time()))
        print(f"[check] {slug} 失败: {e}", flush=True)
        return {"ok": False, "error": str(e)}
    finally:
        _PCS_LOCK.release()

    share_best = info.get("best") or {}
    local_best = local_ep_scores(slug)
    found = info["max_ep"]
    local_max = max(local_best) if local_best else 0
    start_ep = store.sub_map().get(slug, 1)

    # 缺集 / 可升级都只看「起始集之后」的，避免把不打算下的老集算成缺失
    missing = [e for e in share_best
               if e not in local_best and e >= start_ep]
    upgradable = [e for e in share_best
                  if e in local_best and share_best[e] > local_best[e] and e >= start_ep]

    store.touch_policy(slug, last_check=int(time.time()), last_found=found,
                       new_eps=len(missing), upgradable=len(upgradable))
    print(f"[check] {slug}: 分享 {found} 集/{info.get('label')}，本地 {local_max} 集，"
          f"缺 {len(missing)} 集，可升级 {len(upgradable)} 集", flush=True)

    work = bool(missing or upgradable)
    pol = store.get_policy(slug)
    if work:
        parts = []
        if missing:
            parts.append(f"缺 {len(missing)} 集")
        if upgradable:
            parts.append(f"{len(upgradable)} 集可升级到更高画质")
        notify.notify(f"《{sh.get('title', slug)}》有新内容", "，".join(parts))
        if pol.get("auto_dl"):
            busyp = (_PHASES.get(slug) or {}).get("phase", "")
            if busyp not in ("排队中", "清理网盘暂存", "转存中", "列目录", "下载中"):
                if enqueue(slug):
                    print(f"[check] {slug}: 自动下载已入队（缺 {len(missing)}，升级 {len(upgradable)}）",
                      flush=True)
    return {"ok": True, "found": found, "local": local_max,
            "new": len(missing), "upgradable": len(upgradable),
            "auto": bool(pol.get("auto_dl"))}


def check_loop() -> None:
    """每 5 分钟扫一遍，谁到点了就检查谁（每轮最多 2 部，避免扎堆）。"""
    time.sleep(45)
    while True:
        try:
            now = time.time()
            subs = set(store.subscribed())
            pols = store.all_policy()
            due = []
            for slug in subs:
                p = pols.get(slug) or {}
                hrs = int(p.get("check_hours") or 6)
                last = int(p.get("last_check") or 0)
                if now - last < max(1, hrs) * 3600:
                    continue
                busyp = (_PHASES.get(slug) or {}).get("phase", "")
                if busyp in ("排队中", "清理网盘暂存", "转存中", "列目录", "下载中"):
                    continue
                due.append(slug)
            for slug in due[:2]:
                do_check(slug)
        except Exception:
            print(traceback.format_exc(), flush=True)
        time.sleep(300)


# ---------- HTTP ----------

@app.on_event("startup")
def _startup() -> None:
    store.init()
    notify.set_sink(push_event)
    try:
        if not store.list_documents() and DOC_URL:
            store.add_document(DOC_URL, "主文档")
            store.seed_sources_from_links()
        elif DOC_URL:
            store.add_document(DOC_URL, "主文档")
    except Exception:
        print(traceback.format_exc(), flush=True)
    threading.Thread(target=_worker, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=check_loop, daemon=True).start()


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/manifest.webmanifest")
def pwa_manifest():
    return FileResponse(Path(__file__).parent / "static" / "manifest.webmanifest",
                        media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return FileResponse(Path(__file__).parent / "static" / "sw.js",
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/icon-{size}.png")
def pwa_icon_png(size: str):
    if size not in ("180", "192", "512"):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    p = Path(__file__).parent / "static" / ("icon-" + size + ".png")
    if not p.exists():
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return FileResponse(p, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/icon-{size}.png")
def pwa_icon_png(size: str):
    """PWA 图标：icon-192.png / icon-512.png（由 icon.svg 渲染而来）。"""
    if size not in ("192", "512"):
        return JSONResponse({"ok": False, "error": "no such icon"}, status_code=404)
    p = Path(__file__).parent / "static" / f"icon-{size}.png"
    if not p.exists():
        return JSONResponse({"ok": False, "error": "no such icon"}, status_code=404)
    return FileResponse(p, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/icon.svg")
def pwa_icon():
    return FileResponse(Path(__file__).parent / "static" / "icon.svg",
                        media_type="image/svg+xml")


@app.get("/login")
def login_page():
    return FileResponse(Path(__file__).parent / "static" / "login.html")


@app.post("/api/login/start")
def login_start():
    return login.start(DOC_URL)


@app.get("/api/login/status")
def login_status():
    return login.status()


# ---------------- 夸克 ----------------

@app.post("/api/quark/login/start")
def quark_login_start():
    return quark.start()


@app.get("/api/quark/login/status")
def quark_login_status():
    s = quark.status()
    s["verify"] = quark.verify() if s.get("has_cookie") else {"ok": False, "error": "未登录"}
    return s


@app.get("/api/quark/login.png")
def quark_login_png():
    p = quark.shot_path()
    if not p.exists():
        return JSONResponse({"ok": False, "error": "还没有截图"}, status_code=404)
    return FileResponse(p, media_type="image/png",
                        headers={"Cache-Control": "no-store"})


@app.post("/api/quark/cookie")
def quark_save_cookie(req: dict | None = None):
    raw = (req or {}).get("cookie", "")
    try:
        quark.save_cookie(raw)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "verify": quark.verify()}


@app.post("/api/quark/test")
def quark_test():
    v = quark.verify()
    if not v.get("ok"):
        return {"ok": False, "error": v.get("error")}
    try:
        files = quark.share_tree("https://pan.quark.cn/s/7f0aabbf988c")
        return {"ok": True, "verify": v, "sample_files": len(files),
                "first": [f[1] for f in files[:3]]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "verify": v}


@app.get("/settings")
def settings_page():
    return FileResponse(Path(__file__).parent / "static" / "settings.html")


@app.get("/api/settings")
def get_settings():
    m = settings.masked()
    info = {"reachable": False, "logged_in": _ALIST["client"] is not None, "error": ""}
    try:
        info["reachable"] = Alist(ALIST_URL, "", "", "").ping()
    except Exception as e:
        info["error"] = str(e)[:200]
    # 首次打开时尝试登录一次，之后复用（避免触发 Alist 的失败登录限流）
    if not info["logged_in"] and not _ALIST["tried"] and m.get("has_pass"):
        _ALIST["tried"] = True
        try:
            alist()
            info["logged_in"] = True
        except Exception as e:
            info["error"] = str(e)[:200]
    return {"cfg": m, "probe": info,
            "aria2": aria().stats(),
            "free_gb": round(shutil.disk_usage(dl_root()).free / 1024**3, 1),
            "dl": dl_status(),
            "env": {"alist_url": ALIST_URL, "aria2_rpc": ARIA2_RPC,
                    "download_dir": str(dl_root())}}


class NewDirReq(BaseModel):
    parent: str = ""
    name: str


@app.post("/api/dirs")
def make_dir(req: NewDirReq):
    """在选目录的地方新建文件夹。"""
    allowed = [DATA_ROOT.resolve(), DL_ROOT.resolve()]
    base = Path(req.parent).resolve() if req.parent else DATA_ROOT.resolve()
    if not any(str(base) == str(a) or str(base).startswith(str(a) + "/") for a in allowed):
        return {"ok": False, "error": "只能在这里面建目录"}
    name = (req.name or "").strip().strip("/")
    if not name:
        return {"ok": False, "error": "名字不能为空"}
    if any(ch in name for ch in ("..", "\\", "\0")) or name.startswith("."):
        return {"ok": False, "error": "名字里有非法字符"}
    target = base / name
    if target.exists():
        return {"ok": False, "error": "这个文件夹已经存在了"}
    try:
        target.mkdir(parents=True, exist_ok=False)
    except OSError as e:
        return {"ok": False, "error": f"建不了：{e}"}
    return {"ok": True, "path": str(target), "name": name}


@app.get("/api/dirs")
def list_dirs(path: str = ""):
    """浏览容器内可见的目录，供选择下载位置。"""
    allowed = [DATA_ROOT.resolve(), DL_ROOT.resolve()]
    p = Path(path).resolve() if path else DATA_ROOT.resolve()
    if not any(str(p) == str(a) or str(p).startswith(str(a) + "/") for a in allowed):
        return JSONResponse({"ok": False, "error": "只能浏览数据卷内的目录"}, status_code=400)
    dirs = []
    try:
        for d in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if not d.is_dir() or d.name.startswith(".") or d.name.startswith("@"):
                continue
            dirs.append({"name": d.name, "path": str(d)})
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"读不了这个目录：{e}"}, status_code=400)

    parent = ""
    up = p.parent
    if str(up) != str(p) and any(str(up) == str(a) or str(up).startswith(str(a) + "/") for a in allowed):
        parent = str(up)
    return {"ok": True, "path": str(p), "parent": parent, "dirs": dirs,
            "roots": [str(a) for a in allowed], "current": str(dl_root())}


class SettingsReq(BaseModel):
    alist_url: str | None = None
    alist_user: str | None = None
    alist_pass: str | None = None
    bduss: str | None = None
    stoken: str | None = None
    notify_type: str | None = None
    notify_key: str | None = None
    min_free_gb: str | None = None
    download_root: str | None = None
    ai_enabled: str | None = None
    ai_key: str | None = None
    ai_base: str | None = None
    ai_model: str | None = None
    auto_pick: str | None = None


def normalize_bduss(raw: str) -> str:
    """容忍整段 Cookie：'BDUSS=xxx; STOKEN=yyy; ...' 也能用。"""
    v = (raw or "").strip().strip('"').strip("'")
    if not v:
        return ""
    if "BDUSS=" in v:
        m = re.search(r"BDUSS=([^;\s\"']+)", v)
        if m:
            return m.group(1).strip()
    if ";" in v:
        v = v.split(";")[0].strip()
    for pref in ("BDUSS=", "bduss="):
        if v.startswith(pref):
            v = v[len(pref):]
    return v.strip()


def extract_stoken(raw: str) -> str:
    m = re.search(r"STOKEN=([^;\s\"']+)", raw or "")
    return m.group(1).strip() if m else ""


@app.post("/api/settings")
def save_settings(req: SettingsReq):
    data = req.model_dump(exclude_none=True)
    raw = data.get("bduss") or ""
    if "bduss" in data:
        data["bduss"] = normalize_bduss(raw)
    st = data.get("stoken") or extract_stoken(raw)
    if st:
        data["stoken"] = st.strip()
    settings.update(data)
    _ALIST["tried"] = False         # 配置变了，允许重新试一次登录
    info = {"reachable": False, "logged_in": False, "error": ""}
    try:
        a = alist()
        info["reachable"] = True
        info["logged_in"] = bool(a.token)
    except Exception as e:
        info["error"] = str(e)[:200]
    return {"ok": True, "cfg": settings.masked(), "probe": info}


@app.get("/api/login/shot")
def login_shot():
    p = login.shot_path()
    if not p.exists():
        return JSONResponse({"ok": False, "error": "还没有截图"}, status_code=404)
    return FileResponse(p, media_type="image/png",
                        headers={"Cache-Control": "no-store, max-age=0"})


class CookieReq(BaseModel):
    cookie: str


@app.post("/api/login/cookie")
def login_set_cookie(req: CookieReq):
    try:
        login.save_cookie(req.cookie)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/app.apk")
def download_apk():
    p = Path("/state/app.apk")
    if not p.exists():
        return JSONResponse({"ok": False, "error": "APK 未上传"}, status_code=404)
    return FileResponse(p, media_type="application/vnd.android.package-archive",
                        filename="kdocs-tracker.apk")


BAIDU_UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")}


def baidu_check(bduss: str) -> dict:
    """用网盘自己的 uinfo 接口判断 BDUSS 是否有效。

    注意：早期版本用 /api/gettemplatevariable 判断，那个接口会返回 errno -6，
    即使 BDUSS 完全有效——曾因此误判，别再用它。
    """
    out = {"ok": False, "user": "", "vip_type": None, "svip": None, "detail": ""}
    if not bduss:
        out["detail"] = "未填 BDUSS"
        return out
    ck = {"BDUSS": bduss}
    try:
        r = httpx.get("https://pan.baidu.com/rest/2.0/xpan/nas",
                      params={"method": "uinfo"}, cookies=ck,
                      headers=BAIDU_UA, timeout=15, follow_redirects=True)
        j = r.json()
        if j.get("errno") == 0:
            out["ok"] = True
            out["user"] = str(j.get("baidu_name") or j.get("netdisk_name") or "")
            vt = j.get("vip_type")
            out["vip_type"] = vt
            out["svip"] = (vt == 2)
        else:
            out["detail"] = f"errno={j.get('errno')} {j.get('errmsg') or ''}".strip()
    except Exception as e:
        out["detail"] = f"{type(e).__name__}: {e}"
    return out


@app.post("/api/verify/baidu")
def verify_baidu():
    """验证整条链路：账号 -> 登录 BaiduPCS-Go -> 转存一个分享 -> 清理。"""
    bduss = settings.get("bduss", ALIST_BDUSS)
    chk = baidu_check(bduss)
    if not chk["ok"]:
        return {"ok": False,
                "error": f"BDUSS 无效：百度返回 {chk['detail']}"}
    if not settings.get("stoken", ""):
        return {"ok": False, "user": chk.get("user", ""), "vip_type": chk.get("vip_type"),
                "error": "缺少 STOKEN：转存接口必须要它。请在设置页填 STOKEN（DevTools → Cookies → STOKEN → Cookie Value）"}

    shows = [s for s in store.all_shows() if (s["links"] or {}).get("百度")]
    if not shows:
        return {"ok": False, "error": "清单里没有百度链接"}
    sh = shows[0]
    surl, pwd = kdocs.baidu_surl(sh["links"]["百度"]["url"])
    if not surl:
        return {"ok": False, "error": "解析 surl 失败"}

    pan = "/verify_probe"
    try:
        pcs_login()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    pcs("rm", pan)
    pcs("mkdir", pan)
    code, out = pcs("cd", pan)
    if code != 0:
        return {"ok": False, "error": f"切换目录失败：{out[-160:]}"}
    code, out = pcs("transfer", f"https://pan.baidu.com/s/{surl}", pwd or "1", timeout=600)
    try:
        pcs("rm", pan)
    except Exception:
        pass
    if "成功" not in out:
        return {"ok": False, "error": f"转存失败：{out[-240:]}",
                "show": sh["title"]}
    return {"ok": True, "show": sh["title"], "file": "（已转存并清理）",
            "user": chk.get("user", ""), "svip": chk.get("svip"),
            "vip_type": chk.get("vip_type"), "bytes": 0}


_STATUS_CACHE = {"ts": 0.0, "val": {}}


@app.get("/api/status")
def status_panel(force: int = 0):
    """百度网盘 / 金山文档 / 磁盘 / 队列 的一览状态。"""
    now = time.time()
    if force or now - _STATUS_CACHE["ts"] > 60:
        bd = settings.get("bduss", ALIST_BDUSS)
        chk = baidu_check(bd) if bd else {"ok": False, "detail": "未配置 BDUSS"}
        st_path, ck = login.cookie_for_scrape()
        ok_ts = int(store.get_meta("last_refresh", "0") or 0)
        fail_ts = int(store.get_meta("last_fail", "0") or 0)
        err = store.get_meta("last_error", "") or ""
        # 健康判定：从未成功 / 最近一次失败 / 太久没成功 / 正常
        if not ok_ts:
            state = "never"
        elif fail_ts > ok_ts and err:
            state = "error"
        elif now - ok_ts > REFRESH_HOURS * 3600 * 2.5:
            state = "stale"
        else:
            state = "ok"
        kd = {
            "ok": state == "ok",
            "state": state,
            "last_refresh": ok_ts,
            "last_fail": fail_ts,
            "last_error": err,
            "count": len(store.all_shows()),
            "last_count": int(store.get_meta("last_count", "0") or 0),
            "last_ms": int(store.get_meta("last_ms", "0") or 0),
            "ago_hours": round((now - ok_ts) / 3600, 1) if ok_ts else None,
            "cookie": bool(st_path or ck),
            "anonymous": not (st_path or ck),
        }
        _STATUS_CACHE.update(ts=now, val={"baidu": chk, "kdocs": kd})

    d = dict(_STATUS_CACHE["val"])
    try:
        u = shutil.disk_usage(dl_root())
        d["disk"] = {"free_gb": round(u.free / 1024**3, 1),
                     "total_gb": round(u.total / 1024**3, 1),
                     "dir": str(dl_root())}
    except Exception:
        d["disk"] = {"free_gb": None, "total_gb": None, "dir": str(dl_root())}

    active = sum(1 for v in _PHASES.values()
                 if v.get("phase") in ("排队中", "清理网盘暂存", "转存中", "列目录", "下载中"))
    d["queue"] = {"paused": _PAUSED["on"], "active": active,
                  "waiting": _QUEUE.qsize(), "next_sync": int(_NEXT_SYNC["ts"]),
                  "refresh_hours": REFRESH_HOURS, "auto_sync": AUTO_SYNC}
    d["notify"] = {"type": settings.get("notify_type", ""),
                   "key": bool(settings.get("notify_key", "")),
                   "on": notify.configured()}
    d["settings"] = {"download_root": settings.get("download_root", "") or str(DL_ROOT),
                     "min_free_gb": settings.get("min_free_gb", "")}
    return d


@app.get("/api/inspect")
def inspect_show(slug: str):
    """画质体检：把分享里每个候选目录的分组、打分、最终选择都打出来（不下载）。"""
    sh = store.show(slug)
    if not sh:
        return JSONResponse({"ok": False, "error": "未找到该剧"}, status_code=404)
    bd = (sh["links"] or {}).get("百度")
    if not bd:
        return JSONResponse({"ok": False, "error": "没有百度链接"}, status_code=400)
    surl, pwd = kdocs.baidu_surl(bd.get("url", ""))
    if not surl:
        return JSONResponse({"ok": False, "error": "解析分享链接失败"}, status_code=400)

    pan = f"/inspect/{slug[:40]}"
    try:
        pcs_login()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    pcs("rm", pan)
    pcs("mkdir", pan)
    code, out = pcs("cd", pan)
    if code != 0:
        return JSONResponse({"ok": False, "error": f"切换目录失败: {out[-120:]}"}, status_code=400)
    code, out = pcs("transfer", f"https://pan.baidu.com/s/{surl}", pwd or "1", timeout=600)
    if "成功" not in out:
        pcs("rm", pan)
        return JSONResponse({"ok": False, "error": f"转存失败: {out[-200:]}"}, status_code=400)

    try:
        files = walk_pan(pan)
    finally:
        pcs("rm", pan)

    groups: dict[str, list] = {}
    for rel, name, size in files:
        if Path(name).suffix.lower() not in VIDEO_EXT:
            continue
        if any(w in rel for w in SKIP_WORDS):
            continue
        groups.setdefault(str(Path(rel).parent), []).append({
            "rel": rel, "name": name, "ep": parse_ep(name), "size": size,
            "score": quality_score(rel), "label": quality_info(rel)[2]})

    out_groups = []
    for g, items in groups.items():
        numbered = sum(1 for x in items if x["ep"] is not None)
        sc = max([quality_score(g)] + [x["score"] for x in items])
        label = quality_info(" ".join([g] + [x["name"] for x in items]))[2]
        out_groups.append({
            "dir": g or "(根目录)",
            "files": len(items),
            "numbered": numbered,
            "score": sc,
            "label": label,
            "sample": [x["name"] for x in items[:3]],
        })
    out_groups.sort(key=lambda x: (-x["score"], -x["numbered"]))

    pref = (store.get_pref(slug) or "best").lower()
    top = max((x["numbered"] for x in out_groups), default=0)
    pool = [x for x in out_groups if top == 0 or x["numbered"] >= max(2, top * 0.5)]
    best = max((x["score"] for x in pool), default=0)
    keep_dirs = {y["dir"] for y in pool if y["score"] == best}
    for x in out_groups:
        x["kept"] = x["dir"] in keep_dirs
    return {"ok": True, "show": sh["title"], "pref": pref,
            "groups": out_groups, "chosen_score": best}


class SourceReq(BaseModel):
    slug: str
    complete: bool = False
    url: str = ""
    pwd: str = ""
    label: str = ""
    provider: str = "百度"


class SourceIdReq(BaseModel):
    id: int = 0
    slug: str = ""


class NotifyCfgReq(BaseModel):
    type: str | None = None
    key: str | None = None


@app.get("/api/notify/config")
def get_notify_config():
    t = settings.get("notify_type", "")
    return {"ok": True, "type": t,
            "has_key": bool(settings.get("notify_key", "")),
            "configured": notify.configured()}


@app.post("/api/notify/config")
def set_notify_config(req: NotifyCfgReq):
    data = {}
    if req.type is not None:
        data["notify_type"] = req.type
    if req.key:
        data["notify_key"] = req.key
    settings.update(data)
    return {"ok": True, "type": settings.get("notify_type", ""),
            "configured": notify.configured()}


@app.get("/api/notifications")
def get_notifications(limit: int = 50):
    return {"ok": True, "items": store.list_notifications(max(1, min(limit, 200))),
            "type": settings.get("notify_type", ""),
            "configured": notify.configured()}


# ---------- 应用内实时推送（SSE） ----------
_EVENTS: list[dict] = []
_EV_SEQ = [0]
_EV_COND = threading.Condition()


def push_event(kind: str, title: str, body: str = "") -> None:
    with _EV_COND:
        _EV_SEQ[0] += 1
        _EVENTS.append({"id": _EV_SEQ[0], "ts": int(time.time()),
                        "kind": kind, "title": title, "body": body})
        del _EVENTS[:-100]
        _EV_COND.notify_all()


@app.get("/api/events")
def events(since: int = 0):
    """长连接：App 里弹通知用（SSE）。"""
    from fastapi.responses import StreamingResponse

    def gen():
        last = since
        yield "retry: 3000\n\n"
        while True:
            with _EV_COND:
                new = [e for e in _EVENTS if e["id"] > last]
                if not new:
                    _EV_COND.wait(timeout=20)
                    new = [e for e in _EVENTS if e["id"] > last]
            if new:
                for e in new:
                    last = e["id"]
                    yield "data: " + json.dumps(e, ensure_ascii=False) + "\n\n"
            else:
                yield ": ping\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


@app.get("/api/events/recent")
def events_recent(limit: int = 20):
    return {"ok": True, "items": _EVENTS[-max(1, min(limit, 100)):][::-1]}


@app.post("/api/events/test")
def events_test():
    push_event("test", "这是一条测试通知", "App 内应该弹出横幅；切到后台则走系统通知")
    return {"ok": True}


@app.get("/api/ai/models")
def ai_models():
    return ai.list_models()


@app.post("/api/ai/test")
def ai_test():
    return ai.ping()


class DocReq(BaseModel):
    id: int = 0
    url: str = ""
    name: str = ""
    enabled: int | None = None


@app.get("/api/documents")
def list_docs():
    return {"ok": True, "docs": store.list_documents(), "main": DOC_URL}


@app.post("/api/documents")
def add_doc(req: DocReq):
    url = (req.url or "").strip()
    if not url.startswith("http"):
        return {"ok": False, "error": "要一个 kdocs 文档链接"}
    did = store.add_document(url, req.name or "文档")
    return {"ok": True, "id": did}


@app.post("/api/documents/delete")
def del_doc(req: DocReq):
    if req.id:
        store.delete_document(req.id)
    return {"ok": True}


@app.post("/api/documents/toggle")
def toggle_doc(req: DocReq):
    if req.id:
        store.update_document(req.id, enabled=1 if req.enabled else 0)
    return {"ok": True}


@app.post("/api/documents/refresh")
def refresh_doc(req: DocReq):
    """立刻抓单个文档（不影响其它文档）。"""
    doc = store.get_document(req.id) if req.id else None
    url = (doc or {}).get("url") or DOC_URL
    if not url:
        return {"ok": False, "error": "没有文档地址"}
    if not _PCS_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "正在下载，稍后再试"}
    try:
        items = []
        is_sheet = False
        try:
            items = scrape(url)
        except Exception as e:
            print(f"[doc] 按文档抓失败（{e}），改按表格抓", flush=True)
        if not items:
            items = scrape_sheet(url)
            is_sheet = True
        if not items:
            return {"ok": False, "error": "解析到 0 条（文档/表格都没内容）"}
        if is_sheet:
            ns, n = store.import_sheet_items(items, (doc or {}).get("name") or "表格" )
        else:
            store.upsert_shows(items)
            n = store.seed_sources_from_links(doc_name=(doc or {}).get("name") or "文档")
        if req.id:
            store.update_document(req.id, last_refresh=int(time.time()),
                                  last_count=len(items), last_error="")
        return {"ok": True, "count": len(items), "new_sources": n}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        _PCS_LOCK.release()


@app.get("/api/sources")
def get_sources(slug: str):
    rows = store.list_sources(slug)
    act = next((r for r in rows if r.get("active")), None)
    return {"ok": True, "sources": rows, "active": act["id"] if act else 0,
            "ai": ai.enabled(), "reason": store.get_meta(f"aireason:{slug}", "")}


@app.post("/api/sources")
def add_source_api(req: SourceReq):
    url = (req.url or "").strip()
    if not url:
        return {"ok": False, "error": "链接不能为空"}
    surl, pwd = kdocs.baidu_surl(url)
    if not surl:
        return {"ok": False, "error": "看不懂这个链接"}
    sid = store.add_source(req.slug, url, req.pwd or pwd or "",
                           req.label or "手动添加", req.provider or "百度")
    return {"ok": True, "id": sid}


@app.post("/api/sources/delete")
def del_source_api(req: SourceIdReq):
    store.delete_source(req.id)
    return {"ok": True}


class LockReq(BaseModel):
    id: int = 0
    locked: bool = True


@app.post("/api/sources/lock")
def lock_source(req: LockReq):
    src = store.get_source(req.id)
    if not src:
        return {"ok": False, "error": "来源不存在"}
    store.set_locked(src["slug"], req.id, req.locked)
    if req.locked:
        store.set_active(src["slug"], req.id)
    return {"ok": True, "locked": req.locked}


@app.post("/api/sources/activate")
def activate_source_api(req: SourceIdReq):
    src = store.get_source(req.id)
    if not src:
        return {"ok": False, "error": "来源不存在"}
    store.set_active(src["slug"], req.id)
    return {"ok": True}


@app.post("/api/sources/choose")
def choose_source_api(req: SourceReq):
    """立即探测该剧所有来源并选一个。

    complete=true 时按「集数覆盖优先」选（换源补全），忽略锁定。
    """
    if not _PCS_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "正在下载，稍后再试"}
    sh = store.show(req.slug) or {}
    _lb = local_ep_scores(req.slug)
    _local_max = max(_lb) if _lb else 0
    _doc_eps = max(int(sh.get("episodes") or 0), int(sh.get("total_eps") or 0))
    _need = _doc_eps if _doc_eps > _local_max else 0
    try:
        chosen = pick_source(req.slug, force=True,
                             prefer_complete=bool(req.complete), need=_need)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        _PCS_LOCK.release()
    if not chosen:
        return {"ok": False, "error": "没有可用的来源"}
    return {"ok": True, "active": chosen["id"], "label": chosen.get("label"),
            "quality": chosen.get("probe_quality"), "eps": chosen.get("probe_eps"),
            "used_ai": chosen.get("used_ai", False),
            "reason": store.get_meta(f"aireason:{req.slug}", "")}


@app.get("/api/files")
def list_files(slug: str):
    """列出某部剧已下载的文件，供详情页逐集管理。"""
    d = show_root(slug)
    out = []
    if d.exists():
        for p in d.rglob("*"):
            if not p.is_file() or p.name.endswith(".BaiduPCS-Go-downloading"):
                continue
            try:
                sz = p.stat().st_size
            except OSError:
                sz = 0
            out.append({"name": p.name, "rel": str(p.relative_to(d)),
                        "ep": parse_ep(p.name), "size": sz})
    out.sort(key=lambda x: (x["ep"] is None, x["ep"] or 0, x["name"]))
    return {"ok": True, "files": out, "dir": str(d)}


class DelFilesReq(BaseModel):
    slug: str
    names: list[str]


@app.post("/api/delete_files")
def delete_files_by_name(req: DelFilesReq):
    d = show_root(req.slug)
    want = set(req.names or [])
    removed = 0
    freed = 0
    if d.exists() and want:
        for p in list(d.rglob("*")):
            if p.is_file() and p.name in want:
                try:
                    freed += p.stat().st_size
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
        for sub in sorted([x for x in d.rglob("*") if x.is_dir()], reverse=True):
            try:
                if not any(sub.iterdir()):
                    sub.rmdir()
            except OSError:
                pass
    n, _ = local_stat(req.slug)
    set_phase(req.slug, "已删除" if n == 0 else "完成", f"删了 {removed} 个，还剩 {n} 个")
    return {"ok": True, "removed": removed, "freed": freed, "left": n}


class DeleteReq(BaseModel):
    slug: str
    upto_ep: int | None = None      # None = 全部删除


@app.post("/api/delete")
def delete_files(req: DeleteReq):
    ph = (_PHASES.get(req.slug) or {}).get("phase", "")
    if ph in ("排队中", "清理网盘暂存", "转存中", "列目录", "下载中", "取消中"):
        return {"ok": False, "error": "正在下载，请先点「取消」再删除"}

    d = show_root(req.slug)
    if not d.exists():
        return {"ok": True, "removed": 0, "freed": 0}

    removed = 0
    freed = 0
    has_dest = bool((store.get_dest(req.slug) or "").strip())
    try:
        if req.upto_ep is None:
            if has_dest:
                # 自定义目录可能还放着别的东西，只删我们下的视频，且不删目录本身
                for p in list(d.rglob("*")):
                    if not p.is_file() or p.suffix.lower() not in VIDEO_EXT:
                        continue
                    if p.name.endswith(".BaiduPCS-Go-downloading"):
                        continue
                    try:
                        freed += p.stat().st_size
                        p.unlink()
                        removed += 1
                    except OSError:
                        pass
            else:
                for p in d.rglob("*"):
                    if p.is_file():
                        try:
                            freed += p.stat().st_size
                        except OSError:
                            pass
                        removed += 1
                shutil.rmtree(d, ignore_errors=True)
        else:
            for p in sorted(d.rglob("*"), reverse=True):
                if not p.is_file():
                    continue
                if p.name.endswith(".BaiduPCS-Go-downloading"):
                    continue
                ep = parse_ep(p.name)
                if ep is None or ep > req.upto_ep:
                    continue
                try:
                    freed += p.stat().st_size
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
            for sub in sorted([x for x in d.rglob("*") if x.is_dir()], reverse=True):
                try:
                    if not any(sub.iterdir()):
                        sub.rmdir()
                except OSError:
                    pass
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    n, size = local_stat(req.slug)
    set_phase(req.slug, "已删除" if n == 0 else "完成",
              f"删了 {removed} 个文件，还剩 {n} 个")
    return {"ok": True, "removed": removed, "freed": freed, "left": n}


@app.get("/api/state")
def state():
    subs = set(store.subscribed())
    ep_map = store.sub_map()
    dest_map = store.all_dest()
    pol_map = store.all_policy()
    src_map = store.all_sources()
    shows = store.all_shows()

    out = []
    for s in shows:
        ph = _PHASES.get(s["slug"]) or {}
        n, size = local_stat(s["slug"])
        pol = pol_map.get(s["slug"]) or {}
        if not pol.get("last_done") and n > 0:
            _ts = newest_ts(s["slug"])
            if _ts:
                store.touch_policy(s["slug"], last_done=_ts)
        srcs = src_map.get(s["slug"]) or []
        act = next((x for x in srcs if x.get("active")), None) or \
              next((x for x in srcs if x.get("enabled")), None)
        out.append({
            "slug": s["slug"],
            "title": s["title"],
            "year": s.get("year", ""),
            "episodes": s.get("episodes", ""),
            "quality": s.get("quality", ""),
            "providers": list((s.get("links") or {}).keys()),
            "category": s.get("category") or "",
            "subscribed": s["slug"] in subs,
            "archived": bool(s.get("archived")),
            "finished": bool(s.get("finished")),
            "total_eps": int(s.get("total_eps") or 0),
            "local": n,
            "bytes": size,
            "phase": ph.get("phase", "") if not s.get("archived") else "已完结",
            "msg": ph.get("msg", ""),
            "error": ph.get("msg", "") if ph.get("phase") == "失败" else "",
            "pending": 0,
            "start_ep": ep_map.get(s["slug"], 1),
            "dest": dest_map.get(s["slug"], ""),
            "quality_pref": store.get_pref(s["slug"]),
            "ep_changed_at": s.get("ep_changed_at", 0) or 0,
            "check_hours": pol.get("check_hours") or 6,
            "auto_dl": bool(pol.get("auto_dl")),
            "new_eps": pol.get("new_eps") or 0,
            "upgradable": pol.get("upgradable") or 0,
            "dead": is_dead_link(s["slug"], ((s.get("links") or {}).get("百度") or {}).get("url", "")),
            "last_check": pol.get("last_check") or 0,
            "last_done": pol.get("last_done") or 0,
            "src_count": len(srcs),
            "src_label": (act or {}).get("label") or "",
            "src_quality": (act or {}).get("probe_quality") or "",
            "src_eps": (act or {}).get("probe_eps") or 0,
            "src_labels": sorted({(x.get("label") or x.get("provider") or "未命名")
                                  for x in srcs if x.get("enabled")}),
            "src_providers": sorted({(x.get("provider") or "百度") for x in srcs if x.get("enabled")}),
        })

    try:
        free_gb = round(shutil.disk_usage(dl_root()).free / 1024**3, 1)
    except Exception:
        free_gb = None

    a = aria()
    return JSONResponse({
        "shows": out,
        "last_refresh": int(store.get_meta("last_refresh", "0") or 0),
        "last_sync": int(store.get_meta("last_sync", "0") or 0),
        "last_error": store.get_meta("last_error", ""),
        "aria2": {"optional": True, "stats": a.stats()},
        "alist_ok": bool(settings.get("alist_pass", ALIST_PASS) and
                         settings.get("bduss", ALIST_BDUSS)),
        "busy": _STATE,
        "paused": _PAUSED["on"],
        "next_sync": int(_NEXT_SYNC["ts"]),
        "free_gb": free_gb,
    })


class TidyReq(BaseModel):
    slug: str


@app.post("/api/tidy")
def tidy_show(req: TidyReq):
    root = show_root(req.slug)
    moved = flatten_dir(root)
    n, size = local_stat(req.slug)
    return {"ok": True, "moved": moved, "files": n, "bytes": size}


class PolicyReq(BaseModel):
    slug: str
    check_hours: int | None = None
    auto_dl: bool | None = None


@app.post("/api/policy")
def set_show_policy(req: PolicyReq):
    if req.check_hours is not None and req.check_hours not in CHECK_ALLOWED:
        return {"ok": False, "error": "只支持 3 / 6 / 24 / 168 小时"}
    p = store.set_policy(req.slug, req.check_hours, req.auto_dl)
    return {"ok": True, "policy": p}


class ArchiveReq(BaseModel):
    slug: str
    on: bool = True


@app.post("/api/archive")
def archive_show(req: ArchiveReq):
    """完结归档：停止追更与自动下载，已下载文件原样保留。"""
    sh = store.show(req.slug)
    if not sh:
        return {"ok": False, "error": "没有这部剧"}
    store.set_archived(req.slug, req.on)
    if req.on:
        _PHASES.pop(req.slug, None)
        set_phase(req.slug, "已完结", "已归档：不再检查/下载，文件保留")
    else:
        _PHASES.pop(req.slug, None)
    return {"ok": True, "archived": req.on, "title": sh.get("title")}


@app.post("/api/check")
def check_now(req: PolicyReq):
    """立刻检查一次（不看时间窗）。"""
    try:
        return do_check(req.slug)
    except Exception as e:
        print(traceback.format_exc(), flush=True)
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"},
                            status_code=200)


class QualityReq(BaseModel):
    slug: str
    quality: str = "best"


@app.post("/api/quality")
def set_quality(req: QualityReq):
    q = (req.quality or "best").strip().lower()
    if q not in ("best", "1080", "720", "smallest"):
        return {"ok": False, "error": "只支持 best / 1080 / 720 / smallest"}
    store.set_pref(req.slug, q)
    return {"ok": True, "quality": q}


class DestReq(BaseModel):
    slug: str
    dir: str = ""


@app.post("/api/dest")
def set_show_dest(req: DestReq):
    d = (req.dir or "").strip()
    if d:
        allowed = [DATA_ROOT.resolve(), DL_ROOT.resolve()]
        try:
            rp = Path(d).resolve()
        except Exception:
            return {"ok": False, "error": "路径无效"}
        if not any(str(rp) == str(a) or str(rp).startswith(str(a) + "/") for a in allowed):
            return {"ok": False, "error": "只能选数据卷内的目录"}
        if not rp.exists():
            return {"ok": False, "error": "目录不存在"}
    store.set_dest(req.slug, d)
    return {"ok": True, "dest": d, "root": str(show_root(req.slug))}


class PauseReq(BaseModel):
    on: bool


@app.post("/api/pause")
def pause(req: PauseReq):
    _PAUSED["on"] = bool(req.on)
    return {"ok": True, "paused": _PAUSED["on"]}


class NotifyReq(BaseModel):
    title: str = "PanFollow 测试"
    body: str = "这是一条测试通知，收到说明配置正确。"


@app.post("/api/notify/test")
def notify_test(req: NotifyReq | None = None):
    req = req or NotifyReq()
    title = req.title or "PanFollow·测试通知"
    body = req.body or "收到这条说明推送配置正确 ✅"
    ok, msg = notify.send(title, body)
    try:
        store.add_notification(title, body, ok, msg, settings.get("notify_type", ""))
    except Exception:
        pass
    return {"ok": ok, "detail": msg[:220]}


class CancelReq(BaseModel):
    slug: str


@app.post("/api/cancel")
def cancel(req: CancelReq):
    _CANCEL.add(req.slug)
    p = _PROC.get(req.slug)
    if p is not None:
        set_phase(req.slug, "取消中", "正在停止当前文件…")
        try:
            p.terminate()
        except Exception:
            pass
    else:
        set_phase(req.slug, "已取消", "已从队列移除")
    return {"ok": True, "slug": req.slug}


class SubReq(BaseModel):
    slug: str
    on: bool
    start_ep: int | None = None


@app.post("/api/subscribe")
def subscribe(req: SubReq):
    if not store.show(req.slug):
        return JSONResponse({"ok": False, "error": "未知条目"}, status_code=404)
    store.set_sub(req.slug, req.on, req.start_ep)
    return {"ok": True, "slug": req.slug, "on": req.on,
            "start_ep": store.sub_map().get(req.slug, 1)}


@app.post("/api/refresh")
def refresh():
    return do_refresh()


@app.post("/api/sync")
def sync():
    return do_sync()


@app.get("/api/health")
def health():
    # Alist / aria2 是早期版本的依赖；现在下载全走容器内的 BaiduPCS-Go，
    # 新装用户不配它们也完全可用 —— 这里只做状态展示，不再报错。
    out = {"ok": True, "alist": False, "aria2": False, "alist_auth": False,
           "error": "", "optional": {"alist": True, "aria2": True},
           "cfg": {"alist": ALIST_URL, "aria2": ARIA2_RPC,
                   "download": str(dl_root()), "download_ok": dl_status()["ok"], "download_error": dl_status()["error"],
                   "chromium": os.environ.get("CHROMIUM_PATH", "(playwright 自带)")}}
    try:
        out["aria2"] = bool(aria().ping())
    except Exception:
        pass
    try:
        out["alist"] = Alist(ALIST_URL, "", "", "").ping()
    except Exception:
        pass
    # 只报告已有登录态，不主动 login（避免触发 Alist 的失败登录限流）
    out["alist_auth"] = _ALIST["client"] is not None
    return out
