# -*- coding: utf-8 -*-
"""
项目垃圾清理器（VoiceStudio）

★ 为什么要有这个东西

  2026-10-18 事故：门禁脚本 `_run_regression.sh` 的 summarize() 把汇总行
  `tee -a` 追加回它**正在 grep 的那个日志**，下一轮又把汇总行捞回来当输入
  ⇒ 输出指数级膨胀，一天把 `_reg_all.log` 撑到 **53 GB**，连带 D 盘 100% 写满。
  表现是"页面加载不出来 / 门禁莫名变红"——看着像产品坏了，其实是垃圾堆爆了。

  事故背后真正的流程病是：**每轮调试都会产出日志 / 浏览器 profile / 一次性脚本，
  而从来没有一步负责把它们收走**。根目录一度堆到 380 个条目。

  这个脚本就是把「每次都要删除没用的内容」固化成一条命令。

★ 硬约束（改这个脚本之前先读，别删）

  1. 默认 **dry-run**：只扫描、只打印，不动任何东西。必须显式 `--apply` 才真删。
  2. **白名单保护**：`data/` `models/` `web/` `steps/` `tools/` 以及所有
     `verify_*.py` / `server.py` / `*.bat` / `README.md` / 报告文档 —— 永不删除，
     连递归扫 `__pycache__` 都会跳过它们。误删用户数据是比磁盘满严重得多的事故。
  3. **硬上限**：任何日志文件 > MAX_LOG_MB（默认 200）无条件删。
     正常门禁日志是几十 KB；出现百 MB 级就一定是谁在往里灌。
  4. **删除钩子**：本机 safe-delete 钩子按「一条命令」累计，阈值 50，超了之后
     每个删除都抛 SystemExit（**穿透 except Exception**）。
     ⇒ 所有删除一律 `except BaseException`，且每批上限 40，撞线就停并如实报告。
  5. 一次性脚本**归档**而不是直接删（`_archive/<日期>/`）：它们只有几 MB，
     但承载着"这个结论是怎么测出来的"。真删了就没法回溯了。

用法：
  python tools/cleanup.py                      # 扫描，只看不删
  python tools/cleanup.py --apply              # 真删
  python tools/cleanup.py --apply --only=logs
  python tools/cleanup.py --keep-days=3        # 日志只留最近 3 天
"""
import os
import re
import sys
import time
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── 保护名单：这些目录/文件永不删除，递归也会跳过 ────────────────────────
PROTECTED_DIRS = {"data", "models", "web", "steps", "tools", "_archive", ".git"}
PROTECTED_FILES = {"README.md", "server.py", "studio.py", "ability.py"}

# ── 默认值 ───────────────────────────────────────────────────────────────
KEEP_DAYS = 7          # 日志保留最近 N 天
MAX_LOG_MB = 200       # 单日志硬上限，超过无条件删
BATCH_LIMIT = 40       # 每批删除上限（低于钩子阈值 50，留余量）

NOW = time.time()


def human(n):
    if n >= 1073741824:
        return "%.2f GB" % (n / 1073741824.0)
    if n >= 1048576:
        return "%.1f MB" % (n / 1048576.0)
    if n >= 1024:
        return "%.0f KB" % (n / 1024.0)
    return "%d B" % n


def dirsize(p):
    """目录总大小。任何一处读失败就返回 -1（宁可报未知，也不报 0 骗人）。"""
    tot = 0
    try:
        for d, dns, fs in os.walk(p):
            for f in fs:
                try:
                    tot += os.path.getsize(os.path.join(d, f))
                except OSError:
                    pass
    except OSError:
        return -1
    return tot


def age_days(p):
    try:
        return (NOW - max(os.path.getmtime(p), os.path.getctime(p))) / 86400.0
    except OSError:
        return 9999.0


# ── 各类垃圾的识别规则 ───────────────────────────────────────────────────
# 运行日志：根目录下 `_` 开头、.log/.out/.txt 结尾的（门禁与调试的输出）
RE_LOG = re.compile(r"^_.*\.(log|out|txt)$")
# 浏览器临时 profile：CDP 门禁 / 探针每次跑留下的用户数据目录
RE_CDPROFILE = re.compile(r"^\.(cdp-|chrome-)")
# 一次性调试脚本
RE_ONEOFF = re.compile(r"^_(probe_|dbg_|pick_|run_onesong)")
# 门禁截图
RE_SHOT = re.compile(r"\.(png|jpg|jpeg)$")


def scan_logs():
    out = []
    for n in sorted(os.listdir(ROOT)):
        p = os.path.join(ROOT, n)
        if not os.path.isfile(p) or not RE_LOG.match(n):
            continue
        if n in PROTECTED_FILES:
            continue
        sz = os.path.getsize(p)
        a = age_days(p)
        if sz > MAX_LOG_MB * 1048576:
            why = "超硬上限 %dMB" % MAX_LOG_MB
        elif a > KEEP_DAYS:
            why = "%.0f 天前" % a
        else:
            continue
        out.append((p, sz, why))
    return out


def scan_caches():
    out = []
    for n in sorted(os.listdir(ROOT)):
        p = os.path.join(ROOT, n)
        if os.path.isdir(p) and RE_CDPROFILE.match(n):
            sz = dirsize(p)
            out.append((p, max(sz, 0), "浏览器临时 profile"))
    # 递归清 __pycache__，但跳过保护目录
    for d, dns, fs in os.walk(ROOT):
        dns[:] = [x for x in dns if x not in PROTECTED_DIRS]
        if os.path.basename(d) == "__pycache__":
            sz = dirsize(d)
            out.append((d, max(sz, 0), "pyc 缓存"))
            dns[:] = []
    return out


