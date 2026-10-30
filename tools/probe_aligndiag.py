# -*- coding: utf-8 -*-
"""逐段把「相位测量」的细节打出来：三个频带各读了多少，闭环复核读了多少。

为什么需要它
------------
端到端日志只在**拒绝**的时候才打细节，采纳的时候只打一行「成品比源晚 X ms」。
于是「某段被挪了 15 ms 却掉 4.8 分」这种事，日志里看不出来是三频带一致认了它、
还是只有一个频带在说话。这一支探针就是把采纳的那几段也摊开看。

关键：把成品**退回**校正前的样子再测（成品切片是「已经挪过的」，
直接测它当然残余≈0，什么都看不出来）。

用法：
    <rvc py> -s tools\\probe_aligndiag.py [段号...]
"""

import glob
import os
import re
import sys

import numpy as np
import soundfile as sf

RVC_ROOT = r"D:\RVC\Retrieval-based-Voice-Conversion-WebUI"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(HERE, "data", "_probe_realseg")
LOG = os.path.join(HERE, "data", "_e2e_new.log")
SR = 16000
BANDS = (900.0, 1200.0, 1500.0)

# ★ 必须在 import rvc_infer 之前切过去（铁律 25：它顶层按 os.getcwd() 写环境变量）
os.chdir(RVC_ROOT)
sys.path.insert(0, os.path.join(HERE, "stages"))
import rvc_infer                                              # noqa: E402


def read_lags(n_seg):
    lags = [0.0] * n_seg
    if os.path.exists(LOG):
        pend = 0.0
        for ln in open(LOG, encoding="utf-8", errors="replace"):
            m = re.search(r"\[align\] 成品比源晚 ([\d.]+) ms", ln)
            if m:
                pend = float(m.group(1))
                continue
            m2 = re.search(r"\[段 (\d+)/\d+\]", ln)
            if m2:
                i = int(m2.group(1)) - 1
                if i < n_seg:
                    lags[i] = pend
                pend = 0.0
    return lags


def main():
    src = sorted(glob.glob(os.path.join(WORK, "in0*.wav")))
    seg = sorted(glob.glob(os.path.join(WORK, "sg0*.wav")))
    n = min(len(src), len(seg))
    lags = read_lags(n)
    want = [int(x) for x in sys.argv[1:]] or list(range(1, n + 1))

    print("段  lag(ms)   900Hz   1200Hz  1500Hz  峰高      闭环残余")
    for segno in want:
        i = segno - 1
        if not (0 <= i < n):
            continue
        s, _ = sf.read(src[i], dtype="float32")
        if s.ndim > 1:
            s = s.mean(1)
        y, _ = sf.read(seg[i], dtype="float32")
        if y.ndim > 1:
            y = y.mean(1)
        lag = lags[i]
        # 退回校正前：校正 = 提前 lag ⇒ 退回去 = 延后 lag
        raw = np.roll(y.astype("float64"),
                      int(round(lag * SR / 1000.0))) if lag > 0 else y.astype("float64")
        m = min(len(s), len(raw))
        cells, peaks = [], []
        for c in BANDS:
            l, p = rvc_infer._env_lag(s[:m].astype("float64"),
                                      raw[:m].astype("float64"), SR, c)
            cells.append("%6.2f" % l)
            peaks.append(p)
        out, got = rvc_infer.align_phase(src[i], raw.astype("float32"), SR, 40.0)
        again = got - lag      # 残余 = 这次读出来的 − 当初挪过去的
        flag = "" if abs(again) < 1.2 else "  ★"
        print(" %d  %6.2f  %s   %.3f   %+6.2f%s"
              % (segno, lag, "  ".join(cells), max(peaks), again, flag))


if __name__ == "__main__":
    main()
