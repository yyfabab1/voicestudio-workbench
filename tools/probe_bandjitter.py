#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""扫「分频抖动」剂量：只抖高频，低频原样过去。

为什么要这个（2026-10-24）
--------------------------
实测这版 Stronger（pitch +0）被判电：**ΔHNR +8.88dB，但 4-8k 只有 ×1.29**。
   ⇒ "削 4-8k" 没东西可削（本来就不高），"全带抖动"要压 8.88dB 就得大剂量，
     中频/质心被护栏挡下 ⇒ 7 档全部 no_safe_dose，识别到了却修不动。
   
物理思路：HNR 是"谐波有多干净"，主要看**高次谐波是否还相干**。
  wow_flutter 的相位偏移 2π·k·f0·τ 正比于谐波次数 k ⇒ 天然先毁高次谐波。
  那把信号按频率切开、**只抖高频**，低频（基频 + 低次谐波 = 音高 + 共振峰 =
  音色/声纹）完全不动，HNR 该降的照样降，而"抹糊"的代价只落在高频。

扫什么：split_hz × jitter_ratio，量 ΔHNR / 4-8k / 2-4k / 质心 / jitter_local。

用法（cwd 任意，用 RVC 自带解释器）：
    python -s tools/probe_bandjitter.py <原唱.wav> <成品.wav> <outdir>
