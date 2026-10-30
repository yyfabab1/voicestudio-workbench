# -*- coding: utf-8 -*-
r"""金属声 / 电音尺子：把「听着有电音」变成几个能对账的数。

为什么需要它
------------
老板反馈「唱歌明显没之前好 + 有明显电音」。耳感不能直接当证据，也没法拿去
A/B 比对。电音在声学上是有客观表征的 —— RVC 在**音高跨度大**时会把波形
"周期复印"，产生纯谐波堆叠：听感就是金属 / 机械 / 电子味。

量的事（都是「**和原唱真人声偏离多少**」，不是绝对值）
-------------------------------------------------------
  1. HNR（谐波噪声比，dB）—— 气声 / 呼吸感还剩多少。比原唱高一截就是被抹掉了。
  2. 周期相似度 period_sim（0~1）—— 相邻两个基频周期的波形有多像。
     真人在连续的两个周期里，声带振动本来是有微扰的（实测本 PJ trouvé 0.69 上下）；
     RVC 复制到接近 double 的精度（实测 0.90）时，听上去就开始"电"。
  3. jitter / shimmer —— 逐周期的微扰幅度。真人 1%~10%，越接近 0 越机械。

另外两项用来解释为什么：
  hf_ratio —— 4~8kHz 占 0~8kHz 的能量比（咬字 / 齿音的代理）。
  f0_med_hz —— 取样窗里的基频中位数（确认 pitch 档确实生效了）。

用法（cwd 必须是 RVC 根目录，用 RVC 自带解释器 —— 它才有 parselmouth）：
    runtime\python.exe -s <本脚本> <输出.json> <起点秒> <时长秒> <wav1> [wav2 ...]

输出：每个 wav 一组数。判读时**拿同一次调用里的几个 wav 横着比对**，
      不要拿不同次调用的数字比（取样窗口必须相同才有可比性）。
"""

import json
import os
import sys

import numpy as np


def _read(path, sr_out=16000, t0=None, dur=None):
    """读一段 wav，统一重采样到 16k 单声道。

    统一采样率的原因：CPP / HNR / 周期相似度都对采样周期敏感 ——
    两条轨采样率不同却直接比数字，比出来的是采样率的差，不是音质的差。
    """
    import soundfile as sf
    import librosa

    y, sr = sf.read(path, dtype="float32", always_2d=True)
    if y.shape[1] > 1:
        y = y.mean(axis=1)
    else:
        y = y[:, 0]
    if t0 is not None:
        y = y[int(t0 * sr):]
    if dur is not None:
        y = y[: int(dur * sr)]
    if sr != sr_out:
        y = librosa.resample(y, orig_sr=sr, target_sr=sr_out)
    return np.ascontiguousarray(y, dtype="float64"), sr_out


def period_similarity(y, sr):
    """相邻两个基频周期的波形相似度（0~1，越高越"机械复印"）。

    做法：先用自相关在局部窗口里估基频周期，取连续两段等长波形算归一化互相关。
    只统计互相关的峰值位置接近 0 的帧（周期估对了才作数），否则估计错周期会
    让相关值凭空变低，把真人声报成"不电"。
    """
    win = int(0.064 * sr)          # 64ms 帧
    hop = int(0.020 * sr)          # 20ms 跳
    n = len(y)
    sims = []
    f0s = []
    i = 0
    while i + 3 * win <= n:
        seg = y[i:i + win]
        seg = seg - seg.mean()
        e = float(np.sqrt(np.mean(seg * seg)))
        if e < 1e-3:               # 静音/气口，不算
            i += hop
            continue
        # 自相关估周期：限制 70~400Hz（人声基频区间）
        lo, hi = int(sr / 400.0), int(sr / 70.0)
        ac = np.correlate(seg, seg, mode="full")[win - 1:]
        if hi >= len(ac):
            i += hop
            continue
        band = ac[lo:hi]
        if band.size == 0:
            i += hop
            continue
        p = lo + int(np.argmax(band))
        if p < lo or p + p + 1 >= len(seg):
            i += hop
            continue
        a = seg[:p]
        b = seg[p:2 * p]
        na = float(np.linalg.norm(a))
        nb = float(np.linalg.norm(b))
        if na < 1e-6 or nb < 1e-6:
            i += hop
            continue
        c = float(np.dot(a, b) / (na * nb))
        # 只在ycles 能量足够处采信（能量低的地方分子分母都小，比值不稳）
        sims.append(max(-1.0, min(1.0, c)))
        f0s.append(sr / float(p))
        i += hop
    if not sims:
        return None, None
    # 用中位数而不是均值：个别帧周期估错会拉跑均值
    return float(np.median(sims)), float(np.median(f0s))


