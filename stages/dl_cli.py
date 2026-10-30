# -*- coding: utf-8 -*-
r"""从网上下一个文件，带进度，落盘是原子的。

为什么单独做一个脚本而不是在 studio.py 里 urllib：
  下载是分钟级的活，必须**像其它步骤一样**能被看见（进度进日志）、能被暂停
  （走 run() 的进程树挂起）。在服务进程里直接 urllib 会占着请求线程，
  进度也进不了任务日志 —— 用户又会看到"卡住了"。

用法：
    python.exe -u <本脚本> <URL> <目标路径>

退出码：0 成功；2 参数错；3 下载失败。
"""
import os
import sys
import time
import urllib.request

if len(sys.argv) < 3:
    print("用法：dl_cli.py <URL> <目标路径>")
    sys.exit(2)

URL = sys.argv[1]
DST = sys.argv[2]
TMP = DST + ".part"

# 有些站会看 UA，不给就 403
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

os.makedirs(os.path.dirname(os.path.abspath(DST)), exist_ok=True)

t0 = time.time()
last = [0.0]


def _rep(got, total):
    """进度不能每字节都打 —— 一分钟能打几十万行，日志会先炸。"""
    now = time.time()
    if now - last[0] < 1.0 and (total <= 0 or got < total):
        return
    last[0] = now
    if total > 0:
        mb, pct, el = got / 1048576.0, 100.0 * got / total, max(now - t0, 0.01)
        sp = got / el / 1048576.0
        print("DL %.1f%% %.1f/%.1f MB %.2f MB/s"
              % (pct, mb, total / 1048576.0, sp), flush=True)
    else:
        print("DL %.1f MB" % (got / 1048576.0), flush=True)


try:
    req = urllib.request.Request(URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        total = int(r.headers.get("Content-Length") or 0)
        print("DL_START %s -> %s（%.1f MB）" % (URL, DST, total / 1048576.0), flush=True)
        with open(TMP, "wb") as f:
            got = 0
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                _rep(got, total)
    # 只下到一半不能算数 —— 半截 .pth 会让训练加载时报看不懂的错
    if total and os.path.getsize(TMP) != total:
        print("DL_FAIL 大小不符：下了 %d / 应到 %d" % (os.path.getsize(TMP), total))
        sys.exit(3)
    os.replace(TMP, DST)
    print("DL_DONE %.1f MB，用时 %.1fs" % (os.path.getsize(DST) / 1048576.0,
                                           time.time() - t0), flush=True)
except Exception as e:
    print("DL_FAIL %s: %s" % (type(e).__name__, str(e)[:200]))
    try:
        if os.path.exists(TMP):
            os.remove(TMP)
    except Exception:
        pass
    sys.exit(3)
