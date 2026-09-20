# -*- coding: utf-8 -*-
"""论文元数据表：一次性落盘到 Excel，之后按需读取（v1.0）

设计目的
--------
把检索到的**全部**论文元数据一次性写进**一个** xlsx，之后按需读取其中一小部分，
而不是把整份表读进上下文 —— 目的是省 token。

写入（驱动程序里调用）：

    from paper_table import save_papers_xlsx
    save_papers_xlsx(papers, OUT, prefix="论文元数据")

按需读取（CLI，输出刻意做得很小）：

    python paper_table.py --xlsx 论文元数据.xlsx --stats
    python paper_table.py --xlsx 论文元数据.xlsx --filter --year-min 2020 --quartile Q1 --limit 20
    python paper_table.py --xlsx 论文元数据.xlsx --filter --has-abstract --select 标题,DOI --limit 50
    python paper_table.py --xlsx 论文元数据.xlsx --selected              # 读用户勾选的「选中」列

三条硬约束（均为实测踩坑所得，不得回退）
----------------------------------------
1. **PaperID / DOI 列写入时必须强制文本格式**。否则 Excel 会把长 ID 显示成科学计数法，
   且事后无法从外观分辨，Phase 2 会全部查不到。
2. **筛选前必须校验字段存在性**。实测曾因数据里没有 `quartile` 字段，
   "只要 Q1" 把 565 篇静默全部排除，看起来像"该领域没有 Q1 论文"。
   本模块遇到不存在的字段会直接报错退出，绝不静默返回空。
3. **xlsx 只是视图，json 才是权威源**。本模块只负责写/读表，不改变既有 json 产出。
"""
import os
import sys
import json
import argparse

# 每个表默认写入的篇数上限（**用户可自定义**，默认 200）。
# 语义：所有查询合并去重之后取前 N 篇；**候选不足 N 篇时有多少存多少**，不补齐、不报错。
try:
    from semantic_scholar_search import DEFAULT_TOTAL_LIMIT
except Exception:                       # 允许本模块被单独使用时的兜底
    DEFAULT_TOTAL_LIMIT = 200

# ---------------------------------------------------------------- 列定义

# (Excel 表头, 取值键, 是否强制文本)
PREFERRED = [
    ("序号", "__index__", False),
    ("PaperID", "paper_id", True),
    ("DOI", "doi", True),
    ("标题", "title", False),
    ("中文标题", "title_cn", False),
    ("作者", "authors", False),
    ("年份", "year", False),
    ("期刊", "venue", False),
    ("分区", "quartile", False),
    ("引用数", "citation_count", False),
    ("BGE分数", "relevance_score", False),
    ("是否期刊", "is_journal", False),
    ("是否会议", "is_conference", False),
    ("是否预印本", "is_preprint", False),
    ("PDF链接", "pdf_url", False),
    ("摘要", "abstract", False),
    ("中文摘要", "abstract_cn", False),
    ("选中", "__selected__", False),
]

# 别名字段：把不同来源的叫法归一到标准列
ALIASES = {
    "citations": "citation_count",
    "citationCount": "citation_count",
    "rel": "relevance_score",
    "relevance": "relevance_score",
    "type": "文献类型",
    "publicationTypes": "文献类型",
}

TEXT_HEADERS = {"PaperID", "DOI"}
COLUMN_WIDTHS = {
    "序号": 6, "PaperID": 42, "DOI": 32, "标题": 60, "中文标题": 40,
    "作者": 28, "年份": 6, "期刊": 32, "分区": 6, "引用数": 8,
    "BGE分数": 8, "是否期刊": 8, "是否会议": 8, "是否预印本": 10,
    "PDF链接": 30, "摘要": 40, "中文摘要": 30, "选中": 6, "文献类型": 16,
}


# ---------------------------------------------------------------- 归一化

