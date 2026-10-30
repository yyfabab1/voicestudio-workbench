# -*- coding: utf-8 -*-
r"""转换噪声补偿（3~10kHz）：按**原唱干声**的长期平均频谱，把 RVC 多出来的那层噪声收掉。

为什么要有这一步
----------------
项目自己的三份报告指向同一件事，而且是"补多少都不够/越补越糟"的那种：

  · `_少声音修复_20261026`：转换在 **6~8k 高 20 dB**、4~6k 高 6 dB；
    4~8k 谱平坦度 0.228 → 0.346（= 噪声化）。现有 deess 实测只压掉 2 dB，**远不够**。
  · `_开源调研与高频诊断_20261018`：成品/原唱 2–4k=63%、4–8k=37%、8–12k=**5.3%**、12–16k≈0。
  · `_索引错配与高频补偿`：RVC 输出有效带宽 **5.7 kHz vs 原唱 11.5 kHz**。

已有的三步都盖不住这一段：
  · `presence_cli.py` 只管 2~5k，而且是**只补不削**；
  · `hf_cli.py` 管 6~12k，搬的是原唱的真高频，但 48k 模型上会"纯亏"（已实测）；
  · `polish.py` 的 deess 量的是"相对自己基线"，不是**相对原唱** ⇒ 抓不到"整体偏亮"。

为什么是"按原唱的频谱差"而不是"硬砍高频"
------------------------------------------
"AI 味/沙哑"不是"高频太多"，是**高频的形状不像人声**：
  · 该有的人声高频（8~12k 的空气感）**缺**；
  · 不该有的转换噪声（4~8k 的宽带毛刺）**多**。
同一条固定 shelf 不可能同时做这两件事 —— 削到噪声没了，空气感也没了。
所以这里量的是**两个文件的长期平均频谱差**，逐 1/3 倍频程给出一条**平滑**曲线：
  · 比原唱高的频段 → 收（最多 `CAP_CUT_DB`）；
  · 比原唱低很多的频段 → 小幅补（最多 `CAP_BOOST_DB`，且只在原唱本来有能量的地方）；
  · 两端用余弦斜坡进出，**不做窄带 notch**（硬边会出金属声，presence_cli 里踩过）。

闭环（铁律 24d）
---------------
补完**复测**：`delta`（各带 |差| 的均值）没有真的变小就不写文件，
stdout 打 `LTAS_SKIP` —— 跟 hf/presence 同一个约定：**不写 wav = 没修**。

用法（cwd 随意，只要 numpy + soundfile）
----------------------------------------
    python ltas_mend.py measure <成品> <原唱> <out.json>
    python ltas_mend.py auto <成品> <原唱> <out.wav> <out.json> [--cap-cut N] [--cap-boost N]
"""

import json
import os
import sys

import numpy as np
import soundfile as sf

# ---- 剂量与判定 ------------------------------------------------------------
BAND_LO, BAND_HI = 3000.0, 10000.0   # 要动的段。3k 以下不碰（共振峰/人声本体）
EDGE_LO, EDGE_HI = 400.0, 1200.0     # 端斜坡宽度（Hz），余弦进出，防硬边金属声
REF_LO, REF_HI = 300.0, 3000.0       # 各自的中频基准：消掉整体响度差，只比形状

CAP_CUT_DB = 6.0      # 单带最多收 6 dB。再深就不是"修噪声"而是"把人声削了"
CAP_BOOST_DB = 3.0    # 单带最多补 3 dB。补多了会从"闷"变成"毛刺"（presence 的教训）
MIN_EXCESS_DB = 1.5   # 高于原唱不到 1.5 dB 的带不动它（人耳下限，白付一次 FFT）
DEFICIT_IGNORE = 3.0  # 低于原唱 3 dB 以内的"缺"不管（那是音色差，不是噪声）
MIN_GAIN_DB = 0.3     # 曲线整体都在这个量级以下 ⇒ 直接 SKIP
NEED_IMPROVE_DB = 0.3 # 复测必须至少好这么多才算"真的变好了"

N_BANDS_PER_OCT = 3   # 1/3 倍频程
SMOOTH_BANDS = 3      # 曲线在带域上做 3 点滑动平均（≈ 1 个倍频程，够平滑）


def _read(path):
    x, sr = sf.read(path, dtype="float32", always_2d=False)
    if x.ndim > 1:
        x = x.mean(axis=1)
    return np.asarray(x, dtype=np.float64), int(sr)


