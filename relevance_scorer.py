#!/usr/bin/env python3
"""
BGE 多关键词标题相关性排序模块 v2.0

使用 **BAAI/bge-base** 模型对论文按标题相关性排序。

v2.0 的两处关键变更
-------------------
1. **模型由 bge-small 换成 bge-base**，与本机另一套 BGE 服务**使用同一套模型**：
       - `BAAI/bge-base-en-v1.5`（英文）
       - `BAAI/bge-base-zh-v1.5`（中文）
   两个模型在本机 `~/.cache/huggingface/hub/` 已有缓存，**不需要重新下载**。
   （旧版用 bge-small，与该常驻服务不一致。）

2. **单篇得分由「各关键词相似度之和」改为「平均」**。
   组内排序不受影响（关键词数固定时求和与求平均单调等价），
   但**跨组比较必须用平均**：否则关键词多的那组得分天然偏高，
   合并排序时会整体占优。

用法
----
    from relevance_scorer import RelevanceScorer

    # 论文检索场景：**固定英文分支**，任务期间保持加载，结束释放
    with RelevanceScorer(lang="en") as s:
        merged = s.rank_groups([(q1, ps1), (q2, ps2), (q3, ps3)], total_limit=200)
    # 退出 with 即释放模型、归还内存

生命周期（重要）
----------------
论文检索只用**英文分支**；常驻 BGE 服务用的是**中文分支**
（那个库是中文内容，`.lklang` = `zh`），两者在**不同进程**里，内存不共享。

- `lang="en"`（默认）：**永远只加载英文模型**。即使查询词里混了中文也不会
  把中文模型拉起来 —— 对论文检索这是正确行为，也避免多占 780 MB。
- 模型一旦加载就一直留在本进程内（**整个检索任务期间持续激活**，不重复加载）。
- ⚠️ **`unload()` 只释放 Python 引用，内存实测不会归还给操作系统。**
  真正可靠的释放方式是**让进程退出**。详见 `search_pipeline.py` 的实测数据。

实测（`bge-base-en-v1.5`，Windows / CPU，2026-09-13）：

| 状态 | 进程内存 |
|---|---|
| 什么都没加载 | 30 MB |
| `import torch` + `sentence_transformers` | **732 MB** |
| 再加载英文模型 | 865 MB |
| `del` 引用 + `gc.collect()` | 858 MB（**只还了 7 MB**）|
| 清 transformers 内部缓存 | 858 MB（无变化）|
| Windows `SetProcessWorkingSetSize` 修剪工作集 | 1 MB（**假象**：再分配内存后回到 250 MB）|

**所以：不要在长驻进程里反复加载/卸载 BGE 来"省内存"。**
应当让 BGE 工作发生在一个短命进程里，用完即退（见 `search_pipeline.py`）。

依赖：
    pip install sentence-transformers torch numpy
    pip install psutil        # 可选，仅用于打印内存占用
"""
import sys
import io
import gc

# Windows GBK 编码兼容（仅在直接运行时启用，import 时不触发）
def _fix_encoding():
    if sys.platform == 'win32' and hasattr(sys.stdout, 'buffer'):
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass


def _rss_mb():
    """当前进程占用内存（MB）；psutil 不可用时返回 None。"""
    try:
        import os
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except Exception:
        return None

import re
import numpy as np
from typing import List, Tuple, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from semantic_scholar_search import PaperInfo


