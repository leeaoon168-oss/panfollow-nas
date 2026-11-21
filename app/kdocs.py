#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kdocs 文档解析：把整页文本切成《剧名》+ 各家分享链接的条目。"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

PROVIDERS = ("百度", "夸克", "迅雷", "阿里", "UC", "123", "移动", "天翼", "光鸭", "115")

TITLE_RE = re.compile(r"《([^》]{1,120})》")
YEAR_RE = re.compile(r"[（(](\d{4})[)）]")
UPD_RE = re.compile(r"(?:更新至|超前完结|完结|全)\s*(\d+)\s*[集话期]")
QUALITY_RE = re.compile(r"【([^】]*(?:4K|1080P|720P|HDR|高码率)[^】]*)】")
LINK_RE = re.compile(r"^\s*(?:%s)\s*[:：]\s*(https?://\S+)\s*$" % "|".join(PROVIDERS))
PROVIDER_OF = re.compile(r"^\s*(%s)\s*[:：]" % "|".join(PROVIDERS))

SAFE = re.compile(r'[\\/:*?"<>|\r\n\t]+')

# 文档里的大章节标题（单独一行）
SECTIONS = ("电视剧", "海外剧集", "动漫", "综艺", "电影", "近期完结",
            "国产剧", "美剧", "韩剧", "日剧", "纪录片", "短剧")


def slugify(title: str) -> str:
    s = SAFE.sub("_", title).strip(" ._")
    return s[:80] or "untitled"


def extract_pwd(url: str) -> str:
    q = parse_qs(urlparse(url).query)
    for k in ("pwd", "password", "code"):
        if k in q:
            return q[k][0]
    return ""


def parse(text: str) -> list[dict]:
    items: list[dict] = []
    cur: dict | None = None
    section = ""

    def flush():
        nonlocal cur
        if cur and cur["links"]:
            items.append(cur)
        cur = None

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue

        if line in SECTIONS:            # 章节标题，记下来归属后面的条目
            flush()
            section = line
            continue

        m_title = TITLE_RE.search(line)
        if m_title:
            flush()
            name = m_title.group(1).strip()
            year = YEAR_RE.search(name)
            upd = UPD_RE.search(line)
            qual = QUALITY_RE.search(line)
            cur = {
                "title": name,
                "slug": slugify(name),
                "year": year.group(1) if year else "",
                "episodes": upd.group(1) if upd else "",
                "quality": qual.group(1) if qual else "",
                "category": section,
                "links": {},
            }
            continue

        m_link = LINK_RE.match(line)
        if m_link and cur is not None:
            prov = PROVIDER_OF.match(line).group(1)
            url = m_link.group(1).rstrip("）)，,。;；")
            if prov not in cur["links"]:
                cur["links"][prov] = {"url": url, "pwd": extract_pwd(url)}

    flush()
    return items


def baidu_surl(url: str) -> tuple[str, str]:
    """从百度分享链接里取出 (surl, pwd)。"""
    m = re.search(r"pan\.baidu\.com/s/([A-Za-z0-9_\-]+)", url or "")
    return (m.group(1) if m else ""), extract_pwd(url)
