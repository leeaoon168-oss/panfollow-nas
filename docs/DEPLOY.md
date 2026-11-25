# 部署指南

**简体中文** · [English](DEPLOY.en.md)

面向"第一次用 Docker 装东西"的人。全程约 10 分钟。

## 0. 你需要准备

| 东西 | 为什么 | 去哪弄 |
|---|---|---|
| Docker + compose | 跑服务 | NAS 应用中心，或 `apt install docker.io docker-compose-plugin` |
| 一个 kdocs 文档/表格链接 | 追更清单的来源 | 你自己维护的那份 |
| 百度网盘 **SVIP** 账号 | 转存和高速下载都要求会员 | — |
| 一份 BDUSS / STOKEN | 让程序能操作你的百度网盘 | 见下面第 2 步 |

> 存储空间：4K 剧一集 1~2GB，先确认 NAS 有地方放。

## 1. 起服务

```bash
git clone <仓库地址> panfollow-nas
cd panfollow-nas
cp .env.example .env
vi .env        # 见下面"配置怎么填"
docker compose up -d --build
```

构建约 3~8 分钟（要装 Chromium 和下载 BaiduPCS-Go）。
完成后打开 `http://<NAS_IP>:8099`。

### 国内构建慢

```bash
# pip 走清华源，PCS 走加速镜像
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
PCS_MIRROR=https://ghfast.top/ \
docker compose up -d --build
```

### 不想构建（用自己的预构建镜像）

仓库的 GitHub Actions 会在每次 push 后构建 `linux/amd64` + `linux/arm64` 双架构镜像：

```bash
docker pull ghcr.io/leeaoon168-oss/panfollow-nas:latest
IMAGE=ghcr.io/leeaoon168-oss/panfollow-nas:latest docker compose up -d
```

启动差不多 10 秒，不占构建时间，也不用管 Debian/pip 源的问题。

## 2. 配置怎么填

`.env` 里只有 4 项是必填的：

```env
DOC_URL=                      # 你的 kdocs 文档/表格地址
ALIST_BDUSS=                  # 百度 BDUSS
ALIST_STOKEN=                 # 百度 STOKEN
DOWNLOAD_HOST_DIR=            # 下载到 NAS 的哪个目录，如 /vol1/1000/Downloads
```

### 怎么拿 BDUSS / STOKEN

1. 电脑浏览器打开 <https://pan.baidu.com> 并登录
2. 按 `F12` → **Application（应用）** → **Cookies** → `https://pan.baidu.com`
3. 找到 `BDUSS`，复制整行的 Value（很长一串）→ 填 `ALIST_BDUSS`
4. 再找 `STOKEN`（同样几十位）→ 填 `ALIST_STOKEN`

> STOKEN 不是所有账号都会出现。没有的话先在网盘里随便打开一个**分享链接**，
> 再回来看 Cookies，通常就有了。

### kdocs 不用填 Cookie

启动后打开 `http://<NAS_IP>:8099/login`，用手机微信扫码登录一次，
Cookie 会存到 `./state/doc_state.json`，之后自动复用。

### 夸克（可选，但很常见）

只追百度的话跳过这节。要用夸克：

1. **Alist**：应用里装好，并在设置页填地址/账号密码（本程序会自动在 Alist 里建一个
   `driver=Quark` 的挂载点 `/quark`，cookie 变了会自动更新）
2. **aria2**：`docker compose up -d` 默认就会起，负责把夸克直链落盘
3. **扫码登录**：设置页 →「连接与账号」→「夸克网盘」→ 用夸克 App 扫码，约 5 秒后状态变「已登录」

> 不登录也能用：夸克来源会参与"集数/画质"的探测与选优，只是不会去下载。

## 3. 三个必做的初始化

1. **扫码登录 kdocs**：`http://<NAS_IP>:8099/login`
2. **设置页检查百度**：`http://<NAS_IP>:8099/settings` → 填 BDUSS（可在这里改）→ 点「验证」
   出现 `SVIP · 用户名` 就对了
3. **抓一次清单**：首页右上角 ⟳（或「发现」→ 刷新）

然后勾选要追的剧 → 底栏「同步」。

## 4. 文件到底落在哪（这是新用户最容易配错的地方）

容器里的路径是**固定**的，宿主机路径由 `.env` 决定：

| `.env` 里填的（宿主机） | 容器里看到 | 代码里的变量 | 作用 |
|---|---|---|---|
| `DOWNLOAD_HOST_DIR` | `/downloads` | `DOWNLOAD_DIR` | **下载文件落在哪** |
| `DATA_HOST_DIR` | `/mnt/data` | `DATA_DIR` | App 里浏览/新建下载目录的范围 |

```
宿主机 /volume1/media/追更   ←─ 挂载 ─→  /downloads   ←─ 代码只会写到这
                                   └─→  /downloads/剧名/xxx.mp4
```

所以：

- 别人的 NAS 用 `/volume1/...`、群晖用 `/volume1/...`、fnOS 用 `/vol1/...`，
  **只要改 `.env` 里这两行，代码一行都不用动**
- 两项都留空时，compose 会回落到 `/vol1/1000/...`，多数情况下**不存在**，
  这时页面顶部会出现红色提示条：

  > **下载目录不可写** · `/downloads`
  > `FileNotFoundError: ...`
  > 检查 docker-compose 里 `DOWNLOAD_HOST_DIR` 是否指向宿主机上真实存在的目录

- 单部剧还能再细分目录：点某部剧 →「目录」→ 点进去，或点「＋ 新建」当场建文件夹
  （新建范围限制在上面这两个挂载点之内）

## 5. 自动化

- 全局：`.env` 里 `SYNC_HOURS=24`，每 24 小时自动刷新清单 + 同步
- 单剧：详情页「每 3h 查」可单独设节奏，「自动下载 开」控制发现新集后是否直接下
- 通知：抽屉 →「消息推送」，支持企业微信 / 邮件 / PushPlus 等

## 常见问题

**构建报 `BaiduPCS-Go 下载失败`**
GitHub 拉不动。加加速前缀：

```bash
PCS_MIRROR=https://ghfast.top/ docker compose up -d --build
```

或者自己下载那个 zip 放到 `app/bin/`，再：

```bash
docker build --build-arg PCS_VERSION=none -t panfollow-nas:local app/
```

**`docker compose up` 报目录不存在**
`DOWNLOAD_HOST_DIR` / `DATA_HOST_DIR` 必须是你 NAS 上**真实存在**的绝对路径。

**页面能开但抓不到剧**
八成是 kdocs 没登录。看 `docker compose logs -f web | grep -i login`，
或者直接打开 `/login` 重新扫码。

**需要装 Alist / aria2 吗？**
只追百度：不用，下载全部由容器内的 BaiduPCS-Go 完成。
要下夸克：需要 Alist；aria2 推荐但不强制（没起会退回内置下载）。
