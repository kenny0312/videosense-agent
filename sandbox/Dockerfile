# ── Stage 5 Sandbox — Docker image ────────────────────────────────────────────
# Minimal Python image. No curl, wget, or git to reduce attack surface.
FROM python:3.11-slim

# Don't run as root inside the container
RUN useradd --create-home sandbox
WORKDIR /app

# Install only what the sandbox needs
COPY sandbox/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the sandbox package (not the whole repo)
COPY sandbox/ ./sandbox/

# Drop to non-root user
USER sandbox

# Cloud Run expects the service to listen on $PORT (default 8080)
ENV PORT=8080
EXPOSE 8080

CMD ["python", "-m", "uvicorn", "sandbox.server:app", "--host", "0.0.0.0", "--port", "8080"]
