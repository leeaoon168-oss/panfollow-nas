# Troubleshooting

[简体中文](TROUBLESHOOTING.md) · **English**

## Baidu

### `检测BDUSS有效性json解析出错: invalid character '<'`

BaiduPCS-Go's session self-check got an HTML anti-bot page instead of JSON — **this does not mean the
account is dead**.

First check whether the session still works:

```bash
docker exec -it panfollow-web BaiduPCS-Go who
```

- Prints `当前帐号 uid: ... 用户名: ...` → the session is fine, the app skips the self-check
- Prints "not logged in" → the BDUSS really expired, grab a new one

Verify the cookie directly (`errno:0` means it's valid):

```bash
curl -s 'https://pan.baidu.com/rest/2.0/xpan/nas?method=uinfo' \
  -H 'Cookie: BDUSS=<your BDUSS>'
```

### `errno 9019 needverify`

Baidu wants a captcha/verification. Retry later, or change your egress IP (some datacenter ranges
are heavily throttled).

### Transfer says "文件重复" (duplicate file)

Leftovers from a previous transfer are still in your drive. The app treats this as success and reuses
them; if the contents are wrong, delete the folder under `/apps/panfollow-nas/` in your drive.

## kdocs

### The document only parses a dozen entries

The editor is a virtualized list and only renders what's in the viewport. PanFollow-NAS scrolls and
merges (`kdocs.py`); if the count suddenly drops, the page structure probably changed.

### The spreadsheet parses to 0 entries

Spreadsheet content is drawn on a canvas. This project drives `window.WPSOpenApi.KSheetApplication`
and reads ranges; if WPS changes that frontend API, the extraction has to be re-reverse-engineered.

### Still "not logged in" after scanning the QR code

The cookie lives in `/state/doc_state.json`. Delete it and scan again:

```bash
docker exec -it panfollow-web rm -f /state/doc_state.json
```

## Downloads

### `sqlite3.OperationalError: unable to open database file`

Too few file descriptors. `docker-compose.override.yml` already raises `nofile` to 65536; if it still
happens, check the host's `fs.file-max`.

### Download finished but the file is 0 bytes

Baidu sometimes preallocates empty shells and fills them later. PanFollow-NAS checks the magic bytes
(`ftyp / MKV / RIFF / FLV / TS`); all-zero files are rejected and re-downloaded.

### Progress stuck at 0%

The BaiduPCS-Go output parser depends on its fixed text format; after upgrading that tool the regex
in `server.py` may need adjusting.

## Notifications

### WeCom / PushPlus return an error

Open **Notifications** in the drawer and look at the raw response of the latest entry:

- `93000 invalid webhook url` → wrong key or URL
- `code 905 未实名认证` → PushPlus requires real-name verification
- `errcode 40013 invalid corpid/appid` → a parameter is mistyped

### The Android app receives nothing

1. In the app: **Notifications → Enable system notifications**, allow the permission dialog
2. MIUI / ColorOS and friends: allow notifications, set **battery saver → unrestricted**, allow autostart
3. A persistent "PanFollow is running in the background" notification should be visible — if it isn't,
   the foreground service never started

## Quark

### A source says "夸克 暂不支持探测" (probing not supported)

That's an older version without the Quark module. Upgrade to v2.0.0+ and run **Probe and pick** again;
probing is guest-level and needs no login.

### Still "not logged in" after scanning

The cookie lives in `/state/quark_cookie.txt`. Delete it and scan again:

```bash
docker exec -it panfollow-web rm -f /state/quark_cookie.txt
```

### Stuck at "listing / waiting for Alist to see the transfer"

Alist caches directory trees. The app refreshes and retries 6 times (about 12 seconds). If it still
fails, check:

- Alist address / credentials in Settings → Connections
- Whether a `/quark` mount exists (the app creates it automatically during a download)
- Whether your Quark free space (~10 GB) is full — transfers fail when it is

### `直链无效 (HTTP 403 / JSON)` (invalid direct link)

Direct links expire. Every link is test-fetched before aria2 gets it. If it keeps happening, tap
**Sync** again, or re-scan the Quark QR so the Alist mount picks up a fresh cookie.

### Quark downloaded a few episodes and stopped

Quark downloads roll in batches: transfer a batch → download → clean up. A failure resumes on the next
sync. If it sits idle, check `docker compose logs --tail=100 web | grep quark`.

### Quark finishes but files are not in the folder I chose

Before v2.1 aria2 could only write to `/downloads`, so files could be left in the staging area. Since
v2.1 everything is downloaded to `/downloads/.quark-tmp/<show>` and then moved into your resource
folder; if the move fails you get an error (it no longer silently switches folders). Check permissions:

```bash
docker exec -it panfollow-web touch /mnt/data/.../.wtest
```

### The source panel says "switch = pin this one"

Tapping **Switch** locks that source, so automatic ranking won't move away from it. To go back to
automatic, unlock it and run **Probe and pick**.

## Reverse proxy

Behind Nginx, SSE must have buffering disabled, otherwise in-app notifications arrive very late:

```nginx
location /api/events {
    proxy_pass http://<NAS_IP>:8099;
    proxy_http_version 1.1;
    proxy_set_header Connection '';
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
}
```
