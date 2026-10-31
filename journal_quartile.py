#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中科院《新锐期刊分区表》查询（LetPub） v3.0

为什么只做中科院分区、不做 JCR
------------------------------
JCR（Clarivate，基于 JIF 的四分位）**没有可用的免费数据源**：需要 Web of Science /
JCR 订阅，Clarivate API 需付费，LetPub 检索页也只给 IF、CiteScore、中科院区，不显示
JCR 四分位。第三方站点（journalmetrics.org 等）给出的是它们**自算**的四分位
（OpenAlex 口径），既不是 JCR 也不是 Scopus 官方 CiteScore 四分位——拿它当 JCR 汇报
会出错。因此本模块只提供中科院新锐分区（1–4 区），不提供任何代理指标。

v3.0 的三处变更（2026-10-30 实测驱动）
--------------------------------------
1. **删除 journalmetrics 分支**（原 Q1–Q4 那套，约 100 行）：口径不可靠，且与中科院
   分区不是一回事。连带删除 `.quartile_cache.json`。
2. **抓取优先 `--backend curl_cffi`**：实测 LetPub 页面用默认三级升级链要 **7.0~9.3 s**，
   强制 curl_cffi 只要 **2.6~3.6 s**（6/6 成功，返回数据还更全 15 KB vs 13 KB）。
   低于 2000 字符视为拿到空壳页，退回默认链重试。
3. **缓存带时间戳 + 负缓存 TTL**：旧实现把 `None` 永久写进缓存，导致主流刊被永久判成
   「未收录」（实测 215 本里 185 本中计，含 Materials、Applied Sciences、IEEE TIM、
   MSSP、IEEE TUFFC）。现在未收录项 30 天后自动重查，`--refresh` 可强制全部重查。

查询顺序
--------
① `searchissn=`（**精确匹配 ISSN 单元格，推荐**；有 ISSN 一律用这条）
② `searchname=`（模糊匹配，遇到缩写会漏，实测 `Comput. Aided Civ. Infrastructure Eng.`
   按刊名就查不到）

两条路径都走 `searchts read`：requests 直连会被 LetPub 挡回约 639 字节的壳页。

取值规则
--------
表格里 `N区` 是**恰好等于**该形式的单元格（不是从标题或正文正则抓，页面正文里还有
大量别的 `区`/`Q1` 字样）。返回 `"1"`~`"4"` 或 `None`。

用法
----
    python journal_quartile.py --issn 2075-5309 2352-7102
    python journal_quartile.py "Journal of Building Engineering"
    python journal_quartile.py --batch issns.txt          # 每行一个 ISSN 或刊名
    python journal_quartile.py --issn --refresh 0041-624X # 绕过缓存重查
