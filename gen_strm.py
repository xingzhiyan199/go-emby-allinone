#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenList -> STRM 生成器
遍历 OpenList 某个挂载目录，为每个视频文件生成 .strm（内含带 sign 的 302 直链）。

环境变量:
  OL_URL     OpenList 地址        (默认 https://openlist.go.altare.global)
  OL_USER    账号                 (默认 admin)
  OL_PASS    密码                 (必填)
  STRM_SRC   OpenList 内的源路径  (默认 /115)
  STRM_OUT   输出目录             (默认 /data/strm)
  STRM_FORCE 1=覆盖已存在文件     (默认 0，增量模式)
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
SRC = (os.environ.get("STRM_SRC", "/115") or "/115").rstrip("/")
OUT = os.environ.get("STRM_OUT", "/data/strm")
FORCE = os.environ.get("STRM_FORCE", "0") == "1"
PAGE_SIZE = int(os.environ.get("STRM_PAGE", "200"))

VIDEO_EXT = {
    ".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".flv",
    ".webm", ".rmvb", ".iso", ".m4v", ".mpg", ".mpeg", ".3gp",
    ".asf", ".f4v", ".vob", ".rm", ".divx", ".ogv",
}

STAT = {"dirs": 0, "files": 0, "strm": 0, "skip": 0, "err": 0}


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


def write_strm(out_dir, name, url):
    stem = os.path.splitext(name)[0]
    dst = os.path.join(out_dir, stem + ".strm")
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


def walk(token, path, out_dir):
    page = 1
    while True:
        d = call("/api/fs/list", {
            "path": path, "page": page, "per_page": PAGE_SIZE,
            "refresh": False, "password": "",
        }, token)
        if d.get("code") != 200:
            log("  ! 列目录失败", path, d.get("message"))
            STAT["err"] += 1
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
                try:
                    g = call("/api/fs/get", {"path": child, "password": ""}, token)
                    sign = ((g.get("data") or {}).get("sign") or "").strip()
                    write_strm(out_dir, name, build_url(child, sign))
                except Exception as e:
                    STAT["err"] += 1
                    log("  ! 失败", name, e)
                if STAT["files"] % 50 == 0:
                    log("  进度: %d 文件 / %d 目录, 生成 %d, 跳过 %d" % (
                        STAT["files"], STAT["dirs"], STAT["strm"], STAT["skip"]))
        if page * PAGE_SIZE >= total or not items:
            return
        page += 1


def main():
    if not PASS:
        raise SystemExit("缺少 OL_PASS")
    log("=== STRM 生成开始 ===")
    log("源: %s  ->  输出: %s  (force=%s)" % (SRC, OUT, FORCE))
    token = login()
    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()
    walk(token, SRC, OUT)
    log("=== 完成 ===")
    log("目录 %d | 视频文件 %d | 生成 %d | 跳过 %d | 失败 %d | 耗时 %.1fs" % (
        STAT["dirs"], STAT["files"], STAT["strm"], STAT["skip"],
        STAT["err"], time.time() - t0))


if __name__ == "__main__":
    main()
