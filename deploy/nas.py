#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通过 SSH 把 kdocs-autodl 部署到飞牛 NAS。

用法（密码走环境变量，不要写进命令行）：
    set DSH_NAS_PASS=*** 
    python deploy/nas.py probe
    python deploy/nas.py upload  --dest /vol1/1000/docker/kdocs-autodl
    python deploy/nas.py deploy  --dest /vol1/1000/docker/kdocs-autodl
    python deploy/nas.py health  --dest /vol1/1000/docker/kdocs-autodl
"""

from __future__ import annotations

import argparse
import os
import posixpath
import sys
from pathlib import Path

import paramiko

try:                                  # Windows 控制台默认 GBK，中文/特殊字符会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HOST = os.environ.get("DSH_NAS_HOST", "192.168.1.100")
USER = os.environ.get("DSH_NAS_USER", "demo")
PASS = os.environ.get("DSH_NAS_PASS", "")

PROJECT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".pylibs", "__pycache__", ".git", "state", "pcs-config",
             "aria2-config", "node_modules", ".venv"}
SKIP_FILES = {".DS_Store"}


def connect() -> paramiko.SSHClient:
    if not PASS:
        sys.exit("请先设置环境变量 DSH_NAS_PASS")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=22, username=USER, password=PASS,
              timeout=20, banner_timeout=30, auth_timeout=30,
              look_for_keys=False, allow_agent=False)
    return c


def run(c: paramiko.SSHClient, cmd: str, sudo: bool = False,
        timeout: int = 120) -> tuple[int, str]:
    if sudo:
        cmd = f"sudo -S -p '' bash -lc {shq(cmd)}"
    stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout, get_pty=False)
    if sudo:
        stdin.write(PASS + "\n")
        stdin.flush()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    return code, (out + err).strip()


def shq(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def probe(c: paramiko.SSHClient) -> None:
    checks = [
        ("系统", "cat /etc/os-release | head -3; uname -m"),
        ("当前用户", "id; whoami; echo HOME=$HOME"),
        ("sudo 权限", "sudo -n true 2>&1 && echo SUDO_NOPASS || echo SUDO_NEEDS_PASS"),
        ("docker", "docker version --format '{{.Server.Version}}' 2>&1 | head -2; "
                   "docker compose version 2>&1 | head -1"),
        ("磁盘/卷", "ls -d /vol* 2>/dev/null; df -h | grep -E 'vol|Filesystem' | head -8"),
        ("用户目录", "ls -la /vol1/1000 2>/dev/null | head -10"),
        ("运行中容器", "docker ps --format '{{.Names}}\\t{{.Image}}\\t{{.Ports}}' 2>&1 | head -20"),
        ("Alist 端口", "ss -lntp 2>/dev/null | grep -E ':5244|:8099|:6800' || netstat -lntp 2>/dev/null | grep -E ':5244|:8099|:6800'"),
        ("Alist 探测", "curl -s -m 4 -o /dev/null -w '%{http_code}' http://127.0.0.1:5244/ping || true"),
        ("出网", "curl -s -m 6 -o /dev/null -w 'github:%{http_code}' https://api.github.com/ ; echo; "
                 "curl -s -m 6 -o /dev/null -w 'kdocs:%{http_code}' https://www.kdocs.cn/"),
    ]
    for name, cmd in checks:
        code, out = run(c, cmd, sudo=True)
        print(f"\n===== {name} (exit {code}) =====")
        print(out or "(空)")


def sftp_mkdirs(sftp, path: str) -> None:
    parts, cur = [p for p in path.split("/") if p], ""
    for p in parts:
        cur += "/" + p
        try:
            sftp.stat(cur)
        except IOError:
            sftp.mkdir(cur)


def upload(c: paramiko.SSHClient, dest: str) -> None:
    sftp = c.open_sftp()
    sftp_mkdirs(sftp, dest)
    files = 0
    for p in PROJECT.rglob("*"):
        rel = p.relative_to(PROJECT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.name in SKIP_FILES or p.suffix == ".pyc":
            continue
        remote = posixpath.join(dest, rel.as_posix())
        if p.is_dir():
            sftp_mkdirs(sftp, remote)
            continue
        sftp_mkdirs(sftp, posixpath.dirname(remote))
        sftp.put(str(p), remote)
        files += 1
    sftp.close()
    print(f"上传完成：{files} 个文件 -> {dest}")


def deploy(c: paramiko.SSHClient, dest: str) -> None:
    steps = [
        ("准备目录", f"mkdir -p {shq(dest)}/state {shq(dest)}/aria2-config"),
        ("修正权限", f"chown -R $(id -u):$(id -g) {shq(dest)} 2>/dev/null || true"),
        ("检查 .env", f"test -f {shq(dest)}/.env && echo HAS_ENV || echo NO_ENV"),
        ("拉镜像并启动", f"cd {shq(dest)} && docker compose up -d --build"),
        ("容器状态", "docker ps --filter name=kdocs --format '{{.Names}}\\t{{.Status}}\\t{{.Ports}}'"),
    ]
    for name, cmd in steps:
        code, out = run(c, cmd, sudo=False, timeout=900)
        print(f"\n===== {name} (exit {code}) =====")
        print(out or "(无输出)")
        if code != 0 and name in ("拉镜像并启动",):
            print("!! 启动失败，后续步骤跳过")
            return


def health(c: paramiko.SSHClient, dest: str) -> None:
    cmds = [
        ("web 健康", "curl -s -m 8 http://127.0.0.1:8099/api/health"),
        ("web 状态", "curl -s -m 8 http://127.0.0.1:8099/api/state | head -c 1200"),
        ("容器日志", "cd %s && docker compose logs --tail=40 web" % shq(dest)),
        ("aria2", "curl -s -m 5 http://127.0.0.1:6800/jsonrpc -d "
                  "'{\"jsonrpc\":\"2.0\",\"id\":\"x\",\"method\":\"aria2.getVersion\"}'"),
    ]
    for name, cmd in cmds:
        code, out = run(c, cmd, sudo=False, timeout=60)
        print(f"\n===== {name} (exit {code}) =====")
        print(out or "(无输出)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["probe", "upload", "deploy", "health", "sh", "put", "get"])
    ap.add_argument("--dest", default="/vol1/1000/docker/kdocs-autodl")
    ap.add_argument("--cmd", default="")
    ap.add_argument("--cmdfile", default="")
    ap.add_argument("--local", default="")
    ap.add_argument("--remote", default="")
    ap.add_argument("--sudo", action="store_true")
    a = ap.parse_args()

    c = connect()
    print(f"已连接 {USER}@{HOST}")
    try:
        if a.action == "probe":
            probe(c)
        elif a.action == "upload":
            upload(c, a.dest)
        elif a.action == "deploy":
            deploy(c, a.dest)
        elif a.action == "health":
            health(c, a.dest)
        elif a.action == "get":
            sftp = c.open_sftp()
            sftp.get(a.remote, a.local)
            sftp.close()
            print(f"已下载 {a.remote} -> {a.local}")
        elif a.action == "put":
            sftp = c.open_sftp()
            sftp_mkdirs(sftp, posixpath.dirname(a.remote) or "/")
            sftp.put(a.local, a.remote)
            sftp.close()
            print(f"已上传 {a.local} -> {a.remote}")
        elif a.action == "sh":
            cmd = a.cmd
            if a.cmdfile:
                cmd = Path(a.cmdfile).read_text(encoding="utf-8")
            code, out = run(c, cmd, sudo=a.sudo, timeout=600)
            print(out)
            print(f"[exit {code}]")
    finally:
        c.close()


if __name__ == "__main__":
    main()
