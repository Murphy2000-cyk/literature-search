# -*- coding: utf-8 -*-
"""六环节检索流水线：检索 → 去重 → 近N年 → 记录合规 → 期刊等级 → 排序

设计要点
--------
1. **每个环节落一份 Excel**，行数递减，便于逐级核对。
2. **每个环节记录淘汰原因**，写进 `筛选淘汰记录.xlsx` 与 `筛选过程记录.md`。
   禁止静默丢行：静默丢行会让人误以为「该领域没文献」。
3. **顺序不可随意调**：
   - 检索阶段**不设年份**，保留全量原始数据，改 N 只改参数、不重花配额；
   - 去重必须在最前（身份层操作），否则后面每环节计数被重复记录污染；
   - 年份与记录合规都是本地免费操作，放在分区查询之前；
   - 分区查询最贵（easyScholar 限 2 次/秒，按 1 秒 1 次串行），必须最后做；
   - 摘要补全放在排序与截取之后，Elsevier / PubMed 调用量只跟最终篇数有关。
4. **筛选只对「有数据可判」的记录做判断**：缺字段的直接排除，原因类型记
   `无数据可判`，与 `不符合条件` 分开统计。摘要非硬性，能拿多少算多少。

用法
----
    python search_pipeline.py --out <目录> --topic "one sentence in English" \\
        "query one" "query two" "query three" "query four"

    python search_pipeline.py --out <目录> --topic "..." --years 5 \\
        --zone 1,2 --zone-basis xr --top 100 "q1" "q2" "q3" "q4"
"""
import argparse
import glob
import json
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paper_table as pt
from semantic_scholar_search import (
    SemanticScholarSearcher, ApiUnavailableError, year_window,
    assert_fields_cover, S2_FIELDS,
    check_keyword_count, check_rounds, MAX_KEYWORDS,
    DEFAULT_PUBLICATION_TYPES, DEFAULT_PER_ROUND,
)
from relevance_scorer import RelevanceScorer
from journal_quartile import zone_map_for_journals
import venue_rules
import keyword_fetch
import abstract_fill

# 已知的多义词陷阱：这些词在别的领域有完全不同的含义，裸用会把跨领域文献
# 一起召回。命中时只打提示，不改查询、不删结果 —— 判断权留给使用者。
AMBIGUOUS_TERMS = {
    "long-term memory": "也指 LSTM（long short-term memory）神经网络",
    "long term memory": "也指 LSTM（long short-term memory）神经网络",
    "working memory": "也是认知心理学的核心概念，会召回心理学实验论文",
    "agent": "也指 agent-based modeling（环境、经济领域的「主体建模」）",
    "transformer": "也指电力变压器",
    "attention": "也指心理学注意机制",
}


def check_query_ambiguity(queries):
    """提示查询里的多义词。**只提示，不改查询、不删结果。**"""
    hits = []
    for q in queries:
        low = (q or "").lower()
        for term, why in AMBIGUOUS_TERMS.items():
            if term in low:
                hits.append((q, term, why))
    if hits:
        print("[WARN] 查询里含多义词，可能召回跨领域文献（仅提示，不影响检索）：")
        for q, term, why in hits:
            print(f'  "{q}" 里的 `{term}`：{why}')
        print("  建议：把限定词一起写进查询，如 `LLM agent memory` 而不是裸的 `agent`")
    return hits


# 淘汰原因分两类：不符合条件（有数据但不满足）/ 无数据可判（缺字段不纳入）
TYPE_NO = "不符合条件"
TYPE_NA = "无数据可判"


# 六类**必得**字段（摘要除外，摘要能拿多少算多少）。
# 这里只做统计与告警，**不拦截流程** —— 空值是数据事实，不是错误，
# 但空在哪一类、空了多少，必须写在报告里让人看见。
REQUIRED_FIELDS = [
    ("题目", "title"), ("DOI", "doi"), ("作者", "authors"),
    ("期刊", "venue"), ("发表时间", "publication_date"), ("关键词", "keywords"),
]
BEST_EFFORT_FIELDS = [("摘要", "abstract")]