# 英文停用词
STOP_WORDS = {
    'the', 'a', 'an', 'of', 'for', 'in', 'on', 'and', 'or', 'is', 'to',
    'with', 'by', 'from', 'at', 'this', 'that', 'are', 'was', 'be', 'has',
    'had', 'have', 'do', 'does', 'did', 'will', 'would', 'can', 'could',
    'should', 'may', 'might', 'not', 'no', 'but', 'if', 'so', 'it', 'its',
    'as', 'an', 'been', 'being', 'were', 'their', 'they', 'them', 'these',
    'those', 'which', 'what', 'when', 'where', 'how', 'all', 'each', 'both',
    'few', 'more', 'most', 'other', 'some', 'such', 'than', 'too', 'very',
    'just', 'about', 'above', 'after', 'again', 'also', 'any', 'because',
    'before', 'between', 'during', 'into', 'through', 'under', 'up', 'down',
    'out', 'off', 'over', 'own', 'same', 'only', 'then', 'there', 'here',
    'while', 'why', 'who', 'whom', 'whose', 'how', 'much', 'many', 'well',
    'get', 'got', 'make', 'made', 'take', 'took', 'come', 'came', 'go',
    'went', 'give', 'gave', 'use', 'used', 'find', 'found', 'know', 'knew',
    'think', 'thought', 'say', 'said', 'tell', 'told', 'see', 'saw',
    'want', 'need', 'try', 'tried', 'ask', 'asked', 'work', 'worked',
    'seem', 'seemed', 'show', 'showed', 'hear', 'heard', 'let', 'keep',
    'kept', 'help', 'helped', 'start', 'started', 'run', 'ran', 'move',
    'moved', 'live', 'lived', 'believe', 'felt', 'feel', 'become', 'became',
    'leave', 'left', 'put', 'bring', 'brought', 'began', 'begin', 'set',
    'meet', 'met', 'sit', 'sat', 'stand', 'stood', 'lose', 'lost', 'pay',
    'paid', 'lead', 'led', 'grow', 'grew', 'draw', 'drew', 'drive', 'drove',
    'break', 'broke', 'sell', 'sold', 'choose', 'chose', 'remain', 'remained',
    'add', 'added', 'change', 'changed', 'end', 'ended', 'open', 'opened',
    'close', 'closed', 'include', 'included', 'provide', 'provided',
    'build', 'built', 'develop', 'developed', 'produce', 'produced',
    'report', 'reported', 'base', 'based', 'study', 'studies', 'result',
    'results', 'method', 'methods', 'approach', 'approaches', 'using',
    'based', 'new', 'paper', 'research', 'analysis', 'system', 'model',
    'data', 'method', 'technique', 'techniques', 'application', 'applications'
}


