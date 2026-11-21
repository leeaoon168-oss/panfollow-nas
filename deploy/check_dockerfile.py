#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Dockerfile 里每个 RUN 块抽出来，用 bash -n 检查语法。"""
import pathlib
import subprocess
import sys
import tempfile

path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "app/Dockerfile")
lines = path.read_text(encoding="utf-8").splitlines()

blocks, cur = [], None
for ln in lines:
    s = ln.rstrip()
    if cur is None and s.upper().startswith("RUN "):
        cur = [s[4:]]
    elif cur is not None:
        cur.append(s)
    if cur is not None and not s.endswith("\\"):
        blocks.append(cur)
        cur = None
if cur:
    blocks.append(cur)

bad = 0
for i, b in enumerate(blocks, 1):
    script = "\n".join(x[:-1] if x.endswith("\\") else x for x in b)
    # 走 stdin 交给 bash，避免 Windows 临时路径与 WSL bash 不互通
    r = subprocess.run(["bash", "-n", "-s"], input=script + "\n",
                       capture_output=True, text=True)
    status = "OK  " if r.returncode == 0 else "FAIL"
    if r.returncode:
        bad += 1
    print(f"  {status} RUN #{i} ({len(script)} 字符)")
    if r.returncode:
        print("   ", (r.stderr or "").strip()[:400])

print("失败:", bad)
sys.exit(1 if bad else 0)
