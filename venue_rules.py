# -*- coding: utf-8 -*-
"""记录合规判定：会议、会议录卷级条目、预印本、撤稿、版面元素、记录可疑

为什么要集中在一处
------------------
这些判据原先散在 `semantic_scholar_search.filter_papers()`、`paper_table` 的筛选
分支和各驱动脚本里，同一件事写两遍，标准必然漂移。现在只在这里定义，
其余模块一律调用 `classify_record()`。

两个方向的坑
------------
1. **漏判**：`publicationTypes` 挡不住会议论文集。NDT.net 的
   `e-Journal of Nondestructive Testing` 是会议录，标题里既没有 conference
   也没有 proceedings；`e-Journal of Nondestructive Testing` 这类还会被标成
   JournalArticle。
2. **误杀**：`Proceedings of the Institution of Mechanical Engineers` 是**正规期刊**
   （IMechE 会刊），看到 proceedings 就排除会误杀。白名单必须优先于黑名单。

「会议录卷级条目」与「会议论文」是两件事
--------------------------------------
- 会议论文：单篇，在会议上宣讲、收入论文集。
- 会议录（会议论文集）：整本卷册，有自己的书名与 ISBN，内含 N 篇论文。
  S2 会把整卷当成一条 paper 返回，标题就是「会名 + 届次 + 城市」，
  例如 `Czech Society for Nondestructive Testing 32nd European Conference
  on Acoustic Emission Testing Prague`。它不是单篇论文，靠
  `publicationTypes` 和刊名规则都抓不到，必须单独判。

剔除与标记的区别
----------------
- **剔除**：会议论文、会议录卷级条目、预印本、撤稿。
- **只标记**：版面元素、记录可疑、勘误。这三类不删行，只加列。
  用户可以在输出表里自行筛选，误判成本只是一列文字。

判据禁用项
----------
**禁止用「标题词数」或「全大写」当判据。** 短标题里有真论文
（`Ultrasonic Methods`），全大写长标题在老文献里极常见。唯一可用的是精确认词表。
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass
import re

# ---------------------------------------------------------------- 会议论文集（按刊名）
# 命中即判为会议论文集（大小写不敏感）
CONF_VENUE_PATTERN = re.compile(
    r"(conference|congress|symposium"
    r"|proceedings of the international"
    r"|web of conferences"
    r"|e-?journal of nondestructive testing"
    r"|journal of physics,? conference series"
    r"|inter-noise"
    r"|advances in engineering technology research"
    r"|bigdata congress"
    r"|e3s web)",
    re.I,
)

# 虽然是 proceedings / congress 字样，但属正规期刊，白名单优先放行
KEEP_VENUE_PATTERN = re.compile(
    r"(proceedings of the institution of mechanical engineers"
    r"|proceedings of the institution of civil engineers"
    r"|proceedings of the national academy of sciences)",
    re.I,
)

# ---------------------------------------------------------------- 会议录卷级条目（按标题）
PROCEEDING_WORD = re.compile(
    r"(conference|proceedings|congress|symposium|workshop|colloquium)", re.I)
# 届次（32nd）或四位数年份（1998 / 2024）
ORDINAL_OR_YEAR = re.compile(r"\b(\d{1,3}\s*(?:st|nd|rd|th))\b|\b(19|20)\d{2}\b", re.I)

# ---------------------------------------------------------------- 撤稿 / 勘误
RETRACT_PATTERN = re.compile(
    r"^\s*(retracted|retraction note|retraction|withdrawn)\b"
    r"|\(retracted", re.I)
ERRATUM_PATTERN = re.compile(
    r"\b(corrigendum|erratum|errata|correction to|expression of concern)\b", re.I)

# 预印本刊名标记
PREPRINT_VENUE_PATTERN = re.compile(
    r"(arxiv|ssrn|research square|biorxiv|medrxiv|preprint)", re.I)

# ---------------------------------------------------------------- 版面元素（精确词表）
# 只做**归一化后精确匹配**，不做任何模糊匹配。宁可漏，不可误杀。
LAYOUT_TITLES = {
    "publications", "contents", "index", "preface", "foreword",
    "editorial", "editorial board", "front matter", "back matter",
    "abstracts", "table of contents", "author index", "subject index",
    "book reviews", "call for papers", "instructions to authors",
    "information for authors", "news", "obituary", "in memoriam",
    "masthead", "contents list", "volume contents",
}


def _norm_title(s):
    """标题归一化：小写、去标点、压缩空白。"""
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def is_conference_venue(name: str) -> bool:
    """刊名是否属于会议论文集（白名单优先）。"""
    if not name:
        return False
    if KEEP_VENUE_PATTERN.search(name):
        return False
    return bool(CONF_VENUE_PATTERN.search(name))


def is_proceedings_volume(title: str, venue: str = "",
                          doi: str = "", abstract: str = "") -> bool:
    """是否会议录的**卷级条目**（整本论文集被当成一条 paper）。

    判据（三条同时成立，压低误杀）：
      1. 标题含会议类词（conference / proceedings / symposium / ...）
      2. 标题含届次（32nd）或四位数年份
      3. 无 DOI **或** 无摘要  —— 单篇会议论文通常至少有其一

    另有白名单：标题属正规期刊卷名的不判。
    """
    if not title:
        return False
    if KEEP_VENUE_PATTERN.search(title) or KEEP_VENUE_PATTERN.search(venue or ""):
        return False
    if not PROCEEDING_WORD.search(title):
        return False
    if not ORDINAL_OR_YEAR.search(title):
        return False
    return (not doi) or (not abstract)


def is_retracted(title: str) -> bool:
    """标题是否标记为撤稿。撤稿记录**剔除**，并单独记数。"""
    return bool(RETRACT_PATTERN.search(title or ""))


def is_erratum(title: str) -> bool:
    """标题是否为勘误/更正。**不剔除**——原文有效，只是需要知道。"""
    return bool(ERRATUM_PATTERN.search(title or ""))


def is_layout_record(title: str) -> bool:
    """是否为期刊版面元素记录（栏目页、目录页、索引页）。

    只做精确词表匹配，**不按词数或大小写判断**。
    """
    return _norm_title(title) in LAYOUT_TITLES


def is_suspicious(doi: str, venue: str, abstract: str) -> bool:
    """记录可疑：DOI、刊名、摘要三者全空。

    三项全空的记录几乎不可能是正常期刊论文。**只标记不剔除**，
    因为偶尔会有确实如此的真论文（例如极老的内部报告）。
    """
    return not (doi or "").strip() and not (venue or "").strip() \
        and not (abstract or "").strip()


def classify_record(title="", venue="", publication_types=None,
                    external_ids=None, doi="", abstract="", year=0) -> dict:
    """一次性判完六类，返回标志位与命中原因。

    Returns:
        {
          "is_conference": bool,          会议论文（剔除）
          "is_proceedings_volume": bool,  会议录卷级条目（剔除）
          "is_preprint": bool,            预印本（剔除）
          "is_retracted": bool,           撤稿（剔除）
          "is_erratum": bool,             勘误（只标记）
          "is_layout_record": bool,       版面元素（只标记）
          "is_suspicious": bool,          记录可疑（只标记）
          "reasons": [str, ...]           命中的中文说明，供淘汰记录引用
        }
    """
    publication_types = publication_types or []
    external_ids = external_ids or {}
    reasons = []

    is_preprint = "ArXiv" in external_ids
    if is_preprint:
        reasons.append("预印本(arXiv ID)")
    elif PREPRINT_VENUE_PATTERN.search(venue or ""):
        is_preprint = True
        reasons.append("预印本(刊名含预印本平台)")

    if "Conference" in publication_types:
        reasons.append("会议论文(publicationTypes)")

    if is_conference_venue(venue):
        reasons.append("会议论文集(刊名规则)")

    vol = is_proceedings_volume(title, venue, doi, abstract)
    if vol:
        reasons.append("会议录卷级条目")

    ret = is_retracted(title)
    if ret:
        reasons.append("撤稿")

    err = is_erratum(title)
    if err:
        reasons.append("勘误(仅标记)")

    layout = is_layout_record(title)
    if layout:
        reasons.append("版面元素(仅标记)")

    susp = is_suspicious(doi, venue, abstract)
    if susp:
        reasons.append("记录可疑(无DOI无刊名无摘要,仅标记)")

    return {
        "is_conference": ("Conference" in publication_types
                          or is_conference_venue(venue)),
        "is_proceedings_volume": vol,
        "is_preprint": is_preprint,
        "is_retracted": ret,
        "is_erratum": err,
        "is_layout_record": layout,
        "is_suspicious": susp,
        "reasons": reasons,
    }


def should_drop(cls: dict, allow_preprint: bool = False,
                allow_conference: bool = False) -> bool:
    """按判定结果决定是否剔除（标记类不剔除）。

    两个开关默认都关（即保持「只要期刊论文」）：

    - `allow_preprint=True`：放行预印本。计算机等领域的预印本是主要载体。
    - `allow_conference=True`：放行**单篇**会议论文。

    以下两类**不受开关影响，永远剔除**：
      - `is_proceedings_volume`：会议录卷级条目。那是整本论文集的记录，
        不是一篇论文，放进来只会污染结果。
      - `is_retracted`：撤稿。学术记录有问题，不能因为放宽类型而放进来。

    ⚠️ 放开会议时要注意「会议」不等于同一质量：NeurIPS 论文与
    Journal of Physics: Conference Series 上的会议论文差别极大，
    放行后需靠后续分区与人工复核兜住。
    """
    if cls.get("is_retracted"):
        return True
    if cls.get("is_proceedings_volume"):
        return True
    if cls.get("is_preprint") and not allow_preprint:
        return True
    if cls.get("is_conference") and not allow_conference:
        return True
    return False


if __name__ == "__main__":
    samples = [
        # (标题, 刊名, DOI, 摘要)
        ("Ultrasonic Methods", "Structural Health Monitoring", "10.1/x", ""),
        ("PUBLICATIONS", "College & research libraries news", "", ""),
        ("RETRACTED: Recognition method of internal concrete structure defects",
         "Journal of Intelligent & Fuzzy Systems", "10.2/x", "x"),
        ("Czech Society for Nondestructive Testing 32nd European Conference "
         "on Acoustic Emission Testing Prague", "", "", ""),
        ("Nondestructive Testing of Concrete by Wave Velocity Methods",
         "Proceedings of the Institution of Mechanical Engineers, Part F",
         "10.3/x", "x"),
        ("Corrigendum to 'Impact echo for slab thickness'",
         "Construction and Building Materials", "10.4/x", "x"),
        ("Estimating Concrete Strength Using Shear Wave Velocity", "", "", ""),
    ]
    for t, v, d, a in samples:
        c = classify_record(title=t, venue=v, doi=d, abstract=a)
        flag = "剔除" if should_drop(c) else "保留"
        print(f"{flag}  {t[:56]:<56} | {';'.join(c['reasons']) or '-'}")
