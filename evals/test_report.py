# -*- coding: utf-8 -*-
"""验证「如实汇报」留痕机制：能区分 确实未命中 / 查询失败 / 达到抓取上限。

用法：python evals/test_report.py
预期：输出「全部通过」，退出码 0。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import semantic_scholar_search as mod
from semantic_scholar_search import SemanticScholarSearcher, ApiUnavailableError


class Resp:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data or {}
        self.text = "{}"
        self.headers = {}

    def json(self):
        return self._data


def make_searcher(fake):
    s = SemanticScholarSearcher(api_key="s2k-test")
    s.REQUEST_INTERVAL = 0
    mod.requests.get = fake
    mod.requests.post = fake
    return s


def run():
    ok = True

    # 场景 1：API 正常但确实未命中
    s = make_searcher(lambda url, params=None, headers=None, **k: Resp(200, {"data": []}))
    s.search_paginated("nothing here", total_limit=100)
    rep = s.search_report()
    cond1 = rep["papers_returned"] == 0 and rep["complete"] and rep["queries_failed"] == 0
    ok &= cond1
    print(f"[1] 确实未命中 -> returned={rep['papers_returned']}, complete={rep['complete']} "
          f"{'OK' if cond1 else 'FAIL'}")
    print(f"      notes={rep['notes']}")

    # 场景 2：查询失败（持续 429）
    s = make_searcher(lambda url, params=None, headers=None, **k: Resp(429))
    s.RETRY_WAITS_S = (0, 0, 0)
    try:
        s.search_paginated("will fail", total_limit=100)
    except ApiUnavailableError:
        pass
    rep = s.search_report()
    cond2 = rep["queries_failed"] == 1 and not rep["complete"]
    ok &= cond2
    print(f"[2] 查询失败 -> failed={rep['queries_failed']}, complete={rep['complete']} "
          f"{'OK' if cond2 else 'FAIL'}")
    print(f"      notes={rep['notes']}")

    # 场景 3：达到抓取上限（可能仍有更多文献）
    def full_get(url, params=None, headers=None, **k):
        off = int((params or {}).get("offset", 0))
        data = [{"paperId": f"p{off + i}", "title": f"t{off + i}", "authors": [],
                 "year": 2020, "venue": "V", "citationCount": 1,
                 "publicationTypes": ["JournalArticle"]} for i in range(100)]
        return Resp(200, {"data": data})

    s = make_searcher(full_get)
    s.search_paginated("lots", total_limit=100)
    rep = s.search_report()
    cond3 = rep["queries_capped"] == 1 and not rep["complete"]
    ok &= cond3
    print(f"[3] 达到上限 -> capped={rep['queries_capped']}, complete={rep['complete']} "
          f"{'OK' if cond3 else 'FAIL'}")
    print(f"      notes={rep['notes']}")

    # 场景 4：报告落盘
    out = os.path.join(os.environ.get("TEMP", "."), "litreport_test")
    txt = s.save_search_report(out)
    cond4 = os.path.exists(txt) and os.path.exists(os.path.join(out, "检索完整性报告.json"))
    ok &= cond4
    print(f"[4] 报告落盘 -> {txt} {'OK' if cond4 else 'FAIL'}")

    print("结论：" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