def _to_dict(item):
    """把 PaperInfo（dataclass）或 dict 统一成 dict。"""
    if isinstance(item, dict):
        d = dict(item)
    elif hasattr(item, "__dict__") and hasattr(item, "paper_id"):
        d = dict(vars(item))
    else:
        raise TypeError(f"无法识别的论文对象类型：{type(item)}")

    out = {}
    extras = {}
    for k, v in d.items():
        key = ALIASES.get(k, k)
        if key in ("paper_id", "doi", "title", "title_cn", "year", "venue",
                   "quartile", "citation_count", "relevance_score",
                   "is_journal", "is_conference", "is_preprint", "pdf_url",
                   "abstract", "abstract_cn", "文献类型"):
            out[key] = v
        else:
            extras[k] = v
    out.update(extras)
    return out


def _cell_value(header, key, rec, idx):
    if key == "__index__":
        return idx
    if key == "__selected__":
        return ""
    v = rec.get(key, "")
    if isinstance(v, (list, tuple)):
        v = "; ".join(str(x) for x in v)
    elif isinstance(v, bool):
        v = "是" if v else "否"
    return "" if v is None else v


def build_columns(records):
    """按「标准列在前 + 额外列在后」生成列定义。"""
    cols = []
    used = set()
    for header, key, text in PREFERRED:
        cols.append((header, key, text))
        used.add(key)
    # 收集额外字段（去掉别名已归一的键）
    extra_keys = []
    for rec in records[:200]:
        for k in rec:
            if k in used or k in extra_keys:
                continue
            extra_keys.append(k)
    for k in extra_keys:
        cols.append((k, k, False))
    return cols


def _ident(item):
    """取一条记录的唯一标识（用于去重）。"""
    if isinstance(item, dict):
        for k in ("paper_id", "doi", "title"):
            v = item.get(k)
            if v:
                return str(v).strip().lower()
        return ""
    for attr in ("paper_id", "doi", "title"):
        v = getattr(item, attr, None)
        if v:
            return str(v).strip().lower()
    return ""


def interleave_by_query(results_by_query, total_limit=DEFAULT_TOTAL_LIMIT):
    """把多个查询的结果按【轮转】融合，再取前 total_limit 篇。

    **为什么必须轮转（实测踩坑）**：驱动脚本原来是按
    「查询 1 的全部 → 查询 2 的全部 → …」合并成一个列表，再取前 N。
    实测（6 个查询 × 100 篇 = 600 候选池，取前 200）结果是：
    查询 1 拿 100 篇、查询 2 拿 100 篇，**查询 3~6 的结果全部被丢弃**，
    连它们各自排第 1 的论文都进不了表 ——
    候选池再大也没用，多轮关键词检索的意义被完全抵消。

    轮转融合后每个查询都有代表，且**各查询内部仍保持 S2 自己的相关性顺序**
    （第 1 轮取各查询的第 1 名，第 2 轮取各查询的第 2 名，依此类推）。

    Args:
        results_by_query: {查询词: [论文, ...]} 或 [[论文, ...], ...]
        total_limit: 融合后取前多少篇（默认 DEFAULT_TOTAL_LIMIT = 200）

    Returns:
        融合去重后的列表，长度不超过 total_limit
    """
    buckets = (list(results_by_query.values())
               if isinstance(results_by_query, dict) else list(results_by_query))
    buckets = [list(b) for b in buckets if b]
    if not buckets:
        return []

    out, seen = [], set()
    idx = 0
    # total_limit=None / 0 表示不截断（与 rank_groups 的语义对齐）——
    # 早期这里直接写 `while len(out) < total_limit`，传 None 会 TypeError。
    limit = total_limit if total_limit else float("inf")
    while len(out) < limit:
        added = False
        for b in buckets:
            if idx >= len(b):
                continue
            key = _ident(b[idx])
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            out.append(b[idx])
            added = True
            if len(out) >= limit:
                break
        if not added:
            break                      # 所有桶都取空了，或剩下的全是重复
        idx += 1
    return out


# ---------------------------------------------------------------- 写表

