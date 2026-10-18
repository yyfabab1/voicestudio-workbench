# -*- coding: utf-8 -*-
"""一次性脚本：把待公开文件里硬编码的用户目录换成运行时解析的写法。

公开仓库前跑一次。本机行为必须**完全等价**（%USERPROFILE% 就是 C:\\Users\\yyfabab），
别人 clone 下去则自动适配。跑完即可删除。

每条替换前都有断言：出现次数不对、或替换后还残留用户名 ⇒ 当场 exit，不静默放过。
"""
import io
import os
import sys

ROOT = r"D:\AHC\VoiceStudio"
USER = "yyfabab"

# (相对路径, 原文, 新文, 期望出现次数)
JOBS = [
    ("verify_jobctl.py",
     'r"C:\\Users\\' + USER + '\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe"',
     'os.path.expandvars(r"%USERPROFILE%\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe")',
     1),
    ("verify_ncm.py",
     'r"C:\\Users\\' + USER + '\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe"',
     'os.path.expandvars(r"%USERPROFILE%\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe")',
     1),
    ("verify_seedvc.py",
     'r"C:\\Users\\' + USER + '\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe"',
     'os.path.expandvars(r"%USERPROFILE%\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe")',
     1),
    ("verify_steps.py",
     'r"C:\\Users\\' + USER + '\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe"',
     'os.path.expandvars(r"%USERPROFILE%\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe")',
     1),
    ("verify_storage.py",
     'r"C:\\Users\\' + USER + '\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe"',
     'os.path.expandvars(r"%USERPROFILE%\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe")',
     1),
    ("verify_train_rounds_ui.py",
     'r"C:\\Users\\' + USER + '\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe"',
     'os.path.expandvars(r"%USERPROFILE%\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe")',
     1),
    ("自动解密网易云ncm.bat",
     'set "PY=C:\\Users\\' + USER + '\\.workbuddy',
     'set "PY=%USERPROFILE%\\.workbuddy',
     1),
    ("_run_regression.sh",
     'PY_MANAGED="C:/Users/' + USER + '/.workbuddy',
     'PY_MANAGED="${REG_PY_MANAGED:-${USERPROFILE:-$HOME}/.workbuddy',
     1),
    ("_run_regression.sh",
     'PY_VENV="C:/Users/' + USER + '/.workbuddy',
     'PY_VENV="${REG_PY_VENV:-${USERPROFILE:-$HOME}/.workbuddy',
     1),
]


def main():
    changed = {}
    for rel, old, new, want in JOBS:
        p = os.path.join(ROOT, rel)
        if not os.path.isfile(p):
            sys.stderr.write("✗ 文件不存在: %s\n" % rel)
            return 3
        with io.open(p, encoding="utf-8") as fh:
            txt = fh.read()
        n = txt.count(old)
        if n != want:
            sys.stderr.write("✗ %s 中 %r 出现 %d 次（期望 %d），中止\n"
                             % (rel, old[:40], n, want))
            return 4
        changed[rel] = changed.get(rel, 0) + n
        with io.open(p, "w", encoding="utf-8") as fh:
            fh.write(txt.replace(old, new))

    for rel, n in sorted(changed.items()):
        print("  ✔ %-28s 替换 %d 处" % (rel, n))
    print("共改 %d 个文件" % len(changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
