FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/0denko/gphotos-to-synology" \
      org.opencontainers.image.description="Unattended background migration of Google Photos Takeout archives to Synology DSM / Synology Photos" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Install ExifTool (metadata repair), gosu (Synology PUID/PGID), and tzdata
RUN apt-get update && apt-get install -y --no-install-recommends \
    libimage-exiftool-perl \
    gosu \
    tzdata \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8765/api/health || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "gphotos_synology.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765"]
