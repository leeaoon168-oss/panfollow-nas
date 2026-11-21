#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用 BaiduPCS-Go 批量转存 + 下载 resources.csv 里的百度网盘分享链接。

每个条目独立处理，流程固定、不依赖解析 ls 的输出：
    mkdir /kdocs/<slug>
    cd    /kdocs/<slug>
    transfer <分享链接> [提取码]
    download /kdocs/<slug> --saveto /downloads/<slug>
    rm    /kdocs/<slug>        # 可选，腾出网盘空间

单条失败不影响后续；状态记在 sqlite 里，重跑只处理未完成的。
"""

from __future__ import annotations

import csv
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import time
import traceback
from pathlib import Path

BIN = os.environ.get("PCS_BIN", "/usr/local/bin/BaiduPCS-Go")
CSV_PATH = Path(os.environ.get("RESOURCES_CSV", "/state/resources.csv"))
DL_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/downloads"))
TMP_DIR = os.environ.get("PAN_TMP_DIR", "/kdocs")          # 网盘内的转存暂存目录
DB_PATH = Path(os.environ.get("STATE_DB", "/state/baidupcs.sqlite"))
COOKIES = os.environ.get("BDUSS_COOKIES", "").strip()
BDUSS = os.environ.get("BDUSS", "").strip()
STOKEN = os.environ.get("STOKEN", "").strip()
MAX_PARALLEL = os.environ.get("MAX_PARALLEL", "1")         # 普通用户=1，SVIP=10~20
FREE_SPACE = os.environ.get("FREE_PAN_SPACE", "0") == "1"
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "0"))          # 0 = 不限制
DL_TIMEOUT = int(os.environ.get("DL_TIMEOUT", str(8 * 3600)))
ONLY_TITLE = os.environ.get("ONLY_TITLE", "").strip()      # 只跑某一部（调试用）

SAFE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def slugify(title: str) -> str:
    s = SAFE.sub("_", title).strip(" ._")
    return s[:80] or "untitled"


def run(*args: str, timeout: int = 300) -> tuple[int, str]:
    try:
        p = subprocess.run([BIN, *args], capture_output=True, text=True,
                           timeout=timeout, errors="replace")
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, out.strip()
    except subprocess.TimeoutExpired:
        return 124, f"超时 {timeout}s"
    except FileNotFoundError:
        return 127, f"找不到可执行文件: {BIN}"


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS task (
               h TEXT PRIMARY KEY, title TEXT, url TEXT,
               status TEXT, note TEXT, ts INTEGER)"""
    )
    conn.commit()
    return conn


def h_of(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def status_of(conn, url: str) -> str | None:
    row = conn.execute("SELECT status FROM task WHERE h=?", (h_of(url),)).fetchone()
    return row[0] if row else None


def mark(conn, title: str, url: str, status: str, note: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO task(h,title,url,status,note,ts) VALUES (?,?,?,?,?,?)",
        (h_of(url), title, url, status, note[:800], int(time.time())),
    )
    conn.commit()


def login() -> None:
    if COOKIES:
        code, out = run("login", "-cookies=" + COOKIES)
    elif BDUSS and STOKEN:
        code, out = run("login", "-bduss=" + BDUSS, "-stoken=" + STOKEN)
    else:
        raise SystemExit("需要 BDUSS_COOKIES 或 BDUSS+STOKEN")
    if code != 0:
        raise SystemExit(f"登录失败: {out}")
    code, out = run("who")
    print(f"[login] {out.splitlines()[-1] if out else 'ok'}", flush=True)
    run("config", "set", "-max_parallel", MAX_PARALLEL)
    run("config", "set", "-max_download_load", "1")
    run("config", "set", "-savedir", str(DL_DIR))
    print(f"[config] max_parallel={MAX_PARALLEL} savedir={DL_DIR}", flush=True)


def load_rows() -> list[dict]:
    if not CSV_PATH.exists():
        raise SystemExit(f"找不到清单 {CSV_PATH}，先跑 scraper 容器")
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as fh:
        return [r for r in csv.DictReader(fh) if (r.get("百度_url") or "").strip()]


def process(conn, row: dict) -> None:
    title = (row.get("title") or "").strip() or "untitled"
    url = (row.get("百度_url") or "").strip()
    pwd = (row.get("百度_pwd") or "").strip()
    slug = slugify(title)
    pan_dir = f"{TMP_DIR}/{slug}"
    local_dir = DL_DIR / slug

    print(f"\n=== 《{title}》 {pwd and '提取码 ' + pwd} ===", flush=True)

    # 清掉上次残留，保证转存目标目录是干净的
    run("rm", pan_dir)
    code, out = run("mkdir", pan_dir)
    if code != 0 and "已存在" not in out:
        print(f"  建目录失败: {out}", flush=True)
    code, out = run("cd", pan_dir)
    if code != 0:
        mark(conn, title, url, "failed", f"cd 失败: {out}")
        print(f"  cd 失败: {out}", flush=True)
        return

    # 转存：URL 自带 pwd 时不要再传提取码
    if "pwd=" in url:
        code, out = run("transfer", url, timeout=600)
    else:
        code, out = run("transfer", url, pwd, timeout=600)
    tail = " / ".join(out.splitlines()[-3:]) if out else ""
    if code != 0:
        mark(conn, title, url, "failed", f"transfer: {tail}")
        print(f"  转存失败: {tail}", flush=True)
        return
    print(f"  转存成功: {tail}", flush=True)

    local_dir.mkdir(parents=True, exist_ok=True)
    code, out = run("download", pan_dir, "--saveto", str(local_dir),
                    "-p", MAX_PARALLEL, timeout=DL_TIMEOUT)
    tail = " / ".join(out.splitlines()[-3:]) if out else ""
    if code != 0:
        mark(conn, title, url, "failed", f"download: {tail}")
        print(f"  下载失败: {tail}", flush=True)
        return

    mark(conn, title, url, "done", str(local_dir))
    print(f"  完成 -> {local_dir}", flush=True)

    if FREE_SPACE:
        code, out = run("rm", pan_dir)
        print(f"  清理网盘暂存: {'ok' if code == 0 else out}", flush=True)


def main() -> int:
    login()
    rows = load_rows()
    if ONLY_TITLE:
        rows = [r for r in rows if ONLY_TITLE in (r.get("title") or "")]
    print(f"[plan] 清单里 {len(rows)} 条百度链接", flush=True)

    conn = db()
    done = skipped = failed = 0
    for i, row in enumerate(rows, 1):
        url = (row.get("百度_url") or "").strip()
        st = status_of(conn, url)
        if st == "done":
            skipped += 1
            continue
        if MAX_ITEMS and done >= MAX_ITEMS:
            print(f"[limit] 已达 MAX_ITEMS={MAX_ITEMS}，本轮停止", flush=True)
            break
        print(f"[{i}/{len(rows)}]", end=" ", flush=True)
        try:
            process(conn, row)
            if status_of(conn, url) == "done":
                done += 1
            else:
                failed += 1
        except Exception:
            failed += 1
            print(traceback.format_exc(), file=sys.stderr, flush=True)

    conn.close()
    print(f"\n[summary] 新完成 {done} / 已存在 {skipped} / 失败 {failed}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
