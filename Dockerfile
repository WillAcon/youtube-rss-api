# Single-container image: JSON API + RSS files on one port.
# Data (channels.json, state.json, cache/, feeds/) lives in /data so it can be a volume.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    YT_API_HOST=0.0.0.0 \
    YT_FEEDS_BASE=/data \
    YT_SERVE_FEEDS=1 \
    YT_REFRESH_INTERVAL=1800

WORKDIR /app

COPY generate.py api.py channels.json ./
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /data/cache /data/feeds \
    && useradd --create-home --uid 10001 app \
    && chown -R app:app /app /data

USER app
EXPOSE 8000
VOLUME ["/data"]

# /api/health is public on purpose, so platform health checks need no token.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % (os.environ.get('YT_API_PORT') or os.environ.get('PORT') or '8000'), timeout=4)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python3", "api.py"]
