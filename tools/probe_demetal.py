# -*- coding: utf-8 -*-
"""探针：去金属声的两个手段，分别多大剂量、组合起来是什么效果（2026-10-24）

到这一步为止已经被数据否掉的路（都写在文件头，免得后人重走）：
  ① 掺白噪声把 HNR 压下来 —— 要掺 35% 才够、HNR 掉到 6.4，等于把成品弄成
     沙沙响（2026-10-24 上午实测）。
  ② 外部相位声码器变调（RVC 只出 pitch=0，变调放外面）—— 4-8k 占比反而从
     ×4.16 涨到 ×5.61，相位声码器自己的 artifacts 比 RVC 更多；再加谱包络
     还原则更闷更电（ΔHNR +11.94）。见 tools/probe_shiftpath.py。
  ③ 换 f0 提取器（pm / fcpe）想让周期"抖"起来 —— 三档 jitter 0.0062~0.0069
     全都远低于真人 0.0113，说明"周期太规整"是声码器固有，不是 f0 曲线的锅。

还站得住的两个手段（本探针要定剂量）：
  A. 周期微扰（wow/flutter）：降 HNR。副作用是 jitter 会跟着涨 ——
     实测 j0.004/j0.006/j0.008 分别把 jitter 抬到 0.0111 / 0.0143 / 0.0175
     （真人 0.0113）。所以**只能用刚好够用的小剂量**，靠 B 分担剩下的活。
  B. 4-8k 架式衰减：直接削掉声码器造出来的虚假高频。依据：
       RVC 不变调时 4-8k 就已是原唱的 ×1.87（声码器固有），变调 +12 再翻到
       ×4.16。目标不是削到原唱那么低（会闷），而是削到判据线以内。

纯测量脚本，不改产品。产品化版本是 stages/demetal_cli.py。
"""
import os
import sys
import json
import subprocess
import shutil

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STAGES = os.path.join(ROOT, "stages")


def read_wav(path):
    import wave
    with wave.open(path, "rb") as w:
        sr, nch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw == 2:
        a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sw == 4:
        a = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError("sampwidth %d" % sw)
    if nch > 1:
        a = a.reshape(-1, nch).mean(axis=1)
    return a.astype(np.float64), sr


def write_wav(path, y, sr):
    import wave
    pcm = np.clip(y, -1.0, 1.0)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((pcm * 32767.0).astype("<i2").tobytes())


def _smooth_noise(n, sr, cutoff_hz, rng):
    win = max(1, int(round(float(sr) / max(1.0, cutoff_hz))))
    raw = rng.standard_normal(n + 2 * win)
    k = np.ones(win, dtype=np.float64) / float(win)
    s = np.convolve(raw, k, mode="same")[:n]
    sd = float(np.std(s))
    return s / sd if sd > 1e-12 else np.zeros(n)


def wow_flutter(y, sr, jitter_ratio, cutoff_hz, rng, f0_hz):
    n = len(y)
    period = float(sr) / max(40.0, f0_hz)
    d = jitter_ratio * period * _smooth_noise(n, sr, cutoff_hz, rng)
    idx = np.arange(n, dtype=np.float64) + d
    i0 = np.clip(np.floor(idx).astype(np.int64), 0, n - 2)
    frac = idx - i0
    return y[i0] * (1.0 - frac) + y[i0 + 1] * frac


def shelf_48k(y, sr, gain_db, lo=4000.0, hi=8000.0, edge=1200.0, n_fft=4096, hop=1024):
    """4-8k 架式衰减（零相位，用 STFT 做，保留时变性）。

    gain_db 为负数 = 衰减。edge 是过渡带宽度（Hz）：直接硬切会在时域振铃，
    听起来像"咝咝响"，所以两端各留一个平滑过渡。
    """
    import librosa
    if not gain_db:
        return y
    S = librosa.stft(y, n_fft=n_fft, hop_length=hop)
    fr = np.fft.rfftfreq(n_fft, 1.0 / sr)
    g = np.ones(len(fr), dtype=np.float64)
    lin = 10.0 ** (float(gain_db) / 20.0)
    # 上升沿 lo-edge → lo，平台 lo → hi，下降沿 hi → hi+edge
    up = (fr > lo - edge) & (fr < lo)
    g[up] = 10.0 ** (float(gain_db) / 20.0
                     * 0.5 * (1.0 - np.cos(np.pi * (fr[up] - (lo - edge)) / edge)))
    g[(fr >= lo) & (fr <= hi)] = lin
    dn = (fr > hi) & (fr < hi + edge)
    g[dn] = 10.0 ** (float(gain_db) / 20.0
                     * 0.5 * (1.0 + np.cos(np.pi * (fr[dn] - hi) / edge)))
    out = librosa.istft(S * g[:, None], hop_length=hop, length=len(y))
    return np.asarray(out, dtype=np.float64)


