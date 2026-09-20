# -*- coding: utf-8 -*-
"""主题闸门：按主题配置判定标题是否属于目标领域

## 判定规则

```
通过 = 命中【全部】闸门（轴）
```

**没有黑名单**（用户 2026-09-20 定稿）。排除非目标领域靠的是**选好轴里的词**，
而不是再加一张排除表 —— 轴越多、机制越多，误杀面越大。

## 配置尺度（用户指定）

**2~3 个闸门，每闸门约 50 个词。**

- 轴内是"或"，50 个精心挑的词足以覆盖主要同义词、缩写、英式/美式拼写与词干变体。
- **轴不要超过 3 个**：每多一轴，"必须全中"的合取更紧，误杀风险越高。
- 建议两轴即可：`方法/信号` + `对象/领域`。

## ⚠️ 闸门是按主题配置的，不是一套词表打天下

内置的 `example-llm-reasoning` 只是**结构示例**，只适配「大语言模型的推理能力」这一个题目。
实测：拿它去判别的题目，13 个真实标题只过 3 个
（题目 B 0/4、题目 C 0/3、题目 D 0/3）。

**换题目就要换配置** —— 写进 `gate_profiles.json`（`save_profile()` / `build_profile()`）。

## 匹配规则（两处实测踩坑）

- 短词（≤5 字符，如 llm / qa / model）**必须词边界匹配** ——
  纯子串会把 sto`**`rag`**`e` 当成 `rag`、w`**`all`**`et` 当成 `wall`，
  把别的领域的论文误放进来。
  实现上给了可选复数 `s?`，`model` 即覆盖 models 而不碰 modelling。
- 匹配前先把重音折叠为 ASCII，这样词表里写 ASCII 拼法也能命中正文里的带重音写法
  （`naive` 也能匹配 `naïve`）。
- ⚠️ **词干不能当词边界词用**：实测把 `model` 当词位时（5 字符走词边界），
  `modelling` / `modelled` **都匹配不上**（`model` 后面还跟着 `l`），一口气误杀 5 篇。
  正确写法是完整词或更长的词干（长度 >5 自动走子串匹配）。

## 过窄自检

`apply_gate()` 在放行率过低时返回告警：这通常说明**配置过窄、正在误杀**，
而不是「该领域没有相关文献」。交付前必须先人工翻看 rejected。

调试：
    python topic_gate.py --list-profiles
    python topic_gate.py --explain "标题" [--profile other-topic]
"""
import os
import re
import json
import unicodedata
from typing import Dict, List, Tuple

# ============================================================ 内置主题配置
# 示例：「大语言模型的推理能力」—— 2 个闸门 × 约 50 词
#
# ⚠️ 这只是一份**结构示例**，用来演示"2~3 个轴、每轴约 50 词"的配置尺度。
#    正式检索前必须为本次题目另建 profile。

# ---- 闸门 1：对象 / 方法（47 词）----
METHOD_TERMS: List[str] = [
    "language model", "large language", "llm", "foundation model",
    "transformer", "decoder", "encoder-decoder", "attention",
    "instruction tuning", "instruction following", "fine-tuning",
    "sft", "rlhf", "dpo", "alignment", "prompt", "prompting",
    "in-context learning", "few-shot", "zero-shot", "chain-of-thought",
    "chain of thought", "self-consistency", "tree of thought",
    "scratchpad", "reasoning step", "distillation", "quantization",
    "mixture of experts", "moe", "agent", "tool use", "tool calling",
    "retrieval augmented", "rag", "context window", "tokenization",
    "pretraining", "post-training", "adapter", "lora", "peft",
    "open-weight", "inference", "decoding", "sampling", "temperature",
]

# ---- 闸门 2：任务 / 评测（47 词）----
# ⚠️ 没有黑名单，所以这一轴的用词就是唯一的口径。
# 轴内是"或"，50 个词足以覆盖主要同义词、缩写与拼写变体；
# 但每多一轴，"必须全中"的合取就更紧，误杀风险越高。
TASK_TERMS: List[str] = [
    "reasoning", "logical reasoning", "deductive", "inductive",
    "abductive", "arithmetic", "math word problem", "mathematical",
    "symbolic", "commonsense", "causal", "multi-hop", "multi step",
    "planning", "question answering", "qa", "benchmark", "evaluation",
    "accuracy", "mmlu", "gsm8k", "humaneval", "bbh",
    "hallucination", "faithfulness", "robustness", "generalization",
    "out-of-distribution", "contamination", "error analysis",
    "chain validity", "self-verification", "verifier", "critique",
    "process reward", "outcome reward", "pass at k", "majority voting",
    "scaling law", "emergent ability", "ablation", "baseline",
    "human evaluation", "annotation", "metric", "score", "correlation",
]

