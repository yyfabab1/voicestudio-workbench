# -*- coding: utf-8 -*-
"""
打包器（VoiceStudio）—— 工作台代码 与 角色数据**分开**打两个包。

★ 为什么分开

  角色数据（data/roles）是**内容资产**，体积大、会一直长；
  工作台（源码 + 门禁）是**程序**，体积小、稳定。
  混在一个仓库里 ⇒ 任何人想拿代码都得先拖几百 MB 音频。
  ⇒ 两仓分开：代码仓轻量可 clone，角色仓按需取。

★ 白名单式打包（重点）

  工作台只收**明确列出**的东西，其余一律不进包。原因很实在：
  根目录堆过 380 个条目，里面 464 MB 是 `_ab_gain` / `_longtest` /
  `_speechlong` 这类实验产物。用"排除法"打包，下次新造一个 `_xxx` 目录
  就会**静默混进包里**；用白名单，新目录默认不进，得显式加。

★ 角色包默认排除大帝（`ra500fb92`，2.69 GB）

  这不是随手加的默认，是老板的明确要求：大帝的数据不出包、不上 GitHub。
  换个说法：这个打包器一旦不带 `--exclude` 跑，就会把大帝漏出去 ⇒
  **`--exclude` 的默认值就钉在这里，别改成空。**

用法：
  python tools/pack.py workbench [--out DIR]
  python tools/pack.py roles [--out DIR] [--exclude ra500fb92,...]
  python tools/pack.py all    [--out DIR] [--exclude ra500fb92]

  默认输出目录：D:/AHC/_dist/
"""
import os
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = "D:/AHC/_dist"
# ★ 大帝：永远排除，别动这一行
KEEP_OUT = "ra500fb92"

# 工作台白名单（顶层文件 / 目录）
WB_TOP_FILES = [
    "server.py", "studio.py", "modelhub.py",
    "seedvc.py", "tone.py", "viz.py", "vizfx.py",
    "fix_role_weight.py", "_gc_orphans.py", "_start_server.py",
    "_cer_metrics.py",
    "README.md", "_run_regression.sh",
]
# ★ `.testlib` 看着像垃圾，但 `verify_music.py` 真的在读它里面的
#   broken.wav / 加密容器样本 ⇒ 必须进包。别因为名字像临时目录就排掉。
WB_TOP_DIRS = ["web", "stages", "steps", "tools", ".testlib"]
WB_TOP_GLOB = [("verify_", ".py"),
               ("启动VoiceStudio", ".bat")]

# 打包时无条件跳过
SKIP_NAMES = {"__pycache__", ".git", "node_modules"}
SKIP_PREFIX = (".cdp-", ".chrome-")
SKIP_SUFFIX = (".log", ".out", ".png", ".pyc", ".tmp")


def want(path, rel):
    """单个文件是否进包（目录层的白名单已在外面挡过）。"""
    b = os.path.basename(path)
    if b in SKIP_NAMES or b.startswith(SKIP_PREFIX) or b.endswith(SKIP_SUFFIX):
        return False
    return True


def add_tree(zf, base, rel_root, log):
    """把一个目录树加进包（跳过垃圾）。"""
    n = 0
    for d, dns, fs in os.walk(os.path.join(base, rel_root)):
        dns[:] = [x for x in dns
                  if x not in SKIP_NAMES and not x.startswith(SKIP_PREFIX)]
        for f in fs:
            p = os.path.join(d, f)
            if not want(p, f):
                continue
            zf.write(p, os.path.relpath(p, base).replace("\\", "/"))
            n += 1
            if len(log) < 8:
                log.append(os.path.relpath(p, base).replace("\\", "/"))
    return n


def pack_workbench(out_dir):
    ts = time.strftime("%Y%m%d_%H%M")
    dst = os.path.join(out_dir, "voicestudio-workbench-%s.zip" % ts)
    log = []
    n = 0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in WB_TOP_FILES:
            p = os.path.join(ROOT, f)
            if os.path.isfile(p):
                zf.write(p, f)
                n += 1
                if len(log) < 8:
                    log.append(f)
        for b in os.listdir(ROOT):
            p = os.path.join(ROOT, b)
            if os.path.isfile(p) and any(b.startswith(pre) and b.endswith(suf)
                                         for pre, suf in WB_TOP_GLOB):
                zf.write(p, b)
                n += 1
        for dd in WB_TOP_DIRS:
            p = os.path.join(ROOT, dd)
            if os.path.isdir(p):
                n += add_tree(zf, ROOT, dd, log)
    return dst, n, log


def pack_roles(out_dir, exclude):
    ts = time.strftime("%Y%m%d_%H%M")
    dst = os.path.join(out_dir, "voicestudio-roles-%s.zip" % ts)
    src = os.path.join(ROOT, "data", "roles")
    if not os.path.isdir(src):
        return None, 0, []
    n = 0
    log = []
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for rid in sorted(os.listdir(src)):
            d = os.path.join(src, rid)
            if not os.path.isdir(d) or rid in exclude:
                continue
            for dd, dns, fs in os.walk(d):
                dns[:] = [x for x in dns
                          if x not in SKIP_NAMES and not x.startswith(SKIP_PREFIX)]
                for f in fs:
                    p = os.path.join(dd, f)
                    if not want(p, f):
                        continue
                    zf.write(p, "roles/%s" % os.path.relpath(p, src).replace("\\", "/"))
                    n += 1
                    if len(log) < 8:
                        log.append("roles/%s" % os.path.relpath(p, src).replace("\\", "/"))
        # 过滤掉大帝条目的 roles.json
        rj = os.path.join(ROOT, "data", "roles.json")
        if os.path.isfile(rj):
            import json
            try:
                with open(rj, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                before = len(data.get("roles", []))
                data["roles"] = [r for r in data.get("roles", [])
                                 if r.get("id") not in exclude]
                zf.writestr("roles.json",
                            json.dumps(data, ensure_ascii=False, indent=2))
                n += 1
                log.append("roles.json（%d → %d 条，已剔除大帝）"
                           % (before, len(data["roles"])))
            except Exception as e:
                log.append("⚠ roles.json 处理失败：%s" % e)
    return dst, n, log


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    cmd = args[0]
    out_dir = DEFAULT_OUT
    exclude = {KEEP_OUT}
    for a in args[1:]:
        if a.startswith("--out="):
            out_dir = a.split("=", 1)[1]
        elif a.startswith("--exclude="):
            exclude = set(x for x in a.split("=", 1)[1].split(",") if x)
    os.makedirs(out_dir, exist_ok=True)

    if cmd in ("workbench", "all"):
        dst, n, log = pack_workbench(out_dir)
        print("工作台包：%s" % dst)
        print("  条目 %d，大小 %.1f MB" % (n, os.path.getsize(dst) / 1048576.0))
        for x in log:
            print("    %s" % x)
    if cmd in ("roles", "all"):
        dst, n, log = pack_roles(out_dir, exclude)
        if dst is None:
            print("⚠ 没找到 data/roles，跳过角色包")
        else:
            print("角色包：%s" % dst)
            print("  条目 %d，大小 %.1f MB" % (n, os.path.getsize(dst) / 1048576.0))
            print("  已排除：%s" % ", ".join(sorted(exclude)))
            for x in log:
                print("    %s" % x)
    return 0


if __name__ == "__main__":
    sys.exit(main())