def _spectrum(x, sr):
    """整段 Hann 窗 FFT ⇒ 长期平均功率谱（presence_cli 同一套口径）。"""
    n = len(x)
    nfft = 1 << int(np.ceil(np.log2(max(n, 2))))
    S = np.abs(np.fft.rfft(x * np.hanning(n), nfft)) ** 2
    f = np.fft.rfftfreq(nfft, 1.0 / float(sr))
    return f, S


def _band_levels(f, S, sr):
    """按 1/3 倍频程取带内平均功率（dB），返回 (中心频率数组, dB 数组, 基准 dB)。"""
    centers = []
    fc = BAND_LO
    while fc < BAND_HI:
        centers.append(fc)
        fc *= 2.0 ** (1.0 / N_BANDS_PER_OCT)
    centers = np.asarray(centers, dtype=np.float64)
    half = 2.0 ** (0.5 / N_BANDS_PER_OCT) - 1.0     # 半带宽比例
    lv = np.empty(len(centers), dtype=np.float64)
    for i, c in enumerate(centers):
        lo, hi = c * (1.0 - half), c * (1.0 + half)
        m = (f >= lo) & (f < hi)
        lv[i] = 10.0 * np.log10(max(1e-20, float(S[m].mean()))) if m.any() else -120.0
    ref = (f >= REF_LO) & (f < REF_HI)
    ref_db = 10.0 * np.log10(max(1e-20, float(S[ref].mean()))) if ref.any() else -120.0
    return centers, lv - ref_db, ref_db


def measure(dst, src):
    """量成品相对原唱在 3~10kHz 的形状差（两边各自减掉自己的中频基准）。

    返回 delta_db = 各带 |conv - src| 的均值（越小越像），以及逐带明细。
    """
    try:
        a, sra = _read(dst)
        b, srb = _read(src)
    except Exception as e:
        sys.stderr.write("LTAS 读不了音频：%s\n" % e)
        return None
    n = min(len(a), len(b))
    if n < 44100:                      # 短于一秒没意义
        return None
    a, b = a[:n], b[:n]
    fa, Sa = _spectrum(a, sra)
    fb, Sb = _spectrum(b, srb)
    ca, la, _ = _band_levels(fa, Sa, sra)
    cb, lb, _ = _band_levels(fb, Sb, srb)
    if len(la) != len(lb):
        return None
    delta = la - lb                     # 正 = 成品比原唱多（噪声），负 = 缺
    return {
        "centers_hz": [round(float(c), 1) for c in ca],
        "delta_db": [round(float(d), 2) for d in delta],
        "mean_abs_db": round(float(np.abs(delta).mean()), 2),
        "mean_signed_db": round(float(delta.mean()), 2),
    }


def _gain_curve(centers, delta, cap_cut, cap_boost):
    """逐带目标增益（dB）：多了收、缺很多才小补；不动作的带记 0。"""
    g = np.zeros(len(centers), dtype=np.float64)
    for i, d in enumerate(delta):
        if d > MIN_EXCESS_DB:
            g[i] = -min(d, cap_cut)
        elif d < -DEFICIT_IGNORE:
            g[i] = min(-d - DEFICIT_IGNORE, cap_boost)
    if SMOOTH_BANDS > 1 and len(g) >= SMOOTH_BANDS:
        k = np.ones(SMOOTH_BANDS) / float(SMOOTH_BANDS)
        pad = SMOOTH_BANDS // 2
        g = np.convolve(np.pad(g, (pad, pad), mode="edge"), k, mode="valid")
    return g


def _apply(x, sr, centers, gain_db):
    """把逐带增益插值到 FFT 频点再乘回去；3k 外恒 1，两端余弦斜坡。"""
    n = len(x)
    nfft = 1 << int(np.ceil(np.log2(max(n, 2))))
    X = np.fft.rfft(x, nfft)
    f = np.fft.rfftfreq(nfft, 1.0 / float(sr))
    db = np.interp(f, centers, gain_db, left=0.0, right=0.0)
    # 斜坡：3k 以下 / 10k 以上强制归零，避免硬边
    ramp_in = np.clip((f - (BAND_LO - EDGE_LO)) / max(EDGE_LO, 1e-9), 0.0, 1.0)
    ramp_out = np.clip(((BAND_HI + EDGE_HI) - f) / max(EDGE_HI, 1e-9), 0.0, 1.0)
    db = db * np.minimum(ramp_in, ramp_out)
    y = np.fft.irfft(X * (10.0 ** (db / 20.0)), nfft)[:n]
    r0 = float(np.sqrt((x ** 2).mean())) + 1e-12        # 保 RMS，别让 A/B 变响度对比
    r1 = float(np.sqrt((y ** 2).mean())) + 1e-12
    return y * (r0 / r1)