"""
import os
import re
import sys
import json
import time
import subprocess
from urllib.parse import quote as _quote

# ---------------------------------------------------------------- 常量

LETPUB_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 ".letpub_zone_cache.json")
# 缓存版本号。结构或取值规则一变就必须 +1，让旧值整体作废。
# v1：{"v":1,"zone":...,"hit":...,"src":...}（无时间戳，None 会被永久复用）
# v2：增加 "ts"，未收录项可按时效重查
LETPUB_CACHE_VERSION = 2

# 未收录（zone is None）的缓存有效期。超过则重查——LetPub 新收录、或上次撞上空壳页。
NEGATIVE_TTL_DAYS = 30

LETPUB_SEARCH = "https://www.letpub.com.cn/index.php?page=journalapp&view=search"

INTERVAL = 1.5                      # 相邻 searchts 调用间隔（秒），对站点友好
_last_req = 0.0

_ZONE_CELL = re.compile(r"^([1-4])区$")
_LINK_NAME = re.compile(r"\[([^\]]+)\]\([^)]*journalid=\d+[^)]*\)")

# 判定"拿到的是空壳页而不是真页面"的字符数阈值。
# 实测：正常检索页 13~15 KB；空壳页约 1.2 KB。
MIN_PAGE_CHARS = 2000


# ---------------------------------------------------------------- 缓存

def load_letpub_cache():
    if os.path.exists(LETPUB_CACHE_FILE):
        try:
            with open(LETPUB_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_letpub_cache(c):
    try:
        with open(LETPUB_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(c, f, indent=2, ensure_ascii=False, sort_keys=True)
    except Exception as e:
        print(f"[WARN] 新锐分区缓存写入失败: {e}", file=sys.stderr)


def _wait():
    global _last_req
    gap = INTERVAL - (time.time() - _last_req)
    if gap > 0:
        time.sleep(gap)
    _last_req = time.time()


def _norm_journal(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


# ---------------------------------------------------------------- 抓取

def _read_url(url, backend=None, timeout=60):
    """调 searchts read 抓页面。

    backend="curl_cffi" 时只用最快的一档（浏览器 TLS 指纹），不升级到
    Jina relay / stealth 浏览器；实测 LetPub 用这一档就够，快 2~3 倍。
    """
    cmd = ["searchts", "read"]
    if backend:
        cmd += ["--backend", backend]
    cmd.append(url)
    return subprocess.run(cmd, capture_output=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def fetch_letpub_page(url, timeout=60):
    """抓 LetPub 检索页：先快档，失败或过薄再走默认三级升级链。

    Returns:
        (markdown 文本, 后端说明)
    """
    try:
        r = _read_url(url, backend="curl_cffi", timeout=timeout)
        if r.returncode == 0 and len((r.stdout or "")) >= MIN_PAGE_CHARS:
            return r.stdout, "curl_cffi"
    except Exception:
        pass                            # 快档失败不报错，直接退默认链

    try:
        r = _read_url(url, timeout=timeout)
    except FileNotFoundError:
        return "", "找不到 searchts 命令"
    except subprocess.TimeoutExpired:
        return "", "searchts 超时"
    except Exception as e:
        return "", f"searchts 异常：{type(e).__name__}"

    if r.returncode != 0:
        return "", f"searchts 退出码 {r.returncode}（该刊可能未被收录）"
    return r.stdout or "", "默认链"


# ---------------------------------------------------------------- 解析

def _parse_letpub(md, want_issn=None, want_name=None):
    """从 LetPub 检索页 markdown 里取分区。返回 (zone, 命中刊名) 或 None。

    行形态：| 2075-5309 | [Buildings](...journalid=11142...) BUILDINGS-BASEL | 8.0 |
            IF: 3.4 ... | 3区 | 大类：工程技术 小类：... | SCIE | Yes | ... |
    """
    fallback = None
    q = _norm_journal(want_name) if want_name else ""
    for line in md.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 6:
            continue
        m = _LINK_NAME.search(cells[1])
        if not m:
            continue
        zone = None
        for c in cells:
            z = _ZONE_CELL.match(c)
            if z:
                zone = z.group(1)
                break
        if not zone:
            continue
        if want_issn:
            if cells[0].replace("–", "-").upper() != want_issn.upper():
                continue
            return zone, m.group(1)
        n = _norm_journal(m.group(1))
        if n == q:
            return zone, m.group(1)
        if fallback is None and q and (n.startswith(q[:12]) or q.startswith(n[:12])):
            fallback = (zone, m.group(1))
    return fallback


# ---------------------------------------------------------------- 查询

def letpub_zone(issn=None, name=None, use_cache=True, refresh=False, timeout=60):
    """查中科院《新锐期刊分区表》。

    Args:
        issn: 期刊 ISSN（优先，精确匹配）
        name: 期刊名（ISSN 缺失时才用，缩写会漏）
        use_cache: False = 完全绕过缓存
        refresh: True = 忽略已有缓存重查（未收录项也会重查）
    Returns:
        (zone|None, 命中刊名|None, 来源说明)
    """
    if not issn and not name:
        return None, None, "既没给 ISSN 也没给刊名"

    key = ("issn:" + str(issn).upper()) if issn else ("name:" + _norm_journal(name))
    cache = load_letpub_cache()

    if use_cache and not refresh:
        e = cache.get(key)
        if isinstance(e, dict) and e.get("v") == LETPUB_CACHE_VERSION:
            # 有值 → 直接用；未收录 → 超过 TTL 才重查
            if e.get("zone") or (time.time() - float(e.get("ts") or 0)
                                 < NEGATIVE_TTL_DAYS * 86400):
                return e.get("zone"), e.get("hit"), "缓存"

    param, value = ("searchissn", issn) if issn else ("searchname", name)
    url = f"{LETPUB_SEARCH}&{param}={_quote(str(value))}&searchsort=relevance"

    # LetPub 会偶发返回空页（实测 CBM 一次通一次空、MSSP 连续两次空）。单次失败**不能**
    # 直接判"未收录"并写进缓存——那正是旧实现毒化缓存的根因。所以未取到时重试一次。
    zone = hn = None
    backend = ""
    for attempt in (1, 2):
        md, backend = fetch_letpub_page(url, timeout=timeout)
        if backend in ("找不到 searchts 命令", "searchts 超时") or backend.startswith("searchts"):
            return None, None, backend
        hit = _parse_letpub(md, want_issn=issn, want_name=name)
        zone, hn = (hit if hit else (None, None))
        if zone:
            break
        if attempt == 1:
            time.sleep(1.0)

    src = "ISSN 精确匹配" if (issn and zone) else ("刊名模糊匹配" if zone else "未查到")
    cache[key] = {"v": LETPUB_CACHE_VERSION, "zone": zone, "hit": hn,
                  "src": src, "backend": backend, "ts": int(time.time())}
    save_letpub_cache(cache)
    return zone, hn, src


def letpub_zone_many(items, verbose=True, refresh=False):
    """批量：items 为 [(issn, name), ...]。有 ISSN 就走 ISSN，否则退回刊名。

    ⚠️ **必须串行**：并发 >4 会被 LetPub 限流，表现为整批返回空结果。
    实测单本 2.6~3.6 s（curl_cffi 档），缓存命中瞬时。
    """
    out = {}
    total = len(items)
    for i, (issn, name) in enumerate(items, 1):
        zone, hn, src = letpub_zone(issn=issn, name=name, refresh=refresh)
        out[(issn, name)] = zone
        if verbose:
            tag = f"{issn or '-'} / {name or '-'}"
            print(f"  [{i}/{total}] {tag[:60]:<60} {zone or 'unknown'}  ({src})",
                  file=sys.stderr)
        _wait()
    return out


# ---------------------------------------------------------------- ISSN 解析
#
# LetPub 的精确匹配键是 **ISSN**，但元数据表里没有 ISSN 列（Semantic Scholar 不返回）。
# 所以先按刊名去重、每刊取一个 DOI，用 Crossref 反查 ISSN，结果缓存复用。
#
# ⚠️ 这段逻辑**只在这里维护一份**：`paper_table.enrich_zone_column()` 与
# `semantic_scholar_search.enrich_quartiles()` 都走 `zone_map_for_journals()`，
# 避免出现"一条路径按 ISSN、另一条按刊名"的不一致（v3.0 之前就是这样）。
ISSN_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               ".issn_cache.json")
CROSSREF_MAILTO = "dsh-litsearch@example.com"


def _crossref_issn(doi, timeout=25):
    """DOI → ISSN（Crossref）。失败返回 None。"""
    if not doi:
        return None
    try:
        import requests
        r = requests.get(f"https://api.crossref.org/works/{doi}",
                         params={"mailto": CROSSREF_MAILTO}, timeout=timeout,
                         headers={"User-Agent": f"DSH-litsearch/1.0 (mailto:{CROSSREF_MAILTO})"})
        if r.status_code != 200:
            return None
        issns = r.json().get("message", {}).get("ISSN") or []
        return issns[0] if issns else None
    except Exception:
        return None


def resolve_issns(name_to_doi, verbose=True):
    """刊名 → ISSN。优先读缓存，缺的用 Crossref 按 DOI 反查。

    Args:
        name_to_doi: {"期刊名": "DOI 或空串"}
    Returns:
        {"期刊名": "ISSN" 或 None}

    实测：82 篇 → 81 本刊 → 81 次请求（首次约 1 分钟），之后走缓存瞬时。
    """
    cache = {}
    if os.path.exists(ISSN_CACHE_FILE):
        try:
            with open(ISSN_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    todo = [(n, d) for n, d in name_to_doi.items() if not cache.get(n) and d]
    if todo and verbose:
        print(f"[ISSN] 缓存未命中 {len(todo)} 本，用 Crossref 按 DOI 反查…", flush=True)
    for i, (name, doi) in enumerate(todo, 1):
        issn = _crossref_issn(doi)
        cache[name] = issn or ""
        if verbose:
            print(f"  [{i}/{len(todo)}] {name[:50]:<50} {issn or 'unknown'}", flush=True)
        time.sleep(0.2)

    try:
        with open(ISSN_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False, sort_keys=True)
    except Exception:
        pass
    return {n: (cache.get(n) or None) for n in name_to_doi}


def zone_map_for_journals(name_to_doi, refresh=False, verbose=True):
    """刊名 → 中科院分区（"1"~"4" 或 None）。ISSN 优先，刊名兜底。

    这是**唯一**的分区批量入口，`paper_table` 与 `semantic_scholar_search` 都调它。
    """
    issn_map = resolve_issns(name_to_doi, verbose=verbose)
    items = [(issn_map.get(n), None if issn_map.get(n) else n) for n in name_to_doi]
    if verbose:
        got = sum(1 for v in issn_map.values() if v)
        print(f"[ZONE] ISSN 已解析 {got}/{len(name_to_doi)} 本；"
              f"开始查中科院分区（单本约 2.6~3.6 秒，串行，勿并发）…", flush=True)
    zones = letpub_zone_many(items, verbose=verbose, refresh=refresh)
    return {n: zones.get((issn_map.get(n), None if issn_map.get(n) else n))
            for n in name_to_doi}


# ---------------------------------------------------------------- CLI

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    as_issn = False
    refresh = False
    args = sys.argv[1:]
    # -h/--help 必须在这里拦掉：下面的循环会 break 出到"把参数当刊名"的分支，
    # 那样 `--help` 会被当成期刊名真的去联网查一次（v3.0 之前的行为）。
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        sys.exit(0)
    while args and args[0].startswith("--"):
        if args[0] == "--issn":
            as_issn = True
        elif args[0] == "--refresh":
            refresh = True
        elif args[0] == "--letpub":     # 兼容老命令行，语义已统一
            pass
        else:
            break
        args = args[1:]

    if args and args[0] == "--batch":
        with open(args[1], "r", encoding="utf-8") as f:
            journals = [ln.strip() for ln in f if ln.strip()]
    else:
        journals = args

    for j in journals:
        if as_issn:
            zone, hit, src = letpub_zone(issn=j, refresh=refresh)
        else:
            zone, hit, src = letpub_zone(name=j, refresh=refresh)
        print(f"{j}\t{zone + '区' if zone else '未收录'}\t{src}")
