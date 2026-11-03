# -*- coding: utf-8 -*-
"""OpenAlex 批量补关键词与摘要

为什么需要这个模块
------------------
Semantic Scholar 的论文对象里**没有 keywords 字段**（请求它会直接 400
`Unrecognized or unsupported fields: [keywords]`），`fieldsOfStudy` 与
`s2FieldsOfStudy` 只是「Engineering / Materials Science」这种粗学科，
不能当关键词用，`tldr` 通常为 null。

OpenAlex 的 `keywords` 是提炼过的领域短语，质量足够做相关性排序的输入；
同一个响应里还带 `abstract_inverted_index`，可以还原成摘要，**不额外花请求**。
因此「取关键词」和「补摘要」在这里合并成一次批量调用。

批量方式
--------
`filter=doi:A|B|C`，管道分隔，50 个一批。按 DOI 查询，所以**没有 DOI 的记录
无法补**，这是硬限制，不要试图按标题猜。
"""
import argparse
import json
import os
import re
import sys
# 控制台编码兜底：Windows 默认 GBK，打印中文/特殊字符会 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import time
import urllib.parse
import urllib.request

MAILTO = "dsh-litsearch@example.com"
UA = f"DSH-litsearch/1.0 (mailto:{MAILTO})"
BASE = "https://api.openalex.org/works"
BATCH = 50
SLEEP = 0.2
SELECT = "doi,title,keywords,abstract_inverted_index"


def norm_doi(d):
    """DOI 归一化：去协议前缀、去 doi: 前缀、小写。"""
    d = (d or "").strip().lower()
    for p in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/",
              "http://dx.doi.org/", "doi:"):
        if d.startswith(p):
            d = d[len(p):]
    return d.strip()


def invert_to_text(inv, max_chars=6000):
    """把 OpenAlex 的 abstract_inverted_index 还原成连续文本。"""
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs or []:
            pos[i] = word
    if not pos:
        return ""
    text = " ".join(pos[i] for i in sorted(pos))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def _get(url, timeout=45):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return -1, None


def fetch_by_dois(dois, batch=BATCH, sleep=SLEEP, verbose=True):
    """按 DOI 批量取关键词与摘要。

    Args:
        dois: DOI 列表（可含重复与空值）
    Returns:
        {归一化 DOI: {"keywords": [...], "keywords_text": str,
                      "abstract": str, "title": str}}
    """
    todo = []
    seen = set()
    for d in dois:
        n = norm_doi(d)
        if n and n not in seen:
            seen.add(n)
            todo.append(n)
    out = {}
    if not todo:
        return out

    total = (len(todo) + batch - 1) // batch
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        url = (f"{BASE}?filter=doi:{urllib.parse.quote('|'.join(chunk), safe='|')}"
               f"&per-page={batch}&select={SELECT}&mailto={MAILTO}")
        st, d = _get(url)
        if st == 200 and d:
            for w in (d.get("results") or []):
                nd = norm_doi(w.get("doi"))
                kws = [k.get("display_name") for k in (w.get("keywords") or [])
                       if k.get("display_name")]
                out[nd] = {
                    "keywords": kws,
                    "keywords_text": ", ".join(kws),
                    "abstract": invert_to_text(w.get("abstract_inverted_index")),
                    "title": w.get("title") or "",
                }
        if verbose:
            print(f"[OpenAlex] 批次 {i//batch+1}/{total} 累计命中 {len(out)}",
                  file=sys.stderr, flush=True)
        time.sleep(sleep)
    return out


def run_on_xlsx(path, limit=None, only_missing_abstract=True,
                quiet=False, out=None):
    """读 xlsx，按 DOI 批量补「关键词」列；缺摘要的行一并补上。

    只填空值，不覆盖已有内容。摘要补上后把「摘要来源」写为 OpenAlex。
    """
    from openpyxl import load_workbook

    wb = load_workbook(path)
    ws = wb.active
    header = [str(c.value).strip() if c.value is not None else ""
              for c in ws[1]]
    col = {name: i + 1 for i, name in enumerate(header)}
    for need in ("DOI", "标题"):
        if need not in col:
            raise SystemExit(f"{path} 缺少「{need}」列，无法按 DOI 补数据")

    i_doi = col["DOI"]
    i_kw = col.get("关键词")
    i_ab = col.get("摘要")
    i_src = col.get("摘要来源")
    if i_kw is None:
        i_kw = ws.max_column + 1
        ws.cell(row=1, column=i_kw, value="关键词")
        if not quiet:
            print(f"[ADD] 新增列「关键词」 -> {i_kw}")
    if i_src is None:
        i_src = ws.max_column + 1
        ws.cell(row=1, column=i_src, value="摘要来源")
        if not quiet:
            print(f"[ADD] 新增列「摘要来源」 -> {i_src}")

    rows = []
    for r in range(2, ws.max_row + 1):
        doi = str(ws.cell(row=r, column=i_doi).value or "").strip()
        if not doi:
            continue
        if limit and len(rows) >= limit:
            break
        rows.append(r)
    if not rows:
        print("[OpenAlex] 没有可查的 DOI 行")
        wb.save(out or path)
        return {}

    dois = [ws.cell(row=r, column=i_doi).value for r in rows]
    data = fetch_by_dois(dois, verbose=not quiet)

    n_kw = n_ab = 0
    for r in rows:
        d = data.get(norm_doi(ws.cell(row=r, column=i_doi).value))
        if not d:
            continue
        if d["keywords_text"]:
            cur = ws.cell(row=r, column=i_kw).value
            if not (cur or "").strip():
                ws.cell(row=r, column=i_kw, value=d["keywords_text"])
                n_kw += 1
        if i_ab and d["abstract"]:
            cur = str(ws.cell(row=r, column=i_ab).value or "").strip()
            if cur and only_missing_abstract:
                continue
            if not cur:
                ws.cell(row=r, column=i_ab, value=d["abstract"])
                n_ab += 1
                if i_src:
                    ws.cell(row=r, column=i_src, value="OpenAlex")

    wb.save(out or path)
    stats = {"查询": len(rows), "OpenAlex命中": len(data),
             "补关键词": n_kw, "补摘要": n_ab}
    if quiet:
        print(f"[QUIET] 查询 {len(rows)} | 命中 {len(data)} | "
              f"补关键词 {n_kw} | 补摘要 {n_ab}")
    else:
        for k, v in stats.items():
            print(f"[STAT] {k}: {v}")
    return stats


def main():
    ap = argparse.ArgumentParser(description="OpenAlex 批量补关键词与摘要")
    ap.add_argument("xlsx", help="论文表路径")
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 行")
    ap.add_argument("--out", default=None, help="另存为新文件")
    ap.add_argument("--quiet", action="store_true", help="只输出一行统计")
    a = ap.parse_args()
    if not os.path.exists(a.xlsx):
        raise SystemExit(f"找不到文件：{a.xlsx}")
    run_on_xlsx(a.xlsx, limit=a.limit, quiet=a.quiet, out=a.out)


if __name__ == "__main__":
    main()
