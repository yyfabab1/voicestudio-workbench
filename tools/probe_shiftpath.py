# -*- coding: utf-8 -*-
"""探针：变调该在 RVC 里做，还是在外面做？（2026-10-24）

背景
----
老板两次反馈「明显电音」，实测根因是**变调幅度**：同一个模型同一套参数，
只把 pitch 从 0 改成 +12，HNR 就从 16.79 涨到 21.93（原唱 11.42）、
4-8k 占比翻 4.16 倍。

假设
----
RVC 的变调是把 f0 乘 2^(n/12) 后**喂给声码器重新合成**。模型是在训练音域上
学的，+12 意味着 f0 翻倍 ⇒ 进入模型没见过的音区 ⇒ 声码器外推出怪声。
而 cand_+0 是在模型熟悉的音区里生成的，音色应该更健康。

于是：让 RVC 只做「音色转换」（pitch=0），**变调放到外面**用相位声码器做。
外面变调的唯一副作用是**共振峰会跟着走**（声音变尖细/卡通），所以要补一步
**谱包络还原** —— 把 +0 版的包络乘回去，只留音高的变化。

本探针一次性量四条轨，横着比（同窗口才可比）：
    A = RVC 内部 +12（现状）
    B = RVC +0 → 外部相位声码器 +12（裸）
    C = B + 谱包络还原（保共振峰）
    R = 原唱参考

纯测量脚本，不改产品。
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
    pcm = (pcm * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def fix_len(y, n):
    if len(y) >= n:
        return y[:n]
    return np.concatenate([y, np.zeros(n - len(y))])


def env_of(S, smooth=41):
    """log 谱域沿频率轴平滑 ⇒ 谱包络（保留共振峰，抹掉谐波细节）。"""
    L = np.log(np.abs(S) ** 2 + 1e-12)
    k = np.ones(smooth, dtype=np.float64) / float(smooth)
    pad = smooth // 2
    Lp = np.pad(L, ((pad, pad), (0, 0)), mode="edge")
    E = np.empty_like(Lp)
    for i in range(Lp.shape[1]):
        E[:, i] = np.convolve(Lp[:, i], k, mode="same")
    return E[pad:pad + L.shape[0], :]


def match_env(src, ref, sr, n_fft=2048, hop=512, smooth=41):
    """把 src 的谱包络换成 ref 的谱包络（共振峰还原），细节（谐波/噪声）不变。"""
    Ss = librosa.stft(src, n_fft=n_fft, hop_length=hop)
    Sr = librosa.stft(ref, n_fft=n_fft, hop_length=hop)
    m = min(Ss.shape[1], Sr.shape[1])
    Ss, Sr = Ss[:, :m], Sr[:, :m]
    Es, Er = env_of(Ss, smooth), env_of(Sr, smooth)
    gain = np.exp(0.5 * (Er - Es))          # 幅度域的比例
    gain = np.clip(gain, 0.05, 20.0)        # 别让静音段炸出来
    out = librosa.istft(Ss * gain, hop_length=hop, length=len(src))
    return fix_len(out, len(src))


def band_db(y, sr, lo, hi):
    """某频段的平均能量（dB），用来看高频补偿的成果有没有被抹掉。"""
    n = min(len(y), sr * 30)
    spec = np.abs(np.fft.rfft(y[:n] * np.hanning(n))) ** 2
    fr = np.fft.rfftfreq(n, 1.0 / sr)
    sel = (fr >= lo) & (fr < hi)
    if not sel.any():
        return None
    return round(float(10.0 * np.log10(spec[sel].mean() + 1e-20)), 2)


def centroid(y, sr):
    n = min(len(y), sr * 30)
    spec = np.abs(np.fft.rfft(y[:n] * np.hanning(n))) ** 2
    fr = np.fft.rfftfreq(n, 1.0 / sr)
    tot = spec.sum()
    if tot <= 0:
        return None
    return round(float((spec * fr).sum() / tot), 0)


def main():
    global librosa
    if len(sys.argv) < 5:
        sys.stdout.write("usage: probe_shiftpath.py <ref> <cand0> <cand12> <outdir> [n_steps]\n")
        return 2
    ref, cand0, cand12, outdir = sys.argv[1:5]
    n_steps = float(sys.argv[5]) if len(sys.argv) > 5 else 12.0
    os.makedirs(outdir, exist_ok=True)

    import librosa  # noqa: E402  （延迟 import：加载慢，且本文件只在探针里用）

    py = sys.executable
    y0, sr = read_wav(cand0)
    sys.stdout.write("sr=%d  len=%.1fs  n_steps=%g\n" % (sr, len(y0) / float(sr), n_steps))

    a_out = os.path.join(outdir, "A_rvc_in.wav")
    shutil.copyfile(cand12, a_out)

    sys.stdout.write("  相位声码器变调中（可能慢）...\n")
    yb = librosa.effects.pitch_shift(y=y0.astype(np.float32), sr=sr, n_steps=n_steps)
    # 保长度 + 峰值对齐（后面所有比值才可比）
    yb = fix_len(np.asarray(yb, dtype=np.float64), len(y0))
    yb = yb / (float(np.max(np.abs(yb))) or 1.0) * (float(np.max(np.abs(y0))) or 1.0)
    b_out = os.path.join(outdir, "B_ext.wav")
    write_wav(b_out, yb, sr)

    sys.stdout.write("  谱包络还原中...\n")
    yc = match_env(yb, y0, sr)
    yc = yc / (float(np.max(np.abs(yc))) or 1.0) * (float(np.max(np.abs(y0))) or 1.0)
    c_out = os.path.join(outdir, "C_ext_env.wav")
    write_wav(c_out, yc, sr)

    z_out = os.path.join(outdir, "Z_rvc_0.wav")
    shutil.copyfile(cand0, z_out)

    paths = [ref, a_out, b_out, c_out, z_out]
    j = os.path.join(outdir, "_m.json")
    subprocess.run([py, "-s", os.path.join(STAGES, "artifact_cli.py"),
                    j, "0", "999"] + paths,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(j, encoding="utf-8") as f:
        data = json.load(f)

    sys.stdout.write("\n%-14s %8s %8s %8s %8s %9s %9s %8s %8s %8s\n"
                     % ("case", "HNR", "dHNR", "psim", "f0Hz", "jitter",
                        "shimmer", "hf x", "cent", "8-12k"))
    base = None
    for d in data:
        if not d.get("ok"):
            sys.stdout.write("FAIL %s %s\n" % (d.get("file"), d.get("err")))
            continue
        nm = d["file"].replace(".wav", "")
        if nm.startswith("probe") or "ref" in nm:
            base = d
        p = os.path.join(outdir, d["file"])
        yy, ssr = read_wav(p) if os.path.isfile(p) else (None, sr)
        db812 = band_db(yy, ssr, 8000, 12000) if yy is not None else None
        ct = centroid(yy, ssr) if yy is not None else None
        sys.stdout.write("%-14s %8s %8s %8s %8s %9s %9s %8s %8s %8s\n"
                         % (nm, d.get("hnr_db"), "-", d.get("period_sim"),
                            d.get("f0_med_hz"), d.get("jitter_local"),
                            d.get("shimmer_local"), d.get("hf_ratio"),
                            ct, db812))
    # 相对原唱的两条关键比值
    if base:
        sys.stdout.write("\n相对原唱（REF hnr=%s hf=%s）：\n" % (base.get("hnr_db"), base.get("hf_ratio")))
        sys.stdout.write("%-14s %10s %10s\n" % ("case", "dHNR(dB)", "hf倍数"))
        for d in data:
            if not d.get("ok"):
                continue
            try:
                dh = float(d.get("hnr_db")) - float(base.get("hnr_db"))
            except (TypeError, ValueError):
                dh = None
            try:
                rel = float(d.get("hf_ratio")) / float(base.get("hf_ratio"))
            except (TypeError, ValueError, ZeroDivisionError):
                rel = None
            sys.stdout.write("%-14s %10s %10s\n"
                             % (d["file"].replace(".wav", ""),
                                ("%+.2f" % dh) if dh is not None else "-",
                                ("%.2f" % rel) if rel is not None else "-"))
    sys.stdout.write("\noutdir=%s\n" % outdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
