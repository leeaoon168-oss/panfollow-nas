# Deployment guide

[简体中文](DEPLOY.md) · **English**

For people installing their first Docker container. Takes about 10 minutes.

[简体中文](DEPLOY.md) · **English**

## 0. What you need

| Item | Why | Where |
|---|---|---|
| Docker + compose | to run the service | your NAS app center, or `apt install docker.io docker-compose-plugin` |
| A kdocs document / spreadsheet URL | the share list to follow | the one you maintain yourself |
| A **Baidu Netdisk SVIP** account | transferring and fast download both need it | — |
| A BDUSS / STOKEN pair | lets the app act on your Baidu account | see step 2 below |

> Disk space: a 4K episode is 1–2 GB, make sure your NAS has room.

## 1. Start it

```bash
git clone https://github.com/leeaoon168-oss/panfollow-nas.git
cd panfollow-nas
cp .env.example .env
vi .env        # see "How to fill the config" below
docker compose up -d --build
```

The build takes 3–8 minutes (installs Chromium, downloads BaiduPCS-Go).
Then open `http://<NAS_IP>:8099`.

### Slow build in mainland China

```bash
# pip via Tsinghua mirror, BaiduPCS-Go via a GitHub proxy
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
PCS_MIRROR=https://ghfast.top/ \
docker compose up -d --build
```

### Don't want to build? Use the prebuilt image

GitHub Actions builds `linux/amd64` + `linux/arm64` on every push:

```bash
docker pull ghcr.io/leeaoon168-oss/panfollow-nas:latest
IMAGE=ghcr.io/leeaoon168-oss/panfollow-nas:latest docker compose up -d
```

Starts in about 10 seconds, no build, no Debian/pip mirror problems.

## 2. How to fill the config

Only 4 values in `.env` are required:

```env
DOC_URL=                      # your kdocs document / spreadsheet URL
ALIST_BDUSS=                  # Baidu BDUSS
ALIST_STOKEN=                 # Baidu STOKEN
DOWNLOAD_HOST_DIR=            # where files land on the NAS, e.g. /vol1/1000/Downloads
```

### How to get BDUSS / STOKEN

1. Open <https://pan.baidu.com> in a desktop browser and sign in
2. Press `F12` → **Application** → **Cookies** → `https://pan.baidu.com`
3. Find `BDUSS`, copy its whole value → `ALIST_BDUSS`
4. Find `STOKEN` (also a long string) → `ALIST_STOKEN`

> Not every account shows `STOKEN` at first. If it's missing, open any **share link** in the drive,
> then look at the cookies again — it usually appears.

### kdocs needs no cookie

Open `http://<NAS_IP>:8099/login` after startup and scan the QR code once with WeChat.
The cookie is stored in `./state/doc_state.json` and reused automatically.

### Quark (optional, but common)

Skip this if you only follow Baidu links. To use Quark:

1. **Alist** — install it and put its address / credentials in Settings. The app creates a
   `driver=Quark` mount at `/quark` automatically and updates it when the cookie changes.
2. **aria2** — started by default with `docker compose up -d`; it writes the Quark direct link to disk.
   (Optional: without it the app falls back to its own streaming downloader.)
3. **QR login** — Settings → Connections → Quark → scan with the Quark app; the status flips to
   "logged in" in about 5 seconds.

> You can use Quark **without** logging in: Quark sources still get probed for episode count and
> quality and take part in source ranking, they just won't be downloaded.

## 3. Three things to do after the first start

1. **Scan the kdocs QR code**: `http://<NAS_IP>:8099/login`
2. **Check Baidu in Settings**: `http://<NAS_IP>:8099/settings` → paste BDUSS → click **Verify**;
   you should see `SVIP · <username>`
3. **Fetch the list once**: the ⟳ button in the header (or Discover → refresh)

Then tick the shows you want → tap **Sync** at the bottom.

## 4. Where files actually land (the most common misconfiguration)

Paths **inside** the container are fixed; the host paths come from `.env`:

| `.env` (host) | Inside container | Code variable | Purpose |
|---|---|---|---|
| `DOWNLOAD_HOST_DIR` | `/downloads` | `DOWNLOAD_DIR` | **where downloaded files go** |
| `DATA_HOST_DIR` | `/mnt/data` | `DATA_DIR` | the tree the folder picker can browse |

```
host  /volume1/media/shows   ←─ mount ─→  /downloads   ←─ the only place code writes to
                                     └─→  /downloads/<show title>/xxx.mp4
```

So:

- Synology/QNAP use `/volume1/...`, fnOS uses `/vol1/...` — **only the two `.env` lines change**,
  no code change needed
- If both are empty, compose falls back to `/vol1/1000/...`, which usually **does not exist**.
  In that case a red banner appears at the top of the page:

  > **Download folder is not writable** · `/downloads`
  > `FileNotFoundError: ...`
  > Check that `DOWNLOAD_HOST_DIR` in docker-compose points at a real host folder

- Per-show subfolders: open a show → **Folder** → navigate, or **＋ New** to create one on the spot
  (creation is limited to the two mount points above)

## 5. Automation

- Global: `SYNC_HOURS=24` in `.env` — refresh the list and sync every 24 hours
- Per show: **Check every 3h** in the detail sheet, and **Auto download** toggles whether new
  episodes are downloaded immediately
- Notifications: drawer → **Notifications**; supports WeCom / Email / PushPlus / Bark / webhook

## FAQ

**Build fails with `BaiduPCS-Go 下载失败`**
GitHub is unreachable. Add a proxy prefix:

```bash
PCS_MIRROR=https://ghfast.top/ docker compose up -d --build
```

Or download the zip yourself, put it under `app/bin/`, then:

```bash
docker build --build-arg PCS_VERSION=none -t panfollow-nas:local app/
```

**`docker compose up` complains the folder does not exist**
`DOWNLOAD_HOST_DIR` / `DATA_HOST_DIR` must be **absolute paths that exist** on your NAS.

**The page opens but no shows are found**
Most likely kdocs is not logged in. Check `docker compose logs -f web | grep -i login`,
or open `/login` and scan again.

**Do I need Alist / aria2?**
Only Baidu: no. Everything is done by BaiduPCS-Go inside the container.
Quark downloads: you need Alist; aria2 is recommended but optional (the app falls back to its
built-in streaming downloader).
