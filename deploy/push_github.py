#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把分发目录推送到 GitHub（不需要安装 git，直接走 REST API）。

用法：
    set GITHUB_TOKEN=ghp_xxx
    python deploy/push_github.py --owner <你的用户名> --repo panfollow-nas

可选：
    --private            建私有仓库
    --description "..."  仓库描述
    --dry-run            只打印要做什么，不真的提交
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

import httpx

API = "https://api.github.com"

SKIP_DIRS = {".git", "__pycache__", "state", "node_modules", ".pylibs", "shots", ".venv"}
SKIP_EXT = {".pyc", ".apk", ".jks", ".idsig", ".log"}


def collect(root: Path) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.suffix in SKIP_EXT:
            continue
        out.append(p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--src", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--description", default="追更 kdocs 文档里的网盘分享，自动把新集下到 NAS")
    ap.add_argument("--private", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--branch", default="main")
    a = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("缺 GITHUB_TOKEN 环境变量")
        return 2

    src = Path(a.src).resolve()
    files = collect(src)
    print(f"待推送 {len(files)} 个文件，根目录 {src}")
    for f in files[:10]:
        print("   ", f.relative_to(src))
    if len(files) > 10:
        print(f"    … 还有 {len(files) - 10} 个")

    if a.dry_run:
        print("\n[dry-run] 不做实际请求")
        return 0

    h = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json",
         "User-Agent": "panfollow-nas-publisher"}

    with httpx.Client(timeout=60, headers=h) as c:
        r = c.get(f"{API}/user")
        if r.status_code != 200:
            print(f"token 无效：{r.status_code} {r.text[:200]}")
            return 1
        me = r.json()
        print(f"已认证：{me['login']}")

        # 建仓库（已存在就复用）
        repo_full = f"{a.owner}/{a.repo}"
        r = c.post(f"{API}/user/repos", json={
            "name": a.repo, "description": a.description,
            "private": bool(a.private), "auto_init": False,
        })
        if r.status_code == 201:
            print(f"已创建仓库 {repo_full}")
        elif r.status_code == 422:
            print(f"仓库已存在，直接推内容：{repo_full}")
        else:
            print(f"建仓库失败：{r.status_code} {r.text[:300]}")
            return 1

        # 逐个文件提交（Contents API）
        ok = fail = 0
        tree = []
        for i, f in enumerate(files, 1):
            rel = f.relative_to(src).as_posix()
            content = base64.b64encode(f.read_bytes()).decode()
            payload = {"message": f"add {rel}", "content": content, "branch": a.branch}
            # 文件已存在则要带 sha
            ex = c.get(f"{API}/repos/{repo_full}/contents/{rel}", params={"ref": a.branch})
            if ex.status_code == 200:
                payload["sha"] = ex.json().get("sha")
                payload["message"] = f"update {rel}"
            r = c.put(f"{API}/repos/{repo_full}/contents/{rel}", json=payload)
            if r.status_code in (200, 201):
                ok += 1
                if i % 10 == 0 or i == len(files):
                    print(f"  已提交 {i}/{len(files)}")
            else:
                fail += 1
                print(f"  失败 {rel}: {r.status_code} {r.text[:160]}")

        print(f"\n完成：成功 {ok}，失败 {fail}")
        print(f"仓库地址：https://github.com/{repo_full}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
