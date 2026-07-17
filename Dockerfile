# VideoSense Agent — API + 聊天前端(容器交付 / Cloud Run)
# 仅打包在线问答所需:api / pipeline / mcp_server / web / sandbox(client)。
# 离线 ingestion/perception 不在运行路径,无需其重型依赖(yt-dlp/ffmpeg)。
FROM python:3.11-slim

WORKDIR /app

# 先装依赖(利用 Docker 层缓存)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷代码(.dockerignore 已排除密钥 / 缓存 / 本地产物)
COPY . .

# 非 root 运行
RUN useradd -m appuser && chown -R appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1
# P0-3 fail-closed:镜像里钉死 APP_ENV=prod → 任何 Cloud Run 部署若【没设 APP_ACCESS_KEYS】
# 会在启动即 raise(server.py),revision 被标 unhealthy 不切流量 → 旧好版本继续服务,
# 绝不会以「无口令裸奔」上线(videosense-pyai 就是这么烧钱的)。本地 uvicorn 直跑不经 Docker,
# APP_ENV 仍未设 → 开发照旧无鉴权。要在容器里临时无鉴权调试:-e APP_DEV_MODE=1。
ENV APP_ENV=prod
# Cloud Run 通过 $PORT 注入端口(默认 8080);shell 形式以便展开变量。
# --proxy-headers:认 Cloud Run 代理的 X-Forwarded-Proto/For → request.base_url 用 https
#   (否则容器内只见 http,拼出来的 plot_url 是 http://)。--forwarded-allow-ips='*':
#   Cloud Run 容器只经 Google 前端代理可达,信任所有上游是安全的。
CMD exec uvicorn api.server:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'
