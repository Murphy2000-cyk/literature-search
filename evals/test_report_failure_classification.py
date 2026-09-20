# -*- coding: utf-8 -*-
"""回归测试：请求失败的分类与部分结果保全。

背景（v1.3.3 连续发现的两个 bug）：
  1. HTTP 504 返回 None，被上层当成"结果为空 = 查询完成"，报告写成
     [ok] 取回 0 篇 —— 把网络故障伪装成"确实未命中"。
  2. 翻页中途失败时，首页已取回的文献随异常丢失（return 未执行到），
     报告里的"已取回 N 篇"无法交付。

用法：python evals/test_report_failure_classification.py
预期：输出「全部通过」，退出码 0。
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import semantic_scholar_search as mod
from semantic_scholar_search import (
    SemanticScholarSearcher, ApiUnavailableError, RequestFailedError,
)


class Resp:
    def __init__(self, status, body="{}"):
        self.status_code = status
        self.text = body
        self.headers = {}

    def json(self):
        return json.loads(self.text)


def searcher_with(fake):
    s = SemanticScholarSearcher(api_key="s2k-test")
    s.REQUEST_INTERVAL = 0
    s.RETRY_WAITS_S = (0, 0, 0)
    mod.requests.get = fake
    mod.requests.post = fake
    return s


def run():
    ok = True

    # 1) HTTP 504 网关超时 —— 必须记为 failed，不能记为 ok
    s = searcher_with(lambda url, params=None, headers=None, **k: Resp(
        504, '{"message":"Network error communicating with endpoint"}'))
    try:
        s.search_paginated("q504", total_limit=200)
    except ApiUnavailableError:
        pass
    rep = s.search_report()
    st = rep["detail"][0]["status"]
    c1 = (st == "failed") and (rep["complete"] is False) and (rep["queries_failed"] == 1)
    ok &= c1
    print(f"[1] HTTP 504 -> status={st}, complete={rep['complete']} {'OK' if c1 else 'FAIL'}")

    # 2) HTTP 400 不可重试 —— 同样必须记为 failed
    s = searcher_with(lambda url, params=None, headers=None, **k: Resp(
        400, '{"message":"bad request"}'))
    try:
        s.search_paginated("q400", total_limit=200)
    except (ApiUnavailableError, RequestFailedError):
        pass
    rep = s.search_report()
    st = rep["detail"][0]["status"]
    c2 = (st == "failed") and (rep["complete"] is False)
    ok &= c2
    print(f"[2] HTTP 400 -> status={st}, complete={rep['complete']} {'OK' if c2 else 'FAIL'}")

    # 3) 真正的空结果（200 + 空 data）—— 才允许记为 ok
    s = searcher_with(lambda url, params=None, headers=None, **k: Resp(200, '{"data":[]}'))
    s.search_paginated("qempty", total_limit=200)
    rep = s.search_report()
    st = rep["detail"][0]["status"]
    c3 = (st == "ok") and (rep["complete"] is True) and (rep["papers_returned"] == 0)
    ok &= c3
    print(f"[3] 真空结果 -> status={st}, complete={rep['complete']} {'OK' if c3 else 'FAIL'}")

    # 4) 翻页中途失败 —— 首页已取回的 100 篇必须被保全
    calls = {"n": 0}

    def page_then_fail(url, params=None, headers=None, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            data = [{"paperId": f"p{i}", "title": f"t{i}", "authors": [], "year": 2020,
                     "venue": "V", "citationCount": 1,
                     "publicationTypes": ["JournalArticle"]} for i in range(100)]
            return Resp(200, json.dumps({"data": data}))
        return Resp(429)

    s = searcher_with(page_then_fail)
    try:
        s.search_paginated("qpartial", total_limit=200)
    except ApiUnavailableError:
        pass
    rep = s.search_report()
    part = s.collected_partial()
    d0 = rep["detail"][0]
    c4 = (len(part) == 100) and (d0["returned"] == 100) and (d0["status"] == "failed")
    ok &= c4
    print(f"[4] 翻页中途失败 -> 保全 {len(part)} 篇, 报告记 {d0['returned']} 篇, "
          f"status={d0['status']} {'OK' if c4 else 'FAIL'}")

    print("结论：" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
