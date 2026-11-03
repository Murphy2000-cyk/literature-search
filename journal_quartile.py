#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""期刊分区查询（easyScholar 主通道，LetPub 兜底） v4.0

分区标准的两套口径
------------------
- **JCR 四分位**（Clarivate，基于 JIF）：easyScholar 开放接口直接返回，`--zone-basis jcr` 使用，
  v2.2.0 起为**默认标准**。
- **中科院分区**（新锐 / 升级版 / 基础版）：同一接口一并返回，`--zone-basis xr` 使用。

为什么不靠第三方站点的自算四分位
--------------------------------
journalmetrics.org 之类给出的是它们**自算**的四分位（OpenAlex 标准），既不是 JCR
也不是 Scopus 官方 CiteScore 四分位，拿它当 JCR 汇报会出错。本模块因此只采信
easyScholar 返回的官方口径，不提供任何代理指标。

数据落地
--------
每次查询结果写入 `data/journal_zone.xlsx`，一行一本刊，带 `查询日期`。
`已收录` 60 天内免查，`未收录` 30 天，`查询失败` 一律重试。

v3.0 的三处变更（2026-10-30 实测驱动）
--------------------------------------
1. **删除 journalmetrics 分支**（原 Q1–Q4 那套，约 100 行）：标准不可靠，且与中科院
   分区不是一回事。连带删除 `.quartile_cache.json`。
2. **抓取优先 `--backend curl_cffi`**：实测 LetPub 页面用默认三级升级链要 **7.0~9.3 s**，
   强制 curl_cffi 只要 **2.6~3.6 s**（6/6 成功，返回数据还更全 15 KB vs 13 KB）。
   低于 2000 字符视为拿到空壳页，退回默认链重试。
3. **缓存带时间戳 + 负缓存 TTL**：旧实现把 `None` 永久写进缓存，导致主流刊被永久判成
   「未收录」（实测 215 本里 185 本中计，含 Materials、Applied Sciences、IEEE TIM、
   MSSP、IEEE TUFFC）。现在未收录项 30 天后自动重查，`--refresh` 可强制全部重查。

查询顺序
--------
① `searchissn=`（**精确匹配 ISSN 单元格，推荐**；有 ISSN 一律用这条）
② `searchname=`（模糊匹配，遇到缩写会漏，实测 `Comput. Aided Civ. Infrastructure Eng.`
   按刊名就查不到）

两条路径都走 `searchts read`：requests 直连会被 LetPub 挡回约 639 字节的壳页。

取值规则
--------
表格里 `N区` 是**恰好等于**该形式的单元格（不是从标题或正文正则抓，页面正文里还有
大量别的 `区`/`Q1` 字样）。返回 `"1"`~`"4"` 或 `None`。

用法
----
    python journal_quartile.py --issn 2075-5309 2352-7102
    python journal_quartile.py "Journal of Building Engineering"
    python journal_quartile.py --batch issns.txt          # 每行一个 ISSN 或刊名
    python journal_quartile.py --issn --refresh 0041-624X # 绕过缓存重查
