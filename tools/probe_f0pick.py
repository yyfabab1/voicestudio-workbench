# -*- coding: utf-8 -*-
"""验证「进程内 f0 打分」这把尺子：**复现** qc_cli 的排序，能不能当校正闸门。

背景
----
tools/probe_segab.py 已经量出来：第 8 段挪 15.06 ms 之后吻合度 94.3% → 89.5%
（**掉 4.8 点**），而 align_phase 的闭环自验证判它"残余 0.00 ms、挪对了"。
⇒ 闭环量的是「有没有挪到我预测的位置」，不是「挪了之后质量涨没涨」。

要加一道"挪了到底涨没涨"的闸门，最直白的是调 qc_cli —— 但它是**一个子进程跑
一次**，模型每次重载，实测 16 次 ≈ 2.5 分钟（每首歌 +60% 时间），付不起。

而 RMVPE 在 rvc_infer 那个进程里**本来就已经能 import**（`from infer.rmvpe import
RMVPE`），模型只加载一次 ⇒ 每段多提两三条 f0 而已，成本接近零。

这一支探针就是先验证：**简化版的 f0 打分能不能复现 qc_cli 的排序**。
排序都对得上，才可以拿它当闸门；对不上就是另一把尺子，不能用（铁律 15）。

判据（逐段）：
  简化版判"校正后更好" / "不校正更好"，必须与 qc_cli 的 A/B 结论一致。

用法：
    <rvc py> -s tools\\probe_f0pick.py
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

# qc_cli 的 A/B 结论（tools/probe_segab.py 实测）：段号 -> (校正后%, 不校正%)
QC_AB = {1: (96.8, 89.2), 2: (89.3, 88.7), 3: (90.0, 90.7), 4: (92.7, 91.8),
         5: (88.5, 89.3), 7: (96.7, 92.9), 8: (89.5, 94.3)}

# ★ 必须在 import 任何 RVC 模块之前切过去（铁律 25）
os.chdir(RVC_ROOT)
sys.path.insert(0, RVC_ROOT)
os.environ.setdefault("rmvpe_root", os.path.join(RVC_ROOT, "assets", "rmvpe"))
import torch                                                  # noqa: E402
from infer.rmvpe import RMVPE                                 # noqa: E402

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
MODEL = RMVPE(os.path.join(os.environ["rmvpe_root"], "rmvpe.pt"),
              is_half=(DEVICE != "cpu"), device=DEVICE)
print("设备 %s ｜ RMVPE 已加载（只加载一次，这就是进程内复用的意义）" % DEVICE)


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


def f0_of(y):
    return np.asarray(MODEL.infer_from_audio(np.asarray(y, dtype=np.float32), thred=0.03),
                      dtype=np.float32).reshape(-1)


def score(a, b):
    """简化版音准打分，口径照抄 qc_cli：双方有声的帧，去整体平移后比音分。"""
    m = int(min(a.size, b.size))
    xs, xd = a[:m], b[:m]
    both = (xs > 1.0) & (xd > 1.0)
    if int(both.sum()) < 20:
        return None
    lx = 1200.0 * np.log2(xs[both])
    ly = 1200.0 * np.log2(xd[both])
    cents = ly - lx
    med = float(np.median(cents))
    dev = cents - med
    ab = np.abs(dev)
    return {
        "tight": float(np.mean(ab < 50.0)),
        "loose": float(np.mean(ab < 150.0)),
        "iqr": float(np.percentile(dev, 75) - np.percentile(dev, 25)),
        "frames": int(both.sum()),
    }


def main():
    src = sorted(glob.glob(os.path.join(WORK, "in0*.wav")))
    seg = sorted(glob.glob(os.path.join(WORK, "sg0*.wav")))
    n = min(len(src), len(seg))
    lags = read_lags(n)

    print("\n段  lag    qc:校正/不校正      简化版:校正/不校正(±50)      (±150)      IQR        排序一致?")
    ok_all = True
    for segno in sorted(QC_AB):
        i = segno - 1
        if not (0 <= i < n) or lags[i] <= 0:
            continue
        s, _ = sf.read(src[i], dtype="float32")
        if s.ndim > 1:
            s = s.mean(1)
        y, _ = sf.read(seg[i], dtype="float32")
        if y.ndim > 1:
            y = y.mean(1)
        lag = lags[i]
        fs = f0_of(s)
        fa = f0_of(y)                                   # 校正后
        fb = f0_of(np.roll(y.astype("float64"),
                           int(round(lag * SR / 1000.0))))   # 退回（不校正）
        sa, sb = score(fs, fa), score(fs, fb)
        if not (sa and sb):
            print(" %d  提不出基频" % segno)
            continue
        qc_a, qc_b = QC_AB[segno]
        same = ((sa["tight"] >= sb["tight"]) == (qc_a >= qc_b))
        ok_all = ok_all and same
        print(" %d  %5.2f  %5.1f / %5.1f        %5.1f / %5.1f        %5.1f / %5.1f   "
              "%5.1f / %5.1f   %s"
              % (segno, lag, qc_a, qc_b, sa["tight"] * 100, sb["tight"] * 100,
                 sa["loose"] * 100, sb["loose"] * 100, sa["iqr"], sb["iqr"],
                 "一致" if same else "★ 不一致"))

    print("\n结论：%s" % ("简化版 f0 打分能复现 qc_cli 的排序，可以当闸门"
                         if ok_all else "★ 简化版与 qc_cli 排序不一致，不能当闸门"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
