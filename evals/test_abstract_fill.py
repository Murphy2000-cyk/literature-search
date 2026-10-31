# -*- coding: utf-8 -*-
"""abstract_fill 自测（全离线，不联网）

覆盖：
1. 各来源的降级顺序与跳过规则（非 Elsevier 论文不查 Elsevier 通道）
2. 无权限（HTTP 200 但无 dc:description）必须记成"无权限"而不是"没摘要"
3. fill_table 能把摘要与来源写回 xlsx，并统计来源分布
4. 没有 DOI 的行不崩

运行：python evals/test_abstract_fill.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.stdout.reconfigure(encoding="utf-8")

import abstract_fill as af

fails = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}: {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(label)


# ---------------------------------------------------------------- 1) 跳过规则
def fake_elsevier(doi, key, timeout=40):
    if not key:
        return None, "没有 ELS_API_KEY"
    return "ELSEVIER-ABS", "命中"


orig = af.from_elsevier
af.from_elsevier = fake_elsevier
try:
    ab, src, log = af.fill_one("10.1016/j.x", key="k", sources=("elsevier",))
    check("Elsevier 论文走 Elsevier 通道", src == "elsevier" and ab == "ELSEVIER-ABS")

    ab2, src2, log2 = af.fill_one("10.1080/abc", key="k", sources=("elsevier",))
    check("非 Elsevier 论文跳过该通道", ab2 is None and any("跳过" in x for x in log2),
          str(log2[-1:]))
finally:
    af.from_elsevier = orig


# ---------------------------------------------------------------- 2) 无权限的措辞
def fake_elsevier_denied(doi, key, timeout=40):
    return None, "无 dc:description（字段 22 个 → 无订阅权限或 IP 不在机构内）"


af.from_elsevier = fake_elsevier_denied
try:
    ab, src, log = af.fill_one("10.1016/j.x", key="k", sources=("elsevier",))
    joined = " ".join(log)
    check("无权限被明确标注", ab is None and "无订阅权限" in joined)
    check("未误记成'没摘要'", "没有摘要" not in joined)
finally:
    af.from_elsevier = orig


# ---------------------------------------------------------------- 3) 降级到下一个源
def fake_crossref_empty(doi, timeout=25):
    return None, "无摘要字段"


def fake_openalex_hit(doi, timeout=25):
    return "OPENALEX-ABS", "命中"


o1, o2 = af.from_crossref, af.from_openalex
af.from_crossref, af.from_openalex = fake_crossref_empty, fake_openalex_hit
try:
    ab, src, log = af.fill_one("10.1000/xyz", key="", sources=("crossref", "openalex"))
    check("Crossref 空 → 落到 OpenAlex", ab == "OPENALEX-ABS" and src == "openalex")
finally:
    af.from_crossref, af.from_openalex = o1, o2


# ---------------------------------------------------------------- 4) 写回 xlsx
from openpyxl import Workbook, load_workbook

tmpd = tempfile.mkdtemp(prefix="dsh_af_")
path = os.path.join(tmpd, "t.xlsx")
wb = Workbook()
ws = wb.active
ws.append(["标题", "DOI", "摘要"])
ws.append(["论文A", "10.1016/j.a", ""])
ws.append(["论文B", "", ""])                # 无 DOI
wb.save(path)

def fake_fill_one(doi, key="", sources=(), sleep=0.2):
    if doi == "10.1016/j.a":
        return "FAKE-ABS", "elsevier", ["elsevier: 命中"]
    return None, "", ["没有 DOI"]

o = af.fill_one
af.fill_one = fake_fill_one
try:
    stat = af.fill_table(path, verbose=False)
finally:
    af.fill_one = o

check("fill_table 统计正确", stat["total"] == 2 and stat["ok"] == 1
      and stat["by_source"].get("elsevier") == 1, str(stat["by_source"]))
check("无 DOI 的记录进失败清单", len(stat["failed"]) == 1)

ws2 = load_workbook(path).active
hdr = [c.value for c in ws2[1]]
row = {hdr[i]: c.value for i, c in enumerate(ws2[2])}
check("摘要已写回", row.get("摘要") == "FAKE-ABS")
check("新增「摘要来源」列", "摘要来源" in hdr and row.get("摘要来源") == "elsevier")

print()
print("结论：全部通过" if not fails else f"结论：{len(fails)} 项失败 -> {fails}")
sys.exit(0 if not fails else 1)
