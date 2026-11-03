# -*- coding: utf-8 -*-
"""摘要补全：Elsevier → PubMed 两档（OpenAlex 由 keyword_fetch 一并处理）

为什么要这个模块
----------------
Semantic Scholar 的检索响应自带摘要，但覆盖率随语料而变，缺的那部分里
Elsevier 居多：它自 2019 年起不向 Crossref / OpenAlex / Semantic Scholar
等第三方元数据平台提供摘要。

三档顺序与各自的定位
--------------------
1. **Elsevier API**：唯一能拿到 Elsevier 摘要的通道。需要
   - API key（<https://dev.elsevier.com> 注册 → Create API Key）
   - 机构订阅 ScienceDirect 或 Scopus
   - **调用时出口 IP 属于该机构**（授权按 IP，不按 key）
   端点 `api.elsevier.com/content/abstract/doi/{DOI}`，取
   `abstracts-retrieval-response.coredata."dc:description"`。
   ⚠️ **无权限时仍返回 HTTP 200**，只是没有 `dc:description` 字段，
   所以判据必须是「有没有这个字段」，不能看状态码。

2. **OpenAlex**：不在本模块里。它按 DOI 批量查询时顺带返回
   `abstract_inverted_index`，与「取关键词」是同一次请求，因此并入
   `keyword_fetch.py`，成本为零。

3. **PubMed**（按 DOI 反查 PMID）：走 DOI 的最后一档。它只在生物医学
   类刊物上有覆盖，工程类文献大多查不到，所以放在最后。

本模块**不覆盖**的：Crossref（对 Elsevier 无摘要、对其它出版商命中率也很低）
与网页渲染（需 searchts 空跑 20~50 秒，成本最高、覆盖最窄）。这两档已删除，
不要再加回来。

key 的存放
----------
优先环境变量 `ELS_API_KEY`，其次 `~/.dsh/env.json` 的 `env.ELS_API_KEY`。
**代码里不写任何 key**，也不要把 key 存进 skill 目录。

用法
----
    python abstract_fill.py 论文元数据.xlsx                 # 只补缺摘要的行
    python abstract_fill.py 论文元数据.xlsx --limit 20      # 先补 20 篇试试
    python abstract_fill.py 论文元数据.xlsx --refresh       # 连已有摘要也重取
    python abstract_fill.py 论文元数据.xlsx --out 补后.xlsx --quiet
"""
import argparse
import json
import os
import re
import sys
import time

# 顺序即优先级。OpenAlex 不在此列，见 keyword_fetch.py
SOURCES = ("elsevier", "pubmed")
MAILTO = "dsh-litsearch@example.com"
UA = f"DSH-litsearch/1.0 (mailto:{MAILTO})"


# ---------------------------------------------------------------- 工具

def load_key():
    """Elsevier API key：环境变量 → ~/.dsh/env.json 的 env.ELS_API_KEY → 空。"""
    k = os.getenv("ELS_API_KEY", "").strip()
    if k:
        return k
    p = os.path.join(os.path.expanduser("~"), ".dsh", "env.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return (json.load(f).get("env", {}).get("ELS_API_KEY", "") or "").strip()
        except Exception:
            return ""
    return ""


def _clean(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _is_elsevier(doi):
    return (doi or "").strip().lower().startswith("10.1016/")


# ---------------------------------------------------------------- 各源

def from_elsevier(doi, key, timeout=40):
    import requests
    if not key:
        return None, "没有 ELS_API_KEY"
    r = requests.get(f"https://api.elsevier.com/content/abstract/doi/{doi}",
                     headers={"X-ELS-APIKey": key, "Accept": "application/json"},
                     timeout=timeout)
    if r.status_code == 404:
        return None, "HTTP 404（该文未进 Scopus/ScienceDirect 索引）"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    c = (r.json().get("abstracts-retrieval-response") or {}).get("coredata") or {}
    ab = c.get("dc:description")
    if isinstance(ab, dict):
        ab = ab.get("$") or ab.get("_")
    if not ab:
        # 关键判据：200 但没有 dc:description = 无订阅权限 / 出口 IP 不在机构内
        return None, f"无 dc:description（字段 {len(c)} 个 → 无订阅权限或 IP 不在机构内）"
    return _clean(ab), "命中"


def from_pubmed(doi, timeout=25):
    """按 DOI 在 PubMed 反查 PMID，再 eutils 取摘要。"""
    import requests
    try:
        r = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                         params={"db": "pubmed", "term": f"{doi}[DOI]",
                                 "retmode": "json"}, timeout=timeout,
                         headers={"User-Agent": UA})
        ids = (r.json().get("esearchresult", {}).get("idlist") or []) if r.status_code == 200 else []
        if not ids:
            return None, "PubMed 未收录该 DOI"
        pmid = ids[0]
        r2 = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                          params={"db": "pubmed", "id": pmid, "rettype": "abstract",
                                  "retmode": "text"}, timeout=timeout,
                          headers={"User-Agent": UA})
        ab = _clean(r2.text)
        # efetch 的 text 模式会把作者/刊名一起返回，粗略截到摘要区间
        m = re.search(r"(?:Abstract|ABSTRACT)[:\s]*(.+)", ab, re.S)
        body = (m.group(1) if m else ab).strip()
        return (body or None), (f"命中（PMID {pmid}）" if body else "取到空摘要")
    except Exception as e:
        return None, f"异常 {type(e).__name__}"