def field_completeness(records, stage=""):
    """统计必得字段的填充率，并列出缺失最多的几类。只报告，不拦截。"""
    n = len(records)
    if not n:
        return {}
    out = {}
    for label, key in REQUIRED_FIELDS + BEST_EFFORT_FIELDS:
        got = sum(1 for r in records
                  if str(r.get(key) or "").strip() and str(r.get(key)) != "0")
        out[label] = (got, n)
    print(f"[完整度] {stage}（{n} 行）")
    for label, (got, tot) in out.items():
        mark = "" if label == "摘要" else ("  ← 缺失" if got < tot else "")
        tag = "（非硬性）" if label == "摘要" else ""
        print(f"    {label:<8}{got:>6}/{tot}{tag}{mark}")
    hard_missing = [(k, v) for k, v in out.items()
                    if k != "摘要" and v[0] < v[1]]
    if hard_missing:
        detail = "、".join(f"{k} 缺 {v[1]-v[0]}" for k, v in hard_missing)
        print(f"[完整度] 必得字段存在缺失：{detail}；"
              f"汇报时必须说明这些空值是数据源缺失，不是筛选造成")
    return out


STAGES = [
    "第1环节_原始检索",
    "第2环节_去重",
    "第3环节_近N年",
    "第4环节_记录合规",
    "第5环节_期刊等级",
    "第6环节_相关度排序",
]


# ---------------------------------------------------------------- 工具

