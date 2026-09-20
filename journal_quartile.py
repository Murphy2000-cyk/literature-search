#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JCR 分区查询（journalmetrics.org） v2.0

v2.0 的两处关键变更
-------------------
1. **改用 `searchts read`（只读）**，不再用 `searchts get`。
   旧实现用 `get`，它是**下载**命令、会在磁盘上落文件，违反本机
   「下载文件必须先经用户同意」的硬规则；同时也是「技能目录莫名出现 HTML 残留」的根源。
   `searchts read` 只把内容读进 stdout，不落盘、无需授权。

2. **改为解析返回 markdown 里的 `| Category | Quartile |` 表格**。
   旧实现靠网页 `<title>` 标签正则抓 `Q1`，页面已改版（现为 OpenAlex 口径的
   Journal Metrics Score + Category/Quartile 表），`<title>` 方案不再可靠。

分区取值规则
------------
表格可能有多行（期刊分属多个 JCR 类别）。本模块取**最好的一档**
（Q1 < Q2 < Q3 < Q4），即"该刊在至少一个类别里是 Q1 就算 Q1"，这是通行做法。
取不到表格时回退到页面里的 `Subfield Quartile`；两者都没有则判定为未收录。

用法
----
    python journal_quartile.py "Buildings" "Optical Fiber Technology"
    python journal_quartile.py --batch journals.txt     # 每行一个刊名
    python journal_quartile.py --detail "Buildings"     # 显示取值来源
"""
import os
import re
import sys
import json
import time
import subprocess

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          ".quartile_cache.json")
# 缓存版本号。解析方法一变就必须 +1，让旧值整体作废：
# v1 是纯字符串（靠网页 <title> 正则），v2 是 {"v":2,"q":...,"src":...}（解析 Category/Quartile 表）。
# 实测证据：v1 把 Buildings 记成 Q2，而实时页面明确是 Q1（两个类别都是 Q1）。
CACHE_VERSION = 2
INTERVAL = 1.5                      # 相邻 searchts 调用间隔（秒），对站点友好
_last_req = 0.0

# markdown 表格行：| Category | Q1 |
TBL_RE = re.compile(r'^\|\s*([^|\n]+?)\s*\|\s*(Q[1-4])\s*\|\s*$', re.M)
# 回退：正文里 "Q1\n\nSubfield Quartile"
SUB_RE = re.compile(r'\b(Q[1-4])\b\s*\n+\s*Subfield\s+Quartile', re.I)


def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_cache(c):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(c, f, indent=2, ensure_ascii=False, sort_keys=True)
    except Exception as e:
        print(f"[WARN] 缓存写入失败: {e}", file=sys.stderr)


def _wait():
    global _last_req
    gap = INTERVAL - (time.time() - _last_req)
    if gap > 0:
        time.sleep(gap)
    _last_req = time.time()


def slugify(name):
    """期刊名 → journalmetrics.org 的 URL slug。"""
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def parse_quartile(content):
    """从 markdown 提取分区。返回 (quartile|None, 来源说明)。"""
    if not content or not content.strip():
        return None, "空响应"
    if "Failed to read" in content or "无法读取" in content:
        return None, "页面读取失败"

    rows = TBL_RE.findall(content)
    if rows:
        qs = [q.upper() for _, q in rows]
        best = min(qs)                      # Q1 < Q2 < Q3 < Q4（字典序即分档序）
        cats = "; ".join(c.strip() for c, _ in rows)
        return best, f"Category/Quartile 表（{len(rows)} 行，取最好档）：{cats}"

    m = SUB_RE.search(content)
    if m:
        return m.group(1).upper(), "Subfield Quartile"

    return None, "页面中没有找到分区信息（可能该刊未被收录）"


def fetch_quartile(journal_name):
    """只读抓取并解析。返回 (quartile|None, 来源说明)。"""
    slug = slugify(journal_name)
    if not slug:
        return None, "刊名为空或无法生成 slug"
    url = f"https://www.journalmetrics.org/journal/{slug}"
    try:
        r = subprocess.run(["searchts", "read", url],
                           capture_output=True, encoding="utf-8",
                           errors="replace", timeout=120)
    except FileNotFoundError:
        return None, "找不到 searchts 命令"
    except subprocess.TimeoutExpired:
        return None, "searchts 超时"
    except Exception as e:
        return None, f"searchts 调用异常：{type(e).__name__}: {e}"

    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        tail = err[-1] if err else ""
        return None, f"searchts 退出码 {r.returncode}（该刊可能未被收录）{('：' + tail) if tail else ''}"
    return parse_quartile(r.stdout or "")


def lookup_detail(journal_name):
    """带缓存的查询，返回 (quartile|None, 来源说明)。"""
    cache = load_cache()
    key = (journal_name or "").strip().lower()
    entry = cache.get(key)
    if isinstance(entry, dict) and entry.get("v") == CACHE_VERSION:
        return entry.get("q"), "缓存：" + str(entry.get("src", ""))
    # 注意：v1 的纯字符串缓存一律视为失效（解析方法已改，旧值不可信），
    # 这里不返回、直接往下走重新抓取。

    _wait()
    q, src = fetch_quartile(journal_name)
    cache[key] = {"v": CACHE_VERSION, "q": q, "src": src}
    save_cache(cache)
    return q, src


def lookup(journal_name):
    """兼容旧调用方（enrich_quartiles 用）：只返回分区字符串或 None。"""
    return lookup_detail(journal_name)[0]


def lookup_many(names, verbose=True):
    """批量查询，返回 {原刊名: 分区或 None}。"""
    out = {}
    uniq = []
    for n in names:
        n = (n or "").strip()
        if n and n not in uniq:
            uniq.append(n)
    total = len(uniq)
    for i, n in enumerate(uniq, 1):
        q, src = lookup_detail(n)
        out[n] = q
        if verbose:
            print(f"  [{i}/{total}] {n} -> {q or 'unknown'}"
                  f"{('  (' + src + ')') if q else '  [' + src + ']'}",
                  file=sys.stderr)
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    detail = False
    args = sys.argv[1:]
    if args and args[0] == "--detail":
        detail = True
        args = args[1:]

    if args and args[0] == "--batch":
        with open(args[1], "r", encoding="utf-8") as f:
            journals = [ln.strip() for ln in f if ln.strip()]
    else:
        journals = args

    for j in journals:
        if detail:
            q, src = lookup_detail(j)
            print(f"{j}\t{q or 'unknown'}\t{src}")
        else:
            print(f"{j}\t{lookup(j) or 'unknown'}")