def _dump(path, obj):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
    except Exception as e:
        sys.stderr.write("LTAS 报告写不了：%s\n" % e)


def cmd_auto(dst, src, out_wav, out_json, cap_cut=None, cap_boost=None):
    cap_cut = float(cap_cut) if cap_cut else CAP_CUT_DB
    cap_boost = float(cap_boost) if cap_boost else CAP_BOOST_DB
    res = {"ok": False, "applied": False}
    m0 = measure(dst, src)
    if not m0:
        _dump(out_json, dict(res, reason="量不出有效频谱"))
        sys.stdout.write("LTAS_FAIL 量不出有效频谱\n")
        return 1
    res["before"] = m0
    centers = np.asarray(m0["centers_hz"], dtype=np.float64)
    delta = np.asarray(m0["delta_db"], dtype=np.float64)
    g = _gain_curve(centers, delta, cap_cut, cap_boost)
    if float(np.abs(g).max()) < MIN_GAIN_DB:
        res["ok"] = True
        res["reason"] = "3~10k 的形状已经够像原唱（最大剂量 %.2f dB）" % float(np.abs(g).max())
        _dump(out_json, res)
        sys.stdout.write("LTAS_SKIP nothing_to_do max_gain=%.2f\n" % float(np.abs(g).max()))
        return 0

    try:
        x, sr = _read(dst)
    except Exception as e:
        _dump(out_json, dict(res, reason="读不了成品：%s" % e))
        sys.stdout.write("LTAS_FAIL 读不了成品\n")
        return 1

    # 闭环：从全量往下试 3 档，第一个"真的更接近原唱"的剂量落盘。
    res["tried"] = []
    for scale in (1.0, 0.7, 0.45):
        y = _apply(x, sr, centers, g * scale)
        tmp = out_wav + ".try.wav"
        try:
            sf.write(tmp, y.astype(np.float32), sr, subtype="PCM_16")
            m1 = measure(tmp, src)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if not m1:
            continue
        res["tried"].append({"scale": scale,
                             "max_gain_db": round(float(np.abs(g * scale).max()), 2),
                             "after_mean_abs_db": m1["mean_abs_db"]})
        if m1["mean_abs_db"] < m0["mean_abs_db"] - NEED_IMPROVE_DB:
            sf.write(out_wav, y.astype(np.float32), sr, subtype="PCM_16")
            res.update({"ok": True, "applied": True, "after": m1,
                        "gain_db": [round(float(v), 2) for v in g * scale],
                        "reason": "按原唱频谱修 3~10k：各带平均差 %.2f → %.2f dB"
                                  % (m0["mean_abs_db"], m1["mean_abs_db"])})
            _dump(out_json, res)
            sys.stdout.write("LTAS_AUTO applied=1 before=%.2f after=%.2f max_gain=%.2f\n"
                             % (m0["mean_abs_db"], m1["mean_abs_db"],
                                float(np.abs(g * scale).max())))
            return 0

    res["ok"] = True
    res["reason"] = "修了但没更接近原唱（试了 %d 档）⇒ 不改动成品" % len(res["tried"])
    _dump(out_json, res)
    sys.stdout.write("LTAS_SKIP no_gain tried=%d\n" % len(res["tried"]))
    return 0


def _opts(argv):
    o = {}
    i = 0
    while i < len(argv):
        if argv[i] == "--cap-cut" and i + 1 < len(argv):
            o["cap_cut"] = argv[i + 1]
            i += 2
        elif argv[i] == "--cap-boost" and i + 1 < len(argv):
            o["cap_boost"] = argv[i + 1]
            i += 2
        else:
            i += 1
    return o


def main():
    a = sys.argv[1:]
    if len(a) >= 4 and a[0] == "measure":
        m = measure(a[1], a[2])
        _dump(a[3], {"ok": bool(m), "measure": m})
        if not m:
            sys.stdout.write("LTAS_FAIL 量不出有效频谱\n")
            return 1
        sys.stdout.write("LTAS_MEASURE mean_abs=%.2f signed=%.2f\n"
                         % (m["mean_abs_db"], m["mean_signed_db"]))
        return 0
    if len(a) >= 5 and a[0] == "auto":
        return cmd_auto(a[1], a[2], a[3], a[4], **_opts(a[5:]))
    sys.stdout.write("usage: ltas_mend.py measure <成品> <原唱> <out.json>\n"
                     "       ltas_mend.py auto <成品> <原唱> <out.wav> <out.json> "
                     "[--cap-cut N] [--cap-boost N]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
