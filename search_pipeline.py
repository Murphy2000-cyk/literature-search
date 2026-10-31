# -*- coding: utf-8 -*-
"""文献检索一步流程：检索 → BGE 每组独立评分 → 合并排序 → 落盘 Excel

为什么要有这个模块
------------------
**内存生命周期是这里最重要的约束。**

实测（2026-09-13，`bge-base-en-v1.5`，Windows/CPU）：

| 状态 | 进程内存 |
|---|---|
| 什么都没加载 | 30 MB |
| `import torch` + `sentence_transformers` | **732 MB** |
| 再加载英文模型 | 865 MB |
| `del` 引用 + `gc.collect()` | 858 MB（**只还了 7 MB**）|
| 清 transformers 内部缓存 | 858 MB（无变化）|
| Windows `SetProcessWorkingSetSize` 修剪 | 1 MB（**假象**：再分配内存后回到 250 MB）|

**结论：进程内无法把 torch 占的内存真正还给操作系统，只有进程退出才能全部释放。**

因此本模块的设计是：
- **一次调用做完整个检索任务**（检索 → 评分 → 排序 → 落盘），
  BGE 只加载一次、**整个任务期间持续激活、不重复加载**；
- 调用返回后**请让进程结束** —— 内存随进程退出全部归还。
- 不要在长期存活的进程里反复调用本模块再指望 `unload()` 释放内存。

用法
----
**标准检索（用户 2026-09-20 指定、10-30 调参）：3~4 轮关键词，每轮 ≤5 词，各取 500 篇（5 页）。**

    python search_pipeline.py --out <目录> \
        "external wall insulation non-destructive testing" \
        "infrared thermography facade insulation debonding" \
        "ground penetrating radar insulation bonding" \
        "insulation layer defect ultrasonic detection"

或编程调用：

    from search_pipeline import run
    res = run(QUERIES, OUT)          # 默认 每轮500篇（5页）/ 入表不截断
    print(res["api_calls"], res["rounds"])

**不做探针轮** —— 轮的"微调变体"本身就承担试探作用。
关键词超 5 个或轮数不在 3~4 之间会**直接报错**（`--loose` 可降级为警告）。

API 调用预算：3~4 轮 × 2 页 = **6~8 次请求**（+ 少量 429 重试）。
检索响应已直接带回 DOI / 摘要 / OA 链接，**不需要再跑 Phase 2 补详情**。

知识库的常驻服务（端口 8767）用的是**中文**分支、在**它自己的进程**里，
和本模块用的英文分支互不影响；本模块所在进程退出后内存即归还。

⚠️ 澄清一个容易产生的误解：「关掉 torch」不会影响别的程序
--------------------------------------------------------
**torch 不是全局开关，而是每个 Python 进程各自 import 的一份库。**
本模块所在进程退出时，操作系统只回收**这个进程**的内存，
**不会触碰**知识库常驻服务那个进程里的 torch 与中文模型。

2026-09-13 实测（三次快照）：

```
实验前 : 端口 8767 监听中，PID=25604，832 MB；查询正常（命中 5，返回 9 条）
实验中 : PID 14992  865 MB   ← 本模块进程（英文 BGE）
         PID 25604  832 MB   ← 知识库常驻服务（中文 BGE）  ← 两个独立进程
实验后 : 端口 8767 仍监听中，PID 仍是 25604（未被重启），832 MB；查询结果完全一致
```

**两个 PID、两份 torch、两份模型，各自独立。** 停掉一边对另一边零影响。

**但内存总量确实会叠加**（这才是真正需要留意的）：

| | 常驻 | 跑论文检索时 |
|---|---|---|
| 知识库常驻服务（中文 BGE） | 832 MB | 832 MB |
| 论文检索进程（英文 BGE） | 0 | 865 ~ 1260 MB |
| **合计** | 832 MB | **约 1.7 ~ 2.1 GB** |

若跑论文检索时想腾出内存，可临时停掉知识库常驻服务
（`python search_reflect.py --stop-daemon`，释放约 825 MB），
下次用知识库检索时它会**自动拉起**，无需手动重启。
"""
import os
import sys
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def per_query_limit(target, page_size=100):
    """每组关键词应取多少篇 = 目标篇数向上取整到整页。

    目标 200 -> 200（2 页）；目标 300 -> 300（3 页）。
    """
    pages = max(1, -(-int(target) // page_size))
    return pages * page_size


def run(queries, out_dir, per_round=None, top_n=None, prefix="论文元数据",
        use_bge=True, publication_types=None, max_keywords=None,
        strict=True, target=None, gate_profile=None, gate_min_pass=20,
        gate_min_rate=0.10, searcher=None, scorer=None, verbose=True):
    """标准检索：3~4 轮关键词（每轮 ≤5 词，各取 500 篇）→ BGE 每组独立评分
    → 合并排序 → 落盘 Excel。

    ⚠️ 标准流程（用户 2026-09-20 指定，务必遵守）
    ------------------------------------------------
    1. **不做探针轮**。"微调变体"本身承担试探作用，不要再单独发请求试词。
    2. **每轮关键词 ≤5 个**，超限直接报错。S2 是相关性检索，写长 = 召回暴跌
       （实测 7 词→100 篇，9 词→11 篇，13 词→6 篇，见 AGENTS.md §3.5.4.1）。
    3. **固定 3~4 轮**，每轮是同一题目的一次**微调**（换角度/同义词/换手段），
       各取 500 篇（5 页）。
    4. **API 调用预算**：3~4 轮 × 2 页 = **6~8 次请求**，加少量 429 重试即完成。
       检索响应已直接带回 DOI/摘要/OA 链接，**不再需要 Phase 2 逐篇补详情**。
    5. **S2 不可用即刻上报**：任一轮失败即保全已取回结果、落盘完整性报告并抛出，
       由调用方立即向用户汇报（不静默、不换源、不用匿名请求）。

    Args:
        queries: 3~4 条关键词（每条 ≤5 词），互为微调变体
        out_dir: 产出目录
        per_round: **每轮**取回篇数（默认 500 = 5 页）
        top_n: **入表**篇数上限，合并去重后取前 N（默认 None = 不截断）
        prefix: 表文件名前缀
        use_bge: True 用 BGE 全局排序；False 退回轮转融合（不加载模型）
        publication_types: 默认 ["JournalArticle", "Review"]（排除会议）
        max_keywords: 单轮关键词上限，默认 MAX_KEYWORDS（5）
        strict: True = 轮数/关键词数不合规直接报错
        target: **已废弃**，仅为兼容旧调用；等价于同时指定 per_round 与 top_n
        gate_profile: **主题闸门配置**（None = 不过闸门）。
            传 profile id（如 "building-envelope-ndt"）或 profile dict。
            ⚠️ **每次检索都应为本次题目准备一条 profile：2~3 个闸门、每闸门约 50 词。**
            内置的只覆盖「建筑外保温无损检测」，用错题目会把候选池杀光。
            闸门按**标题 + 摘要**判（摘要随检索已带回），顺序是
            「全量排序 → 过闸门 → 截断到 top_n」，避免名额浪费。
        gate_min_pass / gate_min_rate: 过窄自检阈值；放行低于阈值会打告警
        searcher / scorer: 仅测试用，可注入替身

    Returns:
        {"papers": 入表篇数, "path": xlsx 路径, "report": 完整性报告,
         "rounds": [(查询, 篇数), ...], "api_calls": 请求次数}

    ⚠️ 本函数返回后**请让进程结束**，否则 BGE 占的内存不会归还（见模块 docstring）。
    """
    from semantic_scholar_search import (
        SemanticScholarSearcher, ApiUnavailableError, check_keyword_count,
        check_rounds, MAX_KEYWORDS, DEFAULT_PER_ROUND, DEFAULT_TOTAL_LIMIT)

    if publication_types is None:
        publication_types = ["JournalArticle", "Review"]
    if max_keywords is None:
        max_keywords = MAX_KEYWORDS

    # 兼容旧调用：target 同时决定每轮篇数与入表篇数
    if target is not None:
        per_round = per_round or per_query_limit(target)
        top_n = top_n or target
    per_round = per_round or DEFAULT_PER_ROUND
    top_n = top_n or DEFAULT_TOTAL_LIMIT

    # ---- 规范校验：轮数与关键词数（不合规直接拦，避免静默少召回）----
    check_rounds(queries, strict=strict)
    for q in queries:
        check_keyword_count(q, max_keywords=max_keywords, strict=strict)

    pages = max(1, -(-int(per_round) // 100))
    est_calls = pages * len(queries)
    if verbose:
        print(f"[PIPELINE] 标准检索：{len(queries)} 轮 × 每轮 {per_round} 篇"
              f"（{pages} 页）→ 候选池最多 {per_round * len(queries)} 篇")
        print(f"[PIPELINE] 预计 {est_calls} 次 S2 请求（+少量 429 重试）；"
              f"检索响应已含 DOI/摘要，无需 Phase 2 补取")
        for i, q in enumerate(queries, 1):
            n, _ = check_keyword_count(q, max_keywords=max_keywords, strict=False)
            print(f"[PIPELINE]   第 {i} 轮（{n} 词）：{q}")

    s = searcher or SemanticScholarSearcher()
    groups, rounds_yield = [], []
    try:
        for i, q in enumerate(queries, 1):          # 串行，不并发
            ps = s.search_paginated(query=q, total_limit=per_round,
                                    publication_types=publication_types)
            groups.append((q, ps))
            rounds_yield.append((q, len(ps)))
            if verbose:
                flag = "" if len(ps) >= 50 else "  ← 偏少，考虑在该轮换措辞"
                print(f"[PIPELINE] 第 {i} 轮 -> {len(ps)} 篇{flag}")
    except ApiUnavailableError:
        # 见 AGENTS.md §4.1：暂停检索、立即向用户汇报，不擅自换源、不用匿名请求
        try:
            s.save_search_report(out_dir)           # 失败也要留痕
        except Exception:
            pass
        # ⚠️ **必须保全中断前已取回的部分结果** —— AGENTS.md 的硬性要求「失败不得丢数据」。
        # 2026-09-20 实测踩坑：早期这里只存报告、不存论文，导致前 3 轮已取回的 700 篇
        # 随进程退出全部丢失，重跑时白花 8 次 S2 请求。
        try:
            # 两处都要取：
            #   ① collected_partial() —— "中断那条查询"在报错前已翻到的页
            #   ② groups —— **已完整取回的轮次**（早期只取①，实测导致
            #      第 1、2 轮已到手的 400 篇全部白丢，重跑又花 8 次请求）
            salvaged = list(s.collected_partial())
            _seen = {getattr(p, "paper_id", "") for p in salvaged}
            for _q, _ps in groups:
                for p in _ps:
                    _pid = getattr(p, "paper_id", "")
                    if _pid not in _seen:
                        _seen.add(_pid)
                        salvaged.append(p)
            if salvaged:
                from paper_table import save_papers_xlsx
                _p = save_papers_xlsx(salvaged, out_dir,
                                      prefix="部分结果_中断前已取回",
                                      sheet_title="部分结果", total_limit=None)
                if verbose:
                    print(f"[PIPELINE] 已保全中断前取回的 {len(salvaged)} 篇 -> {_p}")
        except Exception as _e:
            print(f"[PIPELINE] [WARN] 部分结果保全失败：{_e}")
        if verbose:
            print("[PIPELINE] ⚠️ S2 不可用，检索已暂停。已取回结果已保全，"
                  "请立即向用户汇报并询问：等待恢复，还是降级 Crossref（最后手段）。")
        raise

    rep = s.search_report() if hasattr(s, "search_report") else {}
    try:
        s.save_search_report(out_dir)
    except Exception as e:
        if verbose:
            print(f"[PIPELINE] [WARN] 完整性报告落盘失败：{e}")

    if not any(ps for _, ps in groups):
        raise RuntimeError(
            "3~4 轮查询均未取回任何文献 —— 这属于「确实未命中」，不是检索失败；"
            "请连同所用关键词向用户说明。")

    # ---- 排序：BGE 每组独立评分 → 合并去重 → 全局降序 ----
    # 先做**全量排序**，再过闸门，最后才截断到 top_n ——
    # 顺序颠倒会让被闸门剔除的名额白白浪费掉。
    if use_bge:
        from relevance_scorer import RelevanceScorer
        sc = scorer or RelevanceScorer(lang="en")     # 论文检索固定英文分支
        papers = sc.rank_groups(groups, total_limit=None)
    else:
        from paper_table import interleave_by_query
        # ⚠️ interleave_by_query 要的是 {查询: [论文]} 或 [[论文], ...]，
        # **不能直接喂 groups**（那是 [(查询, [论文]), ...]）——
        # 否则每个元组会被当成一个「桶」，轮转时把查询字符串和论文列表
        # 本身当成论文取出来（实测报 "无法识别的论文对象类型: <class 'str'>"）。
        papers = interleave_by_query([ps for _, ps in groups], total_limit=None)
        if verbose:
            print("[PIPELINE] 未启用 BGE，改用轮转融合")

    # ---- 主题闸门（按【标题 + 摘要】判；用户 2026-09-20 指定接入流水线）----
    gate_stat = None
    if gate_profile is not None:
        from topic_gate import apply_gate
        before = len(papers)
        passed, rejected, gwarn = apply_gate(
            papers, gate_profile, min_pass=gate_min_pass,
            min_rate=gate_min_rate)
        papers = [p for p, _ in passed]
        by_title = sum(1 for _, i in passed if i.get("_source") == "标题")
        gate_stat = {"before": before, "after": len(papers),
                     "rejected": len(rejected),
                     "by_title": by_title,
                     "by_abstract": len(papers) - by_title}
        if verbose:
            print(f"[GATE] 主题闸门（标题+摘要）：{before} → {len(papers)} 篇"
                  f"（剔除 {len(rejected)}；其中靠标题命中 {by_title}、"
                  f"靠摘要才命中 {len(papers) - by_title}）")
        if gwarn:
            print(f"[GATE] {gwarn}")
        if not papers:
            raise RuntimeError(
                "主题闸门把所有候选都剔除了：这几乎一定是**闸门配置过窄/用错主题**，"
                "不是「该领域没有文献」。请检查 gate_profile，或先用 --loose 看原始排序。")

    papers = papers[:top_n] if top_n else papers

    # ---- 落盘 ----
    from paper_table import save_papers_xlsx
    path = save_papers_xlsx(papers, out_dir, prefix=prefix, total_limit=top_n)

    calls = getattr(s, "request_count", None)
    if verbose:
        try:
            import psutil
            mb = psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
            print(f"[PIPELINE] 本进程当前占用 {mb:.0f} MB（含 BGE）；"
                  f"**请让本进程结束以归还内存**")
        except Exception:
            pass
        print(f"[PIPELINE] 本次共 {calls} 次 S2 请求")
        print(f"[PIPELINE] 完成 -> {path}")

    return {"papers": len(papers), "path": path, "report": rep,
            "rounds": rounds_yield, "api_calls": calls, "gate": gate_stat}


def main():
    ap = argparse.ArgumentParser(
        description="标准文献检索（3~4 轮 ≤5 词关键词 → BGE 每组独立评分 → "
                    "合并排序 → 落盘 Excel）")
    ap.add_argument("queries", nargs="+",
                    help="3~4 轮关键词，每轮 ≤5 词，互为微调变体")
    ap.add_argument("--out", required=True, help="产出目录")
    ap.add_argument("--per-round", type=int, default=None,
                    help="每轮取回篇数（默认 500 = 5 页）")
    ap.add_argument("--top-n", type=int, default=None,
                    help="入表篇数上限，取前 N；默认不限（闸门后有多少写多少）")
    ap.add_argument("--prefix", default="论文元数据")
    ap.add_argument("--no-bge", action="store_true",
                    help="不加载 BGE，改用轮转融合（不占内存）")
    ap.add_argument("--loose", action="store_true",
                    help="放宽校验：轮数不是 3~4、或关键词超过 5 个时只警告不报错")
    ap.add_argument("--gate-profile", default=None,
                    help="主题闸门配置 id（gate_profiles.json 或内置 "
                         "building-envelope-ndt）；不给则不过闸门")
    ap.add_argument("--gate-min-pass", type=int, default=20,
                    help="过窄自检：放行篇数低于此值即告警（默认 20）")
    ap.add_argument("--gate-min-rate", type=float, default=0.10,
                    help="过窄自检：放行比例低于此值即告警（默认 0.10）")
    ap.add_argument("--target", type=int, default=None,
                    help="[已废弃] 同时指定每轮篇数与入表篇数")
    ap.add_argument("--json", help="把结果报告也写进这个 json")
    args = ap.parse_args()

    res = run(args.queries, args.out, per_round=args.per_round,
              top_n=args.top_n, prefix=args.prefix,
              use_bge=not args.no_bge, strict=not args.loose,
              target=args.target, gate_profile=args.gate_profile,
              gate_min_pass=args.gate_min_pass,
              gate_min_rate=args.gate_min_rate)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in res.items()}, f,
                      ensure_ascii=False, indent=2)
        print(f"[SAVE] {args.json}")


if __name__ == "__main__":
    main()
