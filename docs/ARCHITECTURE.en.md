# Architecture

[简体中文](ARCHITECTURE.md) · **English**

## Data flow

```
           ┌──────────── scraping layer (Playwright) ────────────┐
documents  │  document: scroll + merge the virtualized list      │
(several)  │  spreadsheet: read cells + hyperlinks via WPS API   │
           └───────────────────┬─────────────────────────────────┘
                               ▼
                        shows  ──▶  subs (subscribed, start episode, check interval)
                               │
                               ▼
                        sources
                a show can have N share links
                label = which document it came from; dead = invalidated
                               │
                      pick_source()
                  ① probe each source: transfer → list → quality/episodes
                  ② when episodes are missing, "covers what we need" beats quality
                  ③ quality within 5%, or an unknown label → ask the AI
                  ④ write the winner to `active`
                               │
                               ▼
                     download_show()
          BaiduPCS-Go transfer → walk the folder → diff against local files
                    → download only the missing episodes → verify size
                               │
                               ▼
                     /downloads/<show title>/
```

## Key modules

| Module | Responsibility | Notes |
|---|---|---|
| `server.py` | routes, orchestration, schedulers, SSE | a global `_PCS_LOCK` serializes the Baidu channel (BaiduPCS-Go keeps its working directory in global state) |
| `store.py` | SQLite | `shows / subs / sources / documents / policy / files / notifications`; connection retries 5× |
| `kdocs.py` | document parsing | the virtualized list must be scrolled and merged, otherwise you only get a dozen entries |
| `sheetlinks.py` | spreadsheet parsing | content is drawn on a canvas, the DOM has no text; drives `WPSOpenApi.KSheetApplication` and `Hyperlinks.Item(1).Address` |
| `ai.py` | DeepSeek arbitration | returns only a candidate id; results cached 6 h by candidate fingerprint; silently falls back to rules |
| `notify.py` | notifications | multi-channel routing + history; pushing never blocks the main flow |

## Why there is no episode-number parsing

File names in cloud shares are all over the place:

```
第06集.mp4    S01E06.mp4    06.4K.HDR.mkv    EP06 4K.mp4
```

Any regex breaks on some source. Instead the app does a **directory diff**: files in the share minus
files on disk — the remainder is what to download. Independent of naming, naturally idempotent.

Episode numbers are only used for progress display and finished detection, never for download
decisions.

## Source ranking (`rank_sources`)

```
need = document episodes (or total episodes) - local episode count

need > 0:
    ┌ some source covers `need`  → pick the best quality among those
    └ none covers it             → pick the most complete one

need = 0 (already complete):
    quality first, episode counts within 2 treated as equal
```

## Failure handling

| Situation | Behaviour |
|---|---|
| Transfer returns "share gone / cancelled / does not exist" | mark `sources.dead=1`, **immediately retry with the next usable source** |
| Source is alive but has too few episodes | look for a more complete source (`need` first) |
| Baidu's "validate BDUSS" endpoint returns HTML | re-check with `pcs who`, continue if the session is alive |
| Every source fails | store a `dead:<slug>` fingerprint, wait for the list to publish a new link |

## The Quark path

Baidu and Quark take different routes because their APIs differ:

```
Baidu: BaiduPCS-Go (inside the container)
  transfer → list → diff → download directly

Quark:
  probe (guest, no login)
    share token → recursive detail → names/sizes → count episodes, score quality
  download (needs login)
    QR scan → cookie in /state/quark_cookie.txt
    → transfer into your own drive (free tier ~10 GB, so it rolls in batches)
    → Alist's Quark driver exposes a direct link
    → aria2 writes it to disk → clean up the transfer
```

Key points:

- **Probing needs no account**, so Quark sources compete with Baidu in ranking instead of being excluded
- **Downloading needs an account**, because large files must go through `file/download` with your own
  drive credentials
- Quark shares are often **multi-part zip**; they are extracted after download and renamed per episode
- Alist returns JSON/HTML when a direct link expires, so every link is test-fetched with
  `Range: bytes=0-320` before saving

## Where Quark files land (v2.1)

The aria2 container only mounts `/downloads` and cannot write to `/mnt/data`, while resource folders
usually live under `/mnt/data`. So:

```
transfer → Alist direct link → download to /downloads/.quark-tmp/<show>
                                   ↓ this process (root) moves it
                              /mnt/data/.../<show>/xxx.mp4
```

- If the move fails you get an **error** — it never silently picks another folder
- aria2 jobs pointing at `/mnt/data` are purged (they would stay active forever)
- Dedupe is by **episode number**: if the episode exists locally, skip regardless of file name
- When aria2 is unavailable it falls back to the built-in streaming downloader
  (`_quark_fetch`, resumable, with size validation)

## Threading model

- FastAPI (uvicorn) + one download worker thread + one check thread + one refresh scheduler thread
- All Baidu operations are serialized (`_PCS_LOCK`); checks step aside while a download is running
- Spreadsheet and document scraping share one headless Chromium instance, released after each run
