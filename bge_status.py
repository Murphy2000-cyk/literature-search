# -*- coding: utf-8 -*-
"""查看本机 BGE 占用情况：谁在用、跑的是中文还是英文分支、各占多少内存

用途：回答「英文 BGE 现在开着吗」「怎么单独把它关掉而不影响中文侧」。

核心事实
--------
**BGE 按进程隔离，不存在"共享的一侧"。**

- 知识库常驻服务进程：加载 **中文** 分支（`bge-base-zh-v1.5`），长期驻留
- 论文检索进程：加载 **英文** 分支（`bge-base-en-v1.5`），**跑完就退出**

结束论文检索进程时，那个进程的英文 BGE 与它自己那份 torch 一起消失，
**完全不影响**知识库那个进程里的中文 BGE（实测：结束后常驻服务的 PID 都没变）。

用法
----
    python bge_status.py            # 查看当前状态
"""
import os
import sys
# 控制台编码兜底：Windows 默认 GBK，打印中文/特殊字符会 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# 依赖已装进 py313 环境，无需 sys.path.insert
try:
    import psutil
except ImportError:
    print("需要 psutil：python -m pip install psutil")
    sys.exit(1)

# 判定关键词
KB_HINTS = ("reflect_searchd", "webui", "lk.py", "iterative_search", "reflect_webui")
LIT_HINTS = ("search_pipeline", "literature-search", "relevance_scorer",
             "semantic_scholar_search", "paper_table")

KB_PORTS = (8765, 8766, 8767)


def port_owners():
    """端口 -> PID"""
    out = {}
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port in KB_PORTS:
                out[c.laddr.port] = c.pid
    except Exception:
        pass
    return out


def main():
    me = psutil.Process(os.getpid())
    ports = port_owners()

    print("=" * 76)
    print("本机 BGE / Python 进程占用情况")
    print("=" * 76)

    kb_rows, lit_rows, other_rows = [], [], []
    for p in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            name = (p.info["name"] or "").lower()
            if "python" not in name:
                continue
            pid = p.info["pid"]
            rss = (p.info["memory_info"].rss if p.info["memory_info"] else 0) / 1024 / 1024
            try:
                cmd = " ".join(p.cmdline())
            except Exception:
                cmd = "(无法读取命令行)"
            low = cmd.lower()

            served = [pt for pt, opid in ports.items() if opid == pid]
            if served or any(h in low for h in KB_HINTS):
                tag = f"知识库常驻服务（中文分支），端口 {served}" if served else "知识库相关（中文分支）"
                kb_rows.append((pid, rss, tag, cmd))
            elif any(h in low for h in LIT_HINTS):
                # 光看路径不够：本工具自己、journal_quartile 等也在同一目录下。
                # 真正加载了 BGE 的进程内存 ≥ 700 MB（torch import 就要 700+），
                # 所以用内存做二次判据。
                if pid == os.getpid():
                    other_rows.append((pid, rss, "本状态查看工具自身", cmd))
                elif rss >= 300:
                    lit_rows.append((pid, rss, "论文检索（★ 英文 BGE 已加载）", cmd))
                else:
                    lit_rows.append((pid, rss, "论文检索相关（未加载 BGE）", cmd))
            else:
                other_rows.append((pid, rss, "其他 python", cmd))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    def show(rows, title):
        print(f"\n【{title}】{len(rows)} 个")
        if not rows:
            print("  （无）")
            return
        for pid, rss, tag, cmd in sorted(rows, key=lambda x: -x[1]):
            print(f"  PID {pid:>6}  {rss:>7.0f} MB  {tag}")
            print(f"          {cmd[:110]}")

    show(kb_rows, "知识库侧（中文 BGE，长期驻留）")
    show(lit_rows, "论文检索侧（英文 BGE，跑完即退）")
    show(other_rows, "其他 python 进程")

    total_kb = sum(r for _, r, _, _ in kb_rows)
    lit_loaded = [r for _, r, t, _ in lit_rows if "★" in t]
    total_lit = sum(lit_loaded)
    print("\n" + "-" * 76)
    print(f"  知识库侧合计 : {total_kb:>7.0f} MB   （一直在，中文 BGE）")
    if lit_loaded:
        print(f"  英文 BGE 合计: {total_lit:>7.0f} MB   "
              f"★ {len(lit_loaded)} 个进程正在加载英文 BGE；它们结束后自动全部归还")
    else:
        print(f"  英文 BGE     : {0:>7.0f} MB   ← 当前没有英文 BGE 在跑（已关）")
    all_rows = kb_rows + lit_rows + other_rows
    print(f"  本机 python 合计: {sum(r for _, r, _, _ in all_rows):>7.0f} MB")

    print("\n" + "=" * 76)
    print("怎么单独关掉英文 BGE 侧")
    print("=" * 76)
    print("  1) 正常情况：什么都不用做。论文检索进程跑完自己退出，")
    print("     英文 BGE 与它那份 torch 一起消失，中文侧零影响。")
    print("  2) 如果某个论文检索进程卡住了，可以只结束它：")
    print("       Stop-Process -Id <上面列出的论文检索侧 PID>")
    print("     ⚠️ 只结束【论文检索侧】的 PID，不要动知识库侧的。")
    print("  3) 反过来，如果跑论文检索时想腾内存，可以临时停掉知识库侧：")
    print("       python \"<知识库 skill 目录>\\scripts\\search_reflect.py\" --stop-daemon")
    print("     释放约 825 MB；下次用知识库时它会自动拉起。")


if __name__ == "__main__":
    main()
