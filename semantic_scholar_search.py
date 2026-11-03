#!/usr/bin/env python3
"""
Semantic Scholar API 文献检索脚本 v1.4.0

认证：API Key 通过**请求头 x-api-key** 传递（与官方 tutorial 一致；不使用 URL 参数、不使用匿名）。
速率：相邻请求最小间隔 2.5 秒（官方限 1 请求/秒，此处主动留足余量）。
失败：首次尝试 + 2 次重试，每次失败后等 10 秒；三次均失败即**暂停检索并向用户提问**
      「降级使用 Crossref，还是持续等待 3~5 分钟后重试」（详见 ApiUnavailableError）。
支持：多轮检索、API级筛选、分页、排序、JSON输出、引用网络扩展、集成分区查询
"""
import sys
# 控制台编码兜底：Windows 默认 GBK，打印中文/特殊字符会 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import io

# Windows GBK 编码兼容（仅在直接运行时启用，import 时不触发）
def _fix_encoding():
    if sys.platform == 'win32' and hasattr(sys.stdout, 'buffer'):
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass  # 已经被包装过或不可用

import requests
import time
import os
import json
from datetime import datetime, date
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, asdict


def _load_env_from_dsh_env() -> None:
    """
    补载 API 凭据（含 SEMANTIC_SCHOLAR_API_KEY）。

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

# 输出目录：可用环境变量 LITSEARCH_OUT_DIR 覆盖；默认 ~/literature-search-out（自动创建）
# 默认值不含任何机器专属路径，换机器或给别人用都能直接跑
DEFAULT_SAVE_DIR = os.getenv(
    "LITSEARCH_OUT_DIR",
    os.path.join(os.path.expanduser("~"), "literature-search-out"),
)


def resolve_save_dir(save_dir: Optional[str]) -> str:
    """确保输出目录存在；不可写时回退到当前目录。"""
    target = save_dir or DEFAULT_SAVE_DIR
    try:
        os.makedirs(target, exist_ok=True)
        return target
    except Exception:
        fallback = os.getcwd()
        print(f"[WARN] Cannot write to {target}, falling back to {fallback}", file=sys.stderr)
        return fallback


@dataclass
class PaperInfo:
    """论文信息（对应「数据种类清单」的七类数据）"""
    paper_id: str
    title: str
    authors: List[str]
    year: int
    venue: str
    citation_count: int
    abstract: str
    doi: str
    pdf_url: str
    is_conference: bool = False
    is_journal: bool = False
    is_preprint: bool = False
    quartile: str = ""
    relevance_score: float = 0.0
    # 公开发表时间：优先 S2 的 publicationDate（YYYY-MM-DD），缺失则空
    publication_date: str = ""
    # 刊名取自哪个字段，便于事后核查回退链走到了哪一级
    venue_source: str = ""
    # ISSN 只取自 publicationVenue.issn，不向任何外部途径反查
    issn: str = ""
    # 关键词由 OpenAlex 在排序前补齐，S2 不提供此字段
    keywords: str = ""
    # 分区判定依据（jcr / xr）与状态
    zone_basis: str = ""
    zone_status: str = ""
    # 摘要来源：S2 / OpenAlex / Elsevier / PubMed / 空
    abstract_source: str = ""
    # 是否命中记录合规规则：撤稿 / 版面元素 / 记录可疑
    is_retracted: bool = False
    is_layout_record: bool = False
    is_suspicious: bool = False
    record_reasons: str = ""


class ApiUnavailableError(RuntimeError):
    """API Key 连续失败，已按策略暂停检索。**必须立即向用户汇报并提问**。

    触发条件：首次请求 + 2 次重试（每次失败后等 10s）全部失败。
    失败包含 429（限流）、401/403（密钥无效或被拒）、5xx（服务端/网关异常，
    如 504 Network error communicating with endpoint）、网络或代理错误。

    触发后【必须】向用户提出二选一问题（**按用户偏好排序**）：
        1) **持续等待 3~5 分钟后重试** —— 一般情况下用户首选等 S2 自行恢复；
        2) 降级使用 Crossref 备用源 —— **最后手段**。

    触发后禁止：
      - 改用匿名请求（匿名走全局共享池，且违背用户明确要求）
      - 自行继续重试或继续等待（是否长等必须由用户决定）
      - 继续遍历剩余查询（会把时间成倍拖长）
      - 把失败当作"没有检索到文献"返回空结果（最危险的静默失败）
    """


class RequestFailedError(RuntimeError):
    """单次请求返回不可重试的错误（如 400/404），该查询失败。

    与 ApiUnavailableError 的区别：这**不代表密钥不可用**，只说明该次请求本身有问题。
    但两者都必须被记录为 status=failed，**绝不能当作"结果为空 = 查询完成"**。
    """


# 向后兼容别名（旧调用方仍可用 RateLimitError 捕获）
RateLimitError = ApiUnavailableError


# relevance search（/paper/search）平台硬上限：offset + limit ≤ 1000。
# 2023-10-31 起由 10000 下调（官方 release notes），旧文档中的 10000 已失效。
# 需要更多文献必须改用 paper/bulkSearch（可分页至千万级）或 Datasets API。
RELEVANCE_MAX_RESULTS = 1000

# 单次 API 请求最多 100 条（平台限制）；这也是"每个查询取多少"的默认值。
DEFAULT_PER_QUERY_LIMIT = 100

# **写入元数据表的篇数上限。默认 None = 不截断**（闸门后有多少写多少）。
#
# 为什么改成"不截断"（2026-10-30 用户定）：
#   检索量已由 DEFAULT_PER_ROUND 唯一决定，表和它不一致就会出现"查 2000 只留 600"的
#   无谓损耗——被截掉的可能正是要按分区/年份再筛的一区刊（实测 BGE 前 200 名常整本漏掉）。
#   想要更小的表，用 `--top N` 显式压小，那是落盘时的选择，不是默认行为。
#   save_papers_xlsx / search_pipeline 均已支持 None = 不截断（0 同义）。
DEFAULT_TOTAL_LIMIT = None

# ---------------------------------------------------------------- 请求字段（唯一定义）
# 「数据种类清单」的 S2 侧字段，**只允许在这里定义一次**。
#
# 历史教训（本文件旧版）：同一份字段串在五个地方各写一份，其中检索请求漏了
# `journal` 与 `publicationVenue`，而解析函数也只读 `venue` —— 请求与解析
# 两边完全自洽，所以永不报错，但大量刊名在 S2 侧明明有、本地却是空列。
# 这类「自洽地错」只能靠 assert_fields_cover() 在启动时拦下。
#
# 七类数据与字段的对应：
#   题目 → title            关键词 → OpenAlex（S2 无此字段）
#   DOI  → externalIds     作者   → authors
#   期刊 → journal / publicationVenue / venue（三级回退）
#   摘要 → abstract         发表时间 → publicationDate / year
S2_FIELDS_LIST = [
    "paperId", "title", "externalIds", "authors",
    "journal", "publicationVenue", "venue",
    "abstract", "publicationDate", "year",
    "citationCount", "publicationTypes", "openAccessPdf",
]
S2_FIELDS = ",".join(S2_FIELDS_LIST)

# 解析函数实际读取的字段集合。新增读取项时必须同步加进 S2_FIELDS_LIST，
# 否则 assert_fields_cover() 会抛错。
PARSED_FIELDS = set(S2_FIELDS_LIST)


def assert_fields_cover(fields: str, need: Optional[set] = None) -> None:
    """字段存在性断言：请求的字段必须覆盖解析要读取的字段。

    **只校验键是否存在，绝不校验值是否为空。**
    键不存在 = 代码 bug（请求参数漏了字段名），启动即抛错；
    键存在但值为空 = 数据事实（这篇论文确实没有），走回退链并保留行。

    不设「某字段空值率超过 X% 就中止」这类闸门 —— 空值率可以进完整性报告，
    不能当作准入条件，否则老文献会因为没摘要而让整次检索失败。
    """
    need = PARSED_FIELDS if need is None else need
    got = {f.strip().split(".")[0] for f in fields.split(",") if f.strip()}
    missing = need - got
    if missing:
        raise AssertionError(
            "请求字段未覆盖解析字段，缺少：" + str(sorted(missing))
            + "。请补进 S2_FIELDS_LIST，不要在调用处临时拼字段串。")


# 兼容旧名：lite 检索只要这几个字段；其余场合一律用 S2_FIELDS
DETAIL_FIELDS = "externalIds,abstract,openAccessPdf"
SEARCH_FIELDS = "paperId,title,authors,year,venue,citationCount,publicationTypes"
SEARCH_FIELDS_FULL = S2_FIELDS


# 按 ID 复取摘要用的窄字段集。**不要拿它喂 _parse_paper** ——
# 它不含 journal/publicationVenue，会触发字段存在性断言；这里只拼字典。
ROUND2_FIELDS = "paperId,abstract,externalIds,openAccessPdf"


def resolve_venue(item: Dict) -> Tuple[str, str]:
    """取刊名：journal.name → publicationVenue.name → venue。

    三级都空时返回 ("", "")，由上层标「刊名为空」。
    **只在 S2 响应内部回退**，不向 Crossref 或任何外部途径反查。

    Returns:
        (刊名, 来源字段名)
    """
    journal = item.get("journal") or {}
    name = (journal.get("name") or "").strip()
    if name:
        return name, "journal.name"

    pv = item.get("publicationVenue") or {}
    name = (pv.get("name") or "").strip()
    if name:
        return name, "publicationVenue.name"

    name = (item.get("venue") or "").strip()
    if name:
        return name, "venue"

    return "", ""


def venue_issn(item: Dict) -> str:
    """ISSN 只取自 publicationVenue.issn，取不到就空，不外部反查。"""
    pv = item.get("publicationVenue") or {}
    return (pv.get("issn") or "").strip()


def year_window(years: int = 10, today: Optional[date] = None) -> Tuple[date, int]:
    """「近 N 年」窗口：返回精确下界日期与下界年份。

    下界 = 今天 - N 年（精确到日）。**只设下界、不设上界**：publicationDate
    可能是 in-press 的未来日期，硬卡上界会把正常论文误删。

    注意 S2 的 `year` 参数只能按年传（`year=A-B`），表达不了「2016-11-02」，
    所以请求端按年份放宽、本地再按日期精确裁。

    Returns:
        (下界日期, 下界年份)
    """
    today = today or date.today()
    try:
        lower = today.replace(year=today.year - years)
    except ValueError:
        # 2 月 29 日遇到非闰年
        lower = today.replace(year=today.year - years, day=28)
    return lower, lower.year


def _classify_record(**kwargs) -> Dict:
    """调 venue_rules 判记录类别；模块缺失或未实现时退化为空判定（不阻断检索）。"""
    try:
        from venue_rules import classify_record
        return classify_record(**kwargs) or {}
    except Exception:
        return {}

# ---------------------------------------------------------------- 关键词上限
# **单条查询最多几个关键词（用户 2026-09-20 指定：5 个）。**
#
# 依据（2026-09-18 对照实测）：S2 是相关性检索，查询词越多实际返回量越少 ——
#     7 词 → 100 篇 ； 9 词 → 11 篇 ； 13 词 → 6 篇
# 少词反而召回更多；靠 3~4 轮**微调变体**覆盖不同角度，
# 而不是把一条查询写成一句话。详见 AGENTS.md §3.5.4.1。
MAX_KEYWORDS = 5

# 标准检索的轮数区间（用户 2026-09-20 指定：固定 3~4 轮）
ROUNDS_MIN = 3
ROUNDS_MAX = 4

# 每轮默认取回篇数（用户 2026-10-30 指定：500 = **5 页**，每页 100 是平台硬上限）。
# 一次"取 500 篇"在接口层是 **5 次请求**（offset 0/100/200/300/400），不是 1 次。
# 请求总量 = ceil(每轮篇数/100) × 轮数；4 轮 500 篇 = 20 次请求。
DEFAULT_PER_ROUND = 300


def check_keyword_count(query: str, max_keywords: int = MAX_KEYWORDS,
                        strict: bool = True) -> Tuple[int, str]:
    """校验单条查询的关键词个数（用户 2026-09-20 指定上限 5 个）。

    为什么必须卡死：S2 relevance 检索对查询长度极敏感，
    实测 7 词 → 100 篇、9 词 → 11 篇、13 词 → 6 篇（同刻对照，见 AGENTS.md §3.5.4.1）。
    查询写长不是"更精确"，而是**几乎召回不到东西**。

    Args:
        query: 查询串
        max_keywords: 允许的最大关键词数（默认 5）
        strict: True = 超限直接抛 ValueError（默认，避免静默少召回）；
                False = 只在返回值里标注

    Returns:
        (关键词个数, 状态说明)

    Raises:
        ValueError: strict=True 且超出上限
    """
    words = [w for w in (query or "").split() if w.strip()]
    n = len(words)
    if n > max_keywords:
        msg = (f"查询 {query!r} 有 {n} 个关键词，超过上限 {max_keywords}。\n"
               f"  实测依据：S2 是相关性检索，查询越长实际返回越少"
               f"（7 词→100 篇，9 词→11 篇，13 词→6 篇）。\n"
               f"  正确做法：拆成 3~4 轮**微调变体**，每轮 ≤{max_keywords} 词，"
               f"靠多轮覆盖角度，而不是把一条查询写长。")
        if strict:
            raise ValueError(msg)
        return n, "超限（未拦截）"
    return n, "ok"


def check_rounds(queries, strict: bool = True) -> str:
    """校验检索轮数是否落在标准区间 3~4 轮内。"""
    n = len(queries or [])
    if n < ROUNDS_MIN or n > ROUNDS_MAX:
        pages = max(1, -(-DEFAULT_PER_ROUND // DEFAULT_PER_QUERY_LIMIT))
        msg = (f"标准检索为 {ROUNDS_MIN}~{ROUNDS_MAX} 轮，当前 {n} 轮。\n"
               f"  每轮是一条 **≤{MAX_KEYWORDS} 词**的关键词微调变体，"
               f"每轮取 {DEFAULT_PER_ROUND} 篇（{pages} 页）。")
        if strict:
            raise ValueError(msg)
        return f"轮数偏离标准（{n} 轮，未拦截）"
    return "ok"


class SemanticScholarSearcher:
    """Semantic Scholar API 搜索器"""

    BASE_URL = "https://api.semanticscholar.org/graph/v1"
    SEARCH_URL = f"{BASE_URL}/paper/search"
    BATCH_URL = f"{BASE_URL}/paper/batch"

    def __init__(self, api_key: Optional[str] = None):
        """
        初始化搜索器

        Args:
            api_key: Semantic Scholar API Key（可选，默认从环境变量读取）
        """
        self.api_key = api_key or os.getenv("SEMANTIC_SCHOLAR_API_KEY")
        self.last_request_time = 0
        self.request_count = 0
        # 相邻请求最小间隔（秒）。官方限 1 RPS，主动留足余量。
        # 2026-09-20 用户指定由 2.5 提到 **4.0**：实测 2.5 秒下连续两次 429，
        # 靠 10 秒重试才成功；新标准流程一轮检索只需 6~8 次请求，
        # 把间隔放宽到 4 秒换取更少重试，整体反而更快也更稳。
        # 请求间隔。默认 4.0 秒；可用环境变量 LITSEARCH_REQUEST_INTERVAL 覆盖。
        # 为什么要可调：实测同一个 key 在 4 秒间隔下仍会在第 6~9 次请求撞 429，
        # 说明有效配额低于官方文档的"1 请求/秒"。间隔调到 8~12 秒能跑完整轮。
        try:
            self.REQUEST_INTERVAL = float(
                os.getenv("LITSEARCH_REQUEST_INTERVAL", "4.0"))
        except ValueError:
            self.REQUEST_INTERVAL = 4.0
        # ⚠️ 这一个计时器是**全局**的：_rate_limit() 在 _make_request 里被所有端点共用，
        # 所以 /paper/search、/paper/batch、/paper/{id} 共享同一个间隔。
        # 这正是官方要求的行为（1 RPS 全端点），**不要**改成按端点分别计时。
        # 失败重试策略（v1.4.0，用户指定）：
        #   首次尝试 → 等 10s → 第 2 次 → 等 10s → 第 3 次 → 仍失败则停止
        # 三次均失败即抛 ApiUnavailableError，**立即暂停检索**并向用户提问：
        #   「降级使用 Crossref，还是持续等待 3~5 分钟后重试？」
        #
        # 为什么必须交给用户决定（2026-09-11 实测依据）：
        #   实测 S2 触发限流后会进入约 60~110 秒的封锁期，封锁期内任何请求都返回 429
        #   （静默 60s 后仍 429，静默 44s 后自行恢复 200）。
        #   因此秒级退避必然三次全败，继续自动重试没有意义；只有分钟级等待才可能成功。
        # 绝不切换匿名请求，绝不无限重试。
        # **官方要求指数退避**（S2 release notes 从 2024-03 起每月重申：
        # "we now require the use of exponential backoff strategies"）。
        # 10 → 20 → 40 秒，累计约 70 秒，正好覆盖官方与实测的 60~110 秒封锁窗口；
        # 固定 10/10 的问题在于两次都落在封锁期内，必然全败。
        self.RETRY_WAITS_S = (10, 20, 40)
        # 用户在"持续等待"分支下选择等待时的静默秒数（3~5 分钟取中值）
        self.LONG_WAIT_S = 240
        # 仅在用户明确选择"持续等待"后由调用方置 True：
        # 三次快试全败后，再静默等 LONG_WAIT_S，然后做最后一次尝试。
        self.long_wait_on_failure = False
        self.failures = 0            # 连续失败次数（诊断用）
        self.last_failure = ""       # 最近一次失败原因（诊断用）
        self.last_failed_endpoint = ""   # 最近一次失败的端点（诊断用）
        self.raw_sink = None              # 原始响应落盘钩子：(url, params, payload) -> None
        self.diagnostics = []        # 逐查询留痕，供"如实汇报"使用
        self.partial_papers = {}     # 查询中断前已取回的部分结果，避免随异常丢失

    def _wait_rate_limit(self):
        """等待速率限制"""
        current_time = time.time()
        elapsed = current_time - self.last_request_time
        if elapsed < self.REQUEST_INTERVAL:
            wait_time = self.REQUEST_INTERVAL - elapsed
            print(f"[RATE] 等待 {wait_time:.1f} 秒...")
            time.sleep(wait_time)
        self.last_request_time = time.time()
        self.request_count += 1

    def _make_request(self, url: str, params: Dict = None, json_data: Dict = None) -> Optional[Dict]:
        """
        发送请求（仅使用配置的 API Key，绝不降级为匿名请求）

        失败重试策略（硬性上限，不无限重试、不持续等待）：
            首次请求失败 → 等 10s → 重试 1
                          → 等 20s → 重试 2
                          → 等 40s → 重试 3
            全部失败 → 抛 ApiUnavailableError，由调用方立即向用户汇报

        - 未配置 API Key 时直接报错（不使用匿名请求，匿名走全局共享池）
        - 429 / 401 / 403 / 5xx / 网络或代理错误 均计入失败并触发上述策略
        - 其他 HTTP 错误（如 400/404）不重试，抛 RequestFailedError（记为 failed，绝不记为 ok）
        """
        if params is None:
            params = {}

        if not self.api_key:
            raise ApiUnavailableError(
                "未配置 SEMANTIC_SCHOLAR_API_KEY。本 skill 不使用匿名请求"
                "（匿名请求走全局共享池，稳定性不可控）。请先配置密钥。"
            )

        headers = {"x-api-key": self.api_key}
        # 等待序列：默认 (10, 10)，即「首次 → 等10s → 第2次 → 等10s → 第3次」
        waits = list(self.RETRY_WAITS_S)
        if self.long_wait_on_failure:
            # 用户已选择"持续等待"分支：三次快试全败后，再等 3~5 分钟做最后一次尝试
            waits.append(self.LONG_WAIT_S)
        total_attempts = 1 + len(waits)   # 首次 + 重试
        reason = ""

        for attempt in range(total_attempts):
            self._wait_rate_limit()
            response = None
            try:
                if json_data:
                    response = requests.post(url, params=params, json=json_data,
                                             headers=headers, timeout=30)
                else:
                    response = requests.get(url, params=params,
                                            headers=headers, timeout=30)
            except requests.exceptions.RequestException as e:
                # 网络 / 代理 / VPN 出口异常，同样属于"密钥暂时不可用"
                reason = f"网络或代理错误（{type(e).__name__}: {e}）"

            if response is not None:
                if response.status_code == 200:
                    payload = response.json()
                    # 第 0 层：原始响应全量落盘。钩子由调用方设置，
                    # 未设置时不影响任何行为。
                    if getattr(self, "raw_sink", None):
                        try:
                            self.raw_sink(url, params, payload)
                        except Exception as _e:
                            print(f"[WARN] 原始响应落盘失败：{_e}")
                    return payload
                if response.status_code in (429, 401, 403) or response.status_code >= 500:
                    # 限流 / 密钥被拒 / 服务端 5xx（如 504 网关超时）都按可重试处理
                    reason = f"HTTP {response.status_code}（{response.text[:120]}）"
                else:
                    # 4xx 等不可重试错误：必须抛异常，使调用方记录为 failed。
                    # 绝不能返回 None —— 那会被上层误判成"结果为空 = 查询完成"，
                    # 从而把网络故障伪装成"确实未命中"，这正是最危险的静默失败。
                    msg = f"HTTP {response.status_code}: {response.text[:160]}"
                    print(f"[ERROR] {msg}")
                    raise RequestFailedError(msg)

            self.failures += 1
            self.last_failure = reason
            # 记下失败的端点：限流按端点施加（实测同一时刻 /paper/search 返回 200
            # 而 /paper/{id} 返回 429），所以报告必须能区分「哪个端点挂了」，
            # 不能只报一句"S2 不可用"。
            try:
                self.last_failed_endpoint = "/" + url.split("/graph/v1/", 1)[1]
            except Exception:
                self.last_failed_endpoint = url
            if attempt < len(waits):
                wait_time = waits[attempt]
                print(f"[RETRY] 第 {attempt + 1}/{total_attempts} 次失败：{reason}；"
                      f"等待 {wait_time}s 后重试（第 {attempt + 2}/{total_attempts} 次尝试）")
                time.sleep(wait_time)
                continue
            break

        raise ApiUnavailableError(
            f"Semantic Scholar 连续 {total_attempts} 次请求均失败"
            f"（依次等待 {'/'.join(str(w) for w in waits)}s 后仍失败）。\n"
            f"失败端点：{getattr(self, 'last_failed_endpoint', '未知')}\n"
            f"最近原因：{reason}\n"
            f"注意：S2 的配额是**全端点共享 1 RPS**（官方口径），"
            f"换端点不能绕过限流，只能退避等待。\n"
            f"\n"
            f"【已暂停检索】不要自行继续重试，不要改用匿名请求，不要遍历剩余查询。\n"
            f"【必须立即向用户提出以下二选一问题（按用户偏好排序）】\n"
            f"    1) **持续等待 3~5 分钟后重试**（首选：一般情况优先等 S2 自行恢复；"
            f"把 long_wait_on_failure 置为 True 后重跑）；\n"
            f"    2) 降级使用 Crossref 备用源（**最后手段**；代价：只能检索到有 DOI 的文献，"
            f"存在系统性覆盖缺口）。\n"
            f"【官方口径】带 key 的配额是 **1 RPS、全端点**，且高峰期会被进一步压制；"
            f"官方明确要求使用指数退避。\n"
            f"【实测依据】限流触发后进入约 60~110 秒封锁期，期内任何请求都是 429。"
            f"限流由 AWS API Gateway 按 API key 施加，key 本身有效"
            f"（封锁期内伪造 key 返回 403 而非 429，说明鉴权先于限流）。"
        )

    def search_first_round(
        self,
        query: str,
        limit: int = 50,
        offset: int = 0,
        year_range: Optional[Tuple[int, int]] = None,
        fields_of_study: Optional[List[str]] = None,
        publication_types: Optional[List[str]] = None,
        sort_by: Optional[str] = None,
        lite: bool = False
    ) -> List[PaperInfo]:
        """
        第一次检索：获取基本信息

        Args:
            query: 搜索关键词
            limit: 返回数量（单次最多100）
            offset: 分页偏移量
            year_range: 年份范围，如 (2020, 2026)
            fields_of_study: 学科领域过滤，如 ["Computer Science", "Engineering"]
            publication_types: 出版物类型过滤，如 ["JournalArticle", "Review"]
            sort_by: 排序方式，如 "citationCount:desc", "publicationDate:desc"
            lite: True = 只取基本信息（旧行为，响应小）；
                  False（默认）= **同时带回 DOI / 摘要 / OA 链接**，
                  这样就不必再逐篇调 /paper/{id} 补详情（省掉 Phase 2 的调用）。
                  实测 DOI 覆盖 88%、摘要 74%，代价是响应体积约 214 KB。

        Returns:
            论文列表（lite=True 时不含摘要）
        """
        print(f"\n{'='*60}")
        print(f"[Round 1] Search: {query}, limit: {limit}, offset: {offset}")
        print(f"{'='*60}")

        params = {
            "query": query,
            "limit": min(limit, 100),
            "offset": offset,
            "fields": SEARCH_FIELDS if lite else SEARCH_FIELDS_FULL
        }
        if year_range:
            params["year"] = f"{year_range[0]}-{year_range[1]}"
        if fields_of_study:
            params["fieldsOfStudy"] = ",".join(fields_of_study)
        if publication_types:
            params["publicationTypes"] = ",".join(publication_types)
        if sort_by:
            params["sort"] = sort_by

        # 失败重试与上报由 _make_request 统一处理（失败即抛 ApiUnavailableError）
        data = self._make_request(self.SEARCH_URL, params=params)
        if not data:
            return []

        papers = [self._parse_paper(item) for item in data.get("data", [])]
        print(f"[SEARCH] Found {len(papers)} papers")
        return papers

    def search_paginated(
        self,
        query: str,
        total_limit: int = DEFAULT_PER_QUERY_LIMIT,
        year_range: Optional[Tuple[int, int]] = None,
        fields_of_study: Optional[List[str]] = None,
        publication_types: Optional[List[str]] = None,
        sort_by: Optional[str] = None,
        lite: bool = False
    ) -> List[PaperInfo]:
        """
        自动分页检索，最多获取**该查询**的 total_limit 篇（默认 100 = 一页）。

        注意：total_limit 是**单个查询**的上限，不是所有查询的总数。
        「总共入表多少篇」由 DEFAULT_TOTAL_LIMIT 控制，**默认 None = 不截断**，
        见 paper_table.save_papers_xlsx() 的 total_limit 参数。

        Semantic Scholar API 单页最多返回 100 篇，通过 offset 翻页。
        offset=0 → 第1-100篇, offset=100 → 第101-200篇, ...

        硬上限：**offset + limit ≤ 1000**（2023-10-31 起平台限制；
        旧文档写的"总上限 10000 篇"已失效）。因此单查询最多只能取回 1000 条，
        需要更多必须改用 paper/bulkSearch。超过该上限平台返回 400，
        会被记为 failed（不会静默截断）。

        Args:
            query: 搜索词
            total_limit: 总获取数量上限（默认500）
            year_range: 年份范围
            fields_of_study: 学科领域
            publication_types: 出版物类型
            sort_by: 排序方式

        Returns:
            去重后的论文列表
        """
        all_papers = {}
        offset = 0
        page_size = 100
        # 【安全阀 1】绝对页数上限。平台限制 offset + limit ≤ 1000，
        # 配合 page_size=100 即最多 10 页。**没有这个上限会死循环**：
        # 循环条件看的是"去重后"的篇数，而 offset 每轮照涨；
        # 一旦 S2 跨页返回重复结果（去重后篇数不增长），条件永远成立、请求无限发下去。
        # 2026-09-13 实测确认（mock 让第 2 页起返回重复 ID，offset 一路涨到 4800 万仍未停）。
        max_pages = max(1, RELEVANCE_MAX_RESULTS // page_size)
        pages = 0

        # 平台硬上限：offset + limit ≤ 1000。超过会被平台以 400 拒绝，
        # 与其等到翻页中途才失败，不如提前明确告知。
        if total_limit > RELEVANCE_MAX_RESULTS:
            print(f"[WARN] total_limit={total_limit} 超过 relevance search 硬上限 "
                  f"{RELEVANCE_MAX_RESULTS}（offset+limit ≤ 1000）。"
                  f"超过后平台将返回 400，本查询会被记为 failed。"
                  f"需要更多文献请改用 paper/bulkSearch。")
        # 逐查询留痕：status ∈ {ok, capped, failed}
        #   ok     = 该查询结果已全部取回
        #   capped = 达到本次抓取上限，可能仍有更多文献未取回（属"部分检索"）
        #   failed = 查询中断，结果不完整
        entry = {"query": query, "requested": total_limit, "returned": 0,
                 "status": "ok", "note": ""}

        try:
            while len(all_papers) < total_limit:
                # 【安全阀 1】到达平台分页上限即停
                if pages >= max_pages:
                    entry["status"] = "capped"
                    entry["note"] = (f"已达平台分页上限 {max_pages} 页"
                                     f"（offset+limit ≤ {RELEVANCE_MAX_RESULTS}），"
                                     f"可能仍有更多文献未取回")
                    break
                batch = self.search_first_round(
                    query, limit=page_size, offset=offset,
                    year_range=year_range, fields_of_study=fields_of_study,
                    publication_types=publication_types, sort_by=sort_by,
                    lite=lite
                )
                if not batch:
                    entry["note"] = "该查询结果已全部取回"
                    break
                before = len(all_papers)
                for p in batch:
                    if p.paper_id not in all_papers:
                        all_papers[p.paper_id] = p
                pages += 1
                offset += page_size
                # 【安全阀 2】本页没有带来任何新论文（全部与已有重复）→ 停止翻页
                if len(all_papers) == before:
                    entry["note"] = (f"第 {pages} 页结果全部与已有重复，"
                                     f"已停止翻页（去重后 {len(all_papers)} 篇）")
                    break
                if len(batch) < page_size:
                    entry["note"] = "该查询结果已全部取回"
                    break  # No more results
            else:
                entry["status"] = "capped"
                entry["note"] = (f"达到本次抓取上限 {total_limit} 篇，"
                                 f"可能仍有更多文献未取回")
        except (ApiUnavailableError, RequestFailedError) as e:
            # 任何请求层失败都记为 failed —— 绝不能落到下面的 "ok" 分支，
            # 否则 5xx/4xx 会被伪装成"该查询结果已全部取回"
            entry["status"] = "failed"
            entry["returned"] = len(all_papers)
            entry["note"] = f"查询中断（已取回 {len(all_papers)} 篇）：{str(e)[:160]}"
            self.diagnostics.append(entry)
            # 关键：保全该查询已取回的部分结果。否则 return 语句执行不到，
            # 已取回的文献会随异常一起丢失，报告里的"已取回 N 篇"就成了空话。
            if all_papers:
                self.partial_papers[query] = list(all_papers.values())
            raise

        entry["returned"] = len(all_papers)
        self.diagnostics.append(entry)
        print(f"[PAGINATED] Total unique: {len(all_papers)} papers")
        return list(all_papers.values())

    def collected_partial(self) -> List[PaperInfo]:
        """返回所有"中断前已取回"的部分结果（跨查询去重）。

        必须在捕获 ApiUnavailableError 后调用并落盘，否则这部分数据会丢失，
        而报告里的"已取回 N 篇"将无法交付给用户。
        """
        seen, out = set(), []
        for papers in self.partial_papers.values():
            for p in papers:
                if p.paper_id not in seen:
                    seen.add(p.paper_id)
                    out.append(p)
        return out

    def search_report(self) -> Dict:
        """汇总本次检索的完整性信息，供"如实汇报"使用。

        必须能区分并如实告知用户的三种情况：
          - 0 篇结果：究竟是"确实未命中"，还是"检索失败/未完成"
          - 部分检索：哪些查询 failed（失败）或 capped（被上限截断）
          - 各环节降级（分区缺失、翻译跳过等，由调用方补充说明）
        """
        diag = self.diagnostics
        failed = [d for d in diag if d["status"] == "failed"]
        capped = [d for d in diag if d["status"] == "capped"]
        returned = sum(d["returned"] for d in diag)
        complete = (not failed) and (not capped)

        notes = []
        if failed:
            notes.append(f"{len(failed)} 个查询失败，本次结果不完整（属部分检索）")
        if capped:
            notes.append(f"{len(capped)} 个查询达到抓取上限，可能仍有更多文献未取回")
        if not diag:
            notes.append("尚未执行任何查询")
        if returned == 0 and failed:
            notes.append("0 篇结果源于查询失败，不能据此判断该领域是否存在文献")
        elif returned == 0 and diag:
            notes.append("确实未命中任何文献（关键词层面），但不等于该领域空白，"
                         "需说明所用关键词与筛选条件")

        return {
            "queries_total": len(diag),
            "queries_failed": len(failed),
            "queries_capped": len(capped),
            "papers_returned": returned,
            "requests_sent": self.request_count,
            "complete": complete,
            "notes": notes,
            "detail": diag,
        }

    def save_search_report(self, out_dir: str, prefix: str = "检索完整性报告") -> str:
        """把检索完整性报告写入 out_dir（json + txt），返回 txt 路径。

        该报告必须随调研结果一并交付或在汇报中体现，不得省略。
        """
        rep = self.search_report()
        os.makedirs(out_dir, exist_ok=True)

        with open(os.path.join(out_dir, f"{prefix}.json"), "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)

        lines = [
            "=" * 60,
            "检索完整性报告",
            "=" * 60,
            f"查询总数：{rep['queries_total']}",
            f"失败查询：{rep['queries_failed']}",
            f"达到抓取上限的查询：{rep['queries_capped']}",
            f"累计取回文献：{rep['papers_returned']} 篇",
            f"实际请求次数：{rep['requests_sent']}",
            f"结果是否完整：{'是' if rep['complete'] else '否（存在失败或截断，属部分检索）'}",
            "-" * 60,
            "逐查询明细：",
        ]
        for d in rep["detail"]:
            extra = f" | {d['note']}" if d.get("note") else ""
            lines.append(f"  [{d['status']}] {d['query']} -> 取回 {d['returned']}/{d['requested']}{extra}")
        if rep["notes"]:
            lines.append("-" * 60)
            lines.append("需要如实告知用户的事项：")
            for n in rep["notes"]:
                lines.append(f"  - {n}")
        lines.append("=" * 60)

        txt_path = os.path.join(out_dir, f"{prefix}.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"[SAVE] 检索完整性报告: {txt_path}")
        return txt_path

    def search_second_round(self, paper_ids: List[str]) -> Dict[str, Dict]:
        """
        第二次检索：批量获取摘要

        Args:
            paper_ids: 论文 ID 列表（最多500个/批）

        Returns:
            字典：paper_id -> {abstract, doi, pdf_url}
        """
        print(f"\n{'='*60}")
        print(f"[Round 2] Fetching abstracts for {len(paper_ids)} papers")
        print(f"{'='*60}")

        if not paper_ids:
            return {}

        all_result = {}
        for i in range(0, len(paper_ids), 500):
            batch_ids = paper_ids[i:i+500]
            params = {"fields": ROUND2_FIELDS}
            json_data = {"ids": batch_ids}

            data = self._make_request(self.BATCH_URL, params=params, json_data=json_data)
            if not data:
                continue

            for item in data:
                if item:
                    pid = item.get("paperId", "")
                    all_result[pid] = {
                        "abstract": item.get("abstract", "") or "",
                        "doi": item.get("externalIds", {}).get("DOI", ""),
                        "pdf_url": item.get("openAccessPdf", {}).get("url", "") if item.get("openAccessPdf") else ""
                    }

        print(f"[Round 2] Got abstracts for {len(all_result)} papers")
        return all_result

    def batch_query_papers(
        self,
        paper_ids: List[str],
        fields: Optional[List[str]] = None
    ) -> List[PaperInfo]:
        """
        通过 paper_id 批量精准查询论文详情。

        使用 Semantic Scholar batch API，单次最多 500 个 ID。

        Args:
            paper_ids: 论文 ID 列表
            fields: 需要查询的字段列表，默认返回全部常用字段

        Returns:
            PaperInfo 列表（顺序与输入可能不同）
        """
        if not paper_ids:
            return []

        if fields is None:
            # 与 S2_FIELDS 同源，另加引用/参考文献的 paperId
            fields = S2_FIELDS_LIST + ["references.paperId", "citations.paperId"]

        fields_str = ",".join(fields)

        print(f"\n{'='*60}")
        print(f"[BATCH] Querying {len(paper_ids)} papers by ID")
        print(f"{'='*60}")

        all_papers = []
        for i in range(0, len(paper_ids), 500):
            batch_ids = paper_ids[i:i+500]
            params = {"fields": fields_str}
            json_data = {"ids": batch_ids}

            # ⚠️ 这里**不做拆批重试**。原因有两条：
            #   1) 限流期是分钟级，拆批等于在封锁期内把请求数翻倍，只会延长封锁；
            #   2) 项目已定规则：失败走统一的重试与上报，由用户决定等待还是换源。
            # `/paper/batch` 单次最多 500 个 ID，这是接口上限，与检索的每轮 300 篇无关。
            data = self._make_request(self.BATCH_URL, params=params,
                                      json_data=json_data)
            if not data:
                continue

            for item in data:
                if item:
                    paper = self._parse_paper(item)
                    all_papers.append(paper)

            print(f"[BATCH] Got {len(all_papers)} papers so far...")

        print(f"[BATCH] Total: {len(all_papers)} papers")
        return all_papers

    def get_paper_details(self, paper_id: str) -> Optional[Dict]:
        """
        获取单篇论文的详细信息（含引用和参考文献列表）

        Args:
            paper_id: 论文 ID

        Returns:
            论文详情字典，包含 citations 和 references 列表
        """
        url = f"{self.BASE_URL}/paper/{paper_id}"
        params = {"fields": S2_FIELDS + ",citations,references"}
        return self._make_request(url, params=params)

    def expand_citations(
        self,
        seed_papers: List[str],
        direction: str = "both",
        limit_per_paper: int = 20
    ) -> List[PaperInfo]:
        """
        从种子论文扩展引用网络。

        Args:
            seed_papers: 种子论文 ID 列表
            direction: "citations"（引用了种子的）、"references"（种子引用的）、"both"
            limit_per_paper: 每篇种子论文最多扩展多少篇

        Returns:
            扩展后的论文列表（排除种子论文自身）
        """
        print(f"\n{'='*60}")
        print(f"[EXPAND] Expanding citations from {len(seed_papers)} seed papers")
        print(f"{'='*60}")

        expanded = {}
        for i, pid in enumerate(seed_papers, 1):
            print(f"  [{i}/{len(seed_papers)}] Fetching {pid[:12]}...")
            details = self.get_paper_details(pid)
            if not details:
                continue

            targets = []
            if direction in ("citations", "both"):
                targets.extend(details.get("citations", []) or [])
            if direction in ("references", "both"):
                targets.extend(details.get("references", []) or [])

            count = 0
            for t in targets:
                if count >= limit_per_paper:
                    break
                tid = t.get("paperId")
                if tid and tid not in seed_papers and tid not in expanded:
                    expanded[tid] = self._parse_paper(t)
                    count += 1

        print(f"[EXPAND] Found {len(expanded)} new papers")
        return list(expanded.values())

    def _parse_paper(self, item: Dict) -> PaperInfo:
        """解析论文数据。

        「期刊信息」按 journal.name → publicationVenue.name → venue 三级回退，
        **只在 S2 响应内部回退，不走任何外部途径**。三级都空则置空并记下来源，
        由上层标「刊名为空」——不删行，因为刊名缺失不等于这篇论文不合格。
        """
        authors = [a.get("name", "") for a in item.get("authors", [])]
        external_ids = item.get("externalIds", {}) or {}
        doi = external_ids.get("DOI", "") or ""
        open_access = item.get("openAccessPdf")
        pdf_url = open_access.get("url", "") if open_access else ""
        venue, venue_source = resolve_venue(item)

        # 使用 API 的 publicationTypes 字段判断（替代关键词判断）
        pub_types = item.get("publicationTypes", []) or []
        is_conference = "Conference" in pub_types
        is_journal = "JournalArticle" in pub_types or "Review" in pub_types

        # 预印本检测
        is_preprint = "ArXiv" in external_ids
        if not is_preprint:
            venue_lower = venue.lower()
            preprint_markers = ["arxiv", "ssrn", "research square", "biorxiv", "medrxiv", "preprint"]
            is_preprint = any(m in venue_lower for m in preprint_markers)

        title = item.get("title", "") or ""
        abstract = item.get("abstract", "") or ""
        cls = _classify_record(
            title=title, venue=venue, publication_types=pub_types,
            external_ids=external_ids, doi=doi, abstract=abstract,
            year=item.get("year") or 0,
        )

        return PaperInfo(
            paper_id=item.get("paperId", ""),
            title=title,
            authors=authors,
            year=item.get("year") or 0,
            venue=venue,
            citation_count=item.get("citationCount", 0) or 0,
            abstract=abstract,
            doi=doi,
            pdf_url=pdf_url,
            is_conference=bool(is_conference or cls.get("is_conference")),
            is_journal=is_journal,
            is_preprint=bool(is_preprint or cls.get("is_preprint")),
            publication_date=item.get("publicationDate", "") or "",
            venue_source=venue_source,
            issn=venue_issn(item),
            abstract_source="S2" if abstract else "",
            is_retracted=bool(cls.get("is_retracted")),
            is_layout_record=bool(cls.get("is_layout_record")),
            is_suspicious=bool(cls.get("is_suspicious")),
            record_reasons=";".join(cls.get("reasons") or []),
        )

    def get_stats(self) -> Dict:
        """获取统计信息"""
        return {"total_requests": self.request_count}


def enrich_quartiles(papers: List[PaperInfo],
                     refresh: bool = False,
                     basis: str = "jcr") -> List[PaperInfo]:
    """按指定分区标准回填 paper.quartile（值为 "1"~"4" 或 ""）。

    分区数据来自 easyScholar 的 `getPublicationRank`，它同时返回中新锐与 JCR
    多套分区，所以标准由 `basis` 决定，不写死：
        "jcr" → JCR 四分位（默认）
        "xr"  → 中科院《新锐期刊分区表》

    流程：
    1. 按刊名去重（easyScholar 是刊名查询接口，不需要 ISSN）
    2. 有 ISSN 的作加成传给 journal_quartile，没有就只用刊名
    3. 写回 paper.quartile，并记录 zone_basis / zone_status
    """
    from journal_quartile import zone_map_for_journals

    venue_doi = {}
    for p in papers:
        v = (p.venue or "").strip()
        if v and v not in venue_doi:
            venue_doi[v] = (p.doi or "")

    print(f"\n[ZONE] Looking up {len(venue_doi)} unique journals "
          f"(zone_basis={basis})...")
    zone_map = zone_map_for_journals(venue_doi, refresh=refresh,
                                     verbose=True, basis=basis)

    for p in papers:
        p.quartile = zone_map.get((p.venue or "").strip()) or ""
        p.zone_basis = basis
        p.zone_status = "已收录" if p.quartile else "未收录"

    return papers


def _default_year_range() -> Tuple[int, int]:
    """默认年份范围：近 10 年（按年份粗算）。

    ⚠️ 仅保留给旧调用方。新流程请用 `year_window(years)`，它返回**精确到日**
    的下界，并能处理「只设下界、不设上界」与年份缺失保留这两条标准。
    """
    this_year = datetime.now().year
    return (this_year - 9, this_year)


# 默认出版物类型（API 筛选）：排除会议，只要期刊文章和综述
DEFAULT_PUBLICATION_TYPES = ["JournalArticle", "Review"]
# 默认分区筛选：**不筛**。
#
# 旧默认值是 ["Q1","Q2"]，这是个陷阱：检索阶段还没查分区，paper.quartile 一律为空，
# 靠 filter_papers 里 `and paper.quartile` 那一句侥幸没生效；一旦调用顺序变成
# "先 enrich_quartiles 查分区、再 filter_papers"，就会突然按 Q1/Q2 砍掉大量文献，
# 与用户「检索时不设分区要求」的要求冲突。
# 现在改为 None：**要按分区筛必须显式传 quartile_filter**。
DEFAULT_QUARTILE_FILTER = None


def _is_conference_venue(venue: str) -> bool:
    """刊名是否属会议论文集（规则见 venue_rules.py）。模块缺失时退化为 False。"""
    try:
        from venue_rules import is_conference_venue
        return is_conference_venue(venue or "")
    except Exception:
        return False


def filter_papers(
    papers: List[PaperInfo],
    author_filter: Optional[str] = None,
    year_range: Optional[Tuple[int, int]] = None,
    paper_type: str = "journal_only",
    quartile_filter: Optional[List[str]] = None
) -> List[PaperInfo]:
    """
    筛选论文。

    默认筛选（用户未指定时生效）：
    - 作者：不筛选
    - 年份：近10年
    - 类型：**排除会议论文与预印本**（journal_only）
    - 分区：**不筛选**（检索阶段分区本来就是空的；要按分区筛必须显式传 quartile_filter）
    - 期刊名：不筛选

    用户指定条件时覆盖默认值。
    """
    if year_range is None:
        year_range = _default_year_range()
    if quartile_filter is None:
        quartile_filter = DEFAULT_QUARTILE_FILTER

    filtered = []
    for paper in papers:
        if author_filter and not any(author_filter.lower() in a.lower() for a in paper.authors):
            continue
        if paper.year is None:
            continue
        if year_range and not (year_range[0] <= paper.year <= year_range[1]):
            continue
        # journal_only 的语义 = **非会议 + 非预印本**。
        # 这里**不再**要求 is_journal=True —— 那会把「S2 没返回 publicationTypes」的
        # 正常期刊论文一并丢掉，属静默漏检。
        # 另外补一条 venue 判据：S2 会把 NDT.net / JPCS / E3S 这类会议论文集
        # 标成 JournalArticle，光看 publicationTypes 挡不住（实测 553 篇里漏 99 篇）。
        if paper_type == "journal_only" and (
                paper.is_conference or paper.is_preprint
                or _is_conference_venue(paper.venue)):
            continue
        if paper_type == "conference_only" and not paper.is_conference:
            continue
        if quartile_filter:
            # 分区取值已改为中科院 1~4 区；传 Q1/Q2 属旧 JCR 标准，必须显式拦下，
            # 否则会静默筛成空表（"看起来该领域没论文"）。
            if any(str(x).upper().startswith("Q") for x in quartile_filter):
                raise ValueError(
                    "quartile_filter 取值是分区数字 1~4（如 ['1','2']），"
                    "不接受 Q1/Q2 这种写法。标准由 zone_basis 决定"
                    "（jcr 或 xr），默认 jcr。")
            if paper.quartile and paper.quartile not in quartile_filter:
                continue
        filtered.append(paper)
    return filtered


def format_paper_basic(paper: PaperInfo) -> str:
    """格式化单篇论文（基本信息）"""
    authors_str = ', '.join(paper.authors[:5])
    if len(paper.authors) > 5:
        authors_str += '...'
    return (f"Title: {paper.title}\n"
            f"Authors: {authors_str}\n"
            f"Journal: {paper.venue}\n"
            f"Year: {paper.year}\n"
            f"Citations: {paper.citation_count}")


def format_paper_full(paper: PaperInfo) -> str:
    """格式化单篇论文（完整信息，含中文翻译）"""
    lines = [format_paper_basic(paper)]
    title_cn = getattr(paper, "title_cn", "")
    if title_cn:
        lines.append(f"Title_CN: {title_cn}")
    if paper.quartile:
        lines.append(f"Quartile: {paper.quartile}")
    if paper.doi:
        lines.append(f"DOI: {paper.doi}")
    if paper.paper_id:
        lines.append(f"PaperID: {paper.paper_id}")
    if paper.relevance_score:
        lines.append(f"BGE Score: {paper.relevance_score:.2f}")
    if paper.pdf_url:
        lines.append(f"PDF: {paper.pdf_url}")
    if paper.abstract:
        lines.append(f"Abstract: {paper.abstract}")
    abstract_cn = getattr(paper, "abstract_cn", "")
    if abstract_cn:
        lines.append(f"Abstract_CN: {abstract_cn}")
    return '\n'.join(lines)


def save_initial_results(
    papers: List[PaperInfo],
    query: str,
    save_dir: Optional[str] = None
) -> str:
    """保存初始检索结果（txt + json）"""
    save_dir = resolve_save_dir(save_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 保存 txt
    filepath_txt = os.path.join(save_dir, f"文献检索_初始数据_{timestamp}.txt")
    output = [
        "=" * 60,
        "文献检索 - 初始数据",
        "=" * 60,
        f"搜索词: {query}",
        f"检索时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"检索结果: {len(papers)} 篇",
        "-" * 60
    ]
    for i, paper in enumerate(papers, 1):
        output.append(f"\n[论文{i}]")
        output.append(format_paper_basic(paper))
    output.append("\n" + "=" * 60)

    with open(filepath_txt, 'w', encoding='utf-8') as f:
        f.write('\n'.join(output))

    # 保存 json
    filepath_json = os.path.join(save_dir, f"文献检索_初始数据_{timestamp}.json")
    json_data = {
        "query": query,
        "timestamp": datetime.now().isoformat(),
        "count": len(papers),
        "papers": [_paper_to_dict(p) for p in papers]
    }
    with open(filepath_json, 'w', encoding='utf-8') as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)

    print(f"[SAVE] Initial data: {filepath_txt}")
    print(f"[SAVE] Initial JSON: {filepath_json}")
    return filepath_txt


def save_filtered_results(
    papers: List[PaperInfo],
    query: str,
    filter_desc: str,
    save_dir: Optional[str] = None,
    translate: bool = True
) -> str:
    """
    保存 Phase 1 筛选结果（txt + json）。

    Phase 1 不包含摘要——只保存基本信息 + 中文标题 + paper_id。
    用户审查后，通过 paper_id 进入 Phase 2 获取摘要详情。
    """
    save_dir = resolve_save_dir(save_dir)
    # 翻译标题（Phase 1 只翻译标题，无摘要）
    if translate:
        try:
            from translator import translate_papers
            papers = list(papers)
            papers = translate_papers(papers, translate_titles_flag=True, translate_abstracts_flag=False)
        except ImportError:
            print("[WARN] translator module not available, skipping translation")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 保存 txt
    filepath_txt = os.path.join(save_dir, f"文献检索_筛选结果_{timestamp}.txt")
    output = [
        "=" * 60,
        "文献检索 - Phase 1 筛选结果",
        "=" * 60,
        f"搜索词: {query}",
        f"筛选条件: {filter_desc}",
        f"检索时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"筛选结果: {len(papers)} 篇",
        "-" * 60,
        "提示: 记下感兴趣的 PaperID，使用 Phase 2 获取摘要详情",
        "-" * 60
    ]
    for i, paper in enumerate(papers, 1):
        output.append(f"\n[论文{i}]")
        output.append(format_paper_full(paper))
    output.append("\n" + "=" * 60)

    with open(filepath_txt, 'w', encoding='utf-8') as f:
        f.write('\n'.join(output))

    # 保存 json（含 paper_id 和 DOI 作为索引）
    filepath_json = os.path.join(save_dir, f"文献检索_筛选结果_{timestamp}.json")
    json_data = {
        "query": query,
        "filter": filter_desc,
        "timestamp": datetime.now().isoformat(),
        "count": len(papers),
        "papers": [_paper_to_dict(p) for p in papers]
    }
    with open(filepath_json, 'w', encoding='utf-8') as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)

    # 保存 paper_id → 标题映射（供 Phase 2 通过标题查找）
    save_paper_mapping(papers, save_dir)

    print(f"[SAVE] Filtered data: {filepath_txt}")
    print(f"[SAVE] Filtered JSON: {filepath_json}")
    return filepath_txt


def _paper_to_dict(paper: PaperInfo) -> Dict:
    """将 PaperInfo 转为字典（用于 JSON 序列化）"""
    d = {
        "paper_id": paper.paper_id,
        "title": paper.title,
        "title_cn": getattr(paper, "title_cn", ""),
        "authors": paper.authors,
        "year": paper.year,
        "venue": paper.venue,
        "quartile": paper.quartile,
        "citation_count": paper.citation_count,
        "is_conference": paper.is_conference,
        "is_journal": paper.is_journal,
        "is_preprint": paper.is_preprint,
        "doi": paper.doi,
        "pdf_url": paper.pdf_url,
        "abstract": paper.abstract,
        "abstract_cn": getattr(paper, "abstract_cn", ""),
        "relevance_score": paper.relevance_score
    }
    return d


def save_paper_mapping(
    papers: List[PaperInfo],
    save_dir: Optional[str] = None
) -> str:
    """
    保存 paper_id → 标题映射文件。

    轻量级 JSON，供 Agent 在 Phase 2 前查找 paper_id。
    用户提到论文标题 → Agent 查此映射 → 获取 paper_id → 进入 Phase 2。

    格式: {"last_updated": "...", "papers": {paper_id: {title, title_cn, year, venue}}}
    """
    save_dir = resolve_save_dir(save_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(save_dir, f"paper_mapping_{timestamp}.json")

    mapping = {}
    for p in papers:
        mapping[p.paper_id] = {
            "title": p.title,
            "title_cn": getattr(p, "title_cn", "") or "",
            "year": p.year,
            "venue": p.venue,
        }

    data = {
        "last_updated": datetime.now().isoformat(),
        "count": len(mapping),
        "papers": mapping,
    }

    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"[SAVE] Paper mapping: {filepath}")
    return filepath


def find_paper_id_by_title(
    title: str,
    mapping_files: Optional[List[str]] = None,
    mapping_dir: Optional[str] = None,
    fuzzy_threshold: float = 0.6
) -> Optional[str]:
    """
    通过标题查找 paper_id。

    查找顺序：
    1. 从本地 mapping 文件模糊匹配（快速，离线）
    2. 未找到则调用 Semantic Scholar API 搜索（准确但需联网）

    Args:
        title: 论文标题（完整或部分）
        mapping_files: 指定 mapping 文件列表（可选）
        mapping_dir: mapping 文件所在目录
        fuzzy_threshold: 模糊匹配阈值

    Returns:
        paper_id 或 None
    """
    # 1. 本地映射查找
    import glob as gmod

    if mapping_dir is None:
        mapping_dir = DEFAULT_SAVE_DIR

    if mapping_files is None:
        mapping_files = sorted(
            gmod.glob(os.path.join(mapping_dir, "paper_mapping_*.json")),
            reverse=True  # 最新的优先
        )

    for mf in mapping_files:
        try:
            with open(mf, 'r', encoding='utf-8') as f:
                data = json.load(f)
            papers = data.get("papers", {})

            # 精确匹配
            for pid, info in papers.items():
                if info.get("title", "").lower() == title.lower():
                    print(f"[FIND] Exact match in {os.path.basename(mf)}: {pid}")
                    return pid

            # 模糊匹配（标题包含关系）
            title_lower = title.lower()
            for pid, info in papers.items():
                stored_title = info.get("title", "").lower()
                if title_lower in stored_title or stored_title in title_lower:
                    print(f"[FIND] Fuzzy match in {os.path.basename(mf)}: {pid}")
                    return pid

        except (json.JSONDecodeError, KeyError):
            continue

    # 2. API 搜索回退
    print(f"[FIND] Not found in mapping, searching API for: {title[:80]}...")
    searcher = SemanticScholarSearcher()
    results = searcher.search_first_round(title, limit=5)
    if results:
        best = results[0]
        print(f"[FIND] API result: {best.paper_id} ({best.title[:80]})")
        return best.paper_id

    return None


def print_stats(searcher: SemanticScholarSearcher):
    """打印统计信息"""
    stats = searcher.get_stats()
    print(f"\n{'='*60}")
    print(f"[STATS] Total requests: {stats['total_requests']}")
    print(f"{'='*60}")


# 测试函数
def test_search():
    """测试检索功能"""
    API_KEY = os.getenv("SEMANTIC_SCHOLAR_API_KEY")
    if not API_KEY:
        print("[ERROR] Environment variable SEMANTIC_SCHOLAR_API_KEY not set")
        print("Apply for API Key: https://www.semanticscholar.org/product/api#api-key-form")
        print("Then set: setx SEMANTIC_SCHOLAR_API_KEY \"your_key\"")
        return
    searcher = SemanticScholarSearcher()  # Read from env var

    print("=" * 60)
    print("Test literature search (interval 1.5s)")
    print("=" * 60)

    # First round
    papers = searcher.search_first_round("knowledge graph construction", limit=3)

    if papers:
        print(f"\n[Found {len(papers)} papers]")
        for paper in papers:
            print(f"  - {paper.title} ({paper.year})")

        save_initial_results(papers, "knowledge graph construction")

        # Filter
        filtered = filter_papers(papers, year_range=(2020, 2026))
        print(f"\n[After filter: {len(filtered)} papers]")

        if filtered:
            # Second round
            paper_ids = [p.paper_id for p in filtered]
            details = searcher.search_second_round(paper_ids)

            for paper in filtered:
                detail = details.get(paper.paper_id, {})
                paper.abstract = detail.get("abstract", "")
                paper.doi = detail.get("doi", "")
                paper.pdf_url = detail.get("pdf_url", "")

            save_filtered_results(filtered, "knowledge graph construction", "Year: 2020-2026")

        print_stats(searcher)
    else:
        print("No papers found")


if __name__ == "__main__":
    _fix_encoding()
    test_search()
