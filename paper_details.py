#!/usr/bin/env python3
"""
论文详情查询 + 翻译模块 v1.2.1 （Phase 2）

根据 paper_id 列表，通过 Semantic Scholar API 获取论文详情并翻译摘要。

使用场景：
  1. Phase 1 搜索完成后，用户审查结果，选出感兴趣的 paper_id
  2. 通过本脚本查询这些论文的完整信息（摘要、DOI、PDF、引用等）
  3. 自动翻译摘要为中文

输入方式：
  - 命令行参数：python paper_details.py id1 id2 id3 ...
  - JSON 文件：python paper_details.py --json results.json
  - 文本文件：python paper_details.py --file ids.txt （每行一个 ID）
  - 标准输入：echo "id1 id2" | python paper_details.py --stdin

输出：
  - 论文详情_YYYYMMDD_HHMMSS.txt  — 完整信息含中英文摘要
  - 论文详情_YYYYMMDD_HHMMSS.json — 结构化数据
"""

import sys
import io
import json
import os
from datetime import datetime
from typing import List, Optional

# 添加当前目录到路径，以便 import 同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from semantic_scholar_search import (
    SemanticScholarSearcher,
    _paper_to_dict,
    find_paper_id_by_title,
    DEFAULT_SAVE_DIR,
    resolve_save_dir,
    ApiUnavailableError,
)


def _looks_like_title(text: str) -> bool:
    """判断输入看起来像标题还是 paper_id"""
    # paper_id 是 40 位十六进制字符串，不含空格
    if len(text) == 40 and all(c in '0123456789abcdef' for c in text):
        return False
    # 包含空格且长度 > 20，大概率是标题
    if ' ' in text and len(text) > 20:
        return True
    return False


