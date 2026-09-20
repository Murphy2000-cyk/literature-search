# -*- coding: utf-8 -*-
"""验证失败重试策略（v1.4.0）

策略（用户指定，2026-09-11）：
  首次尝试 → 失败等 10s → 第 2 次 → 失败等 10s → 第 3 次 → 仍失败即暂停检索，
  并向用户提问「降级使用 Crossref，还是持续等待 3~5 分钟后重试」。

校验项：
  1. 首次 + 2 次重试，共 3 次请求后停止，抛 ApiUnavailableError（不多不少）
  2. 每次都携带 API Key（x-api-key），绝不降级为匿名请求
  3. 密钥不出现在 URL 参数中
  4. 异常消息必须包含"二选一"提问（Crossref / 等待 3~5 分钟），否则调用方不会提问
  5. 默认不进入长等待；long_wait_on_failure=True 时才追加最后一次尝试（共 4 次）
  6. 不会无限重试 / 持续等待

用法：python evals/test_ratelimit.py
预期：数秒内结束，输出「全部通过」，退出码 0。
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import semantic_scholar_search as mod  # noqa: E402
from semantic_scholar_search import SemanticScholarSearcher, ApiUnavailableError  # noqa: E402

CALLS = []


class FakeResp:
    """始终返回 429 的假响应"""
    status_code = 429
    text = '{"message":"Too Many Requests"}'
    headers = {}

    def json(self):
        return {}


def fake_request(url, params=None, headers=None, **kwargs):
    CALLS.append({"params": dict(params or {}), "headers": dict(headers or {})})
    return FakeResp()


mod.requests.get = fake_request
mod.requests.post = fake_request

results = []


def check(idx, desc, cond, extra=""):
    results.append(cond)
    print(f"[{idx}] {desc} -> {'OK' if cond else 'FAIL'}{(' | ' + extra) if extra else ''}")


# ---------- 场景 1：默认策略（快失败 + 提问） ----------
s = SemanticScholarSearcher(api_key="s2k-fake-for-test")
s.RETRY_WAITS_S = (1, 1)      # 实测策略为 (10, 10)，测试中缩短以提速
s.REQUEST_INTERVAL = 0.1

t0 = time.time()
raised = False
msg = ""
try:
    s.search_first_round("test query", limit=5)
except ApiUnavailableError as e:
    raised = True
    msg = str(e)
    print(f"PASS: {time.time() - t0:.1f}s 内抛出 ApiUnavailableError")
    print(f"      最近原因: {s.last_failure[:80]}")

check(1, f"抛出 ApiUnavailableError = {raised}", raised)
check(2, f"请求次数 = {len(CALLS)}（期望 3 = 首次 + 2 次重试）", len(CALLS) == 3)

ok_key = all(c["headers"].get("x-api-key") for c in CALLS)
check(3, f"全部携带 x-api-key = {ok_key}（禁止匿名请求）", ok_key)

ok_no_anon = all("api_key" not in c["params"] for c in CALLS)
check(4, f"URL 参数未携带密钥 = {ok_no_anon}", ok_no_anon)

ok_ask = ("Crossref" in msg) and ("3~5" in msg or "3-5" in msg)
check(5, f"异常消息含二选一提问（Crossref / 等待 3~5 分钟）= {ok_ask}", ok_ask)
if not ok_ask:
    print("      实际消息：", msg[:400])

# 默认策略不得暗中长等待
check(6, "默认 long_wait_on_failure = False",
      SemanticScholarSearcher(api_key="x").long_wait_on_failure is False)

# ---------- 场景 2：用户选择"持续等待"分支 ----------
CALLS.clear()
s2 = SemanticScholarSearcher(api_key="s2k-fake-for-test")
s2.RETRY_WAITS_S = (1, 1)
s2.LONG_WAIT_S = 1          # 真实为 240s，测试缩短
s2.REQUEST_INTERVAL = 0.1
s2.long_wait_on_failure = True

raised2 = False
try:
    s2.search_first_round("test query", limit=5)
except ApiUnavailableError as e:
    raised2 = True
    print(f"PASS(2): {time.time() - t0:.1f}s 内抛出 ApiUnavailableError")

check(7, f"长等待模式请求次数 = {len(CALLS)}（期望 4 = 首次 + 2 次重试 + 1 次长等后尝试）",
      len(CALLS) == 4)
check(8, "长等待模式仍然抛出 ApiUnavailableError（不无限重试）", raised2)
check(9, "长等待模式仍全部携带 x-api-key",
      all(c["headers"].get("x-api-key") for c in CALLS))

print(f"结论：{'全部通过' if all(results) else '存在失败项'}")
sys.exit(0 if all(results) else 1)
