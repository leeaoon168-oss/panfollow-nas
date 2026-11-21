#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sqlite 状态：剧集清单、订阅、已入队文件。"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("STATE_DIR", "/state")) / "app.sqlite"


def conn() -> sqlite3.Connection:
    """带重试的连接：容器里 fds 紧张时会出现 unable to open database file。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    last = None
    for i in range(5):
        try:
            c = sqlite3.connect(DB_PATH, timeout=30)
            c.row_factory = sqlite3.Row
            return c
        except sqlite3.OperationalError as e:
            last = e
            msg = str(e).lower()
            if "unable to open" not in msg and "locked" not in msg:
                raise
            time.sleep(0.15 * (i + 1))
    raise last


def init() -> None:
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS shows (
            slug TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            year TEXT, episodes TEXT, quality TEXT, category TEXT,
            links TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            ep_changed_at INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS subs (
            slug TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL,
            start_ep INTEGER DEFAULT 1,
            active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS files (
            slug TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL,          -- queued / done / error
            note TEXT,
            ts INTEGER NOT NULL,
            PRIMARY KEY (slug, name)
        );
        CREATE TABLE IF NOT EXISTS meta (
            k TEXT PRIMARY KEY, v TEXT
        );
        CREATE TABLE IF NOT EXISTS dest (
            slug TEXT PRIMARY KEY,
            dir  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS policy (
            slug TEXT PRIMARY KEY,
            check_hours INTEGER DEFAULT 6,
            auto_dl INTEGER DEFAULT 0,
            last_check INTEGER DEFAULT 0,
            last_found INTEGER DEFAULT 0,
            new_eps INTEGER DEFAULT 0,
            last_done INTEGER DEFAULT 0,
            upgradable INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            title TEXT,
            body TEXT,
            ok INTEGER DEFAULT 0,
            detail TEXT DEFAULT '',
            type TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            name TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            added_at INTEGER DEFAULT 0,
            last_refresh INTEGER DEFAULT 0,
            last_count INTEGER DEFAULT 0,
            last_error TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL,
            provider TEXT DEFAULT '百度',
            url TEXT NOT NULL,
            pwd TEXT DEFAULT '',
            label TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            priority INTEGER DEFAULT 0,
            active INTEGER DEFAULT 0,
            last_probe INTEGER DEFAULT 0,
            probe_eps INTEGER DEFAULT 0,
            probe_max INTEGER DEFAULT 0,
            probe_quality TEXT DEFAULT '',
            probe_score INTEGER DEFAULT 0,
            probe_bytes INTEGER DEFAULT 0,
            probe_note TEXT DEFAULT '',
            locked INTEGER DEFAULT 0,
            UNIQUE(slug, url)
        );
        """)
        # 老库迁移：补 start_ep 列
        cols = {r["name"] for r in c.execute("PRAGMA table_info(subs)").fetchall()}
        if "start_ep" not in cols:
            c.execute("ALTER TABLE subs ADD COLUMN start_ep INTEGER DEFAULT 1")
        if "active" not in cols:
            c.execute("ALTER TABLE subs ADD COLUMN active INTEGER DEFAULT 1")
        scols = {r["name"] for r in c.execute("PRAGMA table_info(shows)").fetchall()}
        if "category" not in scols:
            c.execute("ALTER TABLE shows ADD COLUMN category TEXT")
        if "ep_changed_at" not in scols:
            c.execute("ALTER TABLE shows ADD COLUMN ep_changed_at INTEGER DEFAULT 0")
        srcs_cols = {r["name"] for r in c.execute("PRAGMA table_info(sources)").fetchall()}
        if srcs_cols and "locked" not in srcs_cols:
            c.execute("ALTER TABLE sources ADD COLUMN locked INTEGER DEFAULT 0")
        pcols = {r["name"] for r in c.execute("PRAGMA table_info(policy)").fetchall()}
        if "last_done" not in pcols:
            c.execute("ALTER TABLE policy ADD COLUMN last_done INTEGER DEFAULT 0")
        if "upgradable" not in pcols:
            c.execute("ALTER TABLE policy ADD COLUMN upgradable INTEGER DEFAULT 0")


