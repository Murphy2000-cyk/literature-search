# -*- coding: utf-8 -*-
"""venue_rules 自测：会议论文集识别的正例/反例

运行：python evals/test_venue_rules.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from venue_rules import is_conference_venue

CASES = [
    # (刊名, 期望, 说明)
    ("e-Journal of Nondestructive Testing", True, "NDT.net 会议录，标题里没有 conference"),
    ("Journal of Physics: Conference Series", True, "JPCS"),
    ("E3S Web of Conferences", True, "E3S 会议录"),
    ("INTER-NOISE and NOISE-CON Congress and Conference Proceedings", True, "inter-noise"),
    ("Italian National Conference on Sensors", True, "刊名含 Conference"),
    ("BigData Congress [Services Society]", True, "Congress"),
    ("Advances in Engineering Technology Research", True, "会议录刊名"),
    ("Proceedings of the International Conference on Concrete Pavements", True, "proceedings of the international"),
    ("Structural Health Monitoring", False, "正规期刊，不该误杀"),
    ("Ultrasonics", False, "正规期刊"),
    ("Proceedings of the Institution of Mechanical Engineers, Part F", False,
     "IMechE 会刊是正规期刊，白名单必须放行"),
    ("Proceedings of the Institution of Civil Engineers", False, "ICE 会刊"),
    ("", False, "空刊名"),
    (None, False, "None"),
]

fails = 0
for name, want, why in CASES:
    got = is_conference_venue(name)
    ok = got == want
    fails += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}: {str(name)[:58]:<58} "
          f"期望={want} 实际={got}  ({why})")

print()
print("结论：全部通过" if fails == 0 else f"结论：{fails} 项失败")
sys.exit(0 if fails == 0 else 1)
