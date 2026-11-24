# go-emby-allinone

把 **go-emby**（Emby 兼容的 STRM 播放服务端）和 **PostgreSQL 17** 打包进**同一个容器**，用于 Altare Docker App 这类「应用之间不共享磁盘、也没有托管 PG 可用」的环境。

## 为什么需要这个镜像

- go-emby 强依赖 PostgreSQL（源码里只有 PG 驱动，没有 SQLite）
- Altare 免费版托管 Postgres 上限是 256 MB 内存 / 1 GB 磁盘，而实际库有 1.4 GB，装不下
- Altare 的 Docker App 之间不共享磁盘，两个容器没法互相挂载数据目录

所以只能把 PG 塞进 go-emby 的同一个容器里，数据统一落在 `/data`（Altare 的持久卷）。

## 容器内含

| 组件 | 说明 |
|---|---|
| PostgreSQL 17 | 内嵌运行，`PGDATA=/data/pg`，`127.0.0.1:5432` |
| go-emby | 官方镜像的二进制，监听 `:8097`，健康检查 `/health` |
| gen_strm.py | 后台定时从 OpenList 生成 `.strm` 到 `/data/media/strm` |
| ffmpeg | go-emby 探测媒体信息需要 |

## 持久化

全部落在 `/data`，容器重建不丢：

```
/data/pg       PostgreSQL 数据
/data/app      go-emby 的 instance-id、初始管理员密码
/data/media    STRM 文件（/media 是它的软链接）
/data/backups  go-emby 的数据库备份
/data/machine-id  授权用的机器标识（32 hex，仅生成一次）
```

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ADMIN_PASSWORD` | 空 | go-emby 管理员密码；留空则自动生成并写入 `/data/app/initial-admin-password` |
| `OL_URL` | `https://openlist.go.altare.global` | OpenList 地址 |
| `OL_USER` / `OL_PASS` | `admin` / 空 | OpenList 账号，`OL_PASS` 为空则不生成 STRM |
| `STRM_SRC` | `/115` | OpenList 内的源目录 |
| `STRM_SYNC` | `1` | 是否启用后台 STRM 同步 |
| `STRM_INTERVAL` | `21600` | 两轮同步之间的间隔（秒） |
| `STRM_DELAY` | `0.2` | 每次 API 调用后的延时，用于规避网盘限流 |
| `LICENSE_KEY` | 空 | 留空时走授权服务的 free 模式 |
| `DB_NAME` / `DB_USER` / `DB_PASS` | `emby` / `emby` / `emby_local_pw` | 内置 PG 的库名与账号，仅监听回环 |

## 部署（Altare）

```bash
altr docker create go-emby ghcr.io/xingzhiyan199/go-emby-allinone:latest \
  --memory 3072 --disk 20480 --cpu 60 --port 8097 \
  -e ADMIN_PASSWORD='你的密码' \
  -e OL_PASS='OpenList密码' \
  -e STRM_SRC=/115 \
  --location <region> \
  --token "$ALTARE_TOKEN"
```

## 构建

推送到 `main` 即触发 GitHub Actions 构建并推送到 GHCR：

```
ghcr.io/xingzhiyan199/go-emby-allinone:latest
```