def per_query_limit(target, page_size=100):
    """每组关键词应取多少篇 = 目标篇数向上取整到整页。"""
    pages = max(1, -(-int(target) // page_size))
    return pages * page_size


_EXTRA_ALIAS = {
    "轮次": "round_no", "该轮关键词": "round_query",
    "命中轮次": "hit_rounds", "命中轮数": "hit_count",
    "年份判定依据": "year_basis", "淘汰环节": "drop_stage",
    "淘汰原因": "drop_reason",
}


def _rec(p, **extra):
    """PaperInfo → 表记录（英文标准键 + 中文扩展键）。"""
    d = {
        "paper_id": p.paper_id,
        "doi": p.doi,
        "title": p.title,
        "authors": p.authors,
        "year": p.year,
        "venue": p.venue,
        "citation_count": p.citation_count,
        "is_journal": p.is_journal,
        "is_conference": p.is_conference,
        "is_preprint": p.is_preprint,
        "pdf_url": p.pdf_url,
        "abstract": p.abstract,
        "quartile": p.quartile,
        "relevance_score": p.relevance_score,
        "publication_date": p.publication_date,
        "issn": p.issn,
        "keywords": p.keywords,
        "abstract_source": p.abstract_source,
        "is_retracted": p.is_retracted,
        "is_layout_record": p.is_layout_record,
        "is_suspicious": p.is_suspicious,
        "record_reasons": p.record_reasons,
        "zone_basis": p.zone_basis,
        "zone_status": p.zone_status,
    }
    # 调用方可能用中文键传扩展列，而 PREFERRED 里声明的是英文键。
    # 不做这一步的话 build_columns 会按不同的 key 各建一列，Excel 里出现同名重复列。
    for _k in list(extra):
        extra[_EXTRA_ALIAS.get(_k, _k)] = extra.pop(_k)
    d.update(extra)
    return d


def _raw_sink(raw_dir, tag):
    """返回一个把 S2 原始响应逐次落盘的钩子。"""
    os.makedirs(raw_dir, exist_ok=True)
    box = {"n": 0}

    def sink(url, params, payload):
        box["n"] += 1
        p = os.path.join(raw_dir, f"{tag}_p{box['n']}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    return sink


def _save(records, out_dir, name):
    """用 paper_table 的列定义落盘，并把文件名固定成 name.xlsx。"""
    target = os.path.join(out_dir, name + ".xlsx")
    path = None
    try:
        path = pt.save_papers_xlsx(records, out_dir, prefix=name,
                                   total_limit=None)
    except TypeError:
        path = pt.save_papers_xlsx(records, out_dir, prefix=name)
    if path and os.path.exists(path) and \
            os.path.abspath(path) != os.path.abspath(target):
        os.replace(path, target)
    elif not os.path.exists(target):
        cands = sorted(glob.glob(os.path.join(out_dir, name + "*.xlsx")))
        if cands:
            os.replace(cands[-1], target)
    return target


class Ledger:
    """筛选过程记录 + 筛选淘汰记录。"""

    def __init__(self):
        self.steps = []          # (环节, 进入数, 保留数, {原因: 篇数})
        self.dropped = []

    def add(self, stage, before, kept, reasons, dropped_rows=None):
        self.steps.append((stage, before, kept, reasons or {}))
        self.dropped.extend(dropped_rows or [])

    def write(self, out_dir, params):
        lines = ["# 筛选过程记录", ""]
        lines.append("## 本次参数")
        for k, v in params.items():
            lines.append(f"- {k}: {v}")
        lines.append("")
        lines.append("## 逐环节进出")
        lines.append("")
        lines.append("| 环节 | 进入 | 保留 | 淘汰 | 淘汰原因分布 |")
        lines.append("|---|---|---|---|---|")
        for stage, before, kept, reasons in self.steps:
            dist = "；".join(f"{k} {v}" for k, v in
                             sorted(reasons.items(), key=lambda x: -x[1])) or "-"
            lines.append(f"| {stage} | {before} | {kept} | "
                         f"{max(before - kept, 0)} | {dist} |")
        lines.append("")
        lines.append("> 本文件只记数量与原因分类。逐条明细见 `筛选淘汰记录.xlsx`，"
                     "其中「淘汰环节」「淘汰原因」两列可直接筛。")
        with open(os.path.join(out_dir, "筛选过程记录.md"), "w",
                  encoding="utf-8") as f:
            f.write("\n".join(lines))

        if self.dropped:
            try:
                pt.save_papers_xlsx(self.dropped, out_dir,
                                    prefix="筛选淘汰记录", total_limit=None)
                cands = sorted(glob.glob(os.path.join(
                    out_dir, "筛选淘汰记录*.xlsx")))
                if cands:
                    os.replace(cands[-1],
                               os.path.join(out_dir, "筛选淘汰记录.xlsx"))
            except Exception as e:
                print(f"[WARN] 淘汰记录落盘失败：{type(e).__name__}: {e}")


# ---------------------------------------------------------------- 各环节

def stage_search(searcher, queries, per_round, pub_types, out_dir=None,
                 verbose=True, raw_dir=None):
    """第 1 环节：多轮检索，不设年份。

    S2 不可用时**必须保全已取回结果再抛出**：既要拿 collected_partial()
    （中断那条查询已翻到的页），也要拿前面已完整取回的轮次，并把完整
    性报告与部分结果都落到 out_dir。**不能只抛不存** —— 那会让前几轮
    已经花掉的请求全部白费，是 2026-11-02 实测踩到的真实缺陷。
    """
    groups = []                 # [(query, [papers])]
    merged, rounds_of, query_of = {}, {}, {}
    for i, q in enumerate(queries, 1):
        if raw_dir:
            searcher.raw_sink = _raw_sink(raw_dir, f"r{i}")
        try:
            ps = searcher.search_paginated(query=q, total_limit=per_round,
                                          publication_types=pub_types)
        except ApiUnavailableError:
            dest = out_dir or os.path.dirname(os.path.abspath(__file__))
            try:
                searcher.save_search_report(dest)
            except Exception:
                pass
            salvaged = list(searcher.collected_partial())
            seen = {p.paper_id for p in salvaged}
            for _q, _ps in groups:
                for p in _ps:
                    if p.paper_id not in seen:
                        seen.add(p.paper_id)
                        salvaged.append(p)
            for p in salvaged:
                merged.setdefault(p.paper_id, p)
                rounds_of.setdefault(p.paper_id, [i - 1])
                query_of.setdefault(p.paper_id, q)
            if salvaged:
                try:
                    _save([_rec(p) for p in salvaged], dest,
                          "部分结果_中断前已取回")
                    if verbose:
                        print(f"[保全] 中断前已取回的 {len(salvaged)} 篇已落盘"
                              f" -> {dest}")
                except Exception as e:
                    print(f"[WARN] 部分结果落盘失败：{type(e).__name__}: {e}")
            raise
        groups.append((q, ps))
        for p in ps:
            if p.paper_id not in merged:
                merged[p.paper_id] = p
                rounds_of[p.paper_id] = [i - 1]
                query_of[p.paper_id] = q
            elif (i - 1) not in rounds_of[p.paper_id]:
                rounds_of[p.paper_id].append(i - 1)
        if verbose:
            flag = "" if len(ps) >= 50 else "  ← 偏少，考虑在该轮换措辞"
            print(f"[检索] 第 {i} 轮 -> {len(ps)} 篇，累计唯一 {len(merged)}{flag}")
        time.sleep(0.2)
    return groups, merged, rounds_of, query_of


def stage_years(papers, years):
    """第 3 环节：近 N 年（滑动窗口，只设下界）。"""
    lower, floor_year = year_window(years)
    kept, dropped = [], []
    for p in papers:
        d = (p.publication_date or "").strip()
        ok = None
        basis = ""
        if len(d) >= 10:
            try:
                ok = date.fromisoformat(d[:10]) >= lower
                basis = "日期"
            except ValueError:
                ok = None
        if ok is None:
            if p.year:
                ok = p.year >= floor_year
                basis = "年份"
            else:
                ok = False
                basis = "缺失"
        p.year_basis = basis
        if ok:
            kept.append(p)
        elif basis == "缺失":
            dropped.append((p, TYPE_NA, "无发表时间与年份，无法判定"))
        else:
            dropped.append((p, TYPE_NO, f"早于窗口下界 {lower}"))
    return kept, dropped, lower


def stage_compliance(papers, allow_preprint=False, allow_conference=False):
    """第 4 环节：剔除与标记。

    默认两个开关都关，即「只要期刊论文」（排除会议论文与预印本）。
    `allow_preprint` / `allow_conference` 可分别放行。会议录卷级条目与
    撤稿不受开关影响，永远剔除。
    """
    kept, dropped = [], []
    reasons = {}
    for p in papers:
        cls = venue_rules.classify_record(
            title=p.title, venue=p.venue,
            publication_types=(["Conference"] if p.is_conference else []),
            external_ids=({"ArXiv": 1} if p.is_preprint else {}),
            doi=p.doi, abstract=p.abstract, year=p.year)
        p.is_retracted = bool(cls["is_retracted"])
        p.is_layout_record = bool(cls["is_layout_record"])
        p.is_suspicious = bool(cls["is_suspicious"])
        p.record_reasons = ";".join(cls["reasons"])
        if venue_rules.should_drop(cls, allow_preprint=allow_preprint,
                                   allow_conference=allow_conference):
            key = "其他"
            for r in cls["reasons"]:
                if r.startswith(("会议", "预印本", "撤稿")):
                    key = r
                    break
            dropped.append((p, TYPE_NO, key))
            continue
        # 既无 publicationTypes 又无刊名 → 无法判定是否会议/预印本 → 按新语义排除
        if (not (p.venue or "").strip() and not p.is_journal
                and not p.is_conference and not p.is_preprint):
            dropped.append((p, TYPE_NA, "无文献类型与刊名，无法判定"))
            continue
        kept.append(p)
    reasons = {}
    for _p, _t, _why in dropped:
        k = f"[{_t}] {_why}"
        reasons[k] = reasons.get(k, 0) + 1
    return kept, dropped, reasons


def stage_zones(papers, basis, refresh=False, verbose=True):
    """第 5 环节：查分区并回填。分区为空 = 未查询到，不等于等级不够。"""
    name_to_doi, issns = {}, {}
    for p in papers:
        v = (p.venue or "").strip()
        if v and v not in name_to_doi:
            name_to_doi[v] = p.doi or ""
            if p.issn:
                issns[v] = p.issn
    if not name_to_doi:
        print("[分区] 没有任何刊名，跳过查询")
    else:
        zmap = zone_map_for_journals(name_to_doi, refresh=refresh,
                                     verbose=verbose, issns=issns, basis=basis)
        for p in papers:
            v = (p.venue or "").strip()
            p.quartile = zmap.get(v) or ""
    for p in papers:
        p.zone_basis = basis
        p.zone_status = ("已收录" if p.quartile
                         else ("刊名为空" if not (p.venue or "").strip()
                               else "未查询到"))
    return papers


def stage_rank(papers, topic, use_bge=True, w_title=0.6, w_kw=0.4,
               top_n=None):
    """第 6 环节前半：OpenAlex 取关键词与摘要 → BGE 排序 → 可选截取。"""
    if not papers:
        return papers, []
    dois = [p.doi for p in papers if p.doi]
    if dois:
        print(f"[OpenAlex] 按 DOI 批量取关键词与摘要（{len(dois)} 个 DOI）")
        data = keyword_fetch.fetch_by_dois(dois, verbose=True)
        n_kw = n_ab = 0
        for p in papers:
            d = data.get(keyword_fetch.norm_doi(p.doi))
            if not d:
                continue
            if d["keywords_text"]:
                p.keywords = d["keywords_text"]
                n_kw += 1
            if d["abstract"] and not (p.abstract or "").strip():
                p.abstract = d["abstract"]
                p.abstract_source = "OpenAlex"
                n_ab += 1
        print(f"[OpenAlex] 补关键词 {n_kw} 篇 | 补摘要 {n_ab} 篇")

    if use_bge and topic:
        with RelevanceScorer(lang="en") as sc:
            papers = sc.rank_topic(topic, papers, w_title=w_title, w_kw=w_kw)
    else:
        print("[BGE] 跳过排序（--no-bge 或 --topic 为空）")

    truncated = []
    if top_n and len(papers) > top_n:
        truncated = papers[top_n:]
        papers = papers[:top_n]
        print(f"[截取] 前 {top_n} 篇用于补摘要，其余 {len(truncated)} 篇留在全量表")
    return papers, truncated


def stage_abstracts(papers, verbose=True):
    """第 6 环节后半：Elsevier → PubMed 两档，只处理缺摘要的行。

    OpenAlex 那一档已在取关键词时顺带完成，不重复调用。
    """
    todo = [p for p in papers if not (p.abstract or "").strip() and p.doi]
    if not todo:
        print("[摘要] 没有需要补的行")
        return
    key = abstract_fill.load_key()
    print(f"[摘要] 待补 {len(todo)} 篇；"
          f"Elsevier key {'已找到' if key else '未找到'}")
    for i, p in enumerate(todo, 1):
        ab, src, log = abstract_fill.fill_one(p.doi, key=key)
        if ab:
            p.abstract = ab
            p.abstract_source = src
        if verbose:
            tag = f"OK {src}" if ab else "MISS " + "; ".join(log)[:56]
            print(f"  [{i}/{len(todo)}] {tag} | {(p.title or '')[:52]}")


# ---------------------------------------------------------------- 主流程

def run(queries, topic, out_dir, per_round=None, years=10,
        zone_basis="jcr", zone_filter=("1",), top_n=None,
        use_bge=True, refresh_zone=False, publication_types=None,
        max_keywords=None, strict=True, searcher=None, verbose=True,
        allow_preprint=False, allow_conference=None, domain="general"):
    """跑完六个环节，返回本次产物路径字典。

    ⚠️ 本函数返回后**请让进程结束**，否则 BGE 占的内存不会归还。
    """
    os.makedirs(out_dir, exist_ok=True)
    pub_types = publication_types or DEFAULT_PUBLICATION_TYPES
    per_round = per_round or DEFAULT_PER_ROUND
    max_keywords = max_keywords or MAX_KEYWORDS
    # 领域默认值：计算机领域以会议为主要发表渠道，默认放行单篇会议论文；
    # 其它领域仍默认排除。显式传 allow_conference 时以传入值为准。
    if allow_conference is None:
        allow_conference = (str(domain).strip().lower() == "cs")
    if str(domain).strip().lower() == "cs":
        print("[DOMAIN] 领域=cs：默认放行单篇会议论文"
              "（预印本仍默认排除，需要时加 --allow-preprint）")
    ledger = Ledger()
    out = {}

    # 字段存在性断言：请求字段必须覆盖解析字段，缺一个立刻失败
    assert_fields_cover(S2_FIELDS)

    # 规范校验：轮数与关键词数（不合规直接拦，避免静默少召回）
    check_rounds(queries, strict=strict)
    for q in queries:
        check_keyword_count(q, max_keywords=max_keywords, strict=strict)

    check_query_ambiguity(queries)

    pages = max(1, -(-int(per_round) // 100))
    if verbose:
        print(f"[PIPELINE] {len(queries)} 轮 × 每轮 {per_round} 篇（{pages} 页）"
              f"→ 候选池最多 {per_round * len(queries)} 篇")
        print(f"[PIPELINE] 预计 {pages * len(queries)} 次 S2 请求")

    s = searcher or SemanticScholarSearcher()

    # ---- 第 1 环节 ----
    groups, merged, rounds_of, query_of = stage_search(
        s, queries, per_round, pub_types, out_dir=out_dir, verbose=verbose,
        raw_dir=os.path.join(out_dir, "raw"))
    # 第 1 环节落**原始返回行**：同一篇被多轮召回就出现多行，重复留给第 2 环节去化。
    # 这样「检索 -> 去重」才是两个有实际数据变化的环节。
    rec1 = []
    for i, (q, ps) in enumerate(groups, 1):
        for p in ps:
            rec1.append(_rec(p, round_no=i, round_query=q))
    _seen1 = {(r.get("paper_id"), r.get("round_no")) for r in rec1}
    for pid, p in merged.items():          # 失败中断时 groups 不完整，补上保全的部分
        for idx in rounds_of.get(pid, [0]):
            if (pid, idx + 1) not in _seen1:
                rec1.append(_rec(p, round_no=idx + 1,
                                 round_query=query_of.get(pid, "")))
    out["stage1"] = _save(rec1, out_dir, STAGES[0])
    field_completeness(rec1, "第1环节_原始检索")
    ledger.add(STAGES[0], len(rec1), len(rec1), {})
    try:
        s.save_search_report(out_dir)
    except Exception:
        pass

    if not merged:
        raise RuntimeError(
            "各轮查询均未取回任何文献 —— 这属于「确实未命中」，不是检索失败；"
            "请连同所用关键词向用户说明。")

    # ---- 第 2 环节：真去重。按 paper_id 保留首次出现，累计命中轮次 ----
    order, agg = [], {}
    for r in rec1:
        pid = r.get("paper_id")
        if pid not in agg:
            agg[pid] = {"rec": dict(r), "rounds": []}
            order.append(pid)
        rn = r.get("round_no")
        if rn and rn not in agg[pid]["rounds"]:
            agg[pid]["rounds"].append(rn)
    rec2 = []
    for pid in order:
        base = agg[pid]["rec"]
        rounds = sorted(agg[pid]["rounds"])
        base["hit_rounds"] = ",".join(str(x) for x in rounds)
        base["hit_count"] = len(rounds)
        rec2.append(base)
    papers = [merged[pid] for pid in order if pid in merged]
    out["stage2"] = _save(rec2, out_dir, STAGES[1])
    ledger.add(STAGES[1], len(rec1), len(rec2),
               {"重复记录": max(len(rec1) - len(rec2), 0)})

    # ---- 第 3 环节 ----
    before = len(papers)
    papers, dropped, lower = stage_years(papers, years)
    out["stage3"] = _save([_rec(p, 年份判定依据=p.year_basis) for p in papers],
                          out_dir, STAGES[2])
    r3 = {}
    for _p, _t, _why in dropped:
        k = f"[{_t}] {_why}"
        r3[k] = r3.get(k, 0) + 1
    ledger.add(STAGES[2], before, len(papers), r3,
               [dict(_rec(_p), 淘汰环节=STAGES[2], 淘汰原因=_why, 淘汰原因类型=_t)
                for _p, _t, _why in dropped])

    # ---- 第 4 环节 ----
    before = len(papers)
    papers, dropped, reasons = stage_compliance(
        papers, allow_preprint=allow_preprint,
        allow_conference=allow_conference)
    out["stage4"] = _save([_rec(p) for p in papers], out_dir, STAGES[3])
    ledger.add(STAGES[3], before, len(papers), reasons,
               [dict(_rec(_p), 淘汰环节=STAGES[3], 淘汰原因=_why, 淘汰原因类型=_t)
                for _p, _t, _why in dropped])

    # ---- 第 5 环节 ----
    before = len(papers)
    papers = stage_zones(papers, zone_basis, refresh=refresh_zone,
                         verbose=verbose)
    want = {z.strip().replace("区", "")
            for z in zone_filter} if zone_filter else set()
    kept5, drop5 = [], []
    for p in papers:
        if not (p.venue or "").strip():
            drop5.append((p, TYPE_NA, "无期刊名，无法查分区"))
        elif not p.quartile:
            drop5.append((p, TYPE_NA, "分区未查询到（该刊未被分区库收录）"))
        elif want and p.quartile not in want:
            drop5.append((p, TYPE_NO, f"分区 {p.quartile} 区不在 {sorted(want)}"))
        else:
            kept5.append(p)
    out["stage5"] = _save([_rec(p) for p in kept5], out_dir, STAGES[4])
    zr = {}
    for _p, _t, _why in drop5:
        k = f"[{_t}] {_why}"
        zr[k] = zr.get(k, 0) + 1
    ledger.add(STAGES[4], before, len(kept5), zr,
               [dict(_rec(_p), 淘汰环节=STAGES[4], 淘汰原因=_why,
                     淘汰原因类型=_t) for _p, _t, _why in drop5])
    papers = kept5

    # ---- 第 6 环节 ----
    before = len(papers)
    papers, truncated = stage_rank(papers, topic, use_bge=use_bge, top_n=top_n)
    stage_abstracts(papers, verbose=verbose)
    rec6 = [_rec(p) for p in papers]
    out["stage6"] = _save(rec6, out_dir, STAGES[5])
    field_completeness(rec6, "第6环节_相关度排序")
    if truncated:
        out["stage6_full"] = _save([_rec(p) for p in papers + truncated],
                                   out_dir, STAGES[5] + "_全量")
    ledger.add(STAGES[5], before, len(papers), {})

    params = {
        "检索轮数": len(queries),
        "每轮上限": per_round,
        "近 N 年": f"{years}（下界 {lower}，只设下界，不设上界）",
        "分区标准": zone_basis,
        "放行预印本": "是" if allow_preprint else "否（默认）",
        "放行会议论文": "是" if allow_conference else "否（默认）",
        "领域": domain,
        "分区条件": sorted(want) if want else "不限",
        "排序方式": (f"BGE 单句主题描述（标题 {0.6} + 关键词 {0.4}）"
                     if use_bge else "未排序"),
        "主题描述": topic or "（空）",
        "截取": f"前 {top_n} 篇" if top_n else "不截取",
        "统计窗口": f"{lower} 至 {date.today()}",
    }
    ledger.write(out_dir, params)
    return out


def main():
    ap = argparse.ArgumentParser(description="六环节文献检索流水线")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--topic", default="",
                    help="一句英文主题描述，仅供 BGE 排序（与检索词分开）")
    ap.add_argument("--per-round", type=int, default=None,
                    help="每轮取回上限（默认 300 = 3 页）")
    ap.add_argument("--years", type=int, default=10, help="近 N 年（默认 10）")
    ap.add_argument("--zone-basis", choices=["jcr", "xr"], default="jcr",
                    help="分区标准：jcr（默认）或 xr（中科院新锐）")
    ap.add_argument("--zone", default="1",
                    help="分区条件，逗号分隔，如 1 或 1,2；空串=不限")
    ap.add_argument("--top", type=int, default=None,
                    help="只对前 N 篇补摘要（Elsevier/PubMed），默认全部")
    ap.add_argument("--no-bge", action="store_true", help="不加载 BGE 排序")
    ap.add_argument("--refresh-zone", action="store_true", help="分区全部重查")
    ap.add_argument("--allow-preprint", action="store_true",
                    help="放行预印本（arXiv/SSRN 等）。默认排除。"
                         "计算机等领域预印本是主要载体")
    ap.add_argument("--allow-conference", action="store_true",
                    help="放行单篇会议论文。默认排除（--domain cs 时默认放行）。"
                         "会议录卷级条目与撤稿不受此开关影响，仍会剔除")
    ap.add_argument("--domain", choices=["general", "cs"], default="general",
                    help="检索领域。general（默认）排除会议论文；"
                         "cs = 计算机领域，默认放行会议论文"
                         "（计算机以会议为主要发表渠道）")
    ap.add_argument("--loose", action="store_true",
                    help="放宽校验（轮数非 3~4 或关键词超 5 个时只警告）")
    ap.add_argument("--allow-letpub", action="store_true",
                    help="允许 LetPub 兜底（须用户同意）")
    ap.add_argument("--json", help="把产物路径写到这个 json")
    ap.add_argument("queries", nargs="+", help="英文关键词组，每组 ≤5 词")
    a = ap.parse_args()

    if a.allow_letpub:
        import journal_quartile
        journal_quartile.ALLOW_LETPUB = True

    zone_filter = tuple(z.strip() for z in (a.zone or "").split(",") if z.strip())
    try:
        paths = run(a.queries, a.topic, a.out, per_round=a.per_round,
                    years=a.years, zone_basis=a.zone_basis,
                    zone_filter=zone_filter, top_n=a.top,
                    use_bge=not a.no_bge, refresh_zone=a.refresh_zone,
                    strict=not a.loose,
                    allow_preprint=a.allow_preprint,
                    allow_conference=(a.allow_conference or a.domain == "cs"),
                    domain=a.domain)
    except ApiUnavailableError as e:
        print(f"[FATAL] {e}")
        print("[ACTION] 立即向用户提出二选一：")
        print("  1) 等待 3~5 分钟后重试（首选）")
        print("  2) 改用 Crossref 备用源，并必须说明其缺失：只能检索到有 DOI 的文献、"
              "摘要覆盖低、只返回相关性前 N 条、覆盖偏向有 DOI 的英文期刊")
        raise SystemExit(2)
    for k, v in paths.items():
        print(f"[OUT] {k}: {v}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(paths, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
