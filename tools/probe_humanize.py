# -*- coding: utf-8 -*-
"""探针：给 RVC 成品注入「真人的周期微扰」，量电音指标怎么变。

动机（2026-10-24）
----------------
老板连着两次反馈「明显电音」。实测（stages/artifact_cli.py）：

    轨            HNR     psim    jitter   shimmer   4-8k占比
    原唱(probe)   11.42   0.7936  0.01126  0.09356   0.00393
    RVC +12       21.93   0.9362  0.00667  0.03782   0.01634
    RVC +0        16.79   0.9163  0.01127  0.04672   0.00735

★ 关键观察：成品的 jitter / shimmer **全都低于真人**（周期比真人还规整）。
  这就是「AI 感」的物理形态 —— 声码器把每个周期印得一模一样。
  上一轮走的路是「掺白噪声把 HNR 压下来」，实测要掺 35% 才够、HNR 掉到 6.4，
  等于把成品弄成沙沙响 —— 那条路已被数据否掉。

本探针换一个方向：**注入真人的周期微扰本身**，而不是噪声。
  ① wow/flutter：时变延迟（模拟声带周期长度的逐周期抖动）
  ② shimmer：时变振幅（模拟声带振幅的逐周期抖动）

★ 为什么这条路的副作用天生就小：时移 τ 在第 k 次谐波上造成的相位偏移是
  2π·k·f0·τ —— **正比于频率**。取 τ 使基频（278Hz）相位只偏几度（音准几乎不动），
  第 20 次谐波（5.6kHz）就会偏出上百度的随机相位 ⇒ 高频谐波被抹开。
  于是「电音的两个指标（HNR 高、4-8k 占比高）」会一起降，而音高不动。

这是**纯测量**脚本，不改产品。输出一张剂效表。
"""
import os
import sys
import json
import subprocess
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STAGES = os.path.join(ROOT, "stages")


def read_wav(path):
    """读 wav → (y float32 单声道, sr)。不依赖 librosa（RVC 环境里未必有）。"""
    import wave
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        nch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw == 2:
        a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sw == 4:
        a = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif sw == 1:
        a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError("unsupported sampwidth %d" % sw)
    if nch > 1:
        a = a.reshape(-1, nch).mean(axis=1)
    return a.astype(np.float64), sr