"""
import os
import sys

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "stages"))

import artifact_cli as ac  # noqa: E402

SPLITS = (1500.0, 2500.0, 4000.0)
JITS = (0.010, 0.016, 0.024)
EDGE = 600.0


def read(p):
    y, sr = sf.read(p, dtype="float64")
    if y.ndim > 1:
        y = y.mean(1)
    return np.asarray(y, np.float64), int(sr)


def write(p, y, sr):
    sf.write(p, np.clip(y, -1.0, 1.0), sr, subtype="PCM_16")


def split_band(y, sr, split_hz, n_fft=2048, hop=512):
    """返回 (低, 高)，互补增益，相加 == 原信号。"""
    import librosa

    def stft(x):
        return librosa.stft(x, n_fft=n_fft, hop_length=hop)

    S = stft(y)
    fr = np.fft.rfftfreq(n_fft, 1.0 / sr)
    lo = np.ones(len(fr))
    up = (fr > split_hz - EDGE) & (fr < split_hz)
    lo[up] = 0.5 * (1.0 + np.cos(np.pi * (fr[up] - (split_hz - EDGE)) / EDGE))
    lo[fr >= split_hz] = 0.0
    hi = 1.0 - lo

    def istft(Sx):
        return librosa.istft(Sx, hop_length=hop, length=len(y))

    return istft(S * lo[:, None]), istft(S * hi[:, None])


def _smooth_noise(n, sr, cutoff_hz, rng):
    win = max(1, int(round(float(sr) / max(1.0, cutoff_hz))))
    raw = rng.standard_normal(n + 2 * win)
    k = np.ones(win) / float(win)
    s = np.convolve(raw, k, mode="same")[:n]
    sd = float(np.std(s))
    return s / sd if sd > 1e-12 else np.zeros(n)


def wow(y, sr, jr, rng, f0_hz, cut=600.0):
    n = len(y)
    period = float(sr) / max(40.0, f0_hz)
    d = jr * period * _smooth_noise(n, sr, cut, rng)
    idx = np.arange(n, dtype=np.float64) + d
    i0 = np.clip(np.floor(idx).astype(np.int64), 0, n - 2)
    frac = idx - i0
    return y[i0] * (1.0 - frac) + y[i0 + 1] * frac


def band_db(y, sr, lo, hi):
    sp = np.abs(np.fft.rfft(y * np.hanning(len(y)))) ** 2
    fr = np.fft.rfftfreq(len(y), 1.0 / sr)
    m = (fr >= lo) & (fr < hi)
    return 10.0 * np.log10(float(sp[m].sum()) + 1e-20)


def centroid(y, sr):
    sp = np.abs(np.fft.rfft(y * np.hanning(len(y)))) ** 2
    fr = np.fft.rfftfreq(len(y), 1.0 / sr)
    s = float(sp.sum())
    return float((fr * sp).sum() / s) if s > 0 else 0.0


def main(ref, cand, outdir):
    os.makedirs(outdir, exist_ok=True)
    yr, sr = read(ref)
    yc, sr2 = read(cand)
    if sr != sr2:
        # ★ RVC 输出恒定 48k，原唱轨可能 44.1k —— 统一到成品那一边（不重采成品，
        #   否则量出来的 artifact 指标里混进重采样自己的损失）
        import librosa
        yr = librosa.resample(yr, orig_sr=sr, target_sr=sr2)
        sr = sr2
    n = min(len(yr), len(yc))
    yr, yc = yr[:n], yc[:n]
    m0 = ac.measure(ref, 0, 999)
    b0 = ac.measure(cand, 0, 999)
    if not (m0.get("ok") and b0.get("ok")):
        print("量不出来：%s / %s" % (m0.get("err"), b0.get("err")))
        return 2
    print("REF  HNR %.2f  4-8k占比 %.5f  jitter %.5f  质心 %.0fHz"
          % (m0["hnr_db"], m0["hf_ratio"], m0["jitter_local"], centroid(yr, sr)))
    print("成品 HNR %.2f  4-8k占比 %.5f  jitter %.5f  质心 %.0fHz"
          % (b0["hnr_db"], b0["hf_ratio"], b0["jitter_local"], centroid(yc, sr)))
    d0 = b0["hnr_db"] - m0["hnr_db"]
    rel0 = b0["hf_ratio"] / max(m0["hf_ratio"], 1e-12)
    print("     ΔHNR %+.2f dB   4-8k ×%.2f\n" % (d0, rel0))

    c_mid = band_db(yc, sr, 2000, 4000)
    c_cent = centroid(yc, sr)
    f0 = b0.get("f0_med_hz") or 200.0
    rows = []
    cases = [("base", 0.0, 0.0)]
    for sp_hz in SPLITS:
        for jr in JITS:
            cases.append(("s%d_j%.3f" % (int(sp_hz), jr), sp_hz, jr))
    # 对照：全带抖动（现有手段）
    cases.append(("full_j0.012", 0.0, 0.012))

    print("%-16s %8s %8s %9s %8s %8s %8s"
          % ("case", "ΔHNR", "4-8k×", "jitter", "2-4kΔ", "质心Δ%", "文件"))
    for name, sp_hz, jr in cases:
        rng = np.random.default_rng(1234)
        if sp_hz <= 0:
            y = yc if jr == 0 else wow(yc, sr, jr, rng, f0)
            if jr == 0:
                y = yc
        else:
            lo_b, hi_b = split_band(yc, sr, sp_hz)
            # 低频一点不动；只抖高频
            y = lo_b + (hi_b if jr == 0 else wow(hi_b, sr, jr, rng, f0))
        p = os.path.join(outdir, name + ".wav")
        write(p, y, sr)
        m = ac.measure(p, 0, 999)
        if not m.get("ok"):
            print("%-16s 量不出来 %s" % (name, m.get("err")))
            continue
        dh = m["hnr_db"] - m0["hnr_db"]
        rel = m["hf_ratio"] / max(m0["hf_ratio"], 1e-12)
        dmid = band_db(y, sr, 2000, 4000) - c_mid
        dcent = 100.0 * (centroid(y, sr) / max(c_cent, 1.0) - 1.0)
        rows.append((name, dh, rel, m["jitter_local"], dmid, dcent))
        print("%-16s %+8.2f %8.2f %9.5f %+8.2f %+8.1f  %s"
              % (name, dh, rel, m["jitter_local"], dmid, dcent, name + ".wav"))
    print("\n判据线：ΔHNR < 4.0 且 4-8k < 2.0 且 2-4k 变化 > -2dB 且 质心 > -10%")
    ok = [r for r in rows if r[1] < 4.0 and r[2] < 2.0 and r[4] > -2.0 and r[5] > -10.0]
    print("达标档：%s" % (", ".join("%s(ΔHNR %+.1f, jitter %.4f)"
                                   % (r[0], r[1], r[3]) for r in ok) or "无"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3]))
