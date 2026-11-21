# 排错手册

## 百度

### `检测BDUSS有效性json解析出错: invalid character '<'`

BaiduPCS-Go 自检时拿到的是 HTML 风控页，**不代表账号失效**。

先确认会话是否真的还在：

```bash
docker exec -it panfollow-web BaiduPCS-Go who
```

- 能打印 `当前帐号 uid: ... 用户名: ...` → 会话有效，程序会自动跳过自检（本项目已处理）
- 打印"未登录" → BDUSS 真过期，重新抓

直接验证 Cookie 是否有效（返回 `errno:0` 即正常）：

```bash
curl -s 'https://pan.baidu.com/rest/2.0/xpan/nas?method=uinfo' \
  -H 'Cookie: BDUSS=<你的BDUSS>'
```

### `errno 9019 needverify`

百度风控要求验证。换个时间重试，或换网络出口（部分机房 IP 被重点关照）。

### 转存报"文件重复"

上一次转存的残留在网盘里。程序会当作成功直接复用；如果内容不对，到网盘里清掉 `/apps/panfollow-nas/` 下对应目录。

## kdocs

### 文档只解析到十几条

编辑器是虚拟列表，只渲染视口内的条目。必须滚动合并 —— `kdocs.py` 已实现，若条目剧减说明页面结构变了。

### 表格解析到 0 条

表格内容是 canvas 绘制的。本项目的做法是走 `window.WPSOpenApi.KSheetApplication` 读 Range；
如果 WPS 改了前端接口，需要重新挖。

### 扫码登录后仍提示未登录

Cookie 存在 `/state/doc_state.json`。删掉它重新扫码：

```bash
docker exec -it panfollow-web rm -f /state/doc_state.json
```

## 下载

### `sqlite3.OperationalError: unable to open database file`

文件句柄不够。`docker-compose.override.yml` 已把 `nofile` 调到 65536；若仍出现，
检查宿主机 `fs.file-max`。

### 下载完成但文件是 0 字节

百度有时会预分配空壳文件再填内容。本项目用魔数校验（`ftyp / MKV / RIFF / FLV / TS`）识别，
全零直接判为无效并重下。

### 进度卡在 0%

`BaiduPCS-Go` 的输出解析依赖其固定格式；升级该工具后可能需要同步调整 `server.py` 里的正则。

## 通知

### 企业微信/PushPlus 返回错误

到「消息推送」面板看最近通知的原始返回：

- `93000 invalid webhook url` → key 或地址不对
- `code 905 未实名认证` → PushPlus 需要实名
- `errcode 40013 invalid corpid/appid` → 参数拼错

### 安卓 App 收不到通知

1. App 内「消息推送」→「开启系统通知」，确认系统弹窗点了允许
2. MIUI/ColorOS 等需额外设置：通知管理允许、**省电策略 → 无限制**、自启动允许
3. 通知栏应常驻一条「追更在后台运行」（前台服务标识），没有说明服务没起来

## 夸克

### 来源显示「夸克 暂不支持探测」

旧版本没有夸克模块。升级到 v1.1.0 之后重新「探测并选优」即可；探测是游客级的，不需要登录。

### 扫码后仍显示未登录

cookie 存在 `/state/quark_cookie.txt`。删掉重新扫：

```bash
docker exec -it panfollow-web rm -f /state/quark_cookie.txt
```

### 下载卡在「列目录 / 等 Alist 看到转存内容」

Alist 会缓存目录树。程序会自动 `refresh` 并重试 6 次（约 12 秒）。仍然不行就检查：

- Alist 地址/账号填对了吗（设置 → 连接与账号）
- Alist 里是否已经有 `/quark` 挂载（没有的话程序会在下载时自动建）
- 夸克免费空间是否已满（10GB 左右），满了会转存失败

### `直链无效（HTTP 403 / JSON）`

直链有时效，程序每条都会先试链再交给 aria2。反复出现就重新点一次「同步」，
或者确认 Alist 的 Quark 驱动 cookie 是最新的（重新扫码会同步更新挂载）。

### 夸克只下了几集就停了

夸克是按批滚动下载的：转存一批 → 下载 → 清理。中途失败会在下次同步继续。
如果长时间不动，看日志 `docker compose logs --tail=100 web | grep quark`。

## 反代

用 Nginx 反代时，SSE 必须关缓冲，否则 App 内通知要等很久才出现：

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