def metrics(py, paths, out_json):
    subprocess.run([py, "-s", os.path.join(STAGES, "artifact_cli.py"),
                    out_json, "0", "999"] + paths,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(out_json, encoding="utf-8") as f:
        return {os.path.basename(d.get("file") or ""): d for d in json.load(f)}


def band_db(y, sr, lo, hi):
    n = min(len(y), sr * 30)
    sp = np.abs(np.fft.rfft(y[:n] * np.hanning(n))) ** 2
    fr = np.fft.rfftfreq(n, 1.0 / sr)
    sel = (fr >= lo) & (fr < hi)
    if not sel.any():
        return None
    return round(float(10.0 * np.log10(sp[sel].mean() + 1e-20)), 2)


def cent(y, sr):
    n = min(len(y), sr * 30)
    sp = np.abs(np.fft.rfft(y[:n] * np.hanning(n))) ** 2
    fr = np.fft.rfftfreq(n, 1.0 / sr)
    tot = sp.sum()
    return round(float((sp * fr).sum() / tot), 0) if tot > 0 else None


def main():
    if len(sys.argv) < 4:
        sys.stdout.write("usage: probe_demetal.py <ref> <cand> <outdir>\n")
        return 2
    ref, cand, outdir = sys.argv[1:4]
    os.makedirs(outdir, exist_ok=True)
    py = sys.executable

    y, sr = read_wav(cand)
    pk0 = float(np.max(np.abs(y))) or 1.0

    _j = os.path.join(outdir, "_f0.json")
    subprocess.run([py, "-s", os.path.join(STAGES, "artifact_cli.py"),
                    _j, "0", "999", cand], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    try:
        with open(_j, encoding="utf-8") as f:
            f0 = json.load(f)[0].get("f0_med_hz") or 200.0
    except Exception:
        f0 = 200.0

    rows = []
    # 手段 B 单独：4-8k 削多少
    for g in (0, -2, -4, -6, -9):
        rows.append(("eq%d" % g, 0.0, float(g)))
    # 手段 A 单独：抖动多少（带宽固定 600 —— 上一轮扫出来它最接近真人）
    for jr in (0.004, 0.006, 0.008):
        rows.append(("jit%.3f" % jr, jr, 0.0))
    # 组合：小抖动 + 中等削波
    for jr in (0.004, 0.006):
        for g in (-4, -6):
            rows.append(("j%.3f_eq%d" % (jr, g), jr, float(g)))

    outs = []
    for name, jr, g in rows:
        z = y
        if jr > 0:
            rng = np.random.default_rng(1234)
            z = wow_flutter(z, sr, jr, 600.0, rng, f0)
        if g:
            z = shelf_48k(z, sr, g)
        z = z / (float(np.max(np.abs(z))) or 1.0) * pk0
        p = os.path.join(outdir, "%s.wav" % name)
        write_wav(p, z, sr)
        outs.append((name, p))

    m = metrics(py, [ref, cand] + [p for _, p in outs],
                os.path.join(outdir, "_m.json"))
    base = m.get(os.path.basename(ref), {})

    sys.stdout.write("\nf0=%.1f sr=%d\n" % (f0, sr))
    sys.stdout.write("%-14s %7s %7s %7s %8s %8s %7s %7s %7s %7s\n"
                     % ("case", "HNR", "dHNR", "psim", "jitter", "shimmer",
                        "hf x", "cent", "2-4k", "8-12k"))
    yr, srr = read_wav(ref)
    sys.stdout.write("%-14s %7s %7s %7s %8s %8s %7s %7s %7s %7s\n"
                     % ("REF", base.get("hnr_db"), "-", base.get("period_sim"),
                        base.get("jitter_local"), base.get("shimmer_local"),
                        "1.00", cent(yr, srr), band_db(yr, srr, 2000, 4000),
                        band_db(yr, srr, 8000, 12000)))
    for name, p in outs:
        d = m.get(os.path.basename(p), {})
        yy, ss = read_wav(p)
        try:
            dh = float(d.get("hnr_db")) - float(base.get("hnr_db"))
        except (TypeError, ValueError):
            dh = None
        try:
            rel = float(d.get("hf_ratio")) / float(base.get("hf_ratio"))
        except (TypeError, ValueError, ZeroDivisionError):
            rel = None
        sys.stdout.write("%-14s %7s %7s %7s %8s %8s %7s %7s %7s %7s\n"
                         % (name, d.get("hnr_db"),
                            ("%+.2f" % dh) if dh is not None else "-",
                            d.get("period_sim"), d.get("jitter_local"),
                            d.get("shimmer_local"),
                            ("%.2f" % rel) if rel is not None else "-",
                            cent(yy, ss), band_db(yy, ss, 2000, 4000),
                            band_db(yy, ss, 8000, 12000)))
    sys.stdout.write("\noutdir=%s\n" % outdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