def write_wav(path, y, sr):
    import wave
    y = np.clip(y, -1.0, 1.0)
    pcm = (y * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def _smooth_noise(n, sr, cutoff_hz, rng):
    """平滑随机曲线：白噪声过移动平均低通。返回标准差归一到 1 的数组。

    cutoff_hz 决定「抖动有多快」：
      8Hz   = 磁带 wow/flutter（慢，像跑调）
      60Hz  = 比一个周期慢、但相邻周期已明显不同
      300Hz = 接近逐周期随机（真人 jitter 的量级）
    """
    win = max(1, int(round(float(sr) / max(1.0, cutoff_hz))))
    raw = rng.standard_normal(n + 2 * win)
    k = np.ones(win, dtype=np.float64) / float(win)
    s = np.convolve(raw, k, mode="same")[:n]
    sd = float(np.std(s))
    if sd <= 1e-12:
        return np.zeros(n)
    return s / sd


def wow_flutter(y, sr, jitter_ratio, cutoff_hz, rng, f0_hz):
    """时变延迟（周期抖动）。

    jitter_ratio：目标 jitter（相对周期长度的比例，如 0.011 = 1.1%）。
    实现思路：延迟曲线 d(t) 的标准差取  jitter_ratio * 周期 / sqrt(2)
    —— 两个相邻采样的延迟差才是周期变化量，所以标准差要除以 sqrt(2)。
    实际值以量出来的 jitter 为准（本脚本就是来找这个系数的）。
    """
    n = len(y)
    period = float(sr) / max(40.0, f0_hz)
    s = _smooth_noise(n, sr, cutoff_hz, rng)
    amp = jitter_ratio * period
    d = amp * s                      # 单位：采样点
    idx = np.arange(n, dtype=np.float64) + d
    i0 = np.floor(idx).astype(np.int64)
    frac = idx - i0
    i0 = np.clip(i0, 0, n - 2)
    return y[i0] * (1.0 - frac) + y[i0 + 1] * frac


def shimmer_mod(y, sr, shimmer_ratio, cutoff_hz, rng):
    """时变振幅（振幅抖动）。shimmer_ratio 是振幅的标准差比例。"""
    n = len(y)
    s = _smooth_noise(n, sr, cutoff_hz, rng)
    return y * (1.0 + shimmer_ratio * s)


def main():
    if len(sys.argv) < 3:
        sys.stdout.write("usage: probe_humanize.py <ref.wav> <cand.wav> [outdir]\n")
        return 2
    ref, cand = sys.argv[1], sys.argv[2]
    outdir = sys.argv[3] if len(sys.argv) > 3 else tempfile.mkdtemp(prefix="_hum_")
    os.makedirs(outdir, exist_ok=True)

    py = sys.executable
    y, sr = read_wav(cand)

    # 先量原唱和原始成品的 f0，好把抖动量锚在真实周期上
    def _f0(p):
        j = os.path.join(outdir, "_f0.json")
        subprocess.run([py, "-s", os.path.join(STAGES, "artifact_cli.py"),
                        j, "0", "999", p], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        try:
            with open(j, encoding="utf-8") as f:
                return json.load(f)[0].get("f0_med_hz") or 200.0
        except Exception:
            return 200.0

    f0_ref = _f0(ref)
    f0_cand = _f0(cand)
    sys.stdout.write("f0: ref=%.1f  cand=%.1f  sr=%d\n" % (f0_ref, f0_cand, sr))

    # ---- 剂量表：jitter × 抖动带宽 ----
    # ★ 带宽（cutoff）是第二次扫才加的：第一次只看幅度，结果 jit0.015_c60 和
    #   jit0.015_c300 的 HNR 差了 5dB（14.88 vs 9.93）—— 同样的"抖动量"，
    #   抖得快和抖得慢完全是两回事。真人 jitter 是**逐周期**的，所以带宽必须扫。
    rows = []
    cases = [("orig", None, None, None)]
    for jr in (0.015, 0.025, 0.035):
        for cut in (600.0,):
            cases.append(("j%.3f_c%d" % (jr, int(cut)), jr, cut, 0.0))

    for name, jr, cut, sh in cases:
        if jr is None:
            out = os.path.join(outdir, "orig.wav")
            import shutil
            shutil.copyfile(cand, out)
        else:
            rng = np.random.default_rng(1234)
            z = wow_flutter(y, sr, jr, cut, rng, f0_cand) if jr > 0 else y
            if sh > 0:
                z = shimmer_mod(z, sr, sh, cut, rng)
            # 峰值归一，免得振幅调制把整体电平改了（后面所有比值才可比）
            pk = float(np.max(np.abs(z))) or 1.0
            z = z / pk * float(np.max(np.abs(y)))
            out = os.path.join(outdir, "%s.wav" % name)
            write_wav(out, z, sr)
        rows.append((name, out))

    # ---- 一次量全部（同一窗口，数字才可比）----
    paths = [ref, cand] + [p for _, p in rows if os.path.basename(p) != "orig.wav"]
    j = os.path.join(outdir, "_m.json")
    subprocess.run([py, "-s", os.path.join(STAGES, "artifact_cli.py"),
                    j, "0", "999"] + paths,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(j, encoding="utf-8") as f:
        data = json.load(f)
    m = {os.path.basename(d.get("file") or ""): d for d in data}
    base = m.get(os.path.basename(ref), {})

    def _cent(p):
        y2, s2 = read_wav(p)
        n = min(len(y2), s2 * 30)
        sp = np.abs(np.fft.rfft(y2[:n] * np.hanning(n))) ** 2
        fr = np.fft.rfftfreq(n, 1.0 / s2)
        tot = sp.sum()
        return round(float((sp * fr).sum() / tot), 0) if tot > 0 else None

    def _band(p, lo, hi):
        y2, s2 = read_wav(p)
        n = min(len(y2), s2 * 30)
        sp = np.abs(np.fft.rfft(y2[:n] * np.hanning(n))) ** 2
        fr = np.fft.rfftfreq(n, 1.0 / s2)
        sel = (fr >= lo) & (fr < hi)
        if not sel.any():
            return None
        return round(float(10.0 * np.log10(sp[sel].mean() + 1e-20)), 2)

    sys.stdout.write("\n%-14s %7s %7s %7s %8s %8s %7s %7s %7s\n"
                     % ("case", "HNR", "dHNR", "psim", "jitter", "shimmer",
                        "hf x", "cent", "8-12k"))
    sys.stdout.write("%-14s %7s %7s %7s %8s %8s %7s %7s %7s\n"
                     % ("REF", base.get("hnr_db"), "-", base.get("period_sim"),
                        base.get("jitter_local"), base.get("shimmer_local"),
                        "1.00", _cent(ref), _band(ref, 8000, 12000)))
    for name, p in rows:
        d = m.get(os.path.basename(p), {})
        try:
            dh = float(d.get("hnr_db")) - float(base.get("hnr_db"))
        except (TypeError, ValueError):
            dh = None
        try:
            rel = float(d.get("hf_ratio")) / float(base.get("hf_ratio"))
        except (TypeError, ValueError, ZeroDivisionError):
            rel = None
        sys.stdout.write("%-14s %7s %7s %7s %8s %8s %7s %7s %7s\n"
                         % (name, d.get("hnr_db"),
                            ("%+.2f" % dh) if dh is not None else "-",
                            d.get("period_sim"), d.get("jitter_local"),
                            d.get("shimmer_local"),
                            ("%.2f" % rel) if rel is not None else "-",
                            _cent(p), _band(p, 8000, 12000)))
    sys.stdout.write("\noutdir=%s\n" % outdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
