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
# Cloud Run 通过 $PORT 注入端口(默认 8080);shell 形式以便展开变量
CMD exec uvicorn api.server:app --host 0.0.0.0 --port ${PORT:-8080}
