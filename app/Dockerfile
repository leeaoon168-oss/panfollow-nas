# PanFollow-NAS 运行镜像
#
# 只用官方公开底座，任何人任何地区都能拉
#   python:3.11-slim + Debian 自带 chromium
#
# BaiduPCS-Go 不随仓库分发（体积/协议/架构问题），构建时自动下载，
# 国内可通过 PCS_MIRROR 走加速代理。
#
#   自定义版本：  --build-arg PCS_VERSION=4.0.2
#   自己给地址：  --build-arg PCS_URL=https://your.host/BaiduPCS-Go.zip
#   完全不下载：  --build-arg PCS_VERSION=none   （运行时挂载 /app/bin/BaiduPCS-Go）

FROM python:3.11-slim-bookworm

ARG PIP_INDEX_URL=https://pypi.org/simple
# 默认用 HTTPS：部分网络环境会拦/污染 80 端口的 deb.debian.org
ARG DEBIAN_MIRROR=deb.debian.org
ARG PCS_VERSION=4.0.2
ARG PCS_MIRROR=
ARG PCS_URL=
# buildx 会注入：amd64 / arm64 / arm
ARG TARGETARCH=amd64

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    CHROMIUM_PATH=/usr/bin/chromium \
    PCS_BIN=/app/bin/BaiduPCS-Go

# apt 源统一改 HTTPS（兼容 deb822 与旧的 sources.list 两种格式）
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i -E "s#https?://(deb|security)\.debian\.org#https://${DEBIAN_MIRROR}#g" \
            /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i -E "s#https?://(deb|security)\.debian\.org#https://${DEBIAN_MIRROR}#g" \
            /etc/apt/sources.list; \
    fi; \
    cat /etc/apt/sources.list.d/debian.sources 2>/dev/null || cat /etc/apt/sources.list || true

# Chromium（抓文档用）+ 中文字体（截图/表格不乱码）+ 解压工具（下 PCS 用）
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        chromium \
        fonts-noto-cjk \
        ca-certificates \
        curl \
        unzip \
        tzdata; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir \
        --index-url "$PIP_INDEX_URL" --timeout 30 --retries 3 -r requirements.txt

# ---- BaiduPCS-Go（转存 + 下载的执行器）----
RUN set -eux; \
    mkdir -p /app/bin; \
    if [ "$PCS_VERSION" = "none" ]; then \
        echo "跳过下载 BaiduPCS-Go（运行时要挂载 /app/bin/BaiduPCS-Go）"; \
        exit 0; \
    fi; \
    if [ -n "$PCS_URL" ]; then \
        URLS="$PCS_URL"; \
    else \
        PCS_ARCH=amd64; \
        if [ "$TARGETARCH" = "arm64" ]; then PCS_ARCH=arm64; fi; \
        if [ "$TARGETARCH" = "arm" ]; then PCS_ARCH=armv7; fi; \
        echo "BaiduPCS-Go 目标架构: $TARGETARCH -> $PCS_ARCH"; \
        BASE="https://github.com/qjfoidnh/BaiduPCS-Go/releases/download/v${PCS_VERSION}/BaiduPCS-Go-v${PCS_VERSION}-linux-${PCS_ARCH}.zip"; \
        URLS="${PCS_MIRROR}${BASE} https://github.com/qjfoidnh/BaiduPCS-Go/releases/download/v${PCS_VERSION}/BaiduPCS-Go-v${PCS_VERSION}-linux-${PCS_ARCH}.zip https://ghfast.top/${BASE} https://gh-proxy.com/${BASE}"; \
    fi; \
    ok=0; \
    for u in $URLS; do \
        echo "尝试下载: $u"; \
        if curl -fsSL --retry 3 --connect-timeout 15 -o /tmp/pcs.zip "$u"; then ok=1; break; fi; \
    done; \
    [ "$ok" = "1" ] || { echo "BaiduPCS-Go 下载失败，可用 --build-arg PCS_URL=... 指定镜像"; exit 1; }; \
    unzip -o -q /tmp/pcs.zip -d /tmp/pcs; \
    BIN=$(find /tmp/pcs -type f -name 'BaiduPCS-Go' | head -n1); \
    [ -n "$BIN" ] || { echo "压缩包里没找到 BaiduPCS-Go"; exit 1; }; \
    install -m 0755 "$BIN" /app/bin/BaiduPCS-Go; \
    rm -rf /tmp/pcs /tmp/pcs.zip; \
    /app/bin/BaiduPCS-Go -v || true

COPY . .

EXPOSE 8099

HEALTHCHECK --interval=60s --timeout=5s --start-period=40s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/api/health', timeout=4).status == 200 else 1)"

CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8099"]
