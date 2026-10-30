#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证「高频补偿按不按音高对齐」这件事，到底有没有听感上的后果。

背景
----
`stages/seedvc_cli.py cross` 把 A（RVC 干声）的低频与 B（原唱人声）的高频
按频带拼起来，相位统一用 A 的。而这首歌是 **pitch = -8 半音**：
原唱中位 324Hz、RVC 输出 195Hz。于是 5.5k 以上那段 B 的谐波**间距是 324Hz**，
下面 A 的谐波间距是 195Hz —— **两套栅格叠在一起**。

本探针做两版，只差一件事：
    now   B 原样参与交叉（= 现在的产品行为）
    fix   B 的幅度谱按 r = 2**(pitch/12) 缩放频率轴后再参与交叉
          （即"把原唱更高一段的内容挪下来"，谐波间距与 A 一致）

判据（5.5~8kHz —— 就是被拼进去的那一段）：
    `align` = 落在 A 的 f0 整数倍 ±3% 内的能量 ÷ 该频带总能量。
    fix 应该明显高于 now。差距小 ⇒ 这个假设不成立，别照着它改产品。

用法：
    python tools/probe_hfphase.py <A_rvc.wav> <B_src.wav> \
        --from 160.5 --len 4.8 --pitch -8 --fc 5500 --out data/_preview/hf.png
