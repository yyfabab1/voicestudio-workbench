# -*- coding: utf-8 -*-
"""扫「位移 → 质量」曲线：某一段到底挪多少毫秒才最好。

为什么需要它
------------
`tools/probe_segab.py` 已经证明：**同一段音频，挪 15.06 ms 反而掉 4.8 点**
（89.5 vs 不挪的 94.3），而 align_phase 的闭环自验证却判它"残余很小、挪对了"。

⇒ 闭环量的是「有没有挪到我预测的那个位置」，**不是**「挪了之后质量涨没涨」。
这两件事在测错的时候会分道扬镳。要分清是哪一种，只能把位移扫一遍看曲线：

  · 曲线峰值就在 15 ms  ⇒ 度量没错，是质量口径和相位口径本身就冲突（那就认了）；
  · 曲线峰值在别处（比如 0 或 5 ms）⇒ **lag 测错了**，这才是能修的。

用法：
    <rvc py> -s tools\\probe_shift.py [段号...]
默认扫第 8 段（实测被挪坏那段）+ 第 1 段（挪对了的那段，当对照）。
"""

import glob
import os
import subprocess
import sys

import numpy as np
import soundfile as sf

RVC_ROOT = r"D:\RVC\Retrieval-based-Voice-Conversion-WebUI"
RVC_PY = os.path.join(RVC_ROOT, "runtime", "python.exe")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(HERE, "data", "_probe_realseg")
SR = 16000
SHIFTS = [0, 3, 5, 8, 10, 13, 15, 18, 20, 23, 25, 28, 30]


def main():
    want = [int(x) for x in sys.argv[1:]] or [8, 1]
    src = sorted(glob.glob(os.path.join(WORK, "in0*.wav")))
    seg = sorted(glob.glob(os.path.join(WORK, "sg0*.wav")))
    n = min(len(src), len(seg))
    import tempfile
    tmp = tempfile.mkdtemp(prefix="shift_")

    def qc(pin, path):
        j = os.path.splitext(path)[0] + ".json"
        r = subprocess.run([RVC_PY, "-s", os.path.join(HERE, "stages", "qc_cli.py"),
                            pin, path, j], cwd=RVC_ROOT, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        try:
            import json
            return json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:
            return {}

    for segno in want:
        i = segno - 1
        if not (0 <= i < n):
            print("没有第 %d 段" % segno)
            continue
        pin = src[i]
        y, _ = sf.read(seg[i], dtype="float32")
        if y.ndim > 1:
            y = y.mean(1)
        print("\n第 %d 段：位移 → 吻合%% / IQR" % segno)
        best, bestms, rows = -1.0, 0, []
        for ms in SHIFTS:
            p = os.path.join(tmp, "s%d_%02d.wav" % (segno, ms))
            # ★ 符号：成品比源晚 lag ⇒ 校正是把它**提前** lag。
            #   这里扫的是「提前 ms」，ms=0 就是不挪。
            sf.write(p, np.roll(y, -int(round(ms * SR / 1000.0))), SR)
            q = qc(pin, p)
            m = q.get("match_ratio")
            if not m:
                print("  %5.1f ms  打分失败" % ms)
                continue
            rows.append((ms, m * 100, q.get("dev_iqr_cents", 0)))
            print("  %5.1f ms   %6.1f%%   IQR %.1f" % (ms, m * 100, q.get("dev_iqr_cents", 0)))
            if m > best:
                best, bestms = m, ms
        if rows:
            base = rows[0][1]
            print("  ⇒ 峰值在 **%d ms**（%.1f%%），不挪是 %.1f%%，差 %+.1f 点"
                  % (bestms, best, base, best - base))


if __name__ == "__main__":
    main()
