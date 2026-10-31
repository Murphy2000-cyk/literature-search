# -*- coding: utf-8 -*-
"""会议论文集识别（按刊名判断）

为什么需要它
------------
Semantic Scholar 的 `publicationTypes` 挡不住会议论文集。实测（2026-10-29，553 篇候选）
标为 Conference 的只有 27 篇，但**另有 99 篇刊名明显是会议论文集**却标成 JournalArticle：

    e-Journal of Nondestructive Testing            47 篇
    Italian National Conference on Sensors         16 篇
    Journal of Physics: Conference Series           8 篇
    E3S Web of Conferences                          3 篇
    INTER-NOISE / NOISE-CON                         2 篇
    BigData Congress                                3 篇
    ...

用户说"只要期刊论文、排除会议"时，这 99 篇必须一起排除，否则过滤形同虚设。

两个方向的坑
------------
1. **漏判**：刊名里不一定写 conference。NDT.net 的 `e-Journal of Nondestructive Testing`
   是会议录，标题里既没有 conference 也没有 proceedings。
2. **误杀**：`Proceedings of the Institution of Mechanical Engineers` 是**正规期刊**
   （IMechE 会刊），看到 proceedings 就排除会误杀。

所以这里用「黑名单型正则 + 白名单型正则」两个模式，白名单优先。
"""
import re

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
    r"|proceedings of the institution of civil engineers)",
    re.I,
)


def is_conference_venue(name: str) -> bool:
    """刊名是否属于会议论文集。

    Args:
        name: 期刊/会议名（Semantic Scholar 的 venue 字段）

    Returns:
        True = 判为会议论文集；False = 不像（或属白名单正规期刊）
    """
    if not name:
        return False
    if KEEP_VENUE_PATTERN.search(name):
        return False
    return bool(CONF_VENUE_PATTERN.search(name))


if __name__ == "__main__":
    samples = [
        "Structural Health Monitoring",
        "e-Journal of Nondestructive Testing",
        "Journal of Physics: Conference Series",
        "E3S Web of Conferences",
        "INTER-NOISE and NOISE-CON Congress and Conference Proceedings",
        "Proceedings of the Institution of Mechanical Engineers, Part F",
        "Italian National Conference on Sensors",
    ]
    for s in samples:
        print(f"{'会议' if is_conference_venue(s) else '期刊'}  {s}")
