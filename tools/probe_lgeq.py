# -*- coding: utf-8 -*-
"""局部（时变）高频去过冲 —— 只削超标的窗口，其余原样通过。

为什么需要它（2026-10-24 第四轮实测）：
  整首 hf_ratio 只比原唱高 1.29 倍（看着"正常"），但**逐窗口**看：
    ≥2 倍 41.3%、≥3 倍 31.3%、≥5 倍 23.7%、p90 = 16 倍、p99 = 190 倍
  最狠的一处（114.11s）原唱 0.00009、成品 0.08858 —— **×950**。
  ⇒ 整首平均把局部过冲稀释掉了 ⇒ 系统判"没问题"，人耳听到"破"。
  ⇒ 而按整首统一去处理，对那 59% 本来就正常的窗口是**过度处理** ⇒ 全曲沙哑
    （这正是老板这次的反馈：46 秒破 + 全过程沙哑）。

做法：分频（零相位，完美重建）→ 对高频段乘一个**随时间的增益** g(t) → 加回。
  g(t) 由「该窗口 hf_ratio 相对原唱的倍数」决定，超标才衰减，平滑避免抽水。

用法： probe_lgeq.py <原唱> <成品> <输出目录> [目标倍数] [最大衰减dB]
"""
import io
import json
import os
import sys

import numpy as np
import soundfile as sf
from scipy.signal import butter, filtfilt

WIN = 0.050          # 分析窗
HOP = 0.0125         # 分析跳距（增益曲线要够密，不然会有台阶）
ATTACK = 0.030       # 增益下降（开始削）的时间常数
RELEASE = 0.120      # 增益回升（放开）的时间常数
FC = 4000.0          # 分频点：只动 4k 以上
LO_B, HI_B = 4000.0, 8000.0


def read(p):
    y, sr = sf.read(p, dtype="float64")
    if y.ndim > 1:
        y = y.mean(1)
    return np.asarray(y, dtype=np.float64), int(sr)


def hf_ratio_track(y, sr, win=WIN, hop=HOP):
    """逐窗 hf_ratio（4-8k / 80-8k）。"""
    W = int(win * sr)
    H = int(hop * sr)
    ff = np.fft.rfftfreq(W, 1.0 / sr)
    m_hi = (ff >= LO_B) & (ff < HI_B)
    m_tot = (ff > 80.0) & (ff < 8000.0)
    ts, hrs, rms = [], [], []
    for i in range(0, max(1, len(y) - W), H):
        seg = y[i:i + W]
        sp = np.abs(np.fft.rfft(seg * np.hanning(W))) ** 2
        tot = float(sp[m_tot].sum())
        ts.append(i / float(sr))
        rms.append(float(np.sqrt((seg ** 2).mean())))
        hrs.append((float(sp[m_hi].sum()) / tot) if tot > 0 else 0.0)
    return np.asarray(ts), np.asarray(hrs), np.asarray(rms)


def smooth_env(g, sr, hop_s, attack, release):
    """一阶 IIR，下降快（attack）上升慢（release）—— 跟压缩器一个道理。"""
    a_a = np.exp(-hop_s / max(attack, 1e-6))
    a_r = np.exp(-hop_s / max(release, 1e-6))
    out = np.empty_like(g)
    v = g[0]
    for i, x in enumerate(g):
        a = a_a if x < v else a_r
        v = a * v + (1.0 - a) * x
        out[i] = v
    return out


