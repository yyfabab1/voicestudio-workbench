# -*- coding: utf-8 -*-
"""三层进程 cwd 探针：Popen 起的「孙进程」到底继承了谁的当前目录。

起因：外层分段里，父进程起的**子进程**在我从 VoiceStudio 目录跑探针时，
拼出来的 rmvpe 路径变成了 `D:\\AHC\\VoiceStudio\\assets\\rmvpe\\rmvpe.pt`
（应当是 RVC 根目录下那份）。而父进程明明是带 `cwd=RVC_ROOT` 起的。
到底是「子进程没继承 cwd」还是「继承的是祖父的 cwd」，用眼睛看代码看不出来，
让三层进程各报一次 cwd 就清楚了。

用法（在什么目录下跑都行）：
    <py> -s tools\\probe_cwd.py
"""

import os
import subprocess
import sys

RVC_ROOT = r"D:\RVC\Retrieval-based-Voice-Conversion-WebUI"

CHILD = r'''
import os, subprocess, sys
print("  子进程 cwd = %s" % os.getcwd())
p = subprocess.Popen([sys.executable, "-s", sys.argv[1] + "\\gc.py"],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                     text=True, errors="replace")
out, _ = p.communicate()
print("  孙进程 cwd = %s" % out.strip())
'''

GC = r'''
import os
print(os.getcwd())
'''


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "ch.py"), "w") as f:
        f.write(CHILD)
    with open(os.path.join(here, "gc.py"), "w") as f:
        f.write(GC)
    print("本进程 cwd = %s" % os.getcwd())
    print("以 cwd=%s 起子进程：" % RVC_ROOT)
    p = subprocess.Popen([sys.executable, "-s", os.path.join(here, "ch.py"), here],
                         cwd=RVC_ROOT, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, errors="replace")
    out, _ = p.communicate()
    print(out.rstrip())
    for n in ("ch.py", "gc.py"):
        try:
            os.remove(os.path.join(here, n))
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