DEFAULT_PROFILE: Dict = {
    "id": "example-llm-reasoning",
    "topic": "大语言模型的推理能力（示例）",
    "axes": {
        "对象方法": METHOD_TERMS,
        "任务评测": TASK_TERMS,
    },
}

# ---- 兼容旧名（早期版本按这些名字引用）----
METHOD_ALIASES = METHOD_TERMS
TASK_ALIASES = TASK_TERMS

PROFILES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "gate_profiles.json")


# ============================================================ 配置读写

def load_profiles(path: str = None) -> Dict[str, Dict]:
    """读取 gate_profiles.json。缺失或损坏时返回空字典（不抛错）。"""
    path = path or PROFILES_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    profs = data.get("profiles") if isinstance(data, dict) else None
    return profs if isinstance(profs, dict) else {}


def save_profile(profile: Dict, path: str = None) -> str:
    """把一条主题配置写进 gate_profiles.json（同 id 覆盖）。返回文件路径。"""
    path = path or PROFILES_FILE
    data = {"_说明": "主题闸门配置。每个 profile = 2~3 个闸门（轴），每轴约 50 词；"
                     "命中全部轴才通过。没有黑名单。",
            "profiles": load_profiles(path)}
    data["profiles"][profile["id"]] = {"id": profile["id"],
                                       "topic": profile.get("topic", ""),
                                       "axes": profile.get("axes", {})}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def build_profile(pid: str, topic: str, axes: Dict[str, List[str]]) -> Dict:
    """快速造一条主题配置。

    尺度提醒（用户指定）：**2~3 个闸门，每闸门约 50 词**。
    轴太多会误杀 —— 超过 3 个直接报错。
    """
    if len(axes) > 3:
        raise ValueError(f"闸门不宜超过 3 个（当前 {len(axes)} 个）："
                         f"每多一轴，'必须全中'的合取更紧，误杀风险越高")
    for name, terms in axes.items():
        if not terms:
            raise ValueError(f"闸门「{name}」词表为空")
    return {"id": pid, "topic": topic, "axes": axes}


def resolve_profile(profile=None) -> Dict:
    """把 None / id 字符串 / dict 统一成 profile 字典。"""
    if profile is None:
        return DEFAULT_PROFILE
    if isinstance(profile, dict):
        return profile
    profs = load_profiles()
    if profile in profs:
        return profs[profile]
    if profile == DEFAULT_PROFILE["id"]:
        return DEFAULT_PROFILE
    raise KeyError(f"找不到主题配置 {profile!r}。已有："
                   f"{[DEFAULT_PROFILE['id']] + sorted(profs)}")


# ============================================================ 匹配

_ACCENTS = str.maketrans({"ç": "c", "é": "e", "è": "e", "ê": "e", "ï": "i",
                          "ü": "u", "ö": "o", "ä": "a", "ñ": "n", "ø": "o",
                          "å": "a", "æ": "ae"})


def _norm(text: str) -> str:
    """小写 + 重音折叠：`naïve` → `naive`，词表写 ASCII 即可命中带重音的写法。"""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t.lower().translate(_ACCENTS)


def _match(text: str, terms: List[str]) -> List[str]:
    """命中检测。

    - 长度 ≤5 的词：**词边界**匹配 + 可选复数 `s?`
      （`wall` 覆盖 walls，但不碰 wallet）。
    - 长度 >5 的词：子串匹配，覆盖词干变形（reason* / forecast*）。
    """
    t = _norm(text)
    out = []
    for w in terms:
        key = _norm(w)
        if len(key) <= 5:
            if re.search(r"(?<![a-z])" + re.escape(key) + r"s?(?![a-z])", t):
                out.append(w)
        elif key in t:
            out.append(w)
    return out


def gate(title: str, profile=None, abstract: str = "") -> Tuple[bool, Dict]:
    """按主题配置判定论文是否属于目标领域。

    **判定依据 = 标题 + 摘要**（用户 2026-09-20 指定）。
    检索响应现在已直接带回摘要（实测覆盖 74~76%），白放着不用会白白误杀一批
    「方法只写在摘要里、标题没提」的论文。摘要为空时自动退回只用标题。

    Args:
        title: 论文标题
        profile: None = 内置示例配置（example-llm-reasoning）；
                 字符串 = gate_profiles.json 里的 id；dict = 直接给一条配置
        abstract: 论文摘要（可空）

    Returns:
        (是否通过, {"<闸门名>": [命中词], ..., "_source": "标题" | "标题+摘要"})
        `_source` 说明这次通过是靠标题就够了，还是必须借助摘要 ——
        靠标题命中的更可信，排序打平时可优先。
    """
    prof = resolve_profile(profile)
    text = f"{title or ''} {abstract or ''}".strip()
    info: Dict = {}
    ok = True
    for axis, terms in (prof.get("axes") or {}).items():
        hit = _match(text, terms)
        info[axis] = hit
        if not hit:
            ok = False

    title_only_ok = bool(abstract) and all(
        _match(title or "", t) for t in (prof.get("axes") or {}).values())
    info["_source"] = "标题" if (not abstract or title_only_ok) else "标题+摘要"
    return ok, info