def dynamic_shelf(y, sr, ref, target=2.0, max_atten=12.0, floor=1e-5,
                  band=(4000.0, 8000.0)):
    """返回 (处理后, 增益曲线 dB, 时刻)。

    ★ 用 STFT 逐 bin 施加增益，不用 butter 分频 —— 实测过 butter 8 阶：
      标称削 12dB 实际只掉 5.6dB，因为过渡带太宽、4k 附近的能量一半还留在低通里。
      STFT 域可以精确只动 [lo,hi) 这些 bin。
    """
    import librosa
    ts, hr_c, rms_c = hf_ratio_track(y, sr)
    _, hr_r, rms_r = hf_ratio_track(ref, sr)
    m = min(len(ts), len(hr_r))
    hr_c, hr_r = hr_c[:m], hr_r[:m]
    rms_c, rms_r = rms_c[:m], rms_r[:m]
    ts = ts[:m]

    # 静音/弱声窗口不削（那里的 hf_ratio 没有意义）
    thr_c = np.percentile(rms_c, 40)
    thr_r = np.percentile(rms_r, 40)
    voiced = (rms_c >= thr_c) & (rms_r >= thr_r)

    rr = np.maximum(hr_r, floor)
    ratio = np.where(voiced, hr_c / rr, 0.0)
    over = np.where(ratio > target, 10.0 * np.log10(ratio / target), 0.0)
    need = np.clip(over, 0.0, max_atten)
    need = smooth_env(need, sr, HOP, ATTACK, RELEASE)

    n_fft, hop = 2048, 512
    S = librosa.stft(y, n_fft=n_fft, hop_length=hop)
    fr = np.fft.rfftfreq(n_fft, 1.0 / sr)
    m_band = (fr >= band[0]) & (fr < band[1])
    ft = (np.arange(S.shape[1]) * hop) / float(sr)     # 每帧中心时间
    g_db_f = np.interp(ft, ts, need, left=need[0], right=need[-1])
    g = 10.0 ** (-g_db_f / 20.0)
    S[m_band, :] *= g[None, :]
    out = librosa.istft(S, hop_length=hop, length=len(y))
    return out, need, ts


def main(argv):
    ref_p, cand_p, outdir = argv[1], argv[2], argv[3]
    target = float(argv[4]) if len(argv) > 4 else 2.0
    max_atten = float(argv[5]) if len(argv) > 5 else 12.0
    os.makedirs(outdir, exist_ok=True)
    yr, srr = read(ref_p)
    yc, src = read(cand_p)
    sr = srr
    n = min(len(yr), len(yc))
    yr, yc = yr[:n], yc[:n]

    out, g_db, ts = dynamic_shelf(yc, sr, yr, target, max_atten)
    pk0 = float(np.abs(yc).max()) or 1.0
    out = out / (float(np.abs(out).max()) or 1.0) * pk0
    p = os.path.join(outdir, "lgeq_t%.1f_a%.0f.wav" % (target, max_atten))
    sf.write(p, out, sr)

    # 效果
    _, hr_o, rms_o = hf_ratio_track(out, sr)
    _, hr_r, rms_r = hf_ratio_track(yr, sr)
    m = min(len(hr_o), len(hr_r))
    thr_o = np.percentile(rms_o, 40)
    thr_r = np.percentile(rms_r, 40)
    v = (rms_o[:m] >= thr_o) & (rms_r[:m] >= thr_r) & (hr_r[:m] > 0)
    ratio_o = hr_o[:m][v] / hr_r[:m][v]
    _, hr_c0, _ = hf_ratio_track(yc, sr)
    ratio_c = hr_c0[:m][v] / hr_r[:m][v]

    res = {"out": p, "target": target, "max_atten": max_atten,
           "before_p50": float(np.percentile(ratio_c, 50)),
           "before_p90": float(np.percentile(ratio_c, 90)),
           "before_p99": float(np.percentile(ratio_c, 99)),
           "after_p50": float(np.percentile(ratio_o, 50)),
           "after_p90": float(np.percentile(ratio_o, 90)),
           "after_p99": float(np.percentile(ratio_o, 99)),
           "over5_before": float((ratio_c >= 5).mean() * 100),
           "over5_after": float((ratio_o >= 5).mean() * 100),
           "gain_db_mean": float(g_db.mean()),
           "gain_db_max": float(g_db.max()),
           "gain_active_pct": float((g_db > 0.5).mean() * 100)}
    sys.stdout.write(json.dumps(res, ensure_ascii=False, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
