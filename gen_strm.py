#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenList -> STRM 生成器（并发版）
遍历 OpenList 的挂载目录，为每个视频文件生成 .strm（内含带 sign 的 302 直链）。

环境变量:
  OL_URL      OpenList 地址        (默认 https://openlist.go.altare.global)
  OL_USER     账号                 (默认 admin)
  OL_PASS     密码                 (必填)
  STRM_SRC    OpenList 内的源路径  (默认 /115)
              支持多个，逗号分隔，例如: /移动/探花合集,/移动/2026精点
              设为 AUTO 则自动使用 OpenList 里所有状态正常的挂载点（改挂载名不用改配置）
  STRM_OUT    输出目录             (默认 /data/strm)
  STRM_FORCE  1=覆盖已存在文件     (默认 0，增量模式)
  STRM_PRUNE  1=删除本轮未出现的旧 .strm 及空目录 (默认 1)
              源不可用/子树列目录失败时自动跳过，不会误删
  STRM_WORKERS 并发线程数           (默认 6)
  STRM_DELAY  每次请求之间的延时秒数 (默认 0.2)
  STRM_PAGE   分页大小             (默认 200)
  STRM_EXCLUDE 跳过不扫的目录，逗号分隔的前缀，例如 /3998/手机图片,/3998/同步盘
  STRM_TIMEOUT 单次请求超时秒数     (默认 25)
  STRM_TRIES  单个请求最多尝试次数  (默认 2)
"""
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, as_completed

BASE = os.environ.get("OL_URL", "https://openlist.go.altare.global").rstrip("/")
USER = os.environ.get("OL_USER", "admin")
PASS = os.environ.get("OL_PASS", "")
UA = os.environ.get("OL_UA", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
DELAY = float(os.environ.get("STRM_DELAY", "0.2"))
SRC_RAW = os.environ.get("STRM_SRC", "/115") or "/115"
SRCS = [s.strip().rstrip("/") or "/" for s in SRC_RAW.split(",") if s.strip()]
OUT = os.environ.get("STRM_OUT", "/data/strm")
FORCE = os.environ.get("STRM_FORCE", "0") == "1"
PRUNE = os.environ.get("STRM_PRUNE", "1") == "1"
PAGE_SIZE = int(os.environ.get("STRM_PAGE", "200"))
WORKERS = max(1, int(os.environ.get("STRM_WORKERS", "6")))
TIMEOUT = float(os.environ.get("STRM_TIMEOUT", "25"))
TRIES = max(1, int(os.environ.get("STRM_TRIES", "2")))
EXCLUDE = [e.strip().rstrip("/") for e in
           (os.environ.get("STRM_EXCLUDE", "") or "").split(",") if e.strip()]


def excluded(path):
    """路径命中排除前缀则返回 True（按路径段匹配，避免 /a/b 误伤 /a/bc）"""
    p = path.rstrip("/")
    for e in EXCLUDE:
        if p == e or p.startswith(e + "/"):
            return True
    return False

VIDEO_EXT = {
    ".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".flv",
    ".webm", ".rmvb", ".iso", ".m4v", ".mpg", ".mpeg", ".3gp",
    ".asf", ".f4v", ".vob", ".rm", ".divx", ".ogv",
}

STAT = {"dirs": 0, "files": 0, "total": 0, "strm": 0, "skip": 0, "fast": 0,
        "err": 0, "pruned": 0, "rmdir": 0, "excl": 0}
EXPECTED = set()
BAD_DIRS = set()
SRC_BAD = 0
LOCK = threading.RLock()   # 可重入：log() 会在已持锁的代码块里被调用


def log(*a):
    with LOCK:
        print(time.strftime("%H:%M:%S"), *a, flush=True)


def bump(key, n=1):
    with LOCK:
        STAT[key] = STAT.get(key, 0) + n


def call(path, body=None, token=None, tries=None):
    url = BASE + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    last = None
    for n in range(tries or TRIES):
        try:
            req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
            req.add_header("User-Agent", UA)
            req.add_header("Accept", "application/json, text/plain, */*")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            if token:
                req.add_header("Authorization", token)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                out = json.loads(r.read().decode("utf-8", "replace"))
            if DELAY > 0:
                time.sleep(DELAY)
            return out
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:120]
            except Exception:
                detail = ""
            last = "HTTP %s %s %s" % (e.code, e.reason, detail)
            if e.code in (403, 429):
                time.sleep(3 * (n + 1))
                continue
        except Exception as e:
            last = "%s %s" % (type(e).__name__, e)
        time.sleep(DELAY * (n + 1))
    raise RuntimeError("%s -> %s" % (path, last))


def login():
    d = call("/api/auth/login", {"username": USER, "password": PASS})
    if d.get("code") != 200:
        raise SystemExit("登录失败: %s" % d)
    log("登录成功:", USER)
    return d["data"]["token"]


def resolve_sources(token):
    """STRM_SRC=AUTO 时，自动取 OpenList 里所有状态正常的挂载点作为源。"""
    if SRC_RAW.strip().upper() != "AUTO":
        return SRCS
    d = call("/api/admin/storage/list?page=1&per_page=100", None, token)
    if d.get("code") != 200:
        log("!! 读取挂载列表失败: %s" % d.get("message"))
        return []
    found = []
    for s in ((d.get("data") or {}).get("content") or []):
        mp = s.get("mount_path") or ""
        st = s.get("status") or ""
        log("  发现挂载: %s (%s, status=%s)" % (mp, s.get("driver"), st))
        if mp and st == "work":
            found.append(mp.rstrip("/") or "/")
    if not found:
        log("!! 没有可用的挂载点")
    return found


def build_url(path, sign):
    u = BASE + "/d" + urllib.parse.quote(path)
    if sign:
        u += "?sign=" + urllib.parse.quote(sign, safe="")
    return u


def url_path_of(dst):
    try:
        with open(dst, "r", encoding="utf-8") as f:
            c = f.read().strip()
    except Exception:
        return None
    i = c.find("/d/")
    if i < 0:
        return None
    rest = c[i + 2:]
    j = rest.find("?")
    if j >= 0:
        rest = rest[:j]
    try:
        return urllib.parse.unquote(rest)
    except Exception:
        return rest


def sign_still_valid(dst, path):
    """strm 已存在且指向同一源路径 → sign 未变（OpenList sign 带 :0 表示不过期）。"""
    if not os.path.exists(dst):
        return False
    return url_path_of(dst) == path


def list_all(token, path):
    """列出一个目录的全部条目，自动翻页。返回 (items, err)"""
    page = 1
    items = []
    while True:
        try:
            d = call("/api/fs/list", {
                "path": path, "page": page, "per_page": PAGE_SIZE,
                "refresh": False, "password": "",
            }, token)
        except Exception as e:
            return None, str(e)
        if d.get("code") != 200:
            return None, d.get("message")
        data = d.get("data") or {}
        chunk = data.get("content") or []
        total = data.get("total") or 0
        items.extend(chunk)
        if page * PAGE_SIZE >= total or not chunk:
            return items, None
        page += 1


def crawl(token, src, out_root, pool):
    """并发遍历目录树，返回 [(child_path, out_stem_path)]"""
    global SRC_BAD
    files = []
    stack = [(src, out_root, True)]
    pending = {}
    scanned = 0
    skipped = 0
    while stack or pending:
        while stack:
            p, o, top = stack.pop()
            pending[pool.submit(list_all, token, p)] = (p, o, top)
        if not pending:
            break
        done, _ = wait(list(pending.keys()), return_when=FIRST_COMPLETED)
        for f in done:
            p, o, top = pending.pop(f)
            scanned += 1
            try:
                items, err = f.result()
            except Exception as e:
                items, err = None, str(e)
            if items is None:
                log("  ! 列目录失败", p, err)
                bump("err")
                if top:
                    with LOCK:
                        SRC_BAD += 1
                BAD_DIRS.add(os.path.normpath(o))
                continue
            for it in items:
                name = it.get("name") or ""
                if not name:
                    continue
                child = p.rstrip("/") + "/" + name
                if it.get("is_dir"):
                    if excluded(child):
                        skipped += 1
                        bump("excl")
                        continue
                    bump("dirs")
                    stack.append((child, os.path.join(o, name), False))
                else:
                    ext = os.path.splitext(name)[1].lower()
                    if ext in VIDEO_EXT:
                        files.append((child, os.path.join(o, os.path.splitext(name)[0])))
            # 爬树阶段原本完全静默，大库跑几十分钟看不到进展，这里每 100 个目录报一次
            if scanned % 100 == 0:
                log("  扫描中: 已列 %d 目录 | 发现 %d 视频 | 待列 %d | 已排除 %d"
                    % (scanned, len(files), len(pending) + len(stack), skipped))
    if skipped:
        log("  已按 STRM_EXCLUDE 跳过 %d 个目录" % skipped)
    return files


def emit_one(token, child, stem_path):
    dst = stem_path + ".strm"
    name = os.path.basename(stem_path)
    if not FORCE and sign_still_valid(dst, child):
        EXPECTED.add(os.path.normpath(dst))
        bump("fast")
        bump("skip")
        return
    try:
        g = call("/api/fs/get", {"path": child, "password": ""}, token)
        sign = ((g.get("data") or {}).get("sign") or "").strip()
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(build_url(child, sign))
        EXPECTED.add(os.path.normpath(dst))
        bump("strm")
    except Exception as e:
        bump("err")
        log("  ! 失败", name, e)
        # 取 sign 失败时保留旧 strm，避免清理阶段误删
        if os.path.exists(dst):
            EXPECTED.add(os.path.normpath(dst))


def emit_all(token, files, pool):
    with LOCK:
        STAT["total"] = len(files)
    log("开始取直链并写 strm，共 %d 个视频（并发 %d）" % (len(files), WORKERS))
    futs = [pool.submit(emit_one, token, c, s) for c, s in files]
    done_n = 0
    for _ in as_completed(futs):
        done_n += 1
        bump("files")
        if done_n % 200 == 0 or done_n == len(files):
            with LOCK:
                log("  进度: %d/%d 文件 | 生成 %d | 跳过 %d(免查 %d) | 失败 %d" % (
                    done_n, len(files), STAT["strm"], STAT["skip"], STAT["fast"], STAT["err"]))


def _under_bad(p):
    for b in BAD_DIRS:
        if p == b or p.startswith(b + os.sep):
            return True
    return False


def prune(out):
    out = os.path.normpath(out)
    for root, dirs, files in os.walk(out, topdown=False):
        root_n = os.path.normpath(root)
        for f in files:
            if not f.lower().endswith(".strm"):
                continue
            p = os.path.normpath(os.path.join(root, f))
            if p in EXPECTED or _under_bad(p):
                continue
            try:
                os.remove(p)
                bump("pruned")
            except Exception as e:
                log("  ! 删除失败", p, e)
        if root_n == out or _under_bad(root_n):
            continue
        try:
            if not os.listdir(root_n):
                os.rmdir(root_n)
                bump("rmdir")
        except Exception:
            pass


def main():
    if not PASS:
        raise SystemExit("缺少 OL_PASS")
    log("=== STRM 生成开始 ===")
    log("配置源: %s  ->  输出: %s  (force=%s, prune=%s, workers=%d)" % (
        SRC_RAW, OUT, FORCE, PRUNE, WORKERS))
    if EXCLUDE:
        log("排除目录: %s" % ", ".join(EXCLUDE))
    token = login()
    srcs = resolve_sources(token)
    if not srcs:
        log("!! 无可用源，退出")
        return
    if len(srcs) > 1 or srcs[0] != SRC_RAW.rstrip("/"):
        log("实际源: %s" % ", ".join(srcs))
    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()

    pool = ThreadPoolExecutor(max_workers=WORKERS)
    try:
        all_files = []
        for i, src in enumerate(srcs):
            if i:
                log("--- 切换到下一个源: %s" % src)
            part = crawl(token, src, OUT, pool)
            log("  源 %s 扫描完成: %d 个视频 / 累计 %d 目录" % (src, len(part), STAT["dirs"]))
            all_files.extend(part)
        emit_all(token, all_files, pool)
    finally:
        pool.shutdown(wait=True)

    log("=== 完成 ===")
    log("目录 %d | 视频文件 %d | 生成 %d | 跳过 %d (免查 %d) | 失败 %d | 排除 %d | 耗时 %.1fs" % (
        STAT["dirs"], STAT["files"], STAT["strm"], STAT["skip"], STAT["fast"],
        STAT["err"], STAT["excl"], time.time() - t0))

    if PRUNE:
        if SRC_BAD > 0:
            log("!! %d 个源目录不可用，为保证安全跳过清理（请检查 STRM_SRC）" % SRC_BAD)
        else:
            log("开始清理本轮未出现的旧 .strm …")
            prune(OUT)
            log("清理完成: 删除 %d 个 .strm，%d 个空目录" % (STAT["pruned"], STAT["rmdir"]))
            if STAT["err"]:
                log("   注：本轮有 %d 处失败，相关子树与文件已跳过清理" % STAT["err"])
    else:
        log("清理已关闭（STRM_PRUNE=0）")


if __name__ == "__main__":
    main()