def load_paper_ids(args: List[str]) -> List[str]:
    """
    从多种来源加载 paper_id 列表。
    支持标题输入——自动通过映射文件或 API 解析为 paper_id。
    """
    raw = []

    if not args:
        print("Usage: python paper_details.py <paper_id or title> ...")
        print("       python paper_details.py --json <file.json>")
        print("       python paper_details.py --file <ids.txt>")
        print("       echo 'id1 id2' | python paper_details.py --stdin")
        print()
        print("  Supports both paper_id and paper title as input.")
        print("  Titles are auto-resolved to paper_id via mapping file or API.")
        sys.exit(1)

    if args[0] == "--json":
        with open(args[1], 'r', encoding='utf-8') as f:
            data = json.load(f)
        papers = data.get("papers", data)
        if isinstance(papers, list):
            for p in papers:
                if isinstance(p, dict):
                    raw.append(p.get("paper_id", "") or p.get("title", ""))
                else:
                    raw.append(str(p))

    elif args[0] == "--file":
        with open(args[1], 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    raw.append(line)

    elif args[0] == "--stdin":
        text = sys.stdin.read().strip()
        raw = text.split()

    else:
        raw = args

    # 区分为 paper_id 和标题，标题需要解析
    ids = []
    titles_to_resolve = []

    for item in raw:
        if _looks_like_title(item):
            titles_to_resolve.append(item)
        else:
            ids.append(item)

    # 解析标题 → paper_id
    for title in titles_to_resolve:
        pid = find_paper_id_by_title(title)
        if pid:
            ids.append(pid)
        else:
            print(f"[WARN] Could not resolve: {title[:80]}...")

    # 去重
    seen = set()
    unique = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            unique.append(i)
    return unique


def format_paper_detail(paper) -> str:
    """格式化单篇论文的完整详情"""
    lines = []

    # 标题（含翻译）
    lines.append(f"Title: {paper.title}")
    title_cn = getattr(paper, "title_cn", "")
    if title_cn:
        lines.append(f"Title_CN: {title_cn}")

    # 作者
    authors_str = ', '.join(paper.authors[:10])
    if len(paper.authors) > 10:
        authors_str += '...'
    lines.append(f"Authors: {authors_str}")

    # 期刊信息
    lines.append(f"Journal: {paper.venue}")
    lines.append(f"Year: {paper.year}")
    lines.append(f"Citations: {paper.citation_count}")
    if paper.quartile:
        lines.append(f"Quartile: {paper.quartile}")

    # 标识符
    lines.append(f"PaperID: {paper.paper_id}")
    if paper.doi:
        lines.append(f"DOI: {paper.doi}")
    if paper.pdf_url:
        lines.append(f"PDF: {paper.pdf_url}")

    # 摘要（含翻译）
    if paper.abstract:
        lines.append(f"\nAbstract:")
        lines.append(paper.abstract)
    abstract_cn = getattr(paper, "abstract_cn", "")
    if abstract_cn:
        lines.append(f"\nAbstract_CN:")
        lines.append(abstract_cn)

    return '\n'.join(lines)


def save_detail_results(
    papers: List,
    save_dir: Optional[str] = None
) -> str:
    """保存论文详情结果（txt + json）"""
    save_dir = resolve_save_dir(save_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 保存 txt
    filepath_txt = os.path.join(save_dir, f"论文详情_{timestamp}.txt")
    output = [
        "=" * 60,
        "论文详情查询结果",
        "=" * 60,
        f"查询时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"论文数量: {len(papers)} 篇",
        "-" * 60
    ]
    for i, paper in enumerate(papers, 1):
        output.append(f"\n{'=' * 60}")
        output.append(f"[论文{i}]")
        output.append('=' * 60)
        output.append(format_paper_detail(paper))
    output.append("\n" + "=" * 60)

    with open(filepath_txt, 'w', encoding='utf-8') as f:
        f.write('\n'.join(output))

    # 保存 json
    filepath_json = os.path.join(save_dir, f"论文详情_{timestamp}.json")
    json_data = {
        "timestamp": datetime.now().isoformat(),
        "count": len(papers),
        "papers": [_paper_to_dict(p) for p in papers]
    }
    with open(filepath_json, 'w', encoding='utf-8') as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)

    print(f"\n[SAVE] Detail data: {filepath_txt}")
    print(f"[SAVE] Detail JSON: {filepath_json}")
    return filepath_txt


def main():
    """主流程：加载 ID → 查询详情 → 翻译摘要 → 保存"""
    args = sys.argv[1:]
    paper_ids = load_paper_ids(args)

    if not paper_ids:
        print("[ERROR] No valid paper IDs found")
        sys.exit(1)

    print("=" * 60)
    print("Phase 2: Paper Detail Query + Translation")
    print("=" * 60)
    print(f"Paper IDs: {len(paper_ids)}")
    print()

    # 1. 批量查询论文详情（持续 429 时快速失败并向用户报告，不静默返回空）
    searcher = SemanticScholarSearcher()
    try:
        papers = searcher.batch_query_papers(paper_ids)
    except ApiUnavailableError as e:
        print(f"\n[FATAL] API Key 不可用，已中止：{e}")
        print("[提示] 这不是“查不到论文”。请优先向用户汇报，由用户确认网络（VPN）后重试，"
              "或改用 Crossref 备用源。")
        sys.exit(2)

    if not papers:
        print("[ERROR] No papers found for given IDs")
        sys.exit(1)

    # 如实汇报：取回不齐时明确标注为"部分结果"
    if len(papers) < len(paper_ids):
        missing = len(paper_ids) - len(papers)
        print(f"\n[RESULT] 部分结果：取回 {len(papers)}/{len(paper_ids)} 篇，"
              f"{missing} 个 paper_id 未取回（可能 ID 无效或该文未被收录）。"
              f"汇报时必须说明这一不完整情况。")
    else:
        print(f"\n[RESULT] 取回 {len(papers)}/{len(paper_ids)} 篇（完整）")

    # 2. 翻译摘要
    try:
        from translator import translate_papers
        papers = translate_papers(
            papers,
            translate_titles_flag=False,    # Phase 1 已翻译标题
            translate_abstracts_flag=True   # Phase 2 翻译摘要
        )
    except ImportError:
        print("[WARN] translator module not available, skipping translation")

    # 3. 保存结果
    save_detail_results(papers)

    # 4. 打印摘要
    print(f"\n{'=' * 60}")
    print("Summary")
    print("=" * 60)
    for i, paper in enumerate(papers, 1):
        title_cn = getattr(paper, "title_cn", "")
        has_abs = "Yes" if paper.abstract else "No"
        has_abs_cn = "Yes" if getattr(paper, "abstract_cn", "") else "No"
        print(f"  [{i}] {paper.title[:80]}...")
        if title_cn:
            print(f"      CN: {title_cn[:80]}...")
        print(f"      Abstract: {has_abs} | Abstract_CN: {has_abs_cn}")
        print()


if __name__ == "__main__":
    main()