def upsert_shows(items: list[dict]) -> None:
    now = int(time.time())
    with conn() as c:
        for it in items:
            # 集数发生变化时记一笔，UI 用来显示「刚更新」
            c.execute(
                """INSERT INTO shows(slug,title,year,episodes,quality,category,links,updated_at,ep_changed_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(slug) DO UPDATE SET
                     ep_changed_at=CASE
                        WHEN COALESCE(shows.episodes,'') <> COALESCE(excluded.episodes,'')
                         AND COALESCE(excluded.episodes,'') <> '' THEN ?
                        ELSE shows.ep_changed_at END,
                     title=excluded.title, year=excluded.year,
                     episodes=COALESCE(NULLIF(excluded.episodes,''), shows.episodes),
                     quality=COALESCE(NULLIF(excluded.quality,''), shows.quality),
                     category=COALESCE(NULLIF(excluded.category,''), shows.category),
                     links=CASE WHEN excluded.links IN ('', '{}') THEN shows.links
                                ELSE excluded.links END,
                     updated_at=excluded.updated_at""",
                (it["slug"], it["title"], it.get("year", ""), it.get("episodes", ""),
                 it.get("quality", ""), it.get("category", ""),
                 json.dumps(it["links"], ensure_ascii=False), now, now, now),
            )


def all_shows() -> list[dict]:
    with conn() as c:
        _migrate_archived(c)
        rows = c.execute("SELECT * FROM shows ORDER BY updated_at DESC, title").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["links"] = json.loads(d["links"])
        out.append(d)
    return out