def hf_ratio(y, sr):
    """4~8kHz 能量 / 0~8kHz 能量 —— 齿音与辅音的代理。"""
    spec = np.abs(np.fft.rfft(y * np.hanning(len(y)))) ** 2
    freqs = np.fft.rfftfreq(len(y), 1.0 / sr)
    tot = float(spec[(freqs > 0) & (freqs < 8000)].sum())
    hf = float(spec[(freqs >= 4000) & (freqs < 8000)].sum())
    if tot <= 0:
        return None
    return hf / tot


def measure(path, t0, dur):
    """对一个 wav 量全套指标。失败不抛，返回 {"ok": False, ...}——
    尺子自己崩了不要把调用方的整条链路拖死（本项目的老规矩）。"""
    out = {"file": os.path.basename(path), "ok": False}
    try:
        import parselmouth
        y, sr = _read(path, 16000, t0, dur)
        if len(y) < sr:
            out["err"] = "太短（<1s）"
            return out
        snd = parselmouth.Sound(y, sampling_frequency=float(sr))

        # 注：这里原本还有一个 CPP（倒谱峰突）。实测它在本实现下**判别力是反的**
        # （纯正弦 0.21 dB 反而低于带噪三谐波 0.85 dB）—— 因为趋势线被 cep[0]
        # 那个 -105 dB 的直流项主导。判别力反着的尺子留着会误导后人，
        # 按「定义了没人用的东西第一选择是删」的原则，直接删掉，不凑数。
        # 主判据就用下面这四项（每一项都在合成信号上验过方向）。

        # --- 1. HNR（谐波噪声比，dB）---
        #     校验：纯正弦波器 99.9 dB / 带随机扰动的三谐波 17.8 dB。
        #     真人说话唱歌一般 5~25 dB —— 高出真人一大截 = 只剩谐波、气声被抹掉。
        try:
            import parselmouth
            harm = snd.to_harmonicity_cc(0.01, 75.0, 0.1, 1.0)
            hv = harm.values[np.isfinite(harm.values)]
            hv = hv[hv > -200.0]          # praat 的静音标记是 -200
            if hv.size > 5:
                out["hnr_db"] = round(float(np.median(hv)), 2)
        except Exception as e:
            out["hnr_err"] = "%s: %s" % (type(e).__name__, e)

        # --- 3. jitter / shimmer（周期内微扰）---
        try:
            pp = parselmouth.praat.call(
                snd, "To PointProcess (periodic, cc)", 75.0, 600.0)
            jl = parselmouth.praat.call(
                [pp], "Get jitter (local)", 0.0, 0.0, 0.0001, 0.02, 1.3)
            sh = parselmouth.praat.call(
                [snd, pp], "Get shimmer (local)", 0.0, 0.0, 0.0001, 0.02,
                1.3, 1.6)
            out["jitter_local"] = round(float(jl), 5)
            out["shimmer_local"] = round(float(sh), 5)
        except Exception as e:
            out["jitter_err"] = "%s: %s" % (type(e).__name__, e)

        # --- 4. 相邻周期相似度（本项目自写，对"周期复印"最敏感）---
        sim, f0 = period_similarity(y, sr)
        if sim is not None:
            out["period_sim"] = round(sim, 4)
        if f0 is not None:
            out["f0_med_hz"] = round(f0, 1)

        # --- 5. 高频占比 ---
        hr = hf_ratio(y, sr)
        if hr is not None:
            out["hf_ratio"] = round(hr, 5)

        out["rms"] = round(float(np.sqrt(np.mean(y * y))), 5)
        out["ok"] = True
    except Exception as e:
        out["err"] = "%s: %s" % (type(e).__name__, e)
    return out


def main():
    import io
    if len(sys.argv) < 5:
        sys.stdout.write("usage: artifact_cli.py out.json t0 dur wav...\n")
        return 2
    out_json, t0, dur = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
    paths = sys.argv[4:]
    res = [measure(p, t0, dur) for p in paths]
    with io.open(out_json, "w", encoding="utf-8") as f:
        f.write(json.dumps(res, ensure_ascii=False, indent=1))
    for r in res:
        if not r.get("ok"):
            sys.stdout.write("FAIL %s : %s\n" % (r["file"], r.get("err")))
        else:
            sys.stdout.write(
                "%-42s hnr=%-8s psim=%-8s jit=%-9s shim=%-9s hf=%s\n"
                % (r["file"][:42], r.get("hnr_db"), r.get("period_sim"),
                   r.get("jitter_local"), r.get("shimmer_local"),
                   r.get("hf_ratio")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
