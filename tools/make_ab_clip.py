# -*- coding: utf-8 -*-
r"""把两版干声拼成一条 A/B 试听轨（响度对齐 + 中间留静音）。

为什么要有它：高频补偿这类改动，**看频谱数字不如耳朵听一句**。
但两版必须**同一份 RVC 输出**（只差补偿这一步）才叫受控 A/B ——
各跑一次再比，比出来的是推理噪声不是改动效果。

用法（RVC 的 python 下跑，只有它有 soundfile）：
    runtime\python.exe -s tools\make_ab_clip.py <A.wav> <B.wav> <out.wav> [起点秒] [每段秒]
"""
import os
import sys

import numpy as np
import soundfile as sf

GAP = 0.8          # 段间静音，耳朵需要一下"重置"


def load(path, t0, dur):
    y, sr = sf.read(path, dtype="float64")
    if y.ndim > 1:
        y = y.mean(1)
    a, b = int(t0 * sr), int((t0 + dur) * sr)
    return y[a:b], sr


def rms_align(a, b):
    """按 RMS 对齐 —— 不对齐的话，响的那一边天然"听起来更好"，
    这是 A/B 里最常见的假结论来源。"""
    ra = float(np.sqrt((a ** 2).mean()))
    rb = float(np.sqrt((b ** 2).mean()))
    if rb > 1e-9 and ra > 1e-9:
        b = b * (ra / rb)
    return a, b


def main():
    a_p, b_p, out = sys.argv[1], sys.argv[2], sys.argv[3]
    t0 = float(sys.argv[4]) if len(sys.argv) > 4 else 40.0
    dur = float(sys.argv[5]) if len(sys.argv) > 5 else 30.0
    A, sr = load(a_p, t0, dur)
    B, _ = load(b_p, t0, dur)
    A, B = rms_align(A, B)
    gap = np.zeros(int(GAP * sr))
    y = np.concatenate([A, gap, B])
    # 峰值保护：两段叠一起不该削幅
    pk = float(np.abs(y).max())
    if pk > 0.95:
        y = y * (0.95 / pk)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    sf.write(out, y.astype("float32"), sr)
    print("AB %s  %.1fs+%1.fs @%.0fs  sr=%d  peak=%.3f"
          % (os.path.basename(out), dur, dur, t0, sr, float(np.abs(y).max())))


if __name__ == "__main__":
    main()
