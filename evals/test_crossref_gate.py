# -*- coding: utf-8 -*-
"""验证：未获得用户同意时，Crossref 备用源必须拒绝执行，且不发出任何网络请求。

用法：python evals/test_crossref_gate.py
预期：输出「全部通过」，退出码 0。
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import crossref_fallback as cf

CALLED = []


def fake_get(*a, **k):
    CALLED.append(1)
    raise AssertionError("门禁失效：不应发出任何网络请求")


cf.requests.get = fake_get

ok = True
try:
    cf.search_crossref(["test query"])
    print("FAIL: 未抛 UserApprovalRequired")
    ok = False
except cf.UserApprovalRequired as e:
    print("PASS: 未获用户同意时拒绝执行")
    print(f"      提示语: {str(e)[:80]}...")

if CALLED:
    print(f"FAIL: 门禁失效，发出了 {len(CALLED)} 次网络请求")
    ok = False
else:
    print("PASS: 未发出任何网络请求")

# 确认局限说明确实包含"只能检索到有 DOI 的文献"
if "只能检索到有 DOI" in cf.DOI_ONLY_NOTICE:
    print("PASS: 局限说明包含“只能检索到有 DOI 的文献”")
else:
    print("FAIL: 局限说明缺失 DOI 限制")
    ok = False

print("结论：" + ("全部通过" if ok else "存在失败项"))
sys.exit(0 if ok else 1)
