FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY yt-dlp.conf yta-dlp.conf ./
COPY templates/ ./templates/
COPY static/ ./static/
COPY assets/ ./assets/
COPY deploy/docker/config.json ./config.json

RUN groupadd --gid 1000 dropload \
    && useradd --uid 1000 --gid dropload --create-home dropload \
    && mkdir -p /app/urls /app/tmp /app/files /app/logs /app/data \
    && chown -R dropload:dropload /app

USER dropload

EXPOSE 5100
VOLUME ["/app/urls", "/app/tmp", "/app/files", "/app/logs", "/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5100/', timeout=4)"]

ENTRYPOINT ["python", "docker-entrypoint.py"]
