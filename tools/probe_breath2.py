# -*- coding: utf-8 -*-
"""「补气声」而不是「破坏周期」——能不能降 HNR 而不涨 jitter？

背景（2026-10-24 第四轮）：
  老板反馈「全过程沙哑 + 46 秒破音」。实测拆成两个独立的毛病：
    A 局部高频过冲（46 秒那类，逐窗可达原唱的 10~950 倍）→ 时变 EQ 能修，
      且 jitter 0.01043→0.01062 几乎不动（零副作用）。
    B 电音（HNR 19.0 vs 原唱 10.46）→ 目前只有「抖动」能压（19.0→13.15），
      但 jitter 0.01043→0.01639（+57%）⇒ **这就是老板说的沙哑**。

  ★ 物理上的关键区分：
      真人 HNR 低是因为**有气声（加性宽带噪声）**，谐波本身仍然相干；
      抖动是把**谐波自身打散**（乘性相位破坏）⇒ CPP 掉、jitter 涨 ⇒ 沙哑。
      前者是 breathy（柔），后者是 hoarse（糙）。
  ⇒ 所以要补的是**噪声**，不是破坏谐波。

做法：从原唱里用 HPSS 取出非谐波成分（≈ 真人的气声 + 瞬态，自带正确频谱
      形状和时变包络），按能量比例加到成品上。这跟上一轮被否掉的「掺白噪声」
      不同：白噪声是全带恒定的，沙沙响；这个是按真人气声整形 + 门控的。

用法： probe_breath2.py <原唱> <成品> <输出目录>
"""
import io
import json
import os
import sys

import numpy as np
import soundfile as sf

try:
    import librosa
except Exception:
    librosa = None


def read(p):
    y, sr = sf.read(p, dtype="float64")
    if y.ndim > 1:
        y = y.mean(1)
    return np.asarray(y, dtype=np.float64), int(sr)


def breath_of(y, sr, margin=2.0):
    """返回原唱的非谐波成分（气声 + 瞬态）。"""
    if librosa is None:
        return None
    n_fft, hop = 2048, 512
    D = librosa.stft(y, n_fft=n_fft, hop_length=hop)
    H, P = librosa.decompose.hpss(D, margin=margin)
    return librosa.istft(P, hop_length=hop, length=len(y))


def main(argv):
    ref_p, cand_p, outdir = argv[1], argv[2], argv[3]
    os.makedirs(outdir, exist_ok=True)
    yr, srr = read(ref_p)
    yc, src = read(cand_p)
    sr = srr
    n = min(len(yr), len(yc))
    yr, yc = yr[:n], yc[:n]

    br = breath_of(yr, sr)
    if br is None:
        sys.stdout.write("librosa 不可用\n")
        return 2
    br = br[:n]

    e_br = float(np.sqrt((br ** 2).mean()))
    e_c = float(np.sqrt((yc ** 2).mean()))
    sys.stdout.write("原唱非谐波成分 RMS=%.5f（成品 RMS=%.5f，占 %.1f%%）\n"
                     % (e_br, e_c, 100 * e_br / max(e_c, 1e-12)))

    outs = []
    # alpha = 相对原唱气声原始能量的倍数
    for alpha in (0.0, 0.5, 1.0, 1.5, 2.0, 3.0):
        y = yc + alpha * br
        pk = float(np.abs(y).max()) or 1.0
        if pk > 0.99:
            y = y / pk * 0.99
        p = os.path.join(outdir, "breath_a%.1f.wav" % alpha)
        sf.write(p, y, sr)
        outs.append(p)
    sys.stdout.write(json.dumps({"outs": outs}, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
