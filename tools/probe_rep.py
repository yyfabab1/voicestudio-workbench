# -*- coding: utf-8 -*-
"""重复素材一致性探针：长音频里同一份素材的每一份产物，互相有多像。

长歌压力测试（probe_longrun）跑完 20 段后，第 20 段的指标明显是个离群值
（吻合 86.3% / IQR 17.6，其余 19 段是 92.7~93.7% / 11.0~11.6）。素材是
**同一段重复 20 次**，所以每一份的产物理论上应该彼此高度相似。

这里不去猜它是"最后一段真出问题"还是"我分析时的边界误差"，直接量：
把每一份产物两两比对（幅度谱相关，对时移不敏感），看第 20 份离群多少。

用法：
    <py> -s tools\\probe_rep.py <目录>     # 目录里有 ou00.wav ... ou19.wav
"""

import glob
import os
import re
import sys

import numpy as np
import soundfile as sf


def spec_vec(path, sr_t=16000, t0=5.0, t1=15.0):
    y, sr = sf.read(path, always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)
    y = np.asarray(y, dtype="float64")
    a = int(t0 * sr)
    b = int(t1 * sr)
    seg = y[a:b]
    if len(seg) < sr:
        seg = y[:sr]
    if sr != sr_t:
        k = max(1, sr // sr_t)
        seg = seg[::k]
    seg = seg - seg.mean()
    n = 1 << int(np.ceil(np.log2(max(2, len(seg)))))
    S = np.abs(np.fft.rfft(seg, n=n))
    S = S / (np.linalg.norm(S) + 1e-12)
    return S


def main():
    d = sys.argv[1] if len(sys.argv) > 1 else os.path.join("data", "_probe_longrun")
    # ★ 只收 ouNN.wav；`out.wav`（整条拼接产物）没有数字，混进来会让排序炸掉
    #   第二个参数可换成 "in"，用来查**输入**侧各份是否真的一样
    #   （产物有差异时，先排除"喂进去的东西本来就不一样"）。
    pre = sys.argv[2] if len(sys.argv) > 2 else "ou"
    files = sorted(glob.glob(os.path.join(d, pre + "[0-9][0-9].wav")),
                   key=lambda p: int(re.findall(r"\d+", os.path.basename(p))[0]))
    if len(files) < 4:
        print("段文件不够：%d" % len(files))
        return 2
    V = [spec_vec(p) for p in files]
    m = min(len(v) for v in V)
    V = [v[:m] for v in V]
    base = V[0]
    sims = [float(np.dot(base, v)) for v in V]
    print("各份产物与第 1 份的幅度谱相关（1.0 = 完全一致）：")
    for i, s in enumerate(sims):
        mark = ""
        if i > 0:
            med = float(np.median(sims[1:]))
            if s < med - 0.08:
                mark = "   ★ 明显离群"
        print("  第 %2d 份：%.4f%s" % (i + 1, s, mark))
    arr = np.array(sims[1:])
    print("\n除第 1 份外：中位 %.4f，最小 %.4f（第 %d 份），最大 %.4f"
          % (np.median(arr), arr.min(), int(np.argmin(arr)) + 2, arr.max()))
    # 相邻两份之间的相似：如果只有最后一份离群，这里会看到最后一项突然掉下去
    adj = [float(np.dot(V[i], V[i + 1])) for i in range(len(V) - 1)]
    print("相邻两份的相关：%s" % " ".join("%.3f" % x for x in adj))
    return 0


if __name__ == "__main__":
    sys.exit(main())
