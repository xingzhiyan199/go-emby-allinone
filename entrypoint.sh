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
# 上次被平台强杀（SIGKILL）会残留 postmaster.pid，导致 pg_ctl 误判"已在运行"或起不来。
if [ -f "$PGDATA/postmaster.pid" ]; then
  if pg_isready -h 127.0.0.1 -p "$PGPORT" -q 2>/dev/null; then
    log "postgres 已在运行，跳过启动"
  else
    log "清理残留 postmaster.pid"
    rm -f "$PGDATA/postmaster.pid"
  fi
fi
# 崩溃恢复时 PG 要做全量 fsync，节点存储慢时单次可达 60s 以上。
# 原来 -t 60 会在恢复完成前超时，entrypoint 据此 exit 1，容器被平台判为启动失败，
# 表现为「部署成功但一直 502」。这里放宽到 300s 并重试两次。
PG_OK=0
for attempt in 1 2; do
  if gosu postgres pg_ctl -D "$PGDATA" \
        -o "-c listen_addresses=127.0.0.1 -p $PGPORT -c max_connections=30 -c shared_buffers=128MB -c work_mem=4MB -c maintenance_work_mem=64MB" \
        -l /data/pg.log -w -t 300 start; then
    PG_OK=1; break
  fi
  log "!! postgres 第 $attempt 次启动未成功，8s 后重试"
  tail -20 /data/pg.log
  rm -f "$PGDATA/postmaster.pid"
  sleep 8
done
if [ "$PG_OK" != "1" ]; then
  log "!! postgres 启动失败，放弃"; tail -40 /data/pg.log; exit 1
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
# go-emby 的统计任务会 SET temp_file_limit 等 PG 参数，普通角色没有权限会报
# permission denied to set parameter，导致媒体库大小/统计一直为空。给业务角色超级用户。
psql -h 127.0.0.1 -p "$PGPORT" -U postgres -q \
  -c "ALTER ROLE $DB_USER SUPERUSER" 2>/dev/null || log "!! 提升 $DB_USER 权限失败（可忽略）"

# ---------- 3.5 重置管理员密码（可选，幂等） ----------
# 用法：-e ADMIN_PASSWORD=明文 -e ADMIN_PASSWORD_HASH='$2a$12$...'
# 只在 users 表已存在且能匹配到 admin 时生效；首次启动由 go-emby 自己用 ADMIN_PASSWORD 建号。
if [ -n "${ADMIN_PASSWORD_HASH:-}" ]; then
  if psql -h 127.0.0.1 -p "$PGPORT" -U postgres -d "$DB_NAME" -tAc \
        "SELECT 1 FROM information_schema.tables WHERE table_name='users'" 2>/dev/null | grep -q 1; then
    N=$(psql -h 127.0.0.1 -p "$PGPORT" -U postgres -d "$DB_NAME" -tAc \
        "SELECT count(*) FROM users" 2>/dev/null | tr -d ' ')
    if [ "${N:-0}" -gt 0 ]; then
      ERR=$(psql -h 127.0.0.1 -p "$PGPORT" -U postgres -d "$DB_NAME" -q -v ON_ERROR_STOP=1 \
        -c "UPDATE users SET hash='${ADMIN_PASSWORD_HASH}' WHERE name='admin'" 2>&1)
      if [ $? -eq 0 ]; then
        log "已重置 admin 密码（users=$N）"
      else
        log "!! 重置 admin 密码失败: $ERR"
      fi
    else
      log "users 表为空，交由 go-emby 用 ADMIN_PASSWORD 建号"
    fi
  else
    log "users 表尚未创建，跳过密码重置"
  fi
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
  log "启动 STRM 同步（源 ${STRM_SRC:-AUTO} → $MEDIA_DIR/strm）"
  (
    FAIL=0
    while true; do
      START=$(date +%s)
      STRM_OUT="$MEDIA_DIR/strm" /usr/local/bin/gen_strm.py 2>&1 | sed -u 's/^/[strm] /'
      RC=${PIPESTATUS[0]}
      RAN=$(( $(date +%s) - START ))
      # 跑太短又报错 → 判定为异常，退避，避免疯狂刷日志/打爆 API
      if [ "$RC" -ne 0 ] && [ "$RAN" -lt 60 ]; then
        FAIL=$((FAIL + 1))
        WAIT=$(( FAIL > 5 ? 300 : 30 * FAIL ))
        log "STRM 本轮异常退出 rc=$RC 用时 ${RAN}s，第 ${FAIL} 次，${WAIT}s 后重试"
      else
        FAIL=0
        WAIT="$STRM_INTERVAL"
        log "本轮 STRM 结束 rc=$RC，${WAIT}s 后再跑"
      fi
      sleep "$WAIT"
    done
  ) &
  STRM_PID=$!