class RelevanceScorer:
    """BGE 多关键词标题相关性排序器"""

    # 模型名称 —— 与本机另一套 BGE 服务保持一致，本机已有缓存
    MODEL_EN = "BAAI/bge-base-en-v1.5"
    MODEL_ZH = "BAAI/bge-base-zh-v1.5"

    def __init__(self, model_name: Optional[str] = None, lang: str = "en"):
        """
        初始化排序器（**不加载模型**，首次真正使用时才惰性加载）。

        Args:
            model_name: 直接指定模型名；一旦给出就完全忽略 lang
            lang: `"en"`（默认）| `"zh"` | `"auto"`
                  **论文检索场景必须固定 `"en"`** —— 检索的都是英文文献，
                  并且要避免中英混排的查询词把中文模型也拉起来（多占 780 MB）。
                  `"auto"` 才按查询字符比例自动判断（旧行为，不推荐）。
        """
        self.model = None
        self.model_name = model_name
        self.lang = (lang or "en").strip().lower()
        self._model_loaded = False

    def _ensure_model(self, query: str = ""):
        """确保模型已加载（惰性）。已加载则直接复用，**不会重复加载**。"""
        if self._model_loaded:
            return self.model

        from sentence_transformers import SentenceTransformer

        if self.model_name is None:
            if self.lang == "auto":
                self.model_name = self._detect_model(query)
            else:
                self.model_name = (self.MODEL_ZH if self.lang == "zh"
                                   else self.MODEL_EN)

        before = _rss_mb()
        print(f"[BGE] Loading model: {self.model_name}...")
        self.model = SentenceTransformer(self.model_name)
        self._model_loaded = True
        try:
            dim = self.model.get_embedding_dimension()
        except AttributeError:                      # 旧版 sentence-transformers
            dim = self.model.get_sentence_embedding_dimension()
        after = _rss_mb()
        mem = f"，进程内存 {before:.0f} -> {after:.0f} MB" if before and after else ""
        print(f"[BGE] Model loaded (dim={dim}){mem}")
        return self.model

    def unload(self):
        """释放模型引用。

        ⚠️ **实测：这不会把内存归还给操作系统**（865 -> 858 MB）。
        torch 的 C++ 层内存以及 `import torch` 本身的约 700 MB，
        在进程存活期间都无法释放。**只有进程退出才能全部归还。**

        保留此方法是为了：① 丢掉引用后下次调用会重新加载；
        ② 让意图明确。不要在长驻进程里靠它省内存 —— 见模块 docstring 实测表。
        """
        if not self._model_loaded and self.model is None:
            print("[BGE] 模型未加载，无需释放")
            return False
        before = _rss_mb()
        self.model = None
        self._model_loaded = False
        gc.collect()
        after = _rss_mb()
        mem = (f"，进程内存 {before:.0f} -> {after:.0f} MB"
               f"（仅释放 {before - after:.0f} MB，内存未归还给系统）"
               if before and after else "")
        print(f"[BGE] 已丢弃模型引用{mem}")
        return True

    # 别名，便于按习惯调用
    close = unload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.unload()
        return False

    @staticmethod
    def _detect_model(query: str) -> str:
        """根据查询字符判断使用中文还是英文模型"""
        chinese_chars = sum(1 for c in query if '一' <= c <= '鿿')
        if chinese_chars > len(query) * 0.3:
            return RelevanceScorer.MODEL_ZH
        return RelevanceScorer.MODEL_EN

    @staticmethod
    def split_keywords(query: str) -> List[str]:
        """
        将查询按空格拆分为关键词，去掉停用词。

        示例:
            "large language model reasoning"
            -> ["large", "language", "model", "reasoning"]

        Args:
            query: 查询字符串

        Returns:
            关键词列表（小写，去停用词，长度>1）
        """
        words = re.split(r'[\s,;:!?."\']+', query)
        keywords = [
            w.strip().lower() for w in words
            if w.strip()
            and w.strip().lower() not in STOP_WORDS
            and len(w.strip()) > 1
        ]
        return keywords

    def rank(self, query: str, papers: List['PaperInfo']) -> List['PaperInfo']:
        """
        多关键词标题相关性排序。

        用途：仅排序，不筛选。所有论文保留，按相关性得分降序排列。

        评分逻辑：
        1. 将查询拆分为关键词
        2. 每个关键词用 BGE 编码
        3. 每篇论文标题用 BGE 编码
        4. 计算相似度矩阵 (K个关键词 x N篇论文)
        5. 每篇论文得分 = 各关键词相似度的**平均值**（按列求均值）
        6. 按得分降序排列

        为什么是平均而不是求和：组内排序两者等价（关键词数固定时单调），
        但**跨组比较必须用平均** —— 否则关键词多的那组得分天然偏高，
        多组结果合并排序时会整体占优。

        Args:
            query: 用户查询词（完整查询字符串）
            papers: 论文列表

        Returns:
            排序后的论文列表（原顺序改变，论文不删除）
            每篇论文的 relevance_score 字段已赋值
        """
        if not papers:
            return papers

        keywords = self.split_keywords(query)
        if not keywords:
            print("[BGE] No keywords extracted, returning original order")
            return papers

        print(f"[BGE] Keywords: {keywords}")
        print(f"[BGE] Ranking {len(papers)} papers...")

        self._ensure_model(query)

        titles = [p.title or "" for p in papers]

        # 编码关键词和标题
        kw_embs = self.model.encode(keywords, normalize_embeddings=True)  # (K, D)
        title_embs = self.model.encode(
            titles, normalize_embeddings=True, batch_size=64,
            show_progress_bar=len(titles) > 100
        )  # (N, D)

        # 相似度矩阵: (K, N) — 每个关键词对每篇论文标题的相似度
        sim_matrix = kw_embs @ title_embs.T

        # 每篇论文得分 = 各关键词相似度的平均值（按列求均值）
        # 用平均而非求和：组内排序等价，但跨组可比（见模块 docstring）
        paper_scores = sim_matrix.mean(axis=0)  # (N,)

        # 将得分附加到论文对象上
        for i, p in enumerate(papers):
            p.relevance_score = float(paper_scores[i])

        # 按得分降序排列
        scored = list(zip(papers, paper_scores))
        scored.sort(key=lambda x: -x[1])

        sorted_papers = [p for p, s in scored]

        # 打印前5名
        print(f"[BGE] Top 5:")
        for i, p in enumerate(sorted_papers[:5], 1):
            try:
                print(f"  [{i}] {p.relevance_score:.2f} | {p.title[:80]}")
            except UnicodeEncodeError:
                # Windows GBK console can't handle some Unicode chars
                safe_title = p.title[:80].encode('ascii', errors='replace').decode('ascii')
                print(f"  [{i}] {p.relevance_score:.2f} | {safe_title}")

        return sorted_papers

    @staticmethod
    def _ident(paper) -> str:
        """取论文唯一标识（用于跨组去重）。"""
        for attr in ("paper_id", "doi", "title"):
            v = getattr(paper, attr, None)
            if v:
                return str(v).strip().lower()
        return ""

    def rank_groups(self, groups, total_limit=None, verbose=True):
        """多组关键词**各自独立评分**后，合并做全局排序。

        **核心约束（用户明确要求）**：每一组关键词**只给它自己检索到的文献**评分，
        **绝不能**用别组的关键词去给本组文献打分。

        流程：
            第 1 组关键词 × 第 1 组文献  → 该组每篇得分
            第 2 组关键词 × 第 2 组文献  → 该组每篇得分
            第 3 组关键词 × 第 3 组文献  → 该组每篇得分
                        ↓  合并（跨组去重）
                  按得分全局降序 → 取前 total_limit

        实现细节：
        - 同一标题在多个组里出现时，**标题的编码只算一次**（复用向量），
          但**分数仍按组独立计算** —— 复用的是编码，不是分数，不违反上述约束。
        - 一篇论文若被多个组检索到，取它在这些组中的**最高分**
          （即"它最匹配的那个查询"决定了它的相关性）。

        Args:
            groups: [(查询词, [论文, ...]), ...]
            total_limit: 合并排序后取前多少篇；None = 全部返回

        Returns:
            合并去重、按得分降序的论文列表；每篇的 relevance_score 已赋值
        """
        groups = [(q, list(ps)) for q, ps in groups if ps]
        if not groups:
            return []

        # 一次性编码所有不重复标题（同一标题只编一次）
        title_vec = {}
        for _, ps in groups:
            for p in ps:
                title_vec.setdefault(getattr(p, "title", "") or "", None)
        titles = [t for t, v in title_vec.items() if v is None]

        combined = " ".join(q for q, _ in groups)
        self._ensure_model(combined)

        if verbose:
            print(f"[BGE] 共 {len(groups)} 组关键词，{len(title_vec)} 个不重复标题")
        if titles:
            embs = self.model.encode(titles, normalize_embeddings=True,
                                     batch_size=64,
                                     show_progress_bar=len(titles) > 100)
            for i, t in enumerate(titles):
                title_vec[t] = embs[i]

        best = {}          # ident -> (paper, score)
        for gi, (q, ps) in enumerate(groups, 1):
            kws = self.split_keywords(q)
            if not kws:
                if verbose:
                    print(f"  [{gi}/{len(groups)}] {q[:50]!r} 无可提取关键词，该组记 0 分")
                scores = np.zeros(len(ps))
            else:
                kw_embs = self.model.encode(kws, normalize_embeddings=True)
                t_embs = np.stack([title_vec[getattr(p, "title", "") or ""]
                                   for p in ps])
                scores = (kw_embs @ t_embs.T).mean(axis=0)   # 只用【本组】关键词
            for p, s in zip(ps, scores):
                p.relevance_score = float(s)
                key = self._ident(p)
                prev = best.get(key)
                if prev is None or s > prev[1]:
                    best[key] = (p, float(s))
            if verbose:
                print(f"  [{gi}/{len(groups)}] {q[:50]!r} -> {len(ps)} 篇，"
                      f"得分 {float(scores.min()):.3f}~{float(scores.max()):.3f}")

        merged = sorted(best.values(), key=lambda x: -x[1])
        out = [p for p, _ in merged]
        if total_limit:
            out = out[:total_limit]
        if verbose:
            print(f"[BGE] 合并去重 {len(best)} 篇 -> 取前 {len(out)} 篇")
        return out

    def compare_with_existing(
        self,
        new_papers: List['PaperInfo'],
        existing_titles: List[str],
        threshold: float = 0.85
    ) -> Tuple[List['PaperInfo'], List['PaperInfo']]:
        """
        与已有文献对比，排除相似论文。

        Args:
            new_papers: 新论文列表
            existing_titles: 已有论文标题列表
            threshold: 相似度阈值，超过此值视为已有（默认0.85）

        Returns:
            (kept, removed) — 保留和排除的论文

        策略：
        用 BGE 编码已有论文标题和新论文标题，
        计算相似度矩阵，新论文与任一已有论文相似度 > threshold 则排除。
        """
        new_titles = [p.title or "" for p in new_papers]
        if not new_titles or not existing_titles:
            return new_papers, []

        self._ensure_model(new_titles[0])

        print(f"[BGE] Comparing {len(new_papers)} new papers with {len(existing_titles)} existing...")

        new_embs = self.model.encode(
            new_titles, normalize_embeddings=True, batch_size=64,
            show_progress_bar=len(new_titles) > 100
        )
        exist_embs = self.model.encode(
            existing_titles, normalize_embeddings=True, batch_size=64
        )

        # 相似度矩阵: (new_count, exist_count)
        sim = new_embs @ exist_embs.T
        max_sim = sim.max(axis=1)

        kept, removed = [], []
        for i, p in enumerate(new_papers):
            if max_sim[i] > threshold:
                removed.append(p)
            else:
                kept.append(p)

        print(f"[BGE] Kept: {len(kept)}, Removed (existing): {len(removed)}")
        return kept, removed


def load_existing_titles(folder: str) -> List[str]:
    """
    从文件夹中加载已有论文的标题（从文件名近似提取）

    Args:
        folder: 包含 PDF 文件的文件夹路径

    Returns:
        论文标题列表（从文件名提取，去扩展名）
    """
    import os

    titles = []
    for root, dirs, files in os.walk(folder):
        for f in files:
            if f.lower().endswith('.pdf'):
                name = os.path.splitext(f)[0]
                titles.append(name)

    print(f"[EXISTING] Loaded {len(titles)} paper titles from {folder}")
    return titles


# 测试
if __name__ == "__main__":
    _fix_encoding()
    print("RelevanceScorer module test")
    print(f"Stop words count: {len(STOP_WORDS)}")

    # 测试关键词拆分
    test_query = "large language model reasoning"
    keywords = RelevanceScorer.split_keywords(test_query)
    print(f"Query: {test_query}")
    print(f"Keywords: {keywords}")

    test_query2 = "chain of thought prompting evaluation"
    keywords2 = RelevanceScorer.split_keywords(test_query2)
    print(f"Query: {test_query2}")
    print(f"Keywords: {keywords2}")
