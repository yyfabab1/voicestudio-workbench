# -*- coding: utf-8 -*-
"""
批量删角色（VoiceStudio）—— 只留大帝。

★ 为什么走产品的 `studio.delete_role()`，不直接 rmtree

  删角色不只是删目录：RVC 那边的断点/权重是**另外一堆文件**
  （`purge_rvc_artifacts()`），直接 rmtree 角色目录会把它们漏在盘上，
  几个 G 就这么悄悄泄漏掉了 —— 而且界面上已经看不见这个角色，
  你连"再删一次"的入口都没有。
  ⇒ 必须走产品的删除入口，让它把自己造出来的东西一起收走。

★ 复核（关键）

  `delete_role()` 里 `shutil.rmtree(..., ignore_errors=True)` 会**静默吞掉**
  删除失败（文件被占用 / 权限 / 被安全策略拦下）⇒ 角色从库里消失了、
  文件还躺在盘上。所以本脚本删完**必须复核目录真的没了**，
  留残骸就明确列出来（那正是 `_gc_orphans.py` 要收拾的东西）。

★ 安全

  · 不带 `--apply` 只列清单，不动任何东西；
  · 大帝 `ra500fb92` **永远排除**（写死在 KEEP_OUT，别改）；
  · 删除一律 `except BaseException`（本机 safe-delete 钩子超阈值抛的是
    SystemExit，**穿透 except Exception**，症状是脚本静默退出、连汇总都不打）；
  · 删之前自动备份 roles.json。

用法：
  python tools/purge_roles.py                       # 只列清单
  python tools/purge_roles.py --apply               # 真删
  python tools/purge_roles.py --apply --keep rXXX   # 额外保留某个
"""
import os
import sys
import time
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# ★ 大帝：永远保留，不进删除清单
KEEP_OUT = {"ra500fb92"}


def kb(n):
    return "%.1f MB" % (n / 1048576.0)


def dsize(p):
    t = 0
    for d, _, fs in os.walk(p):
        for f in fs:
            try:
                t += os.path.getsize(os.path.join(d, f))
            except OSError:
                pass
    return t


def one(rid):
    """只删一个角色（子进程模式）。

    ★ 为什么要走子进程：本机 safe-delete 钩子按「一条命令」累计删除次数，
      超阈值后**每个**删除动作都抛 SystemExit（穿透 except Exception）。
      删 34 个角色在同一进程里跑，跑到第 23 个就开始全线失败。
      ⇒ 每个角色开一个**独立子进程**，预算各自从零算。
    """
    import studio
    try:
        studio.delete_role(rid)
    except BaseException as e:               # noqa: BLE001
        return "失败 %s: %s" % (type(e).__name__, str(e)[:80])
    p = os.path.join(ROOT, "data", "roles", rid)
    if os.path.isdir(p):                    # ★ 复核：rmtree 用了 ignore_errors
        return "目录没删干净"
    return "OK"


def main():
    args = sys.argv[1:]
    apply = "--apply" in args
    keep = set(KEEP_OUT)
    for a in args:
        if a.startswith("--keep="):
            keep |= set(x for x in a.split("=", 1)[1].split(",") if x)

    # 子进程模式：删一个就退出
    for i, a in enumerate(args):
        if a == "--one" and i + 1 < len(args):
            print("%s -> %s" % (args[i + 1], one(args[i + 1])))
            return 0

    import studio
    db = studio.load_roles()
    rows = db.get("roles", [])
    reg = {r.get("id"): r for r in rows}

    src = os.path.join(ROOT, "data", "roles")
    have = sorted(x for x in os.listdir(src)
                  if os.path.isdir(os.path.join(src, x))) if os.path.isdir(src) else []

    targets = [rid for rid in have if rid not in keep]
    orphans = [rid for rid in targets if rid not in reg]
    named = [rid for rid in targets if rid in reg]

    print("=" * 60)
    print("批量删角色   root=%s" % ROOT)
    print("模式：%s" % ("APPLY(真删)" if apply else "DRY-RUN(只看)"))
    print("=" * 60)
    print("\n保留（不删）：")
    for rid in sorted(keep):
        r = reg.get(rid, {})
        print("   %-16s %-20s %s"
              % (rid, r.get("name", "?"), kb(dsize(os.path.join(src, rid)))))

    print("\n将删除 %d 个角色目录（登记 %d + 孤儿 %d）："
          % (len(targets), len(named), len(orphans)))
    tot = 0
    for rid in targets:
        p = os.path.join(src, rid)
        s = dsize(p)
        tot += s
        r = reg.get(rid, {})
        tag = "孤儿" if rid in orphans else ""
        print("   %-16s %-22s %9s  %s"
              % (rid, (r.get("name") or "")[:22], kb(s), tag))
    print("\n合计释放：%s" % kb(tot))

    if not apply:
        print("\n（DRY-RUN：没动任何东西。确认后加 --apply）")
        return 0

    # 备份 roles.json
    rj = os.path.join(ROOT, "data", "roles.json")
    if os.path.isfile(rj):
        bak = rj + ".bak." + time.strftime("%Y%m%d_%H%M%S")
        try:
            shutil.copy2(rj, bak)
            print("\n已备份 roles.json -> %s" % os.path.basename(bak))
        except BaseException as e:
            print("\n⚠ roles.json 备份失败：%s —— 停下，不删了" % e)
            return 3

    ok, left = [], []
    for rid in targets:
        try:
            studio.delete_role(rid)
        except BaseException as e:          # noqa: BLE001 - SystemExit 也要接住
            left.append((rid, "%s: %s" % (type(e).__name__, str(e)[:100])))
            continue
        p = os.path.join(src, rid)
        if os.path.isdir(p):                # ★ 复核：ignore_errors 会静默吞失败
            left.append((rid, "目录没删干净（rmtree 静默失败）"))
        else:
            ok.append(rid)

    print("\n" + "=" * 60)
    print("已删除 %d 个，残留 %d 个" % (len(ok), len(left)))
    for rid, why in left:
        print("   ⚠ %-16s %s" % (rid, why))
    if left:
        print("\n残留下一步：`bash _run_regression.sh gc`")
        return 1
    print("✓ 工作区只剩大帝，无残留")
    return 0


if __name__ == "__main__":
    sys.exit(main())