else
  log "STRM 同步未启用（STRM_SYNC=$STRM_SYNC）"
  STRM_PID=""
fi

# ---------- 6. 启动 go-emby ----------
export DATABASE_URL="postgres://$DB_USER:$DB_PASS@127.0.0.1:$PGPORT/$DB_NAME?sslmode=disable"
export LICENSE_KEY="${LICENSE_KEY:-}"
export LICENSE_SERVER_URL="${LICENSE_SERVER_URL:-https://tl.macacaaca.top}"
export MEDIA_INFO_ROOT="$APP_DATA"
# go-emby 遍历媒体目录时不跟随软链，必须给它真实路径 /data/media，
# /media 只作为兼容用的软链保留，不能作为 MEDIA_ROOTS（否则扫库结果为 0）。
export FILE_MANAGER_ROOT="${MEDIA_DIR}"
export MEDIA_ROOTS="${MEDIA_ROOTS:-$MEDIA_DIR}"
export ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
# 让 Go 运行时对内存更保守，降低被平台 cgroup OOM 整容器杀掉（表现为 502）的概率
export GOMEMLIMIT="${GOMEMLIMIT:-1600MiB}"
export GOGC="${GOGC:-50}"

CRASH=0
RESTART_MIN="${RESTART_MIN:-5}"

shutdown() {
  log "收到停止信号，关闭服务"
  trap - TERM INT
  [ -n "${APP_PID:-}" ] && kill -TERM "$APP_PID" 2>/dev/null || true
  [ -n "${PG_GUARD_PID:-}" ] && kill -TERM "$PG_GUARD_PID" 2>/dev/null || true
  [ -n "${STRM_PID:-}" ] && kill -TERM "$STRM_PID" 2>/dev/null || true
  sleep 2
  gosu postgres pg_ctl -D "$PGDATA" -m fast stop >/dev/null 2>&1 || true
  exit 0
}
trap shutdown TERM INT

# ---------- 7. PostgreSQL 守护 ----------
(
  while true; do
    sleep 15
    if ! pg_isready -h 127.0.0.1 -p "$PGPORT" -q 2>/dev/null; then
      log "!! postgres 无响应，尝试原地拉起"
      rm -f "$PGDATA/postmaster.pid"
      gosu postgres pg_ctl -D "$PGDATA" -m immediate -l /data/pg.log -w -t 300 restart >/dev/null 2>&1 \
        || gosu postgres pg_ctl -D "$PGDATA" -l /data/pg.log -w -t 300 start >/dev/null 2>&1 \
        || log "!! postgres 拉起失败"
      # 让 go-emby 断开重连，避免连接池全是死连接
      [ -n "${APP_PID:-}" ] && kill -TERM "$APP_PID" 2>/dev/null || true
    fi
  done
) &
PG_GUARD_PID=$!

# ---------- 8. go-emby 守护循环 ----------
# 进程自己崩了就地拉起（几秒），不必等平台重启整个容器（60~90s + 重扫库）
while true; do
  START_TS=$(date +%s)
  log "启动 go-emby（license=$([ -n "${LICENSE_KEY:-}" ] && echo 'key set' || echo 'free mode')）"
  /usr/local/bin/go-emby &
  APP_PID=$!
  wait "$APP_PID"
  CODE=$?
  UP=$(( $(date +%s) - START_TS ))
  log "go-emby 退出 code=$CODE 存活 ${UP}s"

  # 存活不足 60 秒视为崩溃；连续 8 次说明不是偶发，交给平台整容器重启
  if [ "$UP" -lt 60 ]; then
    CRASH=$((CRASH + 1))
  else
    CRASH=0
  fi
  if [ "$CRASH" -ge 8 ]; then
    log "!! go-emby 连续 ${CRASH} 次快速崩溃，交还平台重启容器"
    gosu postgres pg_ctl -D "$PGDATA" -m fast stop >/dev/null 2>&1 || true
    exit 1
  fi
  DELAY=$(( RESTART_MIN * (CRASH > 3 ? CRASH : 1) ))
  [ "$DELAY" -gt 60 ] && DELAY=60
  log "  ${DELAY}s 后重启 go-emby"
  sleep "$DELAY"
done
