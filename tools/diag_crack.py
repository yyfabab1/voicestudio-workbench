#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把一段歌声画成「能看出病在哪」的图，顺带把客观读数打出来。

为什么是出图不是打数字：破音有多种样子（谐波糊、八度错、接缝裂、过载削波），
数字只能证明"某样东西偏高"，图能一眼看出**是哪一样**。人眼判 5 秒，
比再写三条判据快。

用法：
    python tools/diag_crack.py <wav> --from 156.68 --len 13 \
        --seams 156.78,160.27,165.485,168.06 --out data/_preview/crack.png

图上从上往下：
    ① 频谱（0~8kHz，越亮能量越强）
    ② 叠加的 F0 轨迹（青点，八度错会在上面叠成两条平行线）
    ③ 谐波性 r（绿线，掉到 0.55 以下画红条 —— 那就是"这一下破了"）
    ④ RMS 包络（白线）+ 接缝位置（黄色竖线）
"""
import argparse
import os
import sys

import numpy as np
import scipy.signal as sp

import soundfile as sf  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

TGT_SR = 16000
FRAME = 1024
HOP = 128
WIN = np.hanning(FRAME)
R_BAD = 0.55
RMS_FLOOR = 0.02


def load(path, start, dur):
    x, sr = sf.read(path, dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    a = int(max(0.0, start) * sr)
    b = int(min(len(x), (start + dur) * sr))
    seg = x[a:b]
    if sr != TGT_SR:
        seg = sp.resample_poly(seg, TGT_SR, sr).astype(np.float32)
    return seg


def frames_of(x):
    n = 1 + (len(x) - FRAME) // HOP
    idx = np.arange(n)[:, None] * HOP + np.arange(FRAME)[None, :]
    return x[idx]


def f0_and_r(fr):
    nfft = 2048
    X = np.fft.rfft(fr * WIN[None, :], n=nfft, axis=1)
    ac = np.fft.irfft(X * np.conj(X), n=nfft, axis=1)[:, :FRAME]
    e0 = np.maximum(ac[:, 0], 1e-12)
    lo, hi = max(2, int(TGT_SR / 800.0)), min(FRAME - 2, int(TGT_SR / 70.0))
    band = ac[:, lo:hi]
    k = np.argmax(band, axis=1)
    r = band[np.arange(len(fr)), k] / e0
    f0 = TGT_SR / np.maximum(lo + k, 1.0).astype(np.float64)
    return f0, r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--from", dest="st", default="156.68")
    ap.add_argument("--len", dest="du", type=float, default=13.0)
    ap.add_argument("--seams", default="", help="逗号分隔的换人时刻（秒，绝对值）")
    ap.add_argument("--out", default="data/_preview/crack.png")
    ap.add_argument("--title", default="")
    a = ap.parse_args()
    if ":" in str(a.st):
        m, s = str(a.st).split(":", 1)
        st = float(m) * 60 + float(s)
    else:
        st = float(a.st)
    if not os.path.isfile(a.wav):
        print("文件不在：%s" % a.wav)
        return 2
    x = load(a.wav, st, a.du)
    fr = frames_of(x)
    if len(fr) < 8:
        print("段太短")
        return 2
    n = len(fr)
    rms = np.sqrt((fr ** 2).mean(axis=1) + 1e-12)
    f0, rh = f0_and_r(fr)

    # ---------- 频谱 ----------
    nfft = 1024
    S = np.abs(np.fft.rfft(fr * WIN[None, :], n=nfft, axis=1))
    nb = S.shape[1]
    dB = 20 * np.log10(S + 1e-9)
    dB -= dB.max()
    dB = np.clip((dB + 85.0) / 85.0, 0.0, 1.0)
    w = max(200, min(2200, n))
    hspec = 340
    xs = np.linspace(0, nb - 1, w).astype(np.int32)
    img = dB[:, xs].T                      # (freq, time)
    img = (img * 255).astype(np.uint8)
    img = np.flipud(img)                   # 低频在下
    spec = Image.fromarray(img, "L").resize((w, hspec), Image.NEAREST)
    # 频率轴只保留有用的一段（最高 8k，下面 4k 以下信息密度最高）
    spec = spec.crop((0, int(hspec * (1 - 8000 / (TGT_SR / 2))), w, hspec))
    hs = spec.size[1]

    W = w
    H = hs + 60 + 60
    out = Image.new("RGB", (W, H), (10, 10, 14))
    # 灰度频谱上色：暗蓝 -> 青 -> 黄 -> 白（像 spectral 的高温一路加点 restraint）
    arr = (np.asarray(spec).astype(np.float32) / 255.0) ** 0.85
    col = np.zeros((hs, W, 3), np.uint8)
    col[..., 0] = np.clip(arr * 300 - 60, 0, 255)
    col[..., 1] = np.clip(arr * 380 - 120, 0, 255)
    col[..., 2] = np.clip(arr * 420 - 200, 0, 255)
    out.paste(Image.fromarray(col), (0, 0))
    d = ImageDraw.Draw(out)

    tx = lambda v: int(round(v / max(1, n - 1) * (W - 1)))  # noqa: E731

    # ---------- F0 轨迹（叠在频谱上）----------
    voiced = rms > RMS_FLOOR
    for i in range(n):
        if not voiced[i]:
            continue
        y = hs - int(f0[i] / (TGT_SR / 2.0) * hs * (TGT_SR / 2) / 8000.0 * 1.0)
        # 频率轴被裁到 0~8k：8000 对应整张高
        y = hs - int(np.clip(f0[i] / 8000.0 * hs, 0, hs - 1)) - 1
        d.point((tx(i), max(0, min(hs - 1, y))), fill=(90, 240, 255))

    # ---------- r 曲线 + 破帧红条 ----------
    y0 = hs + 8
    d.line([(0, y0 + 44), (W, y0 + 44)], fill=(40, 60, 50))
    pts = []
    bad_frames = []
    for i in range(n):
        x0 = tx(i)
        y = y0 + 44 - int(np.clip(rh[i], 0, 1) * 44)
        pts.append((x0, y))
        if voiced[i] and rh[i] < R_BAD:
            bad_frames.append(i)
            d.line([(x0, y0 + 46), (x0, y0 + 52)], fill=(255, 70, 70))
    if len(pts) > 1:
        d.line(pts, fill=(120, 255, 150), width=1)
    d.line([(0, y0 + 44 - int(R_BAD * 44)), (W, y0 + 44 - int(R_BAD * 44))],
           fill=(90, 90, 60))

    # ---------- RMS 包络 ----------
    y1 = hs + 58
    yo = H - 3
    pts = []
    for i in range(n):
        pts.append((tx(i), yo - int(np.clip(rms[i] * 4.0, 0, 1) * (yo - y1))))
    if len(pts) > 1:
        d.line(pts, fill=(235, 235, 245))
    # 接缝竖线
    for s in [q for q in (a.seams.split(",") if a.seams else []) if q.strip()]:
        try:
            sv = float(s)
        except ValueError:
            continue
        p = int((sv - st) / a.du * (W - 1))
        if 0 <= p < W:
            d.line([(p, 0), (p, H)], fill=(255, 210, 60))
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    out.save(a.out)

    # ---------- 文字读数 ----------
    bad = len(bad_frames)
    print("文件：%s" % os.path.basename(a.wav))
    print("区间：%.2f ~ %.2f 秒" % (st, st + a.du))
    print("有声帧 %d / %d   破帧 %d（%.1f%%）   mean_r %.3f"
          % (int(voiced.sum()), n, bad, 100.0 * bad / max(1, int(voiced.sum())),
             float(rh[voiced].mean())))
    if bad_frames:
        segs, last = [], [bad_frames[0]]
        for i in bad_frames[1:]:
            if i - last[-1] <= 3:
                last.append(i)
            else:
                segs.append(last)
                last = [i]
        segs.append(last)
        print("破发生在：%s"
              % ", ".join("%.2fs(%d帧)" % (st + s[0] * HOP / float(TGT_SR), len(s))
                          for s in segs))
    # F0 跳线
    fv = f0[voiced]
    tv = np.arange(n)[voiced]
    if len(fv) > 4:
        semi = np.abs(1200 * np.log2(np.maximum(fv[1:], 1e-6) /
                                     np.maximum(fv[:-1], 1e-6)))
        idx = np.where(semi > 600)[0]
        if len(idx):
            print("音高跳线（>6 半音）%d 次：" % len(idx),
                  ", ".join("%.2fs %.0f→%.0fHz"
                            % (st + tv[j] * HOP / float(TGT_SR), fv[j], fv[j + 1])
                            for j in idx[:10]))
        oct_err = np.abs(semi - 1200) < 120
        if oct_err.sum():
            print("  其中像八度错的：%d 次" % int(oct_err.sum()))
    print("图：%s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
