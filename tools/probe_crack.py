#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""量「破音」——不用耳朵判，也不要靠主观。

破音在信号上就三种样子，各有一个客观量：
  1. **过载削波**：样本顶到 ±0.99 ⇒ `clip%`
  2. **周期性断掉**（电音疙瘩 / 撕裂 / 嘶哑）：帧内归一化自相关的峰值 `r`。
     干净的歌声是个周期信号，r 应该接近 1；一破就塌下来。
  3. **音高跳线**（八度错 / 断点）：相邻帧 F0 差超过若干半音算一次。

怎么用它：给两段（出问题的那段 + 一段没问题的当对照），看 `r` 和 `破帧%`
差多少。差得明显 ⇒ 这段确实破了、也拿到了可信的读数；差不多 ⇒ 是别的原因，
别照着"破音"去修。

用法：
    python tools/probe_crack.py <wav> --from 156.7 --len 12.4 --tag "No no no"
    # 加对照段（可多次）
    python tools/probe_crack.py <wav> --from 156.7 --len 12.4 --tag 副歌 \\
        --from 60.9 --len 12.0 --tag 主歌对照
"""
import argparse
import os
import sys

import numpy as np
import scipy.signal as sp

import soundfile as sf  # noqa: E402

TGT_SR = 16000
FRAME = 1024
HOP = 256
WIN = np.hanning(FRAME)
F0_LO, F0_HI = 70.0, 800.0          # 人声合理基频范围
R_BAD = 0.55                        # r 低于这个就算"这一下破了"
RMS_FLOOR = 0.02                    # rms 低于这个当静音，不参与统计
JUMP_CENTS = 600.0                  # 相邻帧音高差超过 600 音分（6 个半音）算一次跳线


def _sec(x):
    """支持 "156.68" 与 "2:36.68" 两种写法。"""
    if ":" in str(x):
        a, b = str(x).split(":", 1)
        return float(a) * 60.0 + float(b)
    return float(x)


def load(path, start, dur):
    x, sr = sf.read(path, dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    a = int(max(0.0, start) * sr)
    b = int(min(len(x), (start + dur) * sr))
    seg = x[a:b]
    if sr != TGT_SR:
        seg = sp.resample_poly(seg, TGT_SR, sr).astype(np.float32)
    return seg, TGT_SR


def frames_of(x):
    n = 1 + (len(x) - FRAME) // HOP
    if n <= 1:
        return np.zeros((0, FRAME), np.float32)
    idx = np.arange(n)[:, None] * HOP + np.arange(FRAME)[None, :]
    return x[idx]


def analyze(x):
    """逐帧：rms / r(谐波性) / f0 / clip 计数。"""
    fr = frames_of(x)
    if not len(fr):
        return None
    n = len(fr)
    rms = np.sqrt((fr ** 2).mean(axis=1) + 1e-12)
    peak = np.abs(fr).max(axis=1)
    clip = (np.abs(fr) > 0.99).sum(axis=1) / float(FRAME)

    nfft = 2048
    X = np.fft.rfft(fr * WIN[None, :], n=nfft, axis=1)
    ac = np.fft.irfft(X * np.conj(X), n=nfft, axis=1)[:, :FRAME]
    e0 = np.maximum(ac[:, 0], 1e-12)
    lo = max(2, int(TGT_SR / F0_HI))
    hi = min(FRAME - 2, int(TGT_SR / F0_LO))
    band = ac[:, lo:hi]
    k = np.argmax(band, axis=1)
    # 抛物线插值取亚 lag 精度（八度错也能差半个音，别用整数 lag 就近一判了事）
    lag = (lo + k).astype(np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        y0 = ac[np.arange(n), np.clip(lo + k - 1, 0, FRAME - 1)]
        y2 = ac[np.arange(n), np.clip(lo + k + 1, 0, FRAME - 1)]
        y1 = np.maximum(band[np.arange(n), k], 1e-12)
        den = (y0 - 2 * y1 + y2)
        shift = np.where(np.abs(den) > 1e-12, 0.5 * (y0 - y2) / den, 0.0)
        lag = np.clip(lag + np.clip(shift, -0.5, 0.5), lo, hi)
    r = band[np.arange(n), k] / e0
    f0 = TGT_SR / np.maximum(lag, 1.0)
    return {"n": n, "rms": rms, "peak": peak, "clip": clip, "r": r, "f0": f0,
            "t": np.arange(n) * (HOP / float(TGT_SR))}


def one(path, start, dur, tag):
    x, sr = load(path, start, dur)
    a = analyze(x)
    if not a:
        print("%-14s  段太短，跳过了" % tag)
        return None
    voiced = a["rms"] > RMS_FLOOR
    nv = int(voiced.sum())
    if nv < 4:
        print("%-14s  有效帧太少 (%d)，这段可能没在唱" % (tag, nv))
        return None
    r_v = a["r"][voiced]
    bad = int((r_v < R_BAD).sum())
    f0v = a["f0"][voiced]
    semis = np.abs(1200.0 * np.log2(np.maximum(f0v[1:], 1e-6) /
                                    np.maximum(f0v[:-1], 1e-6)))
    jumps = int((semis > JUMP_CENTS).sum())
    clipr = float(a["clip"][voiced].mean() * 100.0)
    # 时间梅播：每 0.5 秒一格，看"破在哪一会"
    print("\n[%s]  %.1f~%.1f 秒（%.1f 秒）" % (tag, start, start + dur, dur))
    print("  有声帧 %d / %d（%.0f%%）  mean_r %.3f  破帧 %.1f%%  clip %.3f%%  "
          "F0跳线 %d 次" % (nv, a["n"], 100.0 * nv / max(1, a["n"]),
                            float(r_v.mean()), 100.0 * bad / nv, clipr, jumps))
    step = int(round(0.5 * TGT_SR / HOP))
    print("   时刻    rms     mean_r   破帧%")
    for i in range(0, a["n"], step):
        sl = slice(i, min(a["n"], i + step))
        v = a["rms"][sl] > RMS_FLOOR
        if v.sum() < 3:
            continue
        rr = a["r"][sl][v]
        bar = "#" * int(round(20 * float((rr < R_BAD).mean())))
        print("   %6.1f  %.4f   %.3f   %5.1f%% %s"
              % (start + i * HOP / float(TGT_SR), float(a["rms"][sl][v].mean()),
                 float(rr.mean()), 100.0 * float((rr < R_BAD).mean()), bar))
    worst = np.argsort(a["r"])[:8]
    worst = [w for w in worst if a["rms"][w] > RMS_FLOOR]
    if worst:
        print("  最破的几帧：%s"
              % ", ".join("%.2fs(r=%.2f,f0=%.0f)"
                          % (start + w * HOP / float(TGT_SR), a["r"][w],
                             a["f0"][w]) for w in worst[:6]))
    return {"tag": tag, "nv": nv, "mean_r": float(r_v.mean()),
            "bad": 100.0 * bad / nv, "clip": clipr, "jumps": jumps,
            "f0_med": float(np.median(f0v))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--span", nargs="+", action="append", required=True,
                    metavar="FROM LEN [TAG]",
                    help="给一段（可多次）：--span 156.68 13.0 副歌NoNoNo；"
                         "时刻也可以写 2:36.68 这种")
    a = ap.parse_args()
    if not os.path.isfile(a.wav):
        print("文件不在：%s" % a.wav)
        return 2
    spans = []
    for k, sp in enumerate(a.span):
        if len(sp) < 2:
            print("--span 至少要给起点和长度")
            return 2
        spans.append((_sec(sp[0]), _sec(sp[1]),
                      sp[2] if len(sp) > 2 else "段%d" % (k + 1)))
    tags = [x[2] for x in spans]
    res = []
    for st, du, tg in zip([x[0] for x in spans], [x[1] for x in spans], tags):
        r = one(a.wav, st, du, tg)
        if r:
            res.append(r)
    if len(res) >= 2:
        print("\n=== 对比 ===")
        print("%-14s %8s %8s %8s %8s %8s"
              % ("段", "mean_r", "破帧%", "clip%", "跳线", "F0中位"))
        for r in res:
            print("%-14s %8.3f %8.1f %8.3f %8d %8.0f"
                  % (r["tag"], r["mean_r"], r["bad"], r["clip"], r["jumps"],
                     r["f0_med"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
