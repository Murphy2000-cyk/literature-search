#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文献检索备用数据源：Crossref

================== 使用前置条件（硬性） ==================
1. 只有在 Semantic Scholar 不可用时才考虑使用本模块。
2. 必须先向用户汇报 S2 的失败情况，由**用户明确决定**是否改用 Crossref。
   未经用户同意擅自换源，属于违规操作（脚本会直接拒绝执行）。
3. 必须同时向用户说明 Crossref 的覆盖局限：
   - **只能检索到有 DOI 的文献**；无 DOI 的会议论文、区域性期刊、技术报告、
     标准等一律检索不到。
   - **摘要覆盖率低**（实测约 28%），多数条目只能靠标题判断相关性。
   - `query` 是**相关性排序检索**（底层 Elasticsearch），返回"最相关的前 N 条"，
     不存在"相关阈值"，因此也**没有"确实未命中"的概念**——它几乎不会返回 0 条。
==========================================================

检索机制说明：
  - `query` / `query.bibliographic`：相关性排序的自由文本检索（推荐后者）
  - `query.title`、`query.author`、`query.container-title` 等：限定字段检索
  - `filter=`：精确结构化过滤（类型、年份、ISSN、是否有摘要等），与 query 相互独立
  - `rows`：单次返回条数，**上限 1000**（实测 2000 会被拒）
  - `cursor`：深分页游标，首次传 `cursor=*`，之后用返回的 `next-cursor` 继续，
    可翻越 1000 条上限（无实际深度限制）

用法（命令行）：
    python crossref_fallback.py --approved-by-user --out <目录> "查询1" "查询2" ...
编程调用：
    from crossref_fallback import search_crossref, save_crossref
    papers = search_crossref(["..."], approved_by_user=True)