"""
import os
import re
import sys
# 控制台编码兜底：Windows 默认 GBK，打印中文/特殊字符会 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import json
import time
import requests
import subprocess
from urllib.parse import quote as _quote

# ---------------------------------------------------------------- 常量

LETPUB_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 ".letpub_zone_cache.json")
# 缓存版本号。结构或取值规则一变就必须 +1，让旧值整体作废。
# v1：{"v":1,"zone":...,"hit":...,"src":...}（无时间戳，None 会被永久复用）
# v2：增加 "ts"，未收录项可按时效重查
LETPUB_CACHE_VERSION = 2

# 未收录（zone is None）的缓存有效期。超过则重查——LetPub 新收录、或上次撞上空壳页。
NEGATIVE_TTL_DAYS = 30

LETPUB_SEARCH = "https://www.letpub.com.cn/index.php?page=journalapp&view=search"

INTERVAL = 1.5                      # 相邻 searchts 调用间隔（秒），对站点友好
_last_req = 0.0

_ZONE_CELL = re.compile(r"^([1-4])区$")
_LINK_NAME = re.compile(r"\[([^\]]+)\]\([^)]*journalid=\d+[^)]*\)")

# 判定"拿到的是空壳页而不是真页面"的字符数阈值。
# 实测：正常检索页 13~15 KB；空壳页约 1.2 KB。
MIN_PAGE_CHARS = 2000


# ---------------------------------------------------------------- 缓存

def load_letpub_cache():
    if os.path.exists(LETPUB_CACHE_FILE):
        try:
            with open(LETPUB_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_letpub_cache(c):
    try:
        with open(LETPUB_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(c, f, indent=2, ensure_ascii=False, sort_keys=True)
    except Exception as e:
        print(f"[WARN] 新锐分区缓存写入失败: {e}", file=sys.stderr)


# ------------------------------------------------ Excel 分区表（v2.1.0 起为权威源）
# 分区数据固定存进 skill 目录的 data/journal_zone.xlsx，用户可直接打开看、手改、留档。
# 每行带查询日期（YYYY-MM-DD），用来判断是否重查。旧 JSON 缓存只作一次性迁移来源。

ZONE_XLSX = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "data", "journal_zone.xlsx")
ZONE_HEADER = ["期刊名", "ISSN", "新锐分区", "中科院升级版", "中科院基础版", "JCR分区",
               "影响因子", "五年影响因子", "中科院预警", "状态", "查询日期", "来源"]
ZONE_TTL_DAYS = 60           # 已收录：超过 60 天视为过期，本次用到时会重查一次并更新日期
ST_ZONE, ST_NONE, ST_FAIL = "已收录", "未收录", "查询失败"
# 规范化后仍查不到时，按原因分三态标注，不再一律记「查询失败」
NA_CONF, NA_PREPRINT, NA_OTHER = "无法查询(会议论文)", "无法查询(预印本)", "无法查询(其他原因)"

_CONF_PREFIX = re.compile(
    r"^\s*(proceedings\s+of|spie\b|annual\s+review\s+of\s+progress|"
    r"international\s+workshop|conference\s+on|symposium\s+on)", re.I)
_VOLUME_TAIL = re.compile(r"[,\s]*\bvolume\s+\d+\b.*$", re.I)
_PAREN_TAIL = re.compile(r"\s*\((print|online|electronic)\)\s*$", re.I)


def _na_reason(name):
    """只在两次查询都失败后调用，判断「无法查询」属于哪一类。

    它**不决定是否查询**，只决定失败后挂哪个标签，所以误判的代价只是标签不准，
    不会让本该有分区的论文被跳过。
    """
    s = str(name or "").strip()
    if not s:
        return NA_OTHER
    if re.fullmatch(r"volume\s+\d+", s, re.I):
        return NA_CONF
    try:
        import venue_rules
        if venue_rules.is_conference_venue(s):
            return NA_CONF
    except Exception:
        pass
    # 白名单必须排在兜底正则之前：Proceedings of the Institution of ... 是正规期刊，
    # 若先跑关键字兜底，它会被 "Proceedings" 命中而误标成会议论文。
    if re.search(r"Proceedings\s+of\s+the\s+Institution\s+of\s+"
                 r"(Mechanical|Civil)\s+Engineers", s, re.I):
        return NA_OTHER
    if re.search(r"\b(conference|proceedings|symposium|workshop|congress)\b", s, re.I):
        return NA_CONF
    if re.search(r"\b(preprint|arxiv|biorxiv|medrxiv|ssrn|research\s*square)\b", s, re.I):
        return NA_PREPRINT
    return NA_OTHER


def _normalize_venue(name):
    """刊名规范化。返回 (清洗后的刊名, 直接判定的不可查原因 或 "")。

    规则见 references/journal-name-issues.md 第五节。宁可返回空、记 NULL，
    也不做模糊匹配，避免写入错误分区。
    """
    if not name:
        return "", NA_OTHER
    s = str(name).strip()
    # 会议录判定**必须**复用 venue_rules，不能自己写正则：
    # 那边有 KEEP_VENUE_PATTERN 白名单，能保住
    # 「Proceedings of the Institution of Mechanical Engineers」这类正规期刊。
    try:
        import venue_rules
        if venue_rules.is_conference_venue(s):
            return "", NA_CONF
    except Exception:
        pass
    s = _VOLUME_TAIL.sub("", s).strip()
    if not s or re.fullmatch(r"volume\s+\d+", s, re.I):
        return "", NA_CONF
    s = _PAREN_TAIL.sub("", s).strip()
    # 全大写且无空格：按驼峰/大小写边界拆词
    if s.isupper() and " " not in s and len(s) > 12:
        s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s.title())
    return s, ""

# ---- easyScholar（主通道）------------------------------------------------
# 按刊名查，一次返回中科院分区、JCR 分区、影响因子、五年影响因子、中科院预警。
# 密钥在 easyscholar.cc 注册后到用户中心取，存 ~/.dsh/env.json 的 EASYSCHOLAR_SECRET_KEY。
EASY_BASE = "https://www.easyscholar.cc/open/getPublicationRank"
EASY_ENV = "EASYSCHOLAR_SECRET_KEY"
EASY_TIMEOUT = 30
EASY_INTERVAL = 1.0          # 相邻请求最小间隔（秒）。文档要求 ≤2 次/秒，这里按 1 秒 1 次
_last_easy = 0.0

# ---- LetPub（备用通道）---------------------------------------------------
# 只有中科院分区，没有 JCR 与影响因子。按用户要求：**必须经用户同意才启用**。
ALLOW_LETPUB = False          # 由 --allow-letpub / 调用方显式置 True


def easy_key():
    k = os.getenv(EASY_ENV) or ""
    if k:
        return k
    try:
        j = json.load(open(os.path.join(os.path.expanduser("~"), ".dsh", "env.json"),
                           encoding="utf-8"))
        return (j.get("env") or {}).get(EASY_ENV) or ""
    except Exception:
        return ""


def easyscholar_rank(name, key=None, timeout=EASY_TIMEOUT):
    """按刊名向 easyScholar 查全套分区指标。

    Returns:
        dict | None：{"cas","cas_top","jcr","if","if5","warn","hit"}
        查不到（code!=200 或全空）返回 None；网络/密钥问题抛 RuntimeError。
    """
    key = key or easy_key()
    if not key:
        raise RuntimeError("未配置 EASYSCHOLAR_SECRET_KEY")
    if not name:
        return None
    global _last_easy                      # easyScholar 独立节流：1 秒 1 次
    gap = EASY_INTERVAL - (time.time() - _last_easy)
    if gap > 0:
        time.sleep(gap)
    _last_easy = time.time()
    try:
        r = requests.get(EASY_BASE, params={"secretKey": key, "publicationName": name},
                         timeout=timeout)
    except Exception as e:
        raise RuntimeError(f"easyScholar 请求异常：{type(e).__name__}")
    if r.status_code != 200:
        raise RuntimeError(f"easyScholar HTTP {r.status_code}")
    try:
        d = r.json()
    except Exception:
        raise RuntimeError("easyScholar 返回非 JSON")
    if d.get("code") != 200 or not d.get("data"):
        return None
    # 读 all 而不是 select：select 依赖用户浏览器扩展里勾选了哪些数据集，会随人变化
    off = ((d["data"].get("officialRank") or {}).get("all") or {})
    sel = ((d["data"].get("officialRank") or {}).get("select") or {})

    def pick(*vals):
        for v in vals:
            if v not in (None, "", "null"):
                return str(v).strip()
        return ""

    def _cas(v):
        m = re.search(r"([1-4])\s*区", str(v or ""))
        return m.group(1) if m else pick(v)

    def _quart(v):
        """JCR 四分位归一化为单个数字。

        easyScholar 的 `sci` 字段可能返回 "Q1"、"1区" 或 "1" 三种写法，
        筛选侧只认 "1"~"4"，所以统一取第一个 1~4 的数字。
        """
        m = re.search(r"([1-4])", str(v or ""))
        return m.group(1) if m else ""

    out = {
        "cas": _cas(off.get("xr")),            # 新锐分区
        "cas_base": _cas(off.get("sciUp")),    # 中科院升级版
        "cas_base2": _cas(off.get("sciBase")),  # 中科院基础版
        "jcr": _quart(off.get("sci")),
        "if": pick(off.get("sciif"), sel.get("sciif")),
        "if5": pick(off.get("sciif5"), sel.get("sciif5")),
        "warn": pick(off.get("sciWarn")),      # 注意是 sciWarn，不是 sciWarning
        "source": "easyScholar",
    }
    if not any(out[k] for k in ("cas", "cas_base", "jcr", "if", "if5", "warn")):
        return None
    return out


_zone_rows = None            # 进程内：{key: {"name","issn","zone","status","date"}}
_DEFER_SAVE = False          # 批量查询时置 True，最后统一写一次


def _zone_key(issn, name):
    return ("issn:" + str(issn).upper()) if issn else ("name:" + _norm_journal(name))


ZONE_COLS = ["name", "issn", "cas_up", "cas_base", "cas_base2", "jcr", "if_", "if5",
             "warn", "status", "date", "source"]

def load_zone_xlsx(force=False):
    """读分区表；首次调用会把旧 JSON 缓存并进来（一次性迁移）。"""
    global _zone_rows
    if _zone_rows is not None and not force:
        return _zone_rows
    rows = {}
    try:
        for k, e in (load_letpub_cache() or {}).items():
            if not isinstance(e, dict):
                continue
            ts = float(e.get("ts") or 0)
            rows[k] = {"name": e.get("hit") or "",
                       "issn": k[5:] if k.startswith("issn:") else "",
                       "cas_up": e.get("zone") or "", "cas_base": "",
                       "jcr": "", "if_": "", "if5": "", "warn": "",
                       "status": ST_ZONE if e.get("zone") else ST_NONE,
                       "date": time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "",
                       "source": "LetPub"}
    except Exception:
        pass
    if os.path.exists(ZONE_XLSX):
        try:
            from openpyxl import load_workbook
            wb = load_workbook(ZONE_XLSX, read_only=True, data_only=True)
            ws = wb.worksheets[0]
            it = ws.iter_rows(values_only=True)
            hdr = [str(x or "").strip() for x in (next(it, None) or [])]
            legacy = (len(hdr) == 5)          # 旧的 5 列版：期刊名/ISSN/分区/状态/查询日期
            for r in it:
                if not r:
                    continue
                vals = [str(x or "").strip() for x in r]
                if legacy:
                    vals += [""] * (5 - len(vals))
                    d = {"name": vals[0], "issn": vals[1], "cas_up": vals[2],
                         "cas_base": "", "jcr": "", "if_": "", "if5": "", "warn": "",
                         "status": vals[3], "date": vals[4], "source": "LetPub"}
                else:
                    vals += [""] * (len(ZONE_COLS) - len(vals))
                    d = dict(zip(ZONE_COLS, vals))
                if not (d["name"] or d["issn"]):
                    continue
                rows[_zone_key(d["issn"], d["name"])] = d
            wb.close()
        except Exception as e:
            print(f"[WARN] 读分区表失败，按空表处理: {e}", file=sys.stderr)
    _zone_rows = rows
    return rows


def save_zone_xlsx(rows=None):
    """整表重写：先写 .tmp 再替换，避免中途打断留下坏文件。"""
    rows = rows if rows is not None else (_zone_rows or {})
    try:
        from openpyxl import Workbook
        os.makedirs(os.path.dirname(ZONE_XLSX), exist_ok=True)
        wb = Workbook(); ws = wb.active; ws.title = "期刊分区"
        ws.append(ZONE_HEADER)
        for k in sorted(rows):
            r = rows[k]
            ws.append([r.get(c, "") for c in ZONE_COLS])
        ws.freeze_panes = "A2"
        tmp = ZONE_XLSX + ".tmp"
        wb.save(tmp)
        os.replace(tmp, ZONE_XLSX)
        return True
    except Exception as e:
        print(f"[WARN] 分区表写入失败: {e}", file=sys.stderr)
        return False


def zone_stats():
    rows = load_zone_xlsx()
    n = {"已收录": 0, "未收录": 0, "查询失败": 0}
    src = {}
    for e in rows.values():
        n[e.get("status", "")] = n.get(e.get("status", ""), 0) + 1
        s = e.get("source") or "(空)"
        src[s] = src.get(s, 0) + 1
    return {"总数": len(rows), **n, "按来源": src, "文件": ZONE_XLSX}


def _zone_entry(key, older_than=None):
    """取可用条目；不存在或已过期返回 None。查询失败一律重试。"""
    e = (load_zone_xlsx().get(key))
    if not e:
        return None
    st = e.get("status") or ""
    if st == ST_FAIL:
        return None
    d = e.get("date") or ""
    if not d:
        return None
    try:
        age = (time.time() - time.mktime(time.strptime(d, "%Y-%m-%d"))) / 86400.0
    except Exception:
        return None
    limit = older_than if older_than is not None else (
        ZONE_TTL_DAYS if st == ST_ZONE else NEGATIVE_TTL_DAYS)
    return e if age <= limit else None


def _zone_upsert(key, name, issn, zone, status, cas_base="", jcr="", if_="", if5="",
                 warn="", source="LetPub", cas_base2=""):
    rows = load_zone_xlsx()
    rows[key] = {"name": name or "", "issn": issn or "", "cas_up": zone or "",
                 "cas_base": cas_base or "", "cas_base2": cas_base2 or "",
                 "jcr": jcr or "", "if_": if_ or "",
                 "if5": if5 or "", "warn": warn or "", "status": status,
                 "date": time.strftime("%Y-%m-%d"), "source": source}
    return rows


def _wait():
    global _last_req
    gap = INTERVAL - (time.time() - _last_req)
    if gap > 0:
        time.sleep(gap)
    _last_req = time.time()


def _norm_journal(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


# ---------------------------------------------------------------- 抓取

def _read_url(url, backend=None, timeout=60):
    """调 searchts read 抓页面。

    backend="curl_cffi" 时只用最快的一档（浏览器 TLS 指纹），不升级到
    Jina relay / stealth 浏览器；实测 LetPub 用这一档就够，快 2~3 倍。
    """
    cmd = ["searchts", "read"]
    if backend:
        cmd += ["--backend", backend]
    cmd.append(url)
    return subprocess.run(cmd, capture_output=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def fetch_letpub_page(url, timeout=60):
    """抓 LetPub 检索页：先快档，失败或过薄再走默认三级升级链。

    Returns:
        (markdown 文本, 后端说明)
    """
    try:
        r = _read_url(url, backend="curl_cffi", timeout=timeout)
        if r.returncode == 0 and len((r.stdout or "")) >= MIN_PAGE_CHARS:
            return r.stdout, "curl_cffi"
    except Exception:
        pass                            # 快档失败不报错，直接退默认链

    try:
        r = _read_url(url, timeout=timeout)
    except FileNotFoundError:
        return "", "找不到 searchts 命令"
    except subprocess.TimeoutExpired:
        return "", "searchts 超时"
    except Exception as e:
        return "", f"searchts 异常：{type(e).__name__}"

    if r.returncode != 0:
        return "", f"searchts 退出码 {r.returncode}（该刊可能未被收录）"
    return r.stdout or "", "默认链"


# ---------------------------------------------------------------- 解析

def _parse_letpub(md, want_issn=None, want_name=None):
    """从 LetPub 检索页 markdown 里取分区。返回 (zone, 命中刊名) 或 None。

    行形态：| 2075-5309 | [Buildings](...journalid=11142...) BUILDINGS-BASEL | 8.0 |
            IF: 3.4 ... | 3区 | 大类：工程技术 小类：... | SCIE | Yes | ... |
    """
    fallback = None
    q = _norm_journal(want_name) if want_name else ""
    for line in md.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 6:
            continue
        m = _LINK_NAME.search(cells[1])
        if not m:
            continue
        zone = None
        for c in cells:
            z = _ZONE_CELL.match(c)
            if z:
                zone = z.group(1)
                break
        if not zone:
            continue
        if want_issn:
            if cells[0].replace("–", "-").upper() != want_issn.upper():
                continue
            return zone, m.group(1)
        n = _norm_journal(m.group(1))
        if n == q:
            return zone, m.group(1)
        if fallback is None and q and (n.startswith(q[:12]) or q.startswith(n[:12])):
            fallback = (zone, m.group(1))
    return fallback


# ---------------------------------------------------------------- 查询

def _basis_value(d, basis):
    """按标准从归档行或 easyScholar 结果里取分区值。

    归档行用 `cas_up` 存新锐、`jcr` 存 JCR；easyScholar 结果里新锐叫 `cas`。
    两个键名都认，避免调用方记两套名字。
    """
    b = (basis or "jcr").strip().lower()
    if b == "xr":
        return str(d.get("cas_up") or d.get("cas") or "")
    return str(d.get("jcr") or "")


def letpub_zone(issn=None, name=None, use_cache=True, refresh=False, timeout=60,
                older_than=None, basis="jcr"):
    """查期刊分区（标准由 basis 决定）。

    Args:
        issn: 期刊 ISSN。easyScholar 是刊名接口，ISSN 只作归档键的加成
        name: 期刊名（主查询键）
        use_cache: False = 完全绕过缓存
        refresh: True = 忽略已有缓存重查（未收录项也会重查）
        basis: "jcr"（默认）或 "xr"（中科院新锐）
    Returns:
        (zone|None, 命中刊名|None, 来源说明)
    """
    if not issn and not name:
        return None, None, "既没给 ISSN 也没给刊名"

    # 注意：这里**不做任何查前预判**。会议录、Volume 之类也先用原名真查一次，
    # 因为「看起来像会议录」不等于「一定查不到」，早年 Proceedings of the
    # Institution of Mechanical Engineers 这种正规期刊就会被误杀。
    # 原因标注只在两次查询都失败之后做，见下面的 _na_reason()。

    key = _zone_key(issn, name)

    if use_cache and not refresh:
        e = _zone_entry(key, older_than=older_than)
        if e:
            return ((_basis_value(e, basis) or None), (e.get("name") or None),
                    "分区表缓存(" + (e.get("source") or "") + ")")

    # ---- 主通道：easyScholar（中科院分区两版 + JCR + IF + 预警）----
    if easy_key():
        try:
            rk = easyscholar_rank(name)
        except RuntimeError as ex:
            rk = None
            print(f"[WARN] easyScholar 不可用：{ex}", file=sys.stderr)
        if rk:
            val = _basis_value(rk, basis)
            rows = _zone_upsert(key, name, issn, rk["cas"],
                                ST_ZONE if rk["cas"] else ST_NONE,
                                cas_base=rk["cas_base"], jcr=rk["jcr"], if_=rk["if"],
                                if5=rk["if5"], warn=rk["warn"], source="easyScholar",
                                cas_base2=rk.get("cas_base2", ""))
            if not _DEFER_SAVE:
                save_zone_xlsx(rows)
            return (val or None), name, "easyScholar"

        # 第一次用原名没查到 → 规范化后再试一次（只多花一次请求）
        nm2, _ = _normalize_venue(name)
        if nm2 and nm2.strip().lower() != (name or "").strip().lower():
            try:
                rk = easyscholar_rank(nm2)
            except RuntimeError as ex:
                rk = None
                print(f"[WARN] easyScholar 规范化重试失败：{ex}", file=sys.stderr)
            if rk:
                val = _basis_value(rk, basis)
                rows = _zone_upsert(key, name, issn, rk["cas"],
                                    ST_ZONE if rk["cas"] else ST_NONE,
                                    cas_base=rk["cas_base"], jcr=rk["jcr"], if_=rk["if"],
                                    if5=rk["if5"], warn=rk["warn"], source="easyScholar",
                                    cas_base2=rk.get("cas_base2", ""))
                if not _DEFER_SAVE:
                    save_zone_xlsx(rows)
                return (val or None), name, "easyScholar(规范化后命中)"

        # 两次都查不到 → 这时才判原因。查前不判，避免误杀。
        why = _na_reason(name)
        rows = _zone_upsert(key, name, issn, "", why, source="easyScholar")
        if not _DEFER_SAVE:
            save_zone_xlsx(rows)
        return None, None, why

    # ---- 备用通道：LetPub（只有中科院分区），须用户明确同意 ----
    if not ALLOW_LETPUB:
        rows = _zone_upsert(key, name, issn, "", ST_FAIL, source="需授权(LetPub)")
        if not _DEFER_SAVE:
            save_zone_xlsx(rows)
        return None, None, "LetPub 未授权，已跳过"

    param, value = ("searchissn", issn) if issn else ("searchname", name)
    url = f"{LETPUB_SEARCH}&{param}={_quote(str(value))}&searchsort=relevance"

    # LetPub 会偶发返回空页（实测 CBM 一次通一次空、MSSP 连续两次空）。单次失败**不能**
    # 直接判"未收录"并写进缓存——那正是旧实现毒化缓存的根因。所以未取到时重试一次。
    zone = hn = None
    backend = ""
    for attempt in (1, 2):
        md, backend = fetch_letpub_page(url, timeout=timeout)
        if backend in ("找不到 searchts 命令", "searchts 超时") or backend.startswith("searchts"):
            return None, None, backend
        hit = _parse_letpub(md, want_issn=issn, want_name=name)
        zone, hn = (hit if hit else (None, None))
        if zone:
            break
        if attempt == 1:
            time.sleep(1.0)

    src = "ISSN 精确匹配" if (issn and zone) else ("刊名模糊匹配" if zone else "未查到")
    rows = _zone_upsert(key, hn or name, issn, zone,
                        ST_ZONE if zone else (ST_NONE if backend else ST_FAIL))
    if not _DEFER_SAVE:
        save_zone_xlsx(rows)
    return zone, hn, src


def letpub_zone_many(items, verbose=True, refresh=False, basis="jcr"):
    """批量：items 为 [(issn, name), ...]。

    ⚠️ **必须串行**：easyScholar 限 2 次/秒，这里按 1 秒 1 次，绝不可并发。
    """
    out = {}
    total = len(items)
    global _DEFER_SAVE
    _DEFER_SAVE = True
    try:
        for i, (issn, name) in enumerate(items, 1):
            zone, hn, src = letpub_zone(issn=issn, name=name,
                                        refresh=refresh, basis=basis)
            out[(issn, name)] = zone
            if verbose:
                tag = f"{issn or '-'} / {name or '-'}"
                print(f"  [{i}/{total}] {tag[:60]:<60} {zone or 'unknown'}  ({src})",
                      file=sys.stderr)
            _wait()
    finally:
        _DEFER_SAVE = False
        save_zone_xlsx()      # 整批只写一次盘
    return out


# ---------------------------------------------------------------- 批量入口
#
# easyScholar 是**按刊名**查询的接口，ISSN 不是查询主键，只作归档键的加成。
# 早期版本为了给 LetPub 做精确匹配，先用 Crossref 按 DOI 反查 ISSN 并缓存到
# `.issn_cache.json`；现已删除该路径——分区信息只允许来自 S2 的
# `publicationVenue.issn`（可能为空）或干脆不用 ISSN。


def zone_map_for_journals(name_to_doi, refresh=False, verbose=True,
                          issns=None, basis="jcr"):
    """刊名 → 分区（"1"~"4" 或 None）。

    这是**唯一**的分区批量入口，`paper_table` 与 `semantic_scholar_search` 都调它。

    Args:
        name_to_doi: {"期刊名": "DOI 或空串"}（DOI 目前不参与查询，保留兼容）
        issns: 可选的 {"期刊名": "ISSN"}，来自 S2 的 publicationVenue.issn。
               没有就用纯刊名查，**不做任何外部反查**。
        basis: "jcr"（默认）或 "xr"
    """
    issns = issns or {}
    items = [(issns.get(n) or None, n) for n in name_to_doi]
    if verbose:
        print(f"[ZONE] 开始查分区（basis={basis}，单本约 1 秒，串行，勿并发）…",
              flush=True)
    zones = letpub_zone_many(items, verbose=verbose, refresh=refresh, basis=basis)
    return {n: zones.get((issns.get(n) or None, n)) for n in name_to_doi}


# ---------------------------------------------------------------- CLI

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    as_issn = False
    refresh = False
    args = sys.argv[1:]
    # -h/--help 必须在这里拦掉：下面的循环会 break 出到"把参数当刊名"的分支，
    # 那样 `--help` 会被当成期刊名真的去联网查一次（v3.0 之前的行为）。
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        sys.exit(0)
    while args and args[0].startswith("--"):
        if args[0] == "--issn":
            as_issn = True
        elif args[0] == "--refresh":
            refresh = True
        elif args[0] == "--letpub":     # 兼容老命令行，语义已统一
            pass
        elif args[0] == "--allow-letpub":
            ALLOW_LETPUB = True
        elif args[0] == "--stats":
            st = zone_stats()
            if not os.path.exists(ZONE_XLSX):
                save_zone_xlsx()      # 第一次跑就把表落盘，方便用户直接打开
            print("分区表:", st["文件"])
            for k in ("总数", "已收录", "未收录", "查询失败"):
                print(f"  {k}: {st[k]}")
            sys.exit(0)
        else:
            break
        args = args[1:]

    if args and args[0] == "--batch":
        with open(args[1], "r", encoding="utf-8") as f:
            journals = [ln.strip() for ln in f if ln.strip()]
    else:
        journals = args

    for j in journals:
        if as_issn:
            zone, hit, src = letpub_zone(issn=j, refresh=refresh)
        else:
            zone, hit, src = letpub_zone(name=j, refresh=refresh)
        print(f"{j}\t{zone + '区' if zone else '未收录'}\t{src}")
