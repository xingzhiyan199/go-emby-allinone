#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenList -> STRM 生成器
遍历 OpenList 某个（或多个）挂载目录，为每个视频文件生成 .strm（内含带 sign 的 302 直链）。

环境变量:
  OL_URL     OpenList 地址        (默认 https://openlist.go.altare.global)
  OL_USER    账号                 (默认 admin)
  OL_PASS    密码                 (必填)
  STRM_SRC   OpenList 内的源路径  (默认 /115)
             支持多个，逗号分隔，例如: /移动/探花合集,/移动/2026精点
  STRM_OUT   输出目录             (默认 /data/strm)
  STRM_FORCE 1=覆盖已存在文件     (默认 0，增量模式：内容相同则跳过)
  STRM_PRUNE 1=删除本轮未出现的旧 .strm 及空目录 (默认 1)
             仅当所有源都成功遍历（无任何列目录失败）时才会执行，避免限流误删
  STRM_DELAY 每次请求之间的延时秒数 (默认 0.2)
  STRM_PAGE  分页大小             (默认 200)
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

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

VIDEO_EXT = {
    ".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".flv",
    ".webm", ".rmvb", ".iso", ".m4v", ".mpg", ".mpeg", ".3gp",
    ".asf", ".f4v", ".vob", ".rm", ".divx", ".ogv",
}

STAT = {"dirs": 0, "files": 0, "strm": 0, "skip": 0, "fast": 0, "err": 0, "pruned": 0, "rmdir": 0}
EXPECTED = set()     # 本轮应当存在的 .strm 绝对路径
BAD_DIRS = set()     # 列目录失败的子树，清理时整体跳过
SRC_BAD = 0          # 源目录本身不可用的次数


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def call(path, body=None, token=None, tries=3):
    url = BASE + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    last = None
    for n in range(tries):
        try:
            req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
            req.add_header("User-Agent", UA)
            req.add_header("Accept", "application/json, text/plain, */*")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            if token:
                req.add_header("Authorization", token)
            with urllib.request.urlopen(req, timeout=60) as r:
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
                time.sleep(5 * (n + 1))
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


def build_url(path, sign):
    u = BASE + "/d" + urllib.parse.quote(path)
    if sign:
        u += "?sign=" + urllib.parse.quote(sign, safe="")
    return u


def url_path_of(dst):
    """读出已有 strm 里的源路径（解码后），用于判断是否需要重新取 sign。"""
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
    """strm 已存在且指向同一源路径 → 说明 sign 未变（OpenList 的 sign 带 :0 表示不过期）。"""
    if not os.path.exists(dst):
        return False
    return url_path_of(dst) == path


def write_strm(out_dir, name, url):
    stem = os.path.splitext(name)[0]
    dst = os.path.join(out_dir, stem + ".strm")
    EXPECTED.add(os.path.normpath(dst))
    if not FORCE and os.path.exists(dst):
        try:
            with open(dst, "r", encoding="utf-8") as f:
                if f.read().strip() == url:
                    STAT["skip"] += 1
                    return
        except Exception:
            pass
    os.makedirs(out_dir, exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(url)
    STAT["strm"] += 1


def walk(token, path, out_dir, top=False):
    global SRC_BAD
    page = 1
    while True:
        d = call("/api/fs/list", {
            "path": path, "page": page, "per_page": PAGE_SIZE,
            "refresh": False, "password": "",
        }, token)
        if d.get("code") != 200:
            log("  ! 列目录失败", path, d.get("message"))
            STAT["err"] += 1
            if top:
                SRC_BAD += 1
            BAD_DIRS.add(os.path.normpath(out_dir))
            return
        data = d.get("data") or {}
        items = data.get("content") or []
        total = data.get("total") or 0
        for it in items:
            name = it.get("name") or ""
            child = path.rstrip("/") + "/" + name
            if it.get("is_dir"):
                STAT["dirs"] += 1
                walk(token, child, os.path.join(out_dir, name))
            else:
                ext = os.path.splitext(name)[1].lower()
                if ext not in VIDEO_EXT:
                    continue
                STAT["files"] += 1
                dst = os.path.join(out_dir, os.path.splitext(name)[0] + ".strm")
                # 快速路径：本地 strm 已指向同一源路径 → sign 未变，直接跳过，省一次 fs/get
                if not FORCE and sign_still_valid(dst, child):
                    EXPECTED.add(os.path.normpath(dst))
                    STAT["fast"] += 1
                    STAT["skip"] += 1
                else:
                    try:
                        g = call("/api/fs/get", {"path": child, "password": ""}, token)
                        sign = ((g.get("data") or {}).get("sign") or "").strip()
                        write_strm(out_dir, name, build_url(child, sign))
                    except Exception as e:
                        STAT["err"] += 1
                        log("  ! 失败", name, e)
                        # 取 sign 失败时保留旧 strm，避免清理阶段把它误删
                        if os.path.exists(dst):
                            EXPECTED.add(os.path.normpath(dst))
                if STAT["files"] % 50 == 0:
                    log("  进度: %d 文件 / %d 目录, 生成 %d, 跳过 %d(其中免查 %d)" % (
                        STAT["files"], STAT["dirs"], STAT["strm"], STAT["skip"], STAT["fast"]))
        if page * PAGE_SIZE >= total or not items:
            return
        page += 1


def _under_bad(p):
    for b in BAD_DIRS:
        if p == b or p.startswith(b + os.sep):
            return True
    return False


def prune(out):
    """删除本轮未出现的 .strm 文件，以及因此变空的目录。"""
    out = os.path.normpath(out)
    for root, dirs, files in os.walk(out, topdown=False):
        root_n = os.path.normpath(root)
        for f in files:
            if not f.lower().endswith(".strm"):
                continue
            p = os.path.normpath(os.path.join(root, f))
            if p in EXPECTED:
                continue
            if _under_bad(p):
                continue
            try:
                os.remove(p)
                STAT["pruned"] += 1
            except Exception as e:
                log("  ! 删除失败", p, e)
        if root_n == out:
            continue
        if _under_bad(root_n):
            continue
        try:
            if not os.listdir(root_n):
                os.rmdir(root_n)
                STAT["rmdir"] += 1
        except Exception:
            pass


def main():
    if not PASS:
        raise SystemExit("缺少 OL_PASS")
    log("=== STRM 生成开始 ===")
    log("源: %s  ->  输出: %s  (force=%s, prune=%s)" % (", ".join(SRCS), OUT, FORCE, PRUNE))
    token = login()
    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()
    for i, src in enumerate(SRCS):
        if i:
            log("--- 切换到下一个源: %s" % src)
        walk(token, src, OUT, top=True)
    log("=== 完成 ===")
    log("目录 %d | 视频文件 %d | 生成 %d | 跳过 %d (免查 %d) | 失败 %d | 耗时 %.1fs" % (
        STAT["dirs"], STAT["files"], STAT["strm"], STAT["skip"], STAT["fast"],
        STAT["err"], time.time() - t0))

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