"""
import os
import re
import sys
# 控制台编码兜底：Windows 默认 GBK，打印中文/特殊字符会 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import json
import time
import html
import argparse

import requests

CROSSREF_API = "https://api.crossref.org/works"
DEFAULT_MAILTO = "academic.research@example.com"
DEFAULT_INTERVAL = 0.5
ROWS_MAX = 1000                 # Crossref 单次请求返回条数上限（实测）
DEFAULT_PER_REQUEST = 1000      # 默认一次取到上限（旧版默认是 100，会严重漏检）

DOI_ONLY_NOTICE = (
    "【重要局限】Crossref 只能检索到有 DOI 的文献：无 DOI 的会议论文、区域性期刊、"
    "技术报告、标准等均检索不到；且摘要覆盖率约 28%。汇报时必须向用户说明这一点。"
)


class UserApprovalRequired(PermissionError):
    """未获得用户同意时尝试使用备用源。"""


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())[:80]


def _clean_abstract(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _year_of(item: dict):
    for k in ("published-print", "published-online", "published", "issued"):
        dp = (item.get(k) or {}).get("date-parts") or []
        if dp and dp[0] and dp[0][0]:
            return dp[0][0]
    return None


def score_title(title: str, keywords) -> int:
    """标题命中关键词个数（仅用于排序，不删除任何条目）"""
    t = (title or "").lower()
    return sum(1 for k in keywords if k in t) if keywords else 0


def _fetch_one_query(query, per_request, max_records, from_year, mailto, timeout, interval):
    """取单个查询的结果；max_records 超过单次上限时用 cursor 翻页。"""
    items, cursor, total = [], "*", None
    while True:
        flt = "type:journal-article"
        if from_year:
            flt += f",from-pub-date:{from_year}-01-01"
        params = {
            "query": query,
            "rows": per_request,
            "cursor": cursor,
            # 【必须】显式指定按相关性排序。
            # 实测（2026-09）：省略 sort 时，一旦带上 cursor=*，Crossref 返回的
            # 不再是相关性排序，而是近乎随机的语料顺序 —— 同一 query 前 100 条中
            # 相关条目从 44 条暴跌到 1 条（内容变成牙种植体、超声心动图等）。
            # 显式加上 sort=relevance 后，cursor 首页与无 cursor 首页完全一致。
            "sort": "relevance",
            "filter": flt,
            "select": "DOI,title,author,container-title,issued,published-print,"
                      "published-online,is-referenced-by-count,type,abstract",
            "mailto": mailto,
        }
        try:
            r = requests.get(CROSSREF_API, params=params, timeout=timeout,
                             headers={"User-Agent": f"LiteratureSearch/1.0 (mailto:{mailto})"})
            if r.status_code != 200:
                print(f"[ERR] {query!r} HTTP {r.status_code}: {r.text[:160]}")
                break
            msg = r.json().get("message", {})
        except Exception as e:
            print(f"[ERR] {query!r}: {e}")
            break

        if total is None:
            total = msg.get("total-results")
        batch = msg.get("items", [])
        items.extend(batch)
        cursor = msg.get("next-cursor")
        print(f"[QUERY] {query!r} 本轮 {len(batch)} 条，累计 {len(items)} 条"
              f"（该查询在 Crossref 中共 {total} 条排序记录）")
        if not batch or not cursor:
            break
        if max_records and len(items) >= max_records:
            break
        time.sleep(interval)
    # 同时返回该查询在 Crossref 中被排序的语料总量，用于如实报告覆盖率
    return (items[:max_records] if max_records else items), total


def search_crossref(queries, per_query=DEFAULT_PER_REQUEST, max_records=None,
                    from_year=None, mailto=DEFAULT_MAILTO, interval=DEFAULT_INTERVAL,
                    keywords=None, timeout=45, approved_by_user=False, stats=None):
    """按查询列表检索 Crossref，返回归一化后的论文 dict 列表。

    Args:
        per_query: 单次请求返回条数（上限 1000）
        max_records: 每个查询最多取回多少条；若大于 per_query 则用 cursor 翻页。
                     默认 None（每个查询只取一页，即 per_query 条）。
        approved_by_user: **必须为 True**，表示已先向用户汇报 S2 失败并获用户同意。
    """
    if not approved_by_user:
        raise UserApprovalRequired(
            "Crossref 是备用数据源：必须在 Semantic Scholar 不可用时**先向用户汇报**，"
            "由用户明确决定是否改用 Crossref，之后才可调用本函数。\n"
            "请传入 approved_by_user=True（命令行加 --approved-by-user）。\n" + DOI_ONLY_NOTICE
        )
    if per_query > ROWS_MAX:
        print(f"[WARN] per_query={per_query} 超过 Crossref 上限 {ROWS_MAX}，已截断为 {ROWS_MAX}")
        per_query = ROWS_MAX

    print("\n" + "=" * 60)
    print("Crossref 备用源检索")
    print(DOI_ONLY_NOTICE)
    print("=" * 60)

    merged = {}
    for q in queries:
        items, total = _fetch_one_query(q, per_query, max_records, from_year,
                                        mailto, timeout, interval)
        if stats is not None:
            stats[q] = {"total_results": total, "retrieved": len(items)}
        for it in items:
            doi = (it.get("DOI") or "").lower()
            if doi and doi not in merged:
                merged[doi] = it
        time.sleep(interval)
    return normalize(merged.values(), keywords)


def normalize(items, keywords=None):
    """把 Crossref item 归一化为统一结构（标题去重）。"""
    seen, out = set(), []
    for it in items:
        title = (it.get("title") or [""])[0]
        if not title:
            continue
        yr = _year_of(it)
        if not yr:
            continue
        key = _norm_title(title)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "title": title,
            "year": yr,
            "venue": (it.get("container-title") or [""])[0],
            "type": it.get("type", ""),
            "citations": it.get("is-referenced-by-count", 0),
            "doi": it.get("DOI", ""),
            "authors": [f"{a.get('given','')} {a.get('family','')}".strip()
                        for a in (it.get("author") or [])][:8],
            "abstract": _clean_abstract(it.get("abstract") or "")[:1500],
            "rel": score_title(title, keywords),
        })
    out.sort(key=lambda p: (-p["rel"], -p["citations"]))
    return out


def save_crossref(papers, out_dir, prefix="crossref", min_rel=0, top_n=60):
    """保存 json（全量）与 txt（相关 + 按被引前 N），并附数据源局限说明。"""
    os.makedirs(out_dir, exist_ok=True)
    all_path = os.path.join(out_dir, f"{prefix}_all.json")
    with open(all_path, "w", encoding="utf-8") as f:
        json.dump({"source": "Crossref", "source_limitation": DOI_ONLY_NOTICE,
                   "total": len(papers), "papers": papers},
                  f, ensure_ascii=False, indent=2)

    relevant = [p for p in papers if p["rel"] >= min_rel] if min_rel else papers

    def dump(fn, items):
        lines = [DOI_ONLY_NOTICE, "=" * 60, ""]
        for i, p in enumerate(items, 1):
            lines.append(f"[{i}] (rel={p['rel']}, cit={p['citations']}, {p['year']}) {p['venue']}")
            lines.append(f"    EN: {p['title']}")
            if p["doi"]:
                lines.append(f"    DOI: {p['doi']}")
            if p["abstract"]:
                lines.append(f"    ABS: {p['abstract'][:300]}")
            lines.append("")
        path = os.path.join(out_dir, fn)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return path

    p1 = dump(f"{prefix}_relevant.txt", relevant)
    p2 = dump(f"{prefix}_top_by_citation.txt",
              sorted(relevant, key=lambda p: -p["citations"])[:top_n])
    print(f"[SAVE] {all_path}")
    print(f"[SAVE] {p1}")
    print(f"[SAVE] {p2}")
    return all_path


def main():
    ap = argparse.ArgumentParser(description="Crossref 文献检索（备用源，需用户同意）")
    ap.add_argument("queries", nargs="+", help="一个或多个查询词")
    ap.add_argument("--out", default=os.getcwd(), help="输出目录")
    ap.add_argument("--per-query", type=int, default=DEFAULT_PER_REQUEST,
                    help=f"单次请求条数，上限 {ROWS_MAX}")
    ap.add_argument("--max-records", type=int, default=None,
                    help="每个查询最多取回条数；大于 --per-query 时用 cursor 翻页")
    ap.add_argument("--from-year", type=int, default=None)
    ap.add_argument("--keywords", default="", help="逗号分隔的相关性关键词（用于排序）")
    ap.add_argument("--prefix", default="crossref")
    ap.add_argument("--approved-by-user", action="store_true",
                    help="确认已向用户汇报 S2 失败并获得用户同意使用 Crossref")
    args = ap.parse_args()

    kws = [k.strip().lower() for k in args.keywords.split(",") if k.strip()]
    papers = search_crossref(args.queries, per_query=args.per_query,
                             max_records=args.max_records, from_year=args.from_year,
                             keywords=kws or None, approved_by_user=args.approved_by_user)
    print(f"[TOTAL] {len(papers)} unique works")
    save_crossref(papers, args.out, prefix=args.prefix, min_rel=3 if kws else 0)


if __name__ == "__main__":
    try:
        main()
    except UserApprovalRequired as e:
        print(f"\n[REFUSED] {e}")
        sys.exit(3)