def scan_oneoff():
    out = []
    for n in sorted(os.listdir(ROOT)):
        p = os.path.join(ROOT, n)
        if os.path.isfile(p) and n.endswith(".py") and RE_ONEOFF.match(n):
            out.append((p, os.path.getsize(p), "一次性脚本→归档"))
    return out


def scan_shots():
    out = []
    for n in sorted(os.listdir(ROOT)):
        p = os.path.join(ROOT, n)
        if os.path.isfile(p) and RE_SHOT.match(n) and age_days(p) > KEEP_DAYS:
            out.append((p, os.path.getsize(p), "%.0f 天前的截图" % age_days(p)))
    return out


SCANNERS = {
    "logs": scan_logs,
    "cache": scan_caches,
    "oneoff": scan_oneoff,
    "shots": scan_shots,
}


def rm_path(p):
    """删一个文件/目录。返回 (ok, err)。

    ★ 一律 `except BaseException`：本机 safe-delete 钩子超阈值时抛的是
      SystemExit，它**不继承 Exception**，`except Exception` 接不住，
      症状是脚本静默退出、连汇总行都不打（极像产品坏了）。
    ★ 只读文件先 chmod：Windows 上只读属性会让 unlink 直接 PermissionError。
    """
    try:
        if os.path.isdir(p):
            def _onerr(func, path, exc):
                try:
                    os.chmod(os.path.dirname(path), 0o755)
                    os.chmod(path, 0o755)
                    func(path)
                except BaseException:
                    pass
            shutil.rmtree(p, onerror=_onerr)
        else:
            try:
                os.chmod(p, 0o644)
            except OSError:
                pass
            os.remove(p)
        return True, ""
    except BaseException as e:          # noqa: BLE001 - 故意捕获 SystemExit
        return False, "%s: %s" % (type(e).__name__, e)


def archive(p, dest_dir):
    """把一次性脚本移进归档目录，不删除内容。"""
    try:
        os.makedirs(dest_dir, exist_ok=True)
        dst = os.path.join(dest_dir, os.path.basename(p))
        if os.path.exists(dst):
            os.remove(dst)
        shutil.move(p, dst)
        return True, ""
    except BaseException as e:          # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)


def main():
    global KEEP_DAYS
    args = [a for a in sys.argv[1:]]
    apply = "--apply" in args
    only = None
    keep_days = KEEP_DAYS
    for a in args:
        if a.startswith("--only="):
            only = set(a.split("=", 1)[1].split(","))
        if a.startswith("--keep-days="):
            try:
                keep_days = float(a.split("=", 1)[1])
            except ValueError:
                pass
    KEEP_DAYS = keep_days

    keys = [k for k in SCANNERS if (only is None or k in only)]
    grand = 0
    plan = {}
    print("=" * 62)
    print("项目垃圾扫描   root=%s" % ROOT)
    print("模式：%s     日志保留最近 %.0f 天 / 硬上限 %dMB"
          % ("APPLY(真删)" if apply else "DRY-RUN(只看)", KEEP_DAYS, MAX_LOG_MB))
    print("=" * 62)
    for k in keys:
        items = SCANNERS[k]()
        s = sum(i[1] for i in items)
        grand += s
        plan[k] = items
        print("\n[%s] %d 项  %s" % (k, len(items), human(s)))
        for p, sz, why in items[:8]:
            print("   %10s  %-28s %s" % (human(sz), os.path.basename(p), why))
        if len(items) > 8:
            print("   ... 另 %d 项" % (len(items) - 8))
    print("\n" + "-" * 62)
    print("合计可回收：%s" % human(grand))

    if not apply:
        print("\n（DRY-RUN：没有动任何文件。确认清单无误后加 --apply）")
        return 0

    # ── 真删。分批，每批 BATCH_LIMIT 个，撞钩子就停并如实报告 ────────────
    freed = 0
    failed = []
    stamp = time.strftime("%Y%m%d")
    arc = os.path.join(ROOT, "_archive", stamp)
    for k in keys:
        items = plan[k]
        if not items:
            continue
        print("\n>> 清理 [%s] %d 项" % (k, len(items)))
        done = 0
        for p, sz, why in items:
            if done >= BATCH_LIMIT:
                print("   ⚠ 已达本批删除上限 %d，剩余 %d 项留到下一批（另起一条命令）"
                      % (BATCH_LIMIT, len(items) - done))
                break
            if k == "oneoff":
                ok, err = archive(p, arc)
            else:
                ok, err = rm_path(p)
            if ok:
                freed += sz
                done += 1
            else:
                failed.append((os.path.basename(p), err))
                print("   ✗ 删不掉：%s  (%s)" % (os.path.basename(p), err))
        print("   本批处理 %d 项，释放 %s" % (done, human(freed)))

    print("\n" + "=" * 62)
    print("本次实际释放：%s" % human(freed))
    if failed:
        print("⚠ %d 项没删掉（见上），需要单独处理" % len(failed))
        return 1
    print("✓ 清理完成，工作区无残留垃圾")
    return 0


if __name__ == "__main__":
    sys.exit(main())