def save_papers_xlsx(papers, out_dir, prefix="论文元数据", sheet_title="论文元数据",
                     total_limit=DEFAULT_TOTAL_LIMIT):
    """把论文元数据写成一个 xlsx。返回文件路径。

    total_limit：**写入篇数上限**，默认 DEFAULT_TOTAL_LIMIT（200）。用户可自行定义。
        - 候选多于上限 → 取前 total_limit 篇
        - 候选少于上限 → **有多少存多少**，不补齐、不报错
        - 传 None 或 0 → 不截断，全部写入
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter

    if not papers:
        raise ValueError("papers 为空，拒绝生成空表（会导致'字段缺失/静默全排除'类误判）")

    papers = list(papers)
    available = len(papers)
    if total_limit and total_limit > 0 and available > total_limit:
        papers = papers[:total_limit]
        print(f"[LIMIT] 候选 {available} 篇，按上限 {total_limit} 取前 {len(papers)} 篇入表")
    elif total_limit and total_limit > 0:
        print(f"[LIMIT] 候选 {available} 篇，不足上限 {total_limit}，全部入表")
    else:
        print(f"[LIMIT] 未设上限，全部 {available} 篇入表")

    records = [_to_dict(p) for p in papers]
    cols = build_columns(records)

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{prefix}.xlsx")

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title
    ws.append([c[0] for c in cols])
    for cell in ws[1]:
        cell.font = Font(bold=True)

    text_idx = [i for i, c in enumerate(cols, start=1) if c[2]]
    for i, rec in enumerate(records):
        ws.append([_cell_value(h, k, rec, i + 1) for h, k, _ in cols])
        row = i + 2
        for ci in text_idx:
            ws.cell(row=row, column=ci).number_format = "@"

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for ci, (header, _, _) in enumerate(cols, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = COLUMN_WIDTHS.get(header, 16)

    wb.save(path)
    return path


# ---------------------------------------------------------------- 读表

def read_table(path):
    """读回 xlsx，返回 (headers, rows)。rows 是 dict 列表。"""
    from openpyxl import load_workbook
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到表：{path}")
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    it = ws.iter_rows(values_only=True)
    try:
        headers = [str(h) if h is not None else "" for h in next(it)]
    except StopIteration:
        raise ValueError(f"表为空：{path}")
    rows = []
    for r in it:
        if r is None or all(v is None for v in r):
            continue
        rows.append({headers[i]: r[i] for i in range(min(len(headers), len(r)))})
    wb.close()
    return headers, rows


def require_fields(headers, needed, rows=None):
    """字段存在性校验 —— 缺失即报错，绝不静默返回空（硬约束 2）。

    除了"列不存在"，还要拦"列存在但整列为空"——后者才是最常见的陷阱：
    字段在表里，筛选却把全部记录排除，看起来像「该条件下没有文献」。
    """
    missing = [f for f in needed if f not in headers]
    if missing:
        raise SystemExit(
            f"[ERROR] 表中不存在这些字段：{missing}\n"
            f"        表中实际字段：{headers}\n"
            f"        拒绝继续 —— 字段缺失时静默筛选会把全部记录排除，"
            f"看起来像「该条件下没有文献」，属最危险的静默失败。"
        )
    if rows:
        empty = [f for f in needed
                 if not any(str(r.get(f) or "").strip() for r in rows)]
        if empty:
            raise SystemExit(
                f"[ERROR] 这些字段存在但**整列为空**：{empty}\n"
                f"        共 {len(rows)} 条记录，该列无一有值。\n"
                f"        用它们筛选必然得到 0 条 —— 但那是「数据里没有这个字段」，"
                f"不是「没有符合条件的文献」。\n"
                f"        请先看 --stats 的字段填充情况，或补做该字段（如分区需要单独查询）。"
            )


def _num(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 视图

def show_stats(path, top_venues=15):
    headers, rows = read_table(path)
    n = len(rows)
    out = [f"表：{os.path.basename(path)}",
           f"记录数：{n}",
           f"字段数：{len(headers)}",
           f"字段：{', '.join(headers)}",
           "-" * 60]

    # 字段缺失统计（这是最容易导致误判的地方，必须显式给出）
    out.append("字段填充情况：")
    for h in headers:
        if h in ("序号", "选中"):
            continue
        filled = sum(1 for r in rows if r.get(h) not in (None, ""))
        pct = 100.0 * filled / n if n else 0
        flag = "  <== 大量缺失" if pct < 50 else ""
        out.append(f"  {h:12s} {filled:5d}/{n} ({pct:5.1f}%){flag}")

    # 年份分布
    out.append("-" * 60)
    years = [_num(r.get("年份")) for r in rows]
    years = [int(y) for y in years if y]
    if years:
        lo, hi = min(years), max(years)
        out.append(f"年份：{lo} ~ {hi}")
        buckets = {}
        for y in years:
            b = (y // 5) * 5
            buckets[b] = buckets.get(b, 0) + 1
        for b in sorted(buckets):
            out.append(f"  {b}-{b+4}: {buckets[b]}")

    # 分区分布（含缺失）—— 直接对应"Q1 筛选为什么会全空"
    if "分区" in headers:
        out.append("-" * 60)
        dist = {}
        for r in rows:
            k = (r.get("分区") or "").strip() or "(空)"
            dist[k] = dist.get(k, 0) + 1
        out.append("分区分布：")
        for k, c in sorted(dist.items(), key=lambda x: -x[1]):
            out.append(f"  {k:10s} {c}")

    # Top 期刊
    if "期刊" in headers:
        out.append("-" * 60)
        dist = {}
        for r in rows:
            k = (r.get("期刊") or "").strip()
            if k:
                dist[k] = dist.get(k, 0) + 1
        out.append(f"期刊数：{len(dist)}    出现最多的 {top_venues} 个：")
        for k, c in sorted(dist.items(), key=lambda x: -x[1])[:top_venues]:
            out.append(f"  {c:5d}  {k}")

    out.append("-" * 60)
    out.append("提示：以上只是统计。要看具体条目用 --filter 按需取，不要整表读取。")
    print("\n".join(out))


def apply_filters(rows, args, headers):
    """按条件筛选；先做字段存在性校验。"""
    needed = []
    conds = []

    if args.year_min is not None or args.year_max is not None:
        needed.append("年份")
        def f(r):
            y = _num(r.get("年份"))
            if y is None:
                return False
            if args.year_min is not None and y < args.year_min:
                return False
            if args.year_max is not None and y > args.year_max:
                return False
            return True
        conds.append(("年份范围", f))

    if args.min_citations is not None:
        needed.append("引用数")
        conds.append((f"引用数>={args.min_citations}",
                      lambda r: (_num(r.get("引用数")) or -1) >= args.min_citations))

    if args.quartile:
        needed.append("分区")
        want = {q.strip().upper() for q in args.quartile.split(",")}
        conds.append((f"分区∈{sorted(want)}",
                      lambda r: (r.get("分区") or "").strip().upper() in want))

    if args.title_contains:
        needed.append("标题")
        kw = args.title_contains.lower()
        conds.append((f"标题含'{args.title_contains}'",
                      lambda r: kw in (r.get("标题") or "").lower()))

    if args.venue_contains:
        needed.append("期刊")
        kw = args.venue_contains.lower()
        conds.append((f"期刊含'{args.venue_contains}'",
                      lambda r: kw in (r.get("期刊") or "").lower()))

    if args.has_abstract:
        needed.append("摘要")
        conds.append(("有摘要", lambda r: bool((r.get("摘要") or "").strip())))

    if args.has_doi:
        needed.append("DOI")
        conds.append(("有DOI", lambda r: bool((r.get("DOI") or "").strip())))

    require_fields(headers, needed, rows=rows)

    cur = list(rows)
    audit = [("筛前", len(cur))]
    for name, fn in conds:
        before = len(cur)
        cur = [r for r in cur if fn(r)]
        audit.append((name, before - len(cur)))
    return cur, audit


def sort_rows(rows, col, desc=False):
    """按某列排序。数值列按数值排，否则按字符串排（None/空排最后）。"""
    def key(r):
        v = r.get(col)
        n = _num(v)
        if n is not None:
            return (0, n, "")
        s = str(v or "")
        return (1, 0, s.lower()) if not s else (0, 0, s.lower())
    # 数值优先：分两组排
    def key2(r):
        v = r.get(col)
        n = _num(v)
        if n is None and (v is None or str(v).strip() == ""):
            return (2, 0.0, "")
        if n is None:
            return (1, 0.0, str(v).lower())
        return (0, n, "")
    out = sorted(rows, key=key2)
    if desc:
        # 空值始终排最后，不因降序跑到最前
        empties = [r for r in out if key2(r)[0] == 2]
        rest = [r for r in out if key2(r)[0] != 2]
        out = list(reversed(rest)) + empties
    return out


def show_rows(rows, headers, select=None, limit=20):
    if select:
        cols = [c.strip() for c in select.split(",")]
        require_fields(headers, cols)
    else:
        cols = [h for h in ("序号", "标题", "年份", "期刊", "分区", "引用数", "DOI")
                if h in headers]

    shown = rows[:limit]
    if not shown:
        print("（无匹配记录）")
        return
    for r in shown:
        parts = []
        for c in cols:
            v = r.get(c, "")
            if v is None:
                v = ""
            s = str(v)
            if len(s) > 70:
                s = s[:67] + "..."
            parts.append(f"{c}={s}")
        print(" | ".join(parts))
    if len(rows) > limit:
        print(f"...（共 {len(rows)} 条匹配，此处只显示前 {limit} 条；"
              f"用 --limit 调整，或用 --out 导出全部）")


def export_rows(rows, headers, out_path, select=None):
    """导出筛选结果。

    **select 给定时只导出这几列**（不是只控制显示）。
    按扩展名决定格式：`.txt`/`.md` -> 纯文本；`.csv` -> CSV；其它 -> xlsx。
    """
    cols = [c.strip() for c in select.split(",")] if select else list(headers)
    require_fields(headers, cols)
    ext = os.path.splitext(out_path)[1].lower()

    def cell(r, c):
        v = r.get(c)
        return "" if v is None else v

    if ext in (".txt", ".md"):
        with open(out_path, "w", encoding="utf-8") as f:
            if len(cols) == 1:
                for r in rows:                      # 单列：一行一个值，最干净
                    f.write(f"{cell(r, cols[0])}\n")
            else:
                f.write(" | ".join(cols) + "\n")
                for r in rows:
                    f.write(" | ".join(str(cell(r, c)) for c in cols) + "\n")
        print(f"[SAVE] 已导出 {len(rows)} 条 × {len(cols)} 列 -> {out_path}")
        return out_path

    if ext == ".csv":
        import csv
        with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                w.writerow([cell(r, c) for c in cols])
        print(f"[SAVE] 已导出 {len(rows)} 条 × {len(cols)} 列 -> {out_path}")
        return out_path

    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "筛选后"
    ws.append(cols)
    for r in rows:
        ws.append([cell(r, c) for c in cols])
    for ci, c in enumerate(cols, start=1):
        if c in TEXT_HEADERS:
            for row in range(2, len(rows) + 2):
                ws.cell(row=row, column=ci).number_format = "@"
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(out_path)
    print(f"[SAVE] 已导出 {len(rows)} 条 × {len(cols)} 列 -> {out_path}")
    return out_path


def show_selected(path):
    """读「选中」列，输出被勾选的 PaperID（配合 Phase 2 使用）。"""
    headers, rows = read_table(path)
    require_fields(headers, ["选中"])
    id_col = "PaperID" if "PaperID" in headers else ("DOI" if "DOI" in headers else None)
    if not id_col:
        raise SystemExit("[ERROR] 表中既无 PaperID 也无 DOI，无法定位论文")
    marked = [(r.get(id_col) or "").strip() for r in rows
              if str(r.get("选中") or "").strip().lower()
              in ("是", "y", "yes", "1", "true", "✓", "√")]
    marked = [m for m in marked if m]
    print(f"已勾选 {len(marked)} 条（依据「选中」列，识别 是/y/yes/1/true/✓）")
    for m in marked:
        print(m)


# ---------------------------------------------------------------- 分区回填

def enrich_quartile_column(path, out_path=None, refresh=False, verbose=True):
    """查期刊分区并回填到表的「分区」列（就地更新，或另存到 out_path）。

    依赖 journal_quartile.lookup_detail()，它走 `searchts read`（只读、不落盘）。
    refresh=False 时只补「分区」列为空的行，已有的不动。
    """
    from openpyxl import load_workbook
    from journal_quartile import lookup_detail

    if not os.path.exists(path):
        raise SystemExit(f"[ERROR] 找不到表：{path}")

    wb = load_workbook(path)              # 需要写，不能 read_only
    ws = wb.active
    headers = [c.value for c in ws[1]]

    if "期刊" not in headers:
        raise SystemExit(f"[ERROR] 表中没有「期刊」列，无法查询分区。"
                         f"现有列：{headers}")
    v_idx = headers.index("期刊") + 1
    if "分区" in headers:
        q_idx = headers.index("分区") + 1
    else:
        q_idx = len(headers) + 1
        ws.cell(row=1, column=q_idx, value="分区")
        if verbose:
            print("[INFO] 表中原本没有「分区」列，已在末尾新增")

    # 收集待查行
    targets = []                          # (行号, 刊名)
    for r in range(2, ws.max_row + 1):
        v = ws.cell(row=r, column=v_idx).value
        v = v.strip() if isinstance(v, str) else ""
        if not v:
            continue
        cur = ws.cell(row=r, column=q_idx).value
        cur = cur.strip() if isinstance(cur, str) else ""
        if cur and not refresh:
            continue
        targets.append((r, v))

    if not targets:
        print("没有需要回填的行（分区列已全部有值；要全部重查加 --refresh）")
        return path

    uniq = sorted({n for _, n in targets})
    if verbose:
        print(f"待回填 {len(targets)} 行，涉及 {len(uniq)} 种期刊"
              f"（{'(全部重查)' if refresh else '(只补空值)'}）")

    qmap = {}
    for i, name in enumerate(uniq, 1):
        q, src = lookup_detail(name)
        qmap[name] = q
        if verbose:
            print(f"  [{i}/{len(uniq)}] {name} -> {q or 'unknown'}"
                  f"{('   [' + src + ']') if q else '   [' + src + ']'}")

    filled = 0
    for r, name in targets:
        q = qmap.get(name)
        if q:
            ws.cell(row=r, column=q_idx, value=q)
            filled += 1
        else:
            ws.cell(row=r, column=q_idx, value="unknown")
    # 分区列强制文本，避免 Q1/Q2 之类被 Excel 当别的类型
    for r in range(2, ws.max_row + 1):
        ws.cell(row=r, column=q_idx).number_format = "@"

    dest = out_path or path
    try:
        wb.save(dest)
    except PermissionError:
        raise SystemExit(f"[ERROR] 无法写入 {dest}：文件可能正被 Excel 打开。"
                         f"请关闭后重试，或用 --out 指定另一个文件名。")

    dist = {}
    for name in uniq:
        k = qmap.get(name) or "unknown"
        dist[k] = dist.get(k, 0) + 1

    if not verbose:
        # 省 token 模式：只出一两行，不逐刊打印
        print(f"[QUIET] 回填 {len(targets)} 行 / {len(uniq)} 刊 | "
              + " ".join(f"{k}={v}" for k, v in sorted(dist.items()))
              + f" | 已写回 {filled} 行")
        print(f"[QUIET] -> {dest}")
        unk = dist.get("unknown", 0)
        if unk:
            print(f"[QUIET][须告知用户] {unk} 刊未被 journalmetrics.org 收录，"
                  f"已标 unknown；按分区筛选时它们会被排除，可能漏检本属 Q1/Q2 的期刊")
        return dest

    print("-" * 60)
    print(f"已写回 {filled}/{len(targets)} 行 -> {dest}")
    print("期刊分区分布：" + "  ".join(f"{k}={v}" for k, v in sorted(dist.items())))
    unk = dist.get("unknown", 0)
    if unk:
        print(f"注意：{unk} 种期刊未在 journalmetrics.org 查到分区，已标记 unknown。"
              f"按分区筛选时它们会被排除，须向用户说明可能漏检本属 Q1/Q2 的期刊。")
    return dest


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(
        description="论文元数据表：按需读取 xlsx，避免整表进上下文")
    ap.add_argument("--xlsx", required=True, help="元数据表路径")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--stats", action="store_true", help="只输出统计（输出很小）")
    g.add_argument("--filter", action="store_true", help="按条件筛选并输出少量明细")
    g.add_argument("--selected", action="store_true", help="输出「选中」列勾选的 PaperID")
    g.add_argument("--from-json", metavar="JSON", help="从 json 生成 xlsx（一次性落盘）")
    g.add_argument("--enrich-quartile", action="store_true",
                   help="查期刊分区并回填到「分区」列")

    ap.add_argument("--year-min", type=int)
    ap.add_argument("--year-max", type=int)
    ap.add_argument("--min-citations", type=int)
    ap.add_argument("--quartile", help="逗号分隔，如 Q1,Q2")
    ap.add_argument("--title-contains")
    ap.add_argument("--venue-contains")
    ap.add_argument("--has-abstract", action="store_true")
    ap.add_argument("--has-doi", action="store_true")
    ap.add_argument("--select",
                    help="逗号分隔的列名，如 标题,期刊,分区；"
                         "**同时作用于显示与 --out 导出**（只要这几列）")
    ap.add_argument("--limit", type=int, default=20, help="最多显示多少条（默认 20）")
    ap.add_argument("--top-n", type=int,
                    help="筛选后**只取前 N 条**（按当前顺序，即 BGE 得分降序）；"
                         "与 --out 一起用可产出「一区最相关的前 10 篇」这类结果")
    ap.add_argument("--sort-by", help="先按某列排序再取（如 引用数、BGE分数）")
    ap.add_argument("--desc", action="store_true", help="配合 --sort-by 降序")
    ap.add_argument("--quiet", action="store_true",
                    help="**只输出一行统计**，不打印任何论文明细（省 token）")
    ap.add_argument("--out", help="把结果导出成新的 xlsx")
    ap.add_argument("--refresh", action="store_true",
                    help="配合 --enrich-quartile：连已有分区也一并重查")
    ap.add_argument("--prefix", default="论文元数据")
    ap.add_argument("--top", type=int, default=DEFAULT_TOTAL_LIMIT,
                    help=f"写入表的篇数上限（默认 {DEFAULT_TOTAL_LIMIT}；"
                         f"候选不足则有多少存多少；传 0 表示不限）")

    args = ap.parse_args()

    if args.from_json:
        with open(args.from_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        papers = data.get("papers") if isinstance(data, dict) else data
        if not papers:
            raise SystemExit(f"[ERROR] {args.from_json} 里没有 papers")
        out_dir = os.path.dirname(os.path.abspath(args.xlsx)) or "."
        p = save_papers_xlsx(papers, out_dir, prefix=args.prefix,
                             total_limit=args.top)
        print(f"[SAVE] -> {p}")
        return

    if args.stats:
        show_stats(args.xlsx)
        return

    if args.selected:
        show_selected(args.xlsx)
        return

    if args.enrich_quartile:
        enrich_quartile_column(args.xlsx, out_path=args.out, refresh=args.refresh,
                               verbose=not args.quiet)
        return

    headers, rows = read_table(args.xlsx)
    if args.select:                      # 先校验列名，避免筛完才报错
        require_fields(headers, [c.strip() for c in args.select.split(",")])
    matched, audit = apply_filters(rows, args, headers)

    # 可选：先排序（默认保持原顺序 = BGE 得分降序）
    if args.sort_by:
        require_fields(headers, [args.sort_by])
        matched = sort_rows(matched, args.sort_by, desc=args.desc)

    # 可选：只取前 N 条
    total_matched = len(matched)
    took = total_matched
    if args.top_n and args.top_n > 0:
        matched = matched[:args.top_n]
        took = len(matched)

    if args.quiet:
        # 只出一行，便于调用方低成本判断结果
        before = dict(audit).get("筛前", 0)
        ex = " ".join(f"{n}排除{v}" for n, v in audit if n != "筛前")
        print(f"[QUIET] 筛前 {before} | {ex} | 筛后 {total_matched} | 取前 {took}")
        if not args.out:
            print("[QUIET][WARN] 未指定 --out，结果未落盘（--quiet 不打印明细）")
    else:
        print("筛选审计：")
        for name, val in audit:
            if name == "筛前":
                print(f"  筛前        : {val} 条")
            else:
                print(f"  排除        : {val:5d} 条   <- {name}")
        print(f"  筛后        : {total_matched} 条"
              + (f"，本次只取前 {took} 条" if took != total_matched else ""))
        print("-" * 60)
        show_rows(matched, headers, select=args.select, limit=args.limit)

    if args.out:
        export_rows(matched, headers, args.out, select=args.select)


if __name__ == "__main__":
    main()