# ---------------------------------------------------------------- 主流程

def fill_one(doi_or_title, key="", sources=SOURCES, sleep=0.2):
    """对一篇论文依次试各源。返回 (abstract, source, log)。"""
    doi = (doi_or_title or "").strip()
    log = []
    if not doi:
        return None, "", ["没有 DOI"]
    for src in sources:
        try:
            if src == "elsevier":
                if not _is_elsevier(doi):
                    log.append("elsevier: 跳过（非 Elsevier 论文）")
                    continue
                ab, why = from_elsevier(doi, key)
            elif src == "pubmed":
                ab, why = from_pubmed(doi)
            elif src == "openalex":
                log.append("openalex: 已并入 keyword_fetch 批量通道，此处跳过")
                continue
            else:
                ab, why = None, f"未知来源 {src}"
        except Exception as e:
            ab, why = None, f"异常 {type(e).__name__}"
        log.append(f"{src}: {why}")
        if ab:
            return ab, src, log
        time.sleep(sleep)
    return None, "", log


def fill_table(path, out_path=None, refresh=False, limit=0,
               sources=SOURCES, verbose=True):
    """给 xlsx 补摘要。返回统计字典。"""
    from openpyxl import load_workbook

    key = load_key()
    wb = load_workbook(path)
    ws = wb.active
    headers = [c.value for c in ws[1]]
    if "摘要" not in headers:
        raise SystemExit(f"[ERROR] 表中没有「摘要」列。现有列：{headers}")
    ab_idx = headers.index("摘要") + 1
    doi_idx = headers.index("DOI") + 1 if "DOI" in headers else None
    title_idx = headers.index("标题") + 1 if "标题" in headers else None
    if doi_idx is None:
        raise SystemExit("[ERROR] 表中没有「DOI」列，无法按 DOI 补摘要。")

    src_col = "摘要来源"
    if src_col in headers:
        s_idx = headers.index(src_col) + 1
    else:
        s_idx = len(headers) + 1
        ws.cell(row=1, column=s_idx, value=src_col)

    rows = []
    for r in range(2, ws.max_row + 1):
        cur = str(ws.cell(row=r, column=ab_idx).value or "").strip()
        if cur and not refresh:
            continue
        rows.append(r)
    if limit and limit > 0:
        rows = rows[:limit]

    if not rows:
        print("没有需要补摘要的行（都有摘要了；要全部重取加 --refresh）")
        return {"total": 0, "ok": 0, "by_source": {}, "failed": []}

    if verbose:
        print(f"[FILL] 待补 {len(rows)} 篇；key {'已找到' if key else '未找到（Elsevier 通道将跳过）'}")

    by_source, failed = {}, []
    for i, r in enumerate(rows, 1):
        doi = str(ws.cell(row=r, column=doi_idx).value or "").strip()
        ab, src, log = fill_one(doi, key=key, sources=sources)
        title = str(ws.cell(row=r, column=title_idx).value or "")[:50] if title_idx else ""
        if ab:
            ws.cell(row=r, column=ab_idx).value = ab
            ws.cell(row=r, column=s_idx).value = src
            by_source[src] = by_source.get(src, 0) + 1
            if verbose:
                print(f"  [{i}/{len(rows)}] OK {src:<9} {len(ab):>5} 字 | {title}")
        else:
            ws.cell(row=r, column=s_idx).value = "未补到"
            failed.append({"row": r, "doi": doi, "title": title, "log": log})
            if verbose:
                print(f"  [{i}/{len(rows)}] MISS {'; '.join(log)} | {title}")
        time.sleep(0.2)

    dest = out_path or path
    try:
        wb.save(dest)
    except PermissionError:
        raise SystemExit(f"[ERROR] 无法写入 {dest}：文件可能正被 Excel 打开。")

    stat = {"total": len(rows), "ok": sum(by_source.values()),
            "by_source": by_source, "failed": failed}
    body = "  ".join(f"{k}={v}" for k, v in sorted(by_source.items())) or "无"
    if verbose:
        print("-" * 60)
        print(f"补到 {stat['ok']}/{stat['total']} 篇（{body}）-> {dest}")
        if failed:
            print(f"未补到 {len(failed)} 篇：")
            for f in failed:
                print(f"  - {f['doi']} | {f['title']} | {'; '.join(f['log'])}")
    else:
        print(f"[QUIET] 补到 {stat['ok']}/{stat['total']}（{body}）| 未补到 {len(failed)} -> {dest}")
    return stat


def main():
    ap = argparse.ArgumentParser(
        description="摘要补全：Elsevier → PubMed（OpenAlex 见 keyword_fetch.py）")
    ap.add_argument("xlsx", help="元数据表路径")
    ap.add_argument("--out", help="另存到新文件（默认就地更新）")
    ap.add_argument("--refresh", action="store_true", help="连已有摘要的也重取")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 篇（0=不限）")
    ap.add_argument("--sources", default=",".join(SOURCES),
                    help=f"逗号分隔的来源顺序，默认 {','.join(SOURCES)}")
    ap.add_argument("--quiet", action="store_true", help="只输出一行统计")
    args = ap.parse_args()

    fill_table(args.xlsx, out_path=args.out, refresh=args.refresh,
               limit=args.limit,
               sources=tuple(s.strip() for s in args.sources.split(",") if s.strip()),
               verbose=not args.quiet)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
