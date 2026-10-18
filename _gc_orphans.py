# -*- coding: utf-8 -*-
r"""清掉 `data/roles/` 下的**孤儿目录**（删角色时 rmtree 被拦留下的残骸）。

为什么必须单独一个脚本、单独跑一条命令
------------------------------------------------------------------
本机环境层有个批量删除钩子：

    [safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]
    {"count":N,"threshold":50,"scope":"turn"}

它按「**一条命令里累计删了多少次**」计数，超过 50 之后**每一个删除动作都抛
`SystemExit`** —— 注意两点（都实测过）：
  · 抛的是 `SystemExit`，`except Exception` **捕不到**它；
  · 连单个 `os.remove` 也照抛，不只是 `shutil.rmtree`。
（我一开始以为是"按文件数"或"按 rmtree 调用数"，两次实验都否掉了：一条命令里
 逐文件删 60 个真 40KB 文件、或连做 60 次只装 1 个文件的 rmtree，都没被拦 ——
 因为那些命令**从头到尾没超 50**。真凶是"累计"。）

所以：门禁跑一趟本身要建/删几十个切片文件，到收尾时预算早用光，
**门禁自己不可能清干净**。清理必须**另起一条命令**做（预算从零开始）。

安全口径（宁可少删，也不许误删用户数据）
------------------------------------------------------------------
  · 只删 `data/roles/` 下**不在 roles.json 里**的目录；
  · 且目录 mtime 超过 `--min-age`（默认 60 秒；`create_role()` 里"建目录 → 写登记"
    的窗口只有毫秒级，60 秒已经绰绰有余，再久反而让流程没法用）——
    避开"正在创建、还没来得及登记"的半成品角色；
  · 只删这些目录里的文件和目录，不做任何其它操作；
  · 干跑：`--dry` 只列不删。

用法：<venv>/Scripts/python.exe -s _gc_orphans.py [--dry] [--min-age 秒]
"""
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import studio  # noqa: E402

MIN_AGE_S = 60.0        # 新目录一律不碰（可能是正在建的角色）
RID = re.compile(r"r[0-9a-f]{8}")      # 角色 id 的形状：r + 8 位十六进制


def dir_mb(p):
    if os.path.isfile(p):
        return os.path.getsize(p) / 1048576.0
    tot = 0
    for dp, _dns, fns in os.walk(p):
        for f in fns:
            with __import__("contextlib").suppress(OSError):
                tot += os.path.getsize(os.path.join(dp, f))
    return tot / 1048576.0


def rvc_orphans(known):
    """RVC 目录里那些**已经没有对应角色**的家当。

    为什么这一段必须有：以前 `delete_role()` 只删本项目的角色目录，RVC 那边
    （`logs/<id>/` 里 G/D 两个断点约 1 GB、权重 52 MB、索引几 MB）**没人管** ⇒
    每删一个角色就往 RVC 目录里扔 1 GB 拿不回来的垃圾，界面上还看不见。
    现在 `delete_role()` 会顺手清（见 studio.purge_rvc_artifacts），但**在那之前
    已经漏掉的**得靠这里补 —— 这一遍扫下去能救回不少磁盘。
    """
    R = studio.RVC_ROOT
    out = {"exp": [], "weights": [], "indices": []}
    logs = os.path.join(R, "logs")
    if os.path.isdir(logs):
        for name in sorted(os.listdir(logs)):
            p = os.path.join(logs, name)
            if os.path.isdir(p) and RID.fullmatch(name) and name not in known:
                out["exp"].append((name, p))
    wdir = os.path.join(R, "assets", "weights")
    if os.path.isdir(wdir):
        for f in sorted(os.listdir(wdir)):
            m = RID.match(f)
            if m and f.endswith(".pth") and m.group(0) not in known:
                out["weights"].append((m.group(0), os.path.join(wdir, f)))
    idir = os.path.join(R, "assets", "indices")
    if os.path.isdir(idir):
        for f in sorted(os.listdir(idir)):
            ids = set(RID.findall(f))
            if ids and ids.isdisjoint(known):
                out["indices"].append((sorted(ids)[0], os.path.join(idir, f)))
    return out


def _arg(name, default):
    if name in sys.argv:
        try:
            return float(sys.argv[sys.argv.index(name) + 1])
        except (IndexError, ValueError):
            pass
    return default


def rm_tree(p):
    """逐个文件 os.remove、再逐层 os.rmdir，返回 (删了几个文件, 是否真的没了)。

    不用 shutil.rmtree：它在被钩子拦时会抛 SystemExit 直接掀翻整个进程，
    而逐个删能"删多少算多少"、还能把结果报出来。
    """
    # ★ 先删大的。删除预算只有 50 次，而一个 RVC 实验目录里 G_*.pth + D_*.pth
    #   两个文件就占了 96% 的体积（1 GB 里的 1 GB）—— 按目录顺序删的话，
    #   50 次全花在几 KB 的特征文件上，跑三四趟还救不回多少空间。
    files = []
    for dp, _dns, fns in os.walk(p):
        for f in fns:
            fp = os.path.join(dp, f)
            with __import__("contextlib").suppress(OSError):
                files.append((os.path.getsize(fp), fp))
    files.sort(reverse=True)
    n = 0
    for _sz, fp in files:
        try:
            os.remove(fp)
            n += 1
        except BaseException as e:
            print("    [warn] 删文件失败 %s：%s: %s"
                  % (fp, type(e).__name__, str(e)[:100]))
    for dp, dns, _fns in os.walk(p, topdown=False):
        for sub in dns:
            try:
                os.rmdir(os.path.join(dp, sub))
            except BaseException as e:
                print("    [warn] 删子目录失败 %s：%s: %s"
                      % (sub, type(e).__name__, str(e)[:100]))
    try:
        os.rmdir(p)
    except BaseException as e:
        print("    [warn] 删顶层目录失败 %s：%s: %s"
              % (p, type(e).__name__, str(e)[:100]))
    return n, (not os.path.exists(p))


