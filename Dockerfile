# go-emby + PostgreSQL 17 单容器一体镜像
# 用于 Altare Docker App（应用之间不共享磁盘，托管 PG 免费版只有 256MB/1GB，装不下）
FROM ghcr.io/sd87671067/go-emby:latest AS src

FROM postgres:17-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai \
    PGDATA=/data/pg \
    LISTEN=:8097 \
    MEDIA_INFO_ROOT=/app/data \
    FILE_MANAGER_ROOT=/media \
    MEDIA_ROOTS=/media

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates wget ffmpeg python3 tzdata libvips42 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=src /usr/local/bin/go-emby /usr/local/bin/go-emby
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY gen_strm.py /usr/local/bin/gen_strm.py

RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/gen_strm.py /usr/local/bin/go-emby \
    && mkdir -p /app/data /app/backups /media /data/pg /data/media/strm /data/app /data/backups /run/bootstrap \
    && chown -R postgres:postgres /data/pg

EXPOSE 8097

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=6 \
    CMD wget -q -O /dev/null http://127.0.0.1:8097/health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