def show(slug: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM shows WHERE slug=?", (slug,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["links"] = json.loads(d["links"])
    return d


def _migrate_archived(c) -> None:
    cols = {r[1] for r in c.execute("PRAGMA table_info(shows)").fetchall()}
    if "archived" not in cols:
        c.execute("ALTER TABLE shows ADD COLUMN archived INTEGER DEFAULT 0")
    if "archived_at" not in cols:
        c.execute("ALTER TABLE shows ADD COLUMN archived_at INTEGER DEFAULT 0")
    if "finished" not in cols:
        c.execute("ALTER TABLE shows ADD COLUMN finished INTEGER DEFAULT 0")
    if "total_eps" not in cols:
        c.execute("ALTER TABLE shows ADD COLUMN total_eps INTEGER DEFAULT 0")


def set_archived(slug: str, on: bool) -> None:
    """归档＝完结：停止追更、不再检查/下载，文件原地不动。"""
    with conn() as c:
        _migrate_archived(c)
        c.execute("UPDATE shows SET archived=?, archived_at=? WHERE slug=?",
                  (1 if on else 0, int(time.time()) if on else 0, slug))
        if on:
            # 顺带取消订阅，避免再进同步队列；但保留 subs 行（起始集不丢）
            c.execute("UPDATE subs SET active=0 WHERE slug=?", (slug,))


def archived_slugs() -> list[str]:
    with conn() as c:
        _migrate_archived(c)
        return [r["slug"] for r in
                c.execute("SELECT slug FROM shows WHERE COALESCE(archived,0)=1").fetchall()]


def subscribed() -> list[str]:
    with conn() as c:
        _migrate_archived(c)
        rows = c.execute("""SELECT s.slug FROM subs t JOIN shows s ON s.slug=t.slug
                            WHERE COALESCE(t.active,1)=1 AND COALESCE(s.archived,0)=0""").fetchall()
    return [r["slug"] for r in rows]


def sub_map() -> dict[str, int]:
    """slug -> 起始集（默认 1）。取消订阅也保留这一行，所以起始集不会丢。"""
    with conn() as c:
        rows = c.execute("SELECT slug, COALESCE(start_ep,1) AS ep FROM subs").fetchall()
    return {r["slug"]: int(r["ep"] or 1) for r in rows}


def set_sub(slug: str, on: bool, start_ep: int | None = None) -> None:
    """订阅/取消订阅只翻 active 位，绝不删行——否则起始集会丢。"""
    with conn() as c:
        c.execute(
            """INSERT INTO subs(slug,created_at,start_ep,active) VALUES (?,?,?,?)
               ON CONFLICT(slug) DO UPDATE SET
                 active=excluded.active,
                 start_ep=COALESCE(?, start_ep)""",
            (slug, int(time.time()), start_ep or 1, 1 if on else 0, start_ep))
        if on:
            # 重新订阅＝恢复追更，自动解除归档
            _migrate_archived(c)
            c.execute("UPDATE shows SET archived=0, archived_at=0 WHERE slug=?", (slug,))


def mark_file(slug: str, name: str, status: str, note: str = "") -> None:
    with conn() as c:
        c.execute(
            """INSERT INTO files(slug,name,status,note,ts) VALUES (?,?,?,?,?)
               ON CONFLICT(slug,name) DO UPDATE SET
                 status=excluded.status, note=excluded.note, ts=excluded.ts""",
            (slug, name, status, note[:300], int(time.time())),
        )


def file_status(slug: str) -> dict[str, str]:
    with conn() as c:
        return {r["name"]: r["status"] for r in
                c.execute("SELECT name,status FROM files WHERE slug=?", (slug,))}


def get_meta(k: str, default: str = "") -> str:
    with conn() as c:
        r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def set_meta(k: str, v: str) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES (?,?)", (k, v))


def get_dest(slug: str) -> str:
    with conn() as c:
        r = c.execute("SELECT dir FROM dest WHERE slug=?", (slug,)).fetchone()
    return r["dir"] if r else ""


def set_dest(slug: str, d: str) -> None:
    with conn() as c:
        if d:
            c.execute("INSERT OR REPLACE INTO dest(slug,dir) VALUES (?,?)", (slug, d))
        else:
            c.execute("DELETE FROM dest WHERE slug=?", (slug,))


def all_dest() -> dict[str, str]:
    with conn() as c:
        return {r["slug"]: r["dir"] for r in c.execute("SELECT slug, dir FROM dest")}


def get_pref(slug: str, default: str = "best") -> str:
    return get_meta("q:" + slug, default) or default


def set_pref(slug: str, val: str) -> None:
    if val and val != "best":
        set_meta("q:" + slug, val)
    else:
        with conn() as c:
            c.execute("DELETE FROM meta WHERE k=?", ("q:" + slug,))


# ---------- 每部剧的检测/下载策略 ----------
DEFAULT_POLICY = {"check_hours": 6, "auto_dl": 0, "last_check": 0,
                  "last_found": 0, "new_eps": 0}


def get_policy(slug: str) -> dict:
    with conn() as c:
        r = c.execute("SELECT * FROM policy WHERE slug=?", (slug,)).fetchone()
    return dict(r) if r else dict(DEFAULT_POLICY)


def set_policy(slug: str, check_hours: int | None = None,
               auto_dl: bool | None = None) -> dict:
    with conn() as c:
        c.execute("INSERT INTO policy(slug) VALUES (?) ON CONFLICT(slug) DO NOTHING", (slug,))
        if check_hours is not None:
            c.execute("UPDATE policy SET check_hours=? WHERE slug=?", (int(check_hours), slug))
        if auto_dl is not None:
            c.execute("UPDATE policy SET auto_dl=? WHERE slug=?", (1 if auto_dl else 0, slug))
    return get_policy(slug)


def touch_policy(slug: str, last_check: int | None = None,
                 last_found: int | None = None, new_eps: int | None = None,
                 last_done: int | None = None, upgradable: int | None = None) -> None:
    with conn() as c:
        c.execute("INSERT INTO policy(slug) VALUES (?) ON CONFLICT(slug) DO NOTHING", (slug,))
        if last_check is not None:
            c.execute("UPDATE policy SET last_check=? WHERE slug=?", (int(last_check), slug))
        if last_found is not None:
            c.execute("UPDATE policy SET last_found=? WHERE slug=?", (int(last_found), slug))
        if new_eps is not None:
            c.execute("UPDATE policy SET new_eps=? WHERE slug=?", (int(new_eps), slug))
        if last_done is not None:
            c.execute("UPDATE policy SET last_done=? WHERE slug=?", (int(last_done), slug))
        if upgradable is not None:
            c.execute("UPDATE policy SET upgradable=? WHERE slug=?", (int(upgradable), slug))


def all_policy() -> dict[str, dict]:
    with conn() as c:
        return {r["slug"]: dict(r) for r in c.execute("SELECT * FROM policy")}


# ---------- 资源来源 ----------
def add_source(slug: str, url: str, pwd: str = "", label: str = "",
               provider: str = "百度") -> int:
    with conn() as c:
        c.execute(
            """INSERT INTO sources(slug,provider,url,pwd,label) VALUES (?,?,?,?,?)
               ON CONFLICT(slug,url) DO UPDATE SET
                 label=CASE WHEN excluded.label<>'' THEN excluded.label ELSE sources.label END""",
            (slug, provider, url, pwd, label))
        r = c.execute("SELECT id FROM sources WHERE slug=? AND url=?", (slug, url)).fetchone()
        return int(r["id"]) if r else 0


def _migrate_source_dead(c) -> None:
    cols = {r[1] for r in c.execute("PRAGMA table_info(sources)").fetchall()}
    if "dead" not in cols:
        c.execute("ALTER TABLE sources ADD COLUMN dead INTEGER DEFAULT 0")


def mark_source_dead(sid: int, note: str = "") -> None:
    """这条链接失效了：标记为死链，不再参与选优。"""
    with conn() as c:
        _migrate_source_dead(c)
        c.execute("UPDATE sources SET dead=1, active=0 WHERE id=?", (sid,))
        c.execute("UPDATE sources SET probe_note=? WHERE id=?", ((note or '')[:180], sid))


def revivable_sources(slug: str) -> list[dict]:
    """可选来源：启用的、且没被标死链的，新加的优先。"""
    with conn() as c:
        _migrate_source_dead(c)
        rows = c.execute("""SELECT * FROM sources
                            WHERE slug=? AND COALESCE(enabled,1)=1 AND COALESCE(dead,0)=0
                            ORDER BY id DESC""", (slug,)).fetchall()
    return [dict(r) for r in rows]


def list_sources(slug: str) -> list[dict]:
    with conn() as c:
        _migrate_source_dead(c)
        return [dict(r) for r in c.execute(
            "SELECT * FROM sources WHERE slug=? ORDER BY active DESC, priority DESC, id", (slug,))]


def all_sources() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    with conn() as c:
        for r in c.execute("SELECT * FROM sources ORDER BY slug, priority DESC, id"):
            out.setdefault(r["slug"], []).append(dict(r))
    return out


def get_source(sid: int) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
    return dict(r) if r else None


def update_source(sid: int, **kw) -> None:
    if not kw:
        return
    cols = ", ".join(f"{k}=?" for k in kw)
    with conn() as c:
        c.execute(f"UPDATE sources SET {cols} WHERE id=?", (*kw.values(), sid))


# ---------- 资源文档（可以有多个 kdocs 文档） ----------
def add_notification(title: str, body: str, ok: bool, detail: str, ntype: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO notifications(ts,title,body,ok,detail,type) VALUES (?,?,?,?,?,?)",
                  (int(time.time()), title[:200], body[:800], 1 if ok else 0,
                   (detail or '')[:300], ntype or ''))
        # 只留最近 200 条
        c.execute("""DELETE FROM notifications WHERE id <=
                     (SELECT MAX(id) - 200 FROM notifications)""")


def list_notifications(limit: int = 50) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,))]


