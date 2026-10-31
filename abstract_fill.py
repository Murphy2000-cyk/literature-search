# -*- coding: utf-8 -*-
"""摘要补全：多源降级，补齐元数据表里缺摘要的行

为什么要这个模块（2026-10-30 实测）
----------------------------------
Semantic Scholar 的检索响应自带摘要，但**覆盖率约 84%**，缺的那部分几乎全是 Elsevier：
Elsevier 自 2019 年起不向 Crossref / OpenAlex / Semantic Scholar 等第三方元数据平台
提供摘要。实测一批 65 篇的调研里缺 13 篇，其中 10 篇是 Elsevier、1 篇 IEEE、1 篇 T&F、
1 篇 Springer。手工逐个找源把覆盖率从 80% 提到 98%，本模块就是把那个过程固化下来。

降级顺序（每一档都实测过）
--------------------------
1. **Crossref**：只对非 Elsevier 有效；Elsevier 一律无摘要。
2. **OpenAlex**：`abstract_inverted_index` 有时有、Elsevier 大多为空；有则按位置还原。
3. **Elsevier API**：唯一能拿到 Elsevier 摘要的通道，需要
   - API key（<https://dev.elsevier.com> 注册 → Create API Key）
   - 机构订阅 ScienceDirect 或 Scopus
   - **调用时出口 IP 属于该机构**（授权按 IP，不按 key）
   端点 `api.elsevier.com/content/abstract/doi/{DOI}`，取
   `abstracts-retrieval-response.coredata."dc:description"`。
   ⚠️ **无权限时仍返回 HTTP 200**，只是没有 `dc:description` 字段（只剩 9 个标识符），
   所以判据必须是"有没有这个字段"，不能看状态码。
4. **PubMed**（按 DOI 反查 PMID）：IEEE 等被 PubMed 收录的刊物常有完整摘要。
5. **searchts 渲染出版商页面**：Springer、T&F 实测有效（T&F 能绕开 Cloudflare）；
   ScienceDirect 网页会被拦（403），不要指望它。

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

SOURCES = ("crossref", "openalex", "elsevier", "pubmed", "searchts")
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

def from_crossref(doi, timeout=25):
    import requests
    r = requests.get(f"https://api.crossref.org/works/{doi}",
                     params={"mailto": MAILTO}, timeout=timeout,
                     headers={"User-Agent": UA})
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    ab = _clean(r.json().get("message", {}).get("abstract"))
    return (ab or None), ("命中" if ab else "无摘要字段")


def from_openalex(doi, timeout=25):
    """OpenAlex 的摘要是倒排索引，需要按位置还原。"""
    import requests
    r = requests.get(f"https://api.openalex.org/works/doi:{doi}",
                     params={"mailto": MAILTO}, timeout=timeout,
                     headers={"User-Agent": UA})
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    inv = r.json().get("abstract_inverted_index")
    if not inv:
        return None, "abstract_inverted_index 为空"
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    ab = " ".join(pos[i] for i in sorted(pos))
    return (ab or None), ("命中" if ab else "还原后为空")


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


def from_searchts(doi, timeout=120):
    """用 searchts 渲染 DOI 落地页并抽摘要段落。ScienceDirect 会 403。"""
    import subprocess
    try:
        r = subprocess.run(["searchts", "read", f"https://doi.org/{doi}"],
                           capture_output=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except FileNotFoundError:
        return None, "找不到 searchts"
    except Exception as e:
        return None, f"异常 {type(e).__name__}"
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None, "页面抓取失败"
    lines = (r.stdout or "").splitlines()
    start = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if re.match(r"^#+\s*abstract", s, re.I) or re.match(r"^\*\*abstract\*\*", s, re.I):
            start = i
            break
    if start is None:
        return None, "页面里没有 Abstract 段落"
    buf = []
    for ln in lines[start + 1:]:
        s = ln.strip()
        if s.startswith("#") or re.match(r"^(keywords?|key\s*words)", s, re.I):
            break
        if s.startswith("*  ["):        # 关键词链接行
            break
        if s:
            buf.append(s)
    ab = _clean(" ".join(buf))
    if len(ab) < 80:                    # 太短多半是页面壳
        return None, "摘要段落过短（疑似壳页）"
    return ab, "命中"


# ---------------------------------------------------------------- 主流程

def fill_one(doi_or_title, key="", sources=SOURCES, sleep=0.2):
    """对一篇论文依次试各源。返回 (abstract, source, log)。"""
    doi = (doi_or_title or "").strip()
    log = []
    if not doi:
        return None, "", ["没有 DOI"]
    for src in sources:
        try:
            if src == "crossref":
                if _is_elsevier(doi):
                    log.append("crossref: 跳过（Elsevier 不给 Crossref 摘要）")
                    continue
                ab, why = from_crossref(doi)
            elif src == "openalex":
                ab, why = from_openalex(doi)
            elif src == "elsevier":
                if not _is_elsevier(doi):
                    log.append("elsevier: 跳过（非 Elsevier 论文）")
                    continue
                ab, why = from_elsevier(doi, key)
            elif src == "pubmed":
                ab, why = from_pubmed(doi)
            elif src == "searchts":
                ab, why = from_searchts(doi)
            else:
                ab, why = None, "未知来源"
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
                print(f"  [{i}/{len(rows)}] ✓ {src:<9} {len(ab):>5} 字 | {title}")
        else:
            ws.cell(row=r, column=s_idx).value = "未补到"
            failed.append({"row": r, "doi": doi, "title": title, "log": log})
            if verbose:
                print(f"  [{i}/{len(rows)}] ✗ {'; '.join(log)} | {title}")
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
        description="摘要补全：Crossref → OpenAlex → Elsevier → PubMed → searchts 渲染")
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