def explain(title: str, profile=None, abstract: str = "") -> str:
    """给人读的判定说明（标题 + 摘要）。"""
    prof = resolve_profile(profile)
    ok, info = gate(title, profile, abstract)
    lines = [f"主题配置: {prof.get('topic') or prof.get('id')}",
             f"标题: {title}"]
    if abstract:
        lines.append(f"摘要: {abstract[:110]}…")
    missing = []
    for axis in (prof.get("axes") or {}):
        hit = info.get(axis) or []
        lines.append(f"  {axis}: {hit[:6] or '—— 无 ——'}")
        if not hit:
            missing.append(f"缺「{axis}」")
    if ok:
        lines.append(f"  → 【通过】（命中来源：{info.get('_source')}）")
    else:
        lines.append(f"  → 【剔除】（{'；'.join(missing)}）")
    return "\n".join(lines)


def apply_gate(items, profile=None, title_getter=None, abstract_getter=None,
               min_pass=20, min_rate=0.10):
    """批量过闸门（**标题 + 摘要**），并做**过窄自检**。

    返回 (passed, rejected, warning)：
      - passed / rejected 均为 [(原对象, 命中信息), ...]
      - warning 非空 ⇒ 这个主题配置**可能过窄、正在误杀**，
        交付前必须先人工翻看 rejected 前若干条确认。

    为什么必须有这个自检：闸门是"必须命中所有轴"的合取，
    轴设多了、词写窄了就会大面积误杀 —— 实测一套只适配某个题目的配置拿去判
    别的题目，通过率是 0%，而那不是"该领域没有文献"。
    """
    get_t = title_getter or (
        lambda p: getattr(p, "title", "") or
        (p.get("title", "") if isinstance(p, dict) else ""))
    get_a = abstract_getter or (
        lambda p: getattr(p, "abstract", "") or
        (p.get("abstract", "") if isinstance(p, dict) else ""))
    passed, rejected = [], []
    for p in items:
        ok, info = gate(get_t(p), profile, get_a(p))
        (passed if ok else rejected).append((p, info))

    n, k = len(items), len(passed)
    rate = k / n if n else 0.0
    warning = ""
    if n and (k < min_pass or rate < min_rate):
        warning = (f"⚠️ 主题闸门只放行 {k}/{n}（{rate*100:.1f}%），低于安全阈值"
                   f"（≥{min_pass} 篇且 ≥{min_rate*100:.0f}%）。\n"
                   f"  这通常说明**主题配置过窄、正在误杀**，"
                   f"而不是「该领域没有相关文献」。\n"
                   f"  交付前必须先人工翻看 rejected 前若干条；"
                   f"必要时补该配置的闸门词表。")
    return passed, rejected, warning


# ============================================================ CLI

if __name__ == "__main__":
    import io
    import sys
    import argparse
    if sys.platform == "win32":
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                          errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="主题闸门：判定标题是否属于目标领域")
    ap.add_argument("--profile", help="主题配置 id")
    ap.add_argument("--list-profiles", action="store_true")
    ap.add_argument("--explain", nargs="+", help="逐条解释这些标题")
    ap.add_argument("--size", action="store_true", help="只看内置词表规模")
    args = ap.parse_args()

    def _desc(p):
        return " ∧ ".join(f"{k}({len(v)}词)" for k, v in (p.get("axes") or {}).items())

    if args.list_profiles:
        print(f"内置 {DEFAULT_PROFILE['id']}：{DEFAULT_PROFILE['topic']}")
        print(f"     {_desc(DEFAULT_PROFILE)}")
        for pid, p in load_profiles().items():
            print(f"自定义 {pid}：{p.get('topic')}")
            print(f"     {_desc(p)}")
        print("\n规则：命中【全部】闸门才通过；无黑名单。")
        sys.exit(0)

    if args.size:
        print(_desc(DEFAULT_PROFILE))
        sys.exit(0)

    if args.explain:
        for t in args.explain:
            print("\n" + explain(t, args.profile))
        sys.exit(0)

    print(f"内置配置 {DEFAULT_PROFILE['id']}：{_desc(DEFAULT_PROFILE)}")
    print("规则：命中【全部】闸门才通过；无黑名单。")
    for t in [
        "Chain-of-thought prompting elicits reasoning in large language models",
        "A survey of evaluation benchmarks for multi-step reasoning",
        "Scaling laws for emergent abilities in foundation models",
    ]:
        print("\n" + explain(t))
