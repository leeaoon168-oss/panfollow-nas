#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 index.html 里的 <script> 抽出来，用 node --check 验证语法。"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

NODE = sys.argv[2] if len(sys.argv) > 2 else "node"
p = Path(sys.argv[1])
html = p.read_text(encoding="utf-8")
blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
print(f"{p.name}: {len(blocks)} 个内联 script 块")
bad = 0
for i, code in enumerate(blocks):
    if not code.strip():
        continue
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write("(async()=>{\n" + code + "\n})();\n")
        tmp = f.name
    r = subprocess.run([NODE, "--check", tmp], capture_output=True, text=True)
    if r.returncode != 0:
        bad += 1
        print(f"  块 {i}: 语法错误")
        print("   ", (r.stderr or "").strip().splitlines()[:6])
    else:
        print(f"  块 {i}: OK ({len(code)} 字符)")
sys.exit(1 if bad else 0)
