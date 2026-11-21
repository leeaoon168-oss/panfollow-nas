#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从表格剪贴板文本（TSV）解析剧集清单。

表格列（观影视界那份）：
  A 标记 | B 剧名 | C 国家 | D 集数 | E 夸克 | F 百度 | G 光鸭 | H 画质 | I 更新时间 | J 备注
"""
import re
import sys

RE_NO = re.compile(r"^\d{1,4}$")
COUNTRIES = {"国剧", "国漫", "日剧", "韩剧", "美剧", "英剧", "泰剧", "港剧", "台剧",
             "电影", "综艺", "纪录片", "动漫", "短剧", "海外剧"}
EP_RE = re.compile(r"([\d]+)\s*集")


def parse_sheet_rows(text: str) -> list[dict]:
    out = []
    seen = set()
    for raw in (text or "").split("\n"):
        cells = [c.strip().strip('"') for c in raw.split("\t")]
        if len(cells) < 4:
            continue
        # 找「国家/类型」列
        ci = next((i for i, c in enumerate(cells) if c in COUNTRIES), -1)
        if ci < 1:
            continue
        # 集数列
        ei = next((i for i in range(ci + 1, min(ci + 4, len(cells)))
                   if EP_RE.search(cells[i]) or RE_NO.match(cells[i])), -1)
        if ei < 0:
            continue
        title = cells[ci - 1]
        if not title or len(title) < 2 or title in ("剧集名称", "名称", "剧名"):
            continue
        # 去掉 B/C 这类行号前缀和装饰符
        title = re.sub(r"^[A-Za-z0-9★⭐🔴🟡🟢\s]{0,3}", "", title).strip(" ◆♦*")
        # 统一写法，好和文档来源的条目对上：半角括号转全角、去掉多余空格
        title = re.sub(r"\s*[(（]\s*(\d{4})\s*[)）]\s*", lambda m: "（%s）" % m.group(1), title)
        title = re.sub(r"\s+", "", title)
        if not title or title in seen:
            continue
        seen.add(title)

        ep_txt = cells[ei]
        nums = re.findall(r"(\d{1,4})", ep_txt)
        episodes = nums[-1] if nums else ""

        quality = ""
        for c in cells[ei + 1:ei + 5]:
            if re.search(r"[42]K|1080|720|HDR|高码|臻彩", c, re.I):
                quality = c
                break
        cat = cells[ci]
        out.append({"title": title, "episodes": episodes, "quality": quality,
                    "country": cat})
    return out


if __name__ == "__main__":
    txt = sys.stdin.read()
    rows = parse_sheet_rows(txt)
    print("解析到:", len(rows))
    for r in rows[:10]:
        print("  ", r)
