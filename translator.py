#!/usr/bin/env python3
"""
DeepSeek V4.1 Flash 批量翻译模块 v1.2.1

使用 DeepSeek V4.1 Flash（Anthropic 兼容 API）进行论文标题和摘要的批量翻译。
无推理链 —— 翻译是确定性映射任务，不需要思维链，Flash 模型直接输出最优。

特性：
- 批量翻译标题（一次请求处理多条）
- 批量翻译摘要（长文本自动分段）
- 自动从环境变量读取 API 配置
- 429 重试 + 指数退避
"""

import os
import json
import time
import sys
from typing import List, Optional

import requests


# ---------- 配置 ----------

def _load_env_from_dsh_env() -> None:
    """
    补载 API 凭据。

    凭据文件按优先级依次尝试，格式同为 {"env": {"KEY": "VALUE", ...}}：
      1. ~/.dsh/env.json           （DSH 侧，当前使用）
      2. ~/.claude/settings.json   （Claude Code 旧位置，仅作兼容）

    当本脚本由其他进程（如 DSH 子进程）运行时，环境变量不会自动继承，
    这里主动读取该文件补载（已存在的环境变量优先，不覆盖）。
    """
    home = os.path.expanduser("~")
    cfg = None
    for p in (os.path.join(home, ".dsh", "env.json"),
              os.path.join(home, ".claude", "settings.json")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            break
        except Exception:
            continue
    if not isinstance(cfg, dict):
        return
    for k, v in (cfg.get("env") or {}).items():
        os.environ.setdefault(k, str(v))


_load_env_from_dsh_env()

# 从环境变量读取（优先 ~/.dsh/env.json 的 env 块，其次进程环境变量）
API_KEY = os.getenv("ANTHROPIC_AUTH_TOKEN", "")
BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")
MODEL = os.getenv("LITSEARCH_MODEL", "deepseek-flash")  # Flash 模型，无推理链，速度快

# 每批翻译的最大条数（标题短，可以一次多翻；摘要长，分批处理）
MAX_TITLES_PER_BATCH = 30
MAX_ABSTRACTS_PER_BATCH = 5

# API 请求间隔（秒）
REQUEST_INTERVAL = 0.5


def _build_url(path: str) -> str:
    """构建 API URL"""
    base = BASE_URL.rstrip("/")
    return f"{base}/{path.lstrip('/')}"


def _call_api(messages: List[dict], max_tokens: int = 4096) -> Optional[str]:
    """
    调用 DeepSeek V4.1 Flash API（Anthropic Messages 兼容格式）

    Args:
        messages: 消息列表
        max_tokens: 最大输出 token

    Returns:
        模型返回的文本，失败返回 None
    """
    url = _build_url("v1/messages")
    headers = {
        "x-api-key": API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "messages": messages,
        "thinking": {"type": "disabled"},  # 翻译不需要推理链
    }

    for attempt in range(3):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                # 找到 type="text" 的 block（跳过 thinking block）
                content = data.get("content", [])
                if content and isinstance(content, list):
                    for block in content:
                        if block.get("type") == "text":
                            return block.get("text", "")
                return ""

            if resp.status_code == 429:
                wait = 2 ** (attempt + 1)
                print(f"  [429] Rate limited, waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue

            print(f"  [ERROR] HTTP {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
            if attempt < 2:
                time.sleep(2)
                continue
            return None

        except requests.exceptions.RequestException as e:
            print(f"  [ERROR] {e}", file=sys.stderr)
            if attempt < 2:
                time.sleep(2)
                continue
            return None

    return None


def _parse_title_response(text: str, expected_count: int) -> List[str]:
    """
    解析标题翻译响应。

    兼容多种格式：
    1. 标准 JSON 数组: ["title1", "title2"]
    2. 编号格式: [1] "title1"\n[2] "title2"
    3. 纯文本行: title1\ntitle2
    """
    import re

    text = text.strip()

    # 去除 markdown 代码块
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(l for l in lines if not l.startswith("```"))
        text = text.strip()

    # 方式1: JSON 数组
    for candidate in _extract_json_arrays(text):
        try:
            arr = json.loads(candidate)
            if isinstance(arr, list) and all(isinstance(x, str) for x in arr):
                if len(arr) == expected_count:
                    return arr
        except (json.JSONDecodeError, TypeError):
            continue

    # 方式2: [N] "text" 格式
    pattern = r'\[\d+\]\s*["“]([^"”]+)["”]'
    matches = re.findall(pattern, text)
    if len(matches) == expected_count:
        return [m.strip() for m in matches]

    # 方式3: 按行提取引号内容
    lines = text.split("\n")
    quoted = []
    for line in lines:
        m = re.search(r'["“]([^"”]+)["”]', line)
        if m:
            quoted.append(m.group(1).strip())
    if len(quoted) == expected_count:
        return quoted

    # 方式4: 按行提取，跳过编号前缀
    clean_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 去掉 [N] 前缀
        line = re.sub(r'^\[\d+\]\s*', '', line)
        # 去掉引号
        line = line.strip('\'"“”‘’')
        if line:
            clean_lines.append(line)
    if len(clean_lines) == expected_count:
        return clean_lines

    return []


def _extract_json_arrays(text: str):
    """从文本中提取所有可能的 JSON 数组"""
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == '[':
            if depth == 0:
                start = i
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0 and start >= 0:
                yield text[start:i+1]
                start = -1


def translate_titles(
    titles: List[str],
    source_lang: str = "English",
    target_lang: str = "Simplified Chinese"
) -> List[str]:
    """
    批量翻译论文标题。

    将英文标题批量发给 Flash 模型，一次请求处理多篇。
    要求返回 JSON 数组，精确对应输入顺序。

    Args:
        titles: 英文标题列表
        source_lang: 源语言
        target_lang: 目标语言

    Returns:
        翻译后的中文标题列表（与输入一一对应）
    """
    if not titles:
        return []

    all_translated = []
    total_batches = (len(titles) + MAX_TITLES_PER_BATCH - 1) // MAX_TITLES_PER_BATCH

    for batch_idx in range(0, len(titles), MAX_TITLES_PER_BATCH):
        batch = titles[batch_idx:batch_idx + MAX_TITLES_PER_BATCH]
        batch_num = batch_idx // MAX_TITLES_PER_BATCH + 1
        print(f"  [TITLE] Batch {batch_num}/{total_batches}, {len(batch)} titles",
              file=sys.stderr)

        # 构建带编号的输入
        numbered = "\n".join(f"[{i+1}] {t}" for i, t in enumerate(batch))
        prompt = (
            f"Translate these academic paper titles from {source_lang} to {target_lang}.\n"
            f"Return ONLY a valid JSON array of strings like: "
            f'["title1", "title2", "title3"]\n'
            f"Do NOT add numbers, prefixes, or extra text:\n\n"
            f"{numbered}"
        )

        for retry in range(2):
            result = _call_api([
                {"role": "user", "content": prompt}
            ], max_tokens=2048)

            if result:
                translations = _parse_title_response(result, len(batch))
                if translations and len(translations) == len(batch):
                    all_translated.extend(translations)
                    break
                print(f"  [WARN] Parse failed ({len(translations) if translations else 0}/{len(batch)}), retry {retry+1}",
                      file=sys.stderr)
                if retry == 1:
                    all_translated.extend(batch)
            else:
                if retry == 1:
                    all_translated.extend(batch)

        if batch_idx + MAX_TITLES_PER_BATCH < len(titles):
            time.sleep(REQUEST_INTERVAL)

    return all_translated


def translate_abstracts(
    abstracts: List[str],
    source_lang: str = "English",
    target_lang: str = "Simplified Chinese"
) -> List[str]:
    """
    批量翻译论文摘要。

    摘要较长，每批只处理少量需要输出结构化 JSON。

    Args:
        abstracts: 英文摘要列表
        source_lang: 源语言
        target_lang: 目标语言

    Returns:
        翻译后的中文摘要列表（与输入一一对应）
    """
    if not abstracts:
        return []

    all_translated = []
    total_batches = (len(abstracts) + MAX_ABSTRACTS_PER_BATCH - 1) // MAX_ABSTRACTS_PER_BATCH

    for batch_idx in range(0, len(abstracts), MAX_ABSTRACTS_PER_BATCH):
        batch = abstracts[batch_idx:batch_idx + MAX_ABSTRACTS_PER_BATCH]
        batch_num = batch_idx // MAX_ABSTRACTS_PER_BATCH + 1
        print(f"  [ABSTRACT] Batch {batch_num}/{total_batches}, {len(batch)} abstracts",
              file=sys.stderr)

        # 用编号标记，要求返回 JSON 数组
        numbered = "\n\n".join(
            f"--- PAPER {i+1} ---\n{ab or '[No abstract]'}"
            for i, ab in enumerate(batch)
        )

        prompt = (
            f"Translate these academic paper abstracts from {source_lang} to {target_lang}.\n"
            f"Return ONLY a valid JSON array of translated strings, same order.\n"
            f'Format: ["abstract1", "abstract2", ...]\n\n'
            f"{numbered}"
        )

        for retry in range(2):
            result = _call_api([
                {"role": "user", "content": prompt}
            ], max_tokens=8192)

            if result:
                translations = _parse_title_response(result, len(batch))
                if translations and len(translations) == len(batch):
                    all_translated.extend(translations)
                    break
                print(f"  [WARN] Parse failed ({len(translations) if translations else 0}/{len(batch)}), retry {retry+1}",
                      file=sys.stderr)
                if retry == 1:
                    all_translated.extend(batch)
            else:
                if retry == 1:
                    all_translated.extend(batch)

        if batch_idx + MAX_ABSTRACTS_PER_BATCH < len(abstracts):
            time.sleep(REQUEST_INTERVAL)

    return all_translated


def translate_papers(
    papers: list,
    translate_titles_flag: bool = True,
    translate_abstracts_flag: bool = True,
    source_lang: str = "English",
    target_lang: str = "Simplified Chinese"
) -> list:
    """
    批量翻译论文列表的标题和摘要。

    这是主要入口，供 literature-search 工作流调用。
    直接操作 PaperInfo 对象或 dict 列表。

    Args:
        papers: 论文列表（dict 或 PaperInfo 对象）
        translate_titles_flag: 是否翻译标题
        translate_abstracts_flag: 是否翻译摘要
        source_lang: 源语言
        target_lang: 目标语言

    Returns:
        论文列表（已附加 title_cn 和 abstract_cn 字段）
    """
    if not papers:
        return papers

    # 统一转为 dict 操作
    is_dict_list = isinstance(papers[0], dict)

    # 1. 翻译标题
    if translate_titles_flag:
        titles_en = []
        for p in papers:
            t = p.get("title", "") if is_dict_list else getattr(p, "title", "")
            titles_en.append(t or "")

        print(f"\n[TRANSLATE] Translating {len(titles_en)} titles...")
        titles_cn = translate_titles(titles_en, source_lang, target_lang)

        for i, cn in enumerate(titles_cn):
            if is_dict_list:
                papers[i]["title_cn"] = cn
            else:
                setattr(papers[i], "title_cn", cn)

    # 2. 翻译摘要
    if translate_abstracts_flag:
        # 只翻译有摘要的
        has_abstract = []
        for p in papers:
            ab = p.get("abstract", "") if is_dict_list else getattr(p, "abstract", "")
            has_abstract.append(bool(ab))

        abstracts_en = []
        for p in papers:
            ab = p.get("abstract", "") if is_dict_list else getattr(p, "abstract", "")
            abstracts_en.append(ab or "")

        non_empty_count = sum(has_abstract)
        print(f"[TRANSLATE] Translating {non_empty_count}/{len(abstracts_en)} abstracts...")

        abstracts_cn = translate_abstracts(abstracts_en, source_lang, target_lang)

        for i, cn in enumerate(abstracts_cn):
            if is_dict_list:
                papers[i]["abstract_cn"] = cn
            else:
                setattr(papers[i], "abstract_cn", cn)

    return papers


# ---------- 测试 ----------

def test_translate():
    """快速测试翻译功能"""
    if not API_KEY:
        print("[ERROR] ANTHROPIC_AUTH_TOKEN not set")
        return

    print("=" * 50)
    print("DeepSeek V4.1 Flash Translation Test")
    print("=" * 50)

    test_titles = [
        "Chain-of-thought prompting improves multi-step reasoning accuracy",
        "Chain-of-thought prompting for multi-step reasoning in large language models",
        "A survey of evaluation benchmarks for large language model reasoning",
    ]
    print(f"\nTranslating {len(test_titles)} titles...")
    results = translate_titles(test_titles)
    for en, cn in zip(test_titles, results):
        print(f"  EN: {en}")
        print(f"  CN: {cn}")
        print()

    test_abstracts = [
        "This paper presents a comprehensive review of chain-of-thought prompting techniques for improving multi-step reasoning accuracy.",
    ]
    print(f"Translating {len(test_abstracts)} abstracts...")
    results = translate_abstracts(test_abstracts)
    for en, cn in zip(test_abstracts, results):
        print(f"  EN: {en}")
        print(f"  CN: {cn}")
        print()


if __name__ == "__main__":
    test_translate()
