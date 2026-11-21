# BaiduPCS-Go 的发布包是 Go 静态编译的单文件二进制。
# 国内拉 GitHub Release 经常失败，所以这里不在 build 阶段联网，
# 改成：你先把二进制放到本目录，再 docker build。
#
#   下载 https://github.com/qjfoidnh/BaiduPCS-Go/releases/download/v4.0.2/BaiduPCS-Go-v4.0.2-linux-amd64.zip
#   解压后把 BaiduPCS-Go 放到本目录（覆盖本文件旁边的同名文件），再 build。

FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tzdata python3 \
 && rm -rf /var/lib/apt/lists/*

COPY BaiduPCS-Go /usr/local/bin/BaiduPCS-Go
RUN chmod +x /usr/local/bin/BaiduPCS-Go

COPY sync.py /app/sync.py

ENV BAIDUPCS_GO_CONFIG_DIR=/config \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app
CMD ["python3", "-u", "/app/sync.py"]