def add_document(url: str, name: str = "") -> int:
    with conn() as c:
        c.execute(
            """INSERT INTO documents(url,name,added_at) VALUES (?,?,?)
               ON CONFLICT(url) DO UPDATE SET
                 name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE documents.name END""",
            (url, name, int(time.time())))
        r = c.execute("SELECT id FROM documents WHERE url=?", (url,)).fetchone()
        return int(r["id"]) if r else 0


def list_documents() -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM documents ORDER BY id")]


def get_document(did: int) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
    return dict(r) if r else None


def update_document(did: int, **kw) -> None:
    if not kw:
        return
    cols = ", ".join(f"{k}=?" for k in kw)
    with conn() as c:
        c.execute(f"UPDATE documents SET {cols} WHERE id=?", (*kw.values(), did))


def delete_document(did: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM documents WHERE id=?", (did,))


def set_locked(slug: str, sid: int, locked: bool) -> None:
    """锁定后自动选优不会覆盖它。"""
    with conn() as c:
        if locked:
            c.execute("UPDATE sources SET locked=0 WHERE slug=?", (slug,))
        c.execute("UPDATE sources SET locked=? WHERE id=?", (1 if locked else 0, sid))


def delete_source(sid: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM sources WHERE id=?", (sid,))


def set_active(slug: str, sid: int) -> None:
    with conn() as c:
        c.execute("UPDATE sources SET active=0 WHERE slug=?", (slug,))
        c.execute("UPDATE sources SET active=1 WHERE id=?", (sid,))


def active_source(slug: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM sources WHERE slug=? AND active=1", (slug,)).fetchone()
        if r:
            return dict(r)
        r = c.execute(
            "SELECT * FROM sources WHERE slug=? AND enabled=1 ORDER BY priority DESC, id LIMIT 1",
            (slug,)).fetchone()
    return dict(r) if r else None


def import_sheet_items(items: list[dict], doc_name: str) -> tuple[int, int]:
    """表格导入：没有的剧就建（带链接）；已有的只补空字段；链接一律作为新来源加进去。"""
    new_shows = new_sources = 0
    with conn() as c:
        for it in items:
            row = c.execute("SELECT episodes, quality FROM shows WHERE slug=?",
                            (it["slug"],)).fetchone()
            if not row:
                c.execute(
                    """INSERT INTO shows(slug,title,year,episodes,quality,category,links,
                                         updated_at,finished,total_eps)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (it["slug"], it["title"], it.get("year", ""), it.get("episodes", ""),
                     it.get("quality", ""), it.get("category", ""),
                     json.dumps(it.get("links") or {}, ensure_ascii=False), int(time.time()),
                     1 if it.get("finished") else 0, int(it.get("total_eps") or 0)))
                new_shows += 1
            else:
                sets, vals = [], []
                if not (row["episodes"] or "").strip() and it.get("episodes"):
                    sets.append("episodes=?")
                    vals.append(it["episodes"])
                if not (row["quality"] or "").strip() and it.get("quality"):
                    sets.append("quality=?")
                    vals.append(it["quality"])
                if it.get("finished"):
                    sets.append("finished=?")
                    vals.append(1)
                if it.get("total_eps"):
                    sets.append("total_eps=?")
                    vals.append(int(it["total_eps"]))
                if sets:
                    c.execute("UPDATE shows SET " + ",".join(sets) + " WHERE slug=?",
                              (*vals, it["slug"]))
            for prov, lk in (it.get("links") or {}).items():
                url = (lk or {}).get("url")
                if not url:
                    continue
                cur = c.execute("SELECT id FROM sources WHERE slug=? AND url=?",
                                (it["slug"], url)).fetchone()
                if cur:
                    continue
                c.execute("INSERT INTO sources(slug,provider,url,pwd,label) VALUES (?,?,?,?,?)",
                          (it["slug"], prov, url, (lk or {}).get("pwd", ""), doc_name))
                new_sources += 1
    return new_shows, new_sources


def enrich_from_sheet(items: list[dict]) -> int:
    """表格来源只用来【补充】已有剧的资料：不新增条目、不覆盖已有集数。"""
    n = 0
    with conn() as c:
        for it in items:
            row = c.execute("SELECT episodes, quality, category FROM shows WHERE slug=?",
                            (it["slug"],)).fetchone()
            if not row:
                continue
            upd = {}
            if not (row["episodes"] or "").strip() and it.get("episodes"):
                upd["episodes"] = it["episodes"]
            if not (row["quality"] or "").strip() and it.get("quality"):
                upd["quality"] = it["quality"]
            if not (row["category"] or "").strip() and it.get("category"):
                upd["category"] = it["category"]
            if upd:
                cols = ", ".join(f"{k}=?" for k in upd)
                c.execute(f"UPDATE shows SET {cols} WHERE slug=?", (*upd.values(), it["slug"]))
                n += 1
    return n


def seed_sources_from_links(doc_name: str = "文档自带") -> int:
    """把文档里解析到的链接补成来源记录（只补缺，不覆盖）。"""
    n = 0
    with conn() as c:
        rows = c.execute("SELECT slug, links FROM shows").fetchall()
        for r in rows:
            try:
                links = json.loads(r["links"] or "{}")
            except Exception:
                continue
            for prov, v in links.items():
                url = (v or {}).get("url") or ""
                if not url:
                    continue
                cur = c.execute("SELECT id FROM sources WHERE slug=? AND url=?",
                                (r["slug"], url)).fetchone()
                if cur:
                    continue
                c.execute("INSERT INTO sources(slug,provider,url,pwd,label) VALUES (?,?,?,?,?)",
                          (r["slug"], prov, url, (v or {}).get("pwd", ""), doc_name))
                n += 1
    return n