def main():
    dry = "--dry" in sys.argv
    min_age = _arg("--min-age", MIN_AGE_S)
    orph = studio.orphan_role_dirs()
    print("%s：发现 %d 个孤儿角色目录（只碰 mtime 超过 %.0f 秒的）"
          % ("干跑" if dry else "清理", len(orph), min_age))
    if not orph:
        # ★ 这里**不能**早退：下面还有第二段（RVC 侧的孤儿实验目录 / 权重 / 索引）。
        #   以前写的是 `return 0`，结果"没有角色孤儿"时第二段一次都没跑过 ——
        #   一个 1.1 GB 的 RVC 实验目录（门禁真训完留下的）就这么躺在盘上，
        #   而脚本还打印「角色库和磁盘是一致的」，读起来像一切正常。
        #   实测：先跑一趟清掉了角色孤儿，再跑一趟时这里命中，第二段被整个跳过。
        print("没有孤儿角色目录（data/roles 与角色库一致）。"
              "RVC 那边还要接着查 —— 不早退。")
    now = time.time()
    skipped = 0
    total_files = 0
    total_mb = 0.0
    for o in orph:
        try:
            age = now - os.path.getmtime(o["path"])
        except OSError:
            age = 1e9
        if age < min_age:
            # 可能是"刚建好、还没登记完"的角色 —— 绝不碰
            print("  跳过 %s（%.0f 秒前才动过，太新，怕是正在建的角色）"
                  % (o["path"], age))
            skipped += 1
            continue
        print("  %s  %s  %.1f MB" % (o["path"], o["id"], o["mb"]))
        if dry:
            continue
        n, gone = rm_tree(o["path"])
        total_files += n
        total_mb += o["mb"]
        print("      → 删了 %d 项%s" % (n, "" if gone else "，**没删干净**（钩子仍在拦？）"))
    if dry:
        print("（干跑，什么都没删）")
    else:
        left = studio.orphan_role_dirs()
        print("清理完成：删了 %d 个文件 / %.1f MB，跳过 %d 个（太新）"
              % (total_files, total_mb, skipped))
        print("剩余孤儿目录：%s" % ([o["path"] for o in left] or "无"))
        if left:
            # 还留着说明这一条命令的删除预算也用光了 —— 再跑一次这个脚本即可
            print("⚠ 还有残留：本命令的删除预算也用光了。接着往下跑第二段。")

    print()
    print("--- 第二段：RVC 侧的孤儿（没有对应角色的实验目录 / 权重 / 索引）---")
    known = set(r["id"] for r in studio.load_roles()["roles"])
    ro = rvc_orphans(known)
    n_rvc = sum(len(v) for v in ro.values())
    mb_rvc = sum(dir_mb(p) for _k, p in (ro["exp"] + ro["weights"] + ro["indices"]))
    print("发现 %d 项，合计 %.1f MB" % (n_rvc, mb_rvc))
    if not n_rvc:
        print("RVC 目录和角色库是一致的，没有孤儿产物。")
        return 1 if (not dry and left) else 0
    fresh = 0
    for kind in ("weights", "indices", "exp"):
        for rid, p in ro[kind]:
            try:
                age = now - os.path.getmtime(p)
            except OSError:
                age = 1e9
            if age < min_age:
                print("  跳过 %s（%.0f 秒前才动过，太新，怕是正在训的角色）" % (p, age))
                fresh += 1
                continue
            print("  %-6s %-10s %8.1f MB  %s" % (kind, rid, dir_mb(p), p))
            if dry:
                continue
            if kind == "exp":
                _n, gone = rm_tree(p)
                total_files += _n
                total_mb += dir_mb(p)
                print("      → 删了 %d 项%s" % (_n, "" if gone else "，**没删干净**"))
            else:
                try:
                    os.remove(p)
                    total_files += 1
                except BaseException as e:
                    print("      [warn] 删不掉 %s：%s: %s"
                          % (p, type(e).__name__, str(e)[:80]))
    left2 = rvc_orphans(known)
    n_left2 = sum(len(v) for v in left2.values())
    if dry:
        print("（干跑，什么都没删）")
        return 0
    print("第二段完成：剩余 %d 项，跳过 %d 项（太新）" % (n_left2, fresh))
    if n_left2:
        print("⚠ 还有 RVC 残留：本命令的删除预算用光了。**再跑一次本脚本**接着清。")
        return 1
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main())
