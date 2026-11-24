#!/usr/bin/env bash
# 单容器内同时运行 PostgreSQL 17 与 go-emby，并可选地在后台生成 STRM
# 所有持久化数据都放在 /data（Altare Docker App 的持久卷挂载点）
set -uo pipefail

log() { echo "[entry $(date '+%H:%M:%S')] $*"; }

PGDATA="${PGDATA:-/data/pg}"
APP_DATA="${APP_DATA:-/data/app}"
MEDIA_DIR="${MEDIA_DIR:-/data/media}"
DB_NAME="${DB_NAME:-emby}"
DB_USER="${DB_USER:-emby}"
DB_PASS="${DB_PASS:-emby_local_pw}"
PGPORT="${PGPORT:-5432}"
STRM_SYNC="${STRM_SYNC:-1}"
STRM_INTERVAL="${STRM_INTERVAL:-21600}"

# 固定路径映射到持久卷
mkdir -p "$PGDATA" "$APP_DATA" /data/backups "$MEDIA_DIR/strm" /run/bootstrap
ln -sfn "$MEDIA_DIR" /media
ln -sfn /data/backups /app/backups
chown -R postgres:postgres "$PGDATA" 2>/dev/null || true

# ---------- 1. 初始化数据库集群 ----------
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  log "首次启动，initdb ..."
  if ! gosu postgres initdb -D "$PGDATA" -U postgres \
        --auth-local=trust --auth-host=trust >/data/initdb.log 2>&1; then
    log "!! initdb 失败"; tail -20 /data/initdb.log; exit 1
  fi
  log "initdb 完成"
else
  log "复用已有数据目录 $PGDATA"
fi

# ---------- 2. 启动 PostgreSQL ----------
log "启动 postgres (port=$PGPORT)"
if ! gosu postgres pg_ctl -D "$PGDATA" \
      -o "-c listen_addresses=127.0.0.1 -p $PGPORT -c max_connections=80 -c shared_buffers=192MB -c work_mem=8MB" \
      -l /data/pg.log -w -t 60 start; then
  log "!! postgres 启动失败"; tail -30 /data/pg.log; exit 1
fi

# ---------- 3. 确保角色与数据库存在 ----------
if ! psql -h 127.0.0.1 -p "$PGPORT" -U postgres -tAc \
      "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
  log "创建角色 $DB_USER"
  psql -h 127.0.0.1 -p "$PGPORT" -U postgres -q \
    -c "CREATE ROLE $DB_USER LOGIN PASSWORD '$DB_PASS'" || log "!! 创建角色失败"
fi
if ! psql -h 127.0.0.1 -p "$PGPORT" -U postgres -tAc \
      "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
  log "创建数据库 $DB_NAME"
  psql -h 127.0.0.1 -p "$PGPORT" -U postgres -q \
    -c "CREATE DATABASE $DB_NAME OWNER $DB_USER" || log "!! 创建数据库失败"
fi

# ---------- 4. 生成并持久化授权机器标识 ----------
if [ ! -s /data/machine-id ]; then
  head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n' > /data/machine-id
  log "生成 machine-id: $(cat /data/machine-id)"
fi
cp /data/machine-id /run/license-machine-id
chmod 644 /run/license-machine-id

# ---------- 5. 后台生成 STRM（可选）----------
if [ "$STRM_SYNC" = "1" ] && [ -n "${OL_PASS:-}" ]; then
  log "启动 STRM 同步（源 ${STRM_SRC:-/115} → $MEDIA_DIR/strm）"
  (
    while true; do
      STRM_OUT="$MEDIA_DIR/strm" /usr/local/bin/gen_strm.py 2>&1 | sed -u 's/^/[strm] /'
      log "本轮 STRM 结束，${STRM_INTERVAL}s 后再跑"
      sleep "$STRM_INTERVAL"
    done
  ) &
else
  log "STRM 同步未启用（STRM_SYNC=$STRM_SYNC）"
fi

# ---------- 6. 启动 go-emby ----------
export DATABASE_URL="postgres://$DB_USER:$DB_PASS@127.0.0.1:$PGPORT/$DB_NAME?sslmode=disable"
export LICENSE_KEY="${LICENSE_KEY:-}"
export LICENSE_SERVER_URL="${LICENSE_SERVER_URL:-https://tl.macacaaca.top}"
export MEDIA_INFO_ROOT="$APP_DATA"
export FILE_MANAGER_ROOT="/media"
export MEDIA_ROOTS="/media"
export ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"

log "启动 go-emby（license=$([ -n "${LICENSE_KEY:-}" ] && echo 'key set' || echo 'free mode')）"
/app/go-emby &
APP_PID=$!

shutdown() {
  log "收到停止信号，关闭服务"
  kill -TERM "$APP_PID" 2>/dev/null || true
  sleep 2
  gosu postgres pg_ctl -D "$PGDATA" -m fast stop >/dev/null 2>&1 || true
  exit 0
}
trap shutdown TERM INT

wait "$APP_PID"
CODE=$?
log "go-emby 退出，code=$CODE"
gosu postgres pg_ctl -D "$PGDATA" -m fast stop >/dev/null 2>&1 || true
exit "$CODE"