"""
import argparse
import os
import sys

import numpy as np
import scipy.signal as sp

import soundfile as sf  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

N_FFT = 2048
HOP = 512
WIN = np.hanning(N_FFT + 1)[:N_FFT]


def load(path, start, dur, sr_out=48000):
    a, sr = sf.read(path, dtype="float64", always_2d=True)
    a = a.mean(axis=1)
    a = a[int(start * sr):int((start + dur) * sr)]
    if sr != sr_out:
        a = sp.resample_poly(a, sr_out, sr)
    return a.astype(np.float64), sr_out


def stft(a):
    ap = np.concatenate([np.zeros(N_FFT), a, np.zeros(N_FFT)])
    nfr = 1 + (len(ap) - N_FFT) // HOP
    S = np.empty((nfr, N_FFT // 2 + 1), dtype=np.complex128)
    for i in range(nfr):
        S[i] = np.fft.rfft(ap[i * HOP:i * HOP + N_FFT] * WIN)
    return S, len(ap)


def istft(S, total_len):
    y = np.zeros(total_len)
    wsum = np.zeros(total_len)
    for i in range(S.shape[0]):
        seg = np.fft.irfft(S[i])[:N_FFT]
        s = i * HOP
        y[s:s + N_FFT] += seg * WIN
        wsum[s:s + N_FFT] += WIN ** 2
    y /= np.maximum(wsum, 1e-8)
    return y[N_FFT:total_len - N_FFT]


def cross(A, B, fc, fb, r=1.0):
    """r=1 就是现在的行为；r=2**(pitch/12) 是"按音高对齐"。

    ★ 为什么可以只缩放**幅度**：cross 里相位统一取 A 的（`angle(Sa)`），
      B 只贡献幅度。所以频率轴重映射不需要相位插值，插值误差不会被听见。
    """
    n = min(len(A), len(B))
    A, B = A[:n], B[:n]
    Sa, tl = stft(A)
    Sb, _ = stft(B)
    nb = Sa.shape[1]
    if r != 1.0:
        old = np.arange(nb) / float(r)          # 新 bin k ← 旧 bin k/r
        Sb_mag = np.abs(Sb)
        Sb_mag = np.stack([np.interp(old, np.arange(nb), Sb_mag[i], left=0, right=0)
                           for i in range(Sb.shape[0])])
        Sb = Sb_mag * np.exp(1j * np.angle(Sa))
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / 48000.0)
    w = 1.0 / (1.0 + np.exp(-(freqs - fc) / max(1.0, fb / 4.0)))
    m = np.abs(Sa) * (1.0 - w) + np.abs(Sb) * w
    out = istft(m * np.exp(1j * np.angle(Sa)), tl)[:n]
    pk = float(np.abs(out).max())
    if pk > 1e-9:
        out = out * (float(np.abs(A).max()) / pk)
    return out


def f0_track(x, lo=120.0, hi=700.0):
    """整段取一个稳健 f0：逐帧自相关，取有声帧的中位数。"""
    fr = np.lib.stride_tricks.sliding_window_view(x, N_FFT)[::HOP]
    if len(fr) < 4:
        return 0.0
    fr = fr * WIN
    rms = np.sqrt((fr ** 2).mean(axis=1) + 1e-12)
    th = np.percentile(rms, 60)
    keep = rms > max(th, 1e-4)
    if keep.sum() < 2:
        return 0.0
    ac = np.fft.irfft(np.abs(np.fft.rfft(fr[keep], n=4096, axis=1)) ** 2,
                      n=4096, axis=1)[:, :1200]
    a0 = np.maximum(ac[:, 0], 1e-12)
    l0, l1 = int(48000 / hi), int(48000 / lo)
    band = ac[:, l0:l1] / a0[:, None]
    k = np.argmax(band, axis=1)
    good = band[np.arange(len(k)), k] > 0.45
    if not good.any():
        return 0.0
    return float(np.median(48000.0 / (l0 + k[good])))


def align_ratio(x, f0, f_lo=5500.0, f_hi=8000.0, tol=0.10):
    """5.5~8k 里有多少能量落在 f0 整数倍上（±tol）。

    ★ tol 不能取小：N_FFT=2048 @48k ⇒ bin 宽 23.4Hz，而 5.5k 处 f0=280Hz 的
      3% 只有 8.4Hz —— **比一个 bin 还窄**，量出来全是 4%，两种实现分不开。
      10%（28Hz ≈ 1.2 个 bin）才让它有分辨率。
    """
    S, _ = stft(x)
    mag = np.abs(S)
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / 48000.0)
    sel = (freqs >= f_lo) & (freqs <= f_hi)
    if not sel.any() or f0 <= 0:
        return 0.0
    f = freqs[sel]
    k = np.maximum(1.0, np.round(f / f0))
    near = np.abs(f - k * f0) <= tol * f0
    tot, hit = 0.0, 0.0
    for i in range(mag.shape[0]):
        b = mag[i][sel]
        s = b.sum()
        tot += s
        hit += b[near].sum()
    return hit / tot if tot > 0 else 0.0


def spec_img(x, W=1200, H=320, flo=3000, fhi=9000):
    S, _ = stft(x)
    mag = np.abs(S).T
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / 48000.0)
    sel = (freqs >= flo) & (freqs <= fhi)
    m = 20 * np.log10(mag[sel] + 1e-9)
    m -= m.max()
    m = np.clip((m + 60) / 60, 0, 1)
    xs = np.linspace(0, m.shape[1] - 1, W).astype(int)
    img = (m[:, xs] * 255).astype(np.uint8)
    im = Image.fromarray(np.flipud(img), "L").resize((W, H), Image.BILINEAR)
    arr = (np.asarray(im).astype(np.float32) / 255.0) ** 0.85
    col = np.zeros((H, W, 3), np.uint8)
    col[..., 0] = np.clip(arr * 300 - 60, 0, 255)
    col[..., 1] = np.clip(arr * 380 - 130, 0, 255)
    col[..., 2] = np.clip(arr * 430 - 210, 0, 255)
    return Image.fromarray(col)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("a", help="RVC 干声（未补高频）")
    ap.add_argument("b", help="原唱人声")
    ap.add_argument("--from", dest="st", type=float, default=0.0)
    ap.add_argument("--len", dest="du", type=float, default=5.0)
    ap.add_argument("--pitch", type=float, default=-8.0)
    ap.add_argument("--fc", type=float, default=5500.0)
    ap.add_argument("--fb", type=float, default=1200.0)
    ap.add_argument("--out", default="data/_preview/hf_align.png")
    a = ap.parse_args()
    A, sr = load(a.a, a.st, a.du)
    B, _ = load(a.b, a.st, a.du, sr)
    if len(A) < sr // 2 or len(B) < sr // 2:
        print("两轨都太短或者起点超出长度")
        return 2
    r = 2.0 ** (a.pitch / 12.0)
    print("A=%s  B=%s" % (os.path.basename(a.a), os.path.basename(a.b)))
    print("区间 %.2f~%.2f 秒   pitch %.1f 半音 ⇒ 频率比 r=%.4f"
          % (a.st, a.st + a.du, a.pitch, r))
    f0A, f0B = f0_track(A), f0_track(B)
    print("f0：A(RVC) %.0f Hz   B(原唱) %.0f Hz   实测比值 %.3f（应≈r）"
          % (f0A, f0B, (f0A / f0B) if f0B else 0))
    y_now = cross(A, B, a.fc, a.fb, 1.0)
    y_fix = cross(A, B, a.fc, a.fb, r)
    r_eff = (f0A / f0B) if f0B > 1 else r
    y_auto = cross(A, B, a.fc, a.fb, r_eff)
    # 只用 A 的 f0 当"应有的栅格"；两版都拿它衡量
    q_now = align_ratio(y_now, f0A)
    q_fix = align_ratio(y_fix, f0A)
    q_auto = align_ratio(y_auto, f0A)
    q_a = align_ratio(A, f0A)
    q_b = align_ratio(B, f0A)
    print("\n5.5~8kHz 落在 A 的谐波栅格上的能量占比（越高越「对齐」）：")
    print("  纯 RVC 输出      %.1f%%" % (100 * q_a))
    print("  原唱单独         %.1f%%   ← 用 A 的栅格去量它，本来就该很低"
          % (100 * q_b))
    print("  现在的交叉(now)  %.1f%%" % (100 * q_now))
    print("  移调对齐(fix)    %.1f%%   ← 按 pitch=%.0f 算的 r=%.3f"
          % (100 * q_fix, a.pitch, r))
    print("  实测比对齐(auto) %.1f%%   ← r=%.3f（用两轨实测 f0 之比）"
          % (100 * q_auto, r_eff))
    d = 100 * (max(q_fix, q_auto) - q_now)
    print("  差值 %+.1f 个百分点" % d)

    im1 = spec_img(y_now)
    im2 = spec_img(y_fix)
    W, H = im1.size
    out = Image.new("RGB", (W, H * 2 + 60), (12, 12, 16))
    out.paste(im1, (0, 26))
    out.paste(im2, (0, H + 52))
    dr = ImageDraw.Draw(out)
    dr.text((8, 8), "NOW  (B not shifted)  align=%.1f%%" % (100 * q_now),
            fill=(255, 190, 90))
    dr.text((8, H + 34), "FIX  (B shifted by %.0f st)  align=%.1f%%"
            % (a.pitch, 100 * q_fix), fill=(120, 240, 255))
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    out.save(a.out)
    print("图：%s（上 NOW / 下 FIX，3~9kHz）" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
