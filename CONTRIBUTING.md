# 参与开发

## 本地跑起来

```bash
cp .env.example .env      # 填最少配置，或先只跑静态页面
docker compose up -d --build web
# 打开 http://localhost:8099
```

改 Python 需要重建（没有热重载）：

```bash
docker compose up -d --build web
```

改 `app/static/*.html` 可以热替换：

```bash
docker cp app/static/index.html panfollow-web:/app/static/index.html
```

## 代码约定

- Python 3.11，单文件模块为主，避免过度抽象
- 所有网络请求都要有超时；失败不吞异常，但要在调用方转成用户看得懂的话
- 通知、AI 这类"锦上添花"的能力**永远不能阻塞主流程**（异常只写日志）
- 前端没有构建步骤：单 HTML + 原生 JS，避免引入框架

## 提交

- 一个 PR 一件事，附上"改之前 / 改之后"的实际行为
- 涉及抓取的改动，请在描述里贴上真实日志片段
- 不要提交 `.env`、`state/`、`app/bin/`、安卓签名文件

## 新增一个网盘来源

1. 在 `sources` 里把它当成一条记录（`provider` 字段区分）
2. 在 `probe_source()` 里实现"列目录 + 画质/集数探测"
3. 在 `download_show()` 里实现"下载到目标目录"
4. 探测失败时把原因写进 `probe_note`，UI 会直接显示

## 提交前的本地自检

不装任何工具也能跑：

```bash
python deploy/check_js.py app/static/index.html        # 前端内联脚本语法
python deploy/check_dockerfile.py app/Dockerfile       # Dockerfile 里每段 shell
python -m compileall -q app                            # Python 语法
```

CI 里还会跑 `docker compose config -q` 和完整镜像构建。