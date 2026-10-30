# -*- coding: utf-8 -*-
r"""临场感补偿（2~5kHz）：把 AI 人声缺的那一段「亮度 / 靠前感」补回来。

为什么要有这一步
----------------
老板反复提「不像角色在唱歌 / AI 感太重 / 有电音」。前面几轮都在跟
**HNR（电音）** 和 **高频（齿音）** 较劲，但一直有一块没人管：**2~5kHz**。

实测（2026-10-25，139 条角色成品 vs 原唱干声）：

    频段            角色成品(中位)   原唱干声    差
    800~1.2k(箱音)     +2.30dB      -0.52dB    +2.8dB（凸出）
    **2~5k(临场感)**   -10.35dB     -6.50dB    **-3.9dB（缺）**
    8k 以上(空气感)     -           -          -8.3dB（另有 hf 那一步管）

Sonarworks《Mastering AI Vocals》把这条列为 AI 人声的固有毛病之一：
「Presence enhancement (2-5 kHz): gently boost this range to add the forward
presence that AI vocals often lack」。我们的量测**证实了它在这批素材上真实存在**。

为什么是「补」而不是「削」
--------------------------
另一条（800~1.2k 箱音凸出）**没有做**，是有意的：
  · 那一带是**共振峰** —— 正是"像不像这个角色"的载体，削多了会离角色更远；
  · 而且它是**宽带的能量分布差异**，不是窄峰，窄带 EQ 削不动
    （实测减 4dB，平均只掉 1.2dB）；
  · 拆分实验里削它只多给了一点点收益，却把 psim 压到**低于**原唱（补过了）。
    ⇒ 只补不削，是有数据支撑的选择，不是偷懒。

剂量怎么定
----------
余弦凹陷窗的形状决定「中心 dB」不等于「平均 dB」（窗均值是中心的一半）⇒
按缺口乘一个系数补，别按 1:1。实测 deficit 3.27dB、中心补 4.5dB 时效果最好
（psim 0.8443 ≈ 原唱 0.8448），系数 ≈ 1.4。

补过头会怎样
------------
psim（周期相似度）会掉到**低于**原唱 —— 那就从"电"变成"糊/毛刺"了。
所以有闭环：补完再量一次，缺口没缩小就回滚（铁律 24d）。

用法（cwd 随意，只要 numpy + soundfile）
----------------------------------------
    python presence_cli.py measure <成品> <原唱> <out.json>
    python presence_cli.py auto <成品> <原唱> <out.wav> <out.json> [--max-db N]

stdout 只打一行机器可读的结果（PRES_AUTO / PRES_SKIP / PRES_FAIL），
跟 hf_cli.py 同一个约定：不写 wav = 没修，调用方不许拿它当成品。
"""

import json
import os
import sys

import numpy as np
import soundfile as sf

# 补多少才值得动手。1.5dB 以下人耳基本听不出来，白付一次 FFT 的钱。
MIN_DEFICIT_DB = 1.5
# 中心剂量上限。实测 6dB 就已经把 psim 压到 0.8308（明显低于原唱 0.8448）。
MAX_DOSE_DB = 6.0
# 缺口 -> 中心剂量的系数（余弦窗均值是中心的一半，1:1 补不够）。
DOSE_GAIN = 1.4

BAND_LO, BAND_HI = 2000.0, 5000.0      # 要补的段
REF_LO, REF_HI = 300.0, 3000.0         # 相对基准（人声的主要能量区）


def _read(path):
    x, sr = sf.read(path, dtype="float32", always_2d=False)
    if x.ndim > 1:
        x = x.mean(axis=1)
    return np.asarray(x, dtype=np.float64), int(sr)


def _band_db(x, sr, lo, hi):
    n = len(x)
    if n < int(sr * 0.2):
        return None
    nfft = 1 << int(np.ceil(np.log2(max(n, 2))))
    S = np.abs(np.fft.rfft(x * np.hanning(n), nfft)) ** 2
    f = np.fft.rfftfreq(nfft, 1.0 / float(sr))
    m = (f >= lo) & (f < hi)
    if not m.any():
        return None
    return 10.0 * np.log10(max(1e-20, float(S[m].mean())))


def measure(dst, src):
    """量成品相对原唱在 2~5kHz 缺多少（相对各自的中频基准）。

    两边都用自己的中频做基准，这样"整体响度不同"不会污染结论 ——
    要的是**频谱形状**的差，不是绝对能量的差。
    """
    try:
        a, sra = _read(dst)
        b, srb = _read(src)
    except Exception as e:
        sys.stderr.write("PRES 读不了音频：%s\n" % e)
        return None
    n = min(len(a), len(b))
    if n < int(min(sra, srb) * 0.5):
        return None
    a, b = a[:n], b[:n]
    ra = _band_db(a, sra, REF_LO, REF_HI)
    rb = _band_db(b, srb, REF_LO, REF_HI)
    pa = _band_db(a, sra, BAND_LO, BAND_HI)
    pb = _band_db(b, srb, BAND_LO, BAND_HI)
    if None in (ra, rb, pa, pb):
        return None
    # 相对各自中频的"临场感高度"
    ha, hb = pa - ra, pb - rb
    return {"dst_db": round(float(ha), 2), "src_db": round(float(hb), 2),
            "deficit_db": round(float(hb - ha), 2)}


def _boost(x, sr, db):
    """2~5kHz 余弦凹陷**反向**（= 提）。等响还原，别让"更响"盖过"更好听"。"""
    n = len(x)
    nfft = 1 << int(np.ceil(np.log2(max(n, 2))))
    X = np.fft.rfft(x, nfft)
    f = np.fft.rfftfreq(nfft, 1.0 / float(sr))
    g = np.ones_like(f)
    m = (f >= BAND_LO) & (f <= BAND_HI)
    if not m.any():
        return x
    t = (f[m] - BAND_LO) / max(BAND_HI - BAND_LO, 1e-9)
    # 硬边会出金属声 ⇒ 余弦窗（边缘 0dB、正中 db）
    g[m] = 10.0 ** ((db * 0.5 * (1.0 - np.cos(2.0 * np.pi * t))) / 20.0)
    y = np.fft.irfft(X * g, nfft)[:n]
    r0 = float(np.sqrt((x ** 2).mean())) + 1e-12
    r1 = float(np.sqrt((y ** 2).mean())) + 1e-12
    return y * (r0 / r1)


def _dump(path, obj):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
    except Exception as e:
        sys.stderr.write("PRES 报告写不了：%s\n" % e)


def cmd_auto(dst, src, out_wav, out_json, max_db=None):
    res = {"ok": False, "applied": False, "tried": []}
    cap = float(max_db) if max_db else MAX_DOSE_DB
    m = measure(dst, src)
    if not m:
        _dump(out_json, dict(res, reason="量不出有效频谱"))
        sys.stdout.write("PRES_FAIL 量不出有效频谱\n")
        return 1
    res["measure"] = m
    d = float(m["deficit_db"])
    if d < MIN_DEFICIT_DB:
        # 不缺就不动 —— 跟 hf 的 auto 档同一个约定：SKIP 是**正常结果**
        res["ok"] = True
        res["reason"] = "不缺临场感（缺口 %.1f dB < %.1f）" % (d, MIN_DEFICIT_DB)
        _dump(out_json, res)
        sys.stdout.write("PRES_SKIP deficit=%.1f min=%.1f\n" % (d, MIN_DEFICIT_DB))
        return 0

    try:
        x, sr = _read(dst)
    except Exception as e:
        _dump(out_json, dict(res, reason="读不了成品：%s" % e))
        sys.stdout.write("PRES_FAIL 读不了成品\n")
        return 1

    # ★ 闭环：从全量往下试，补完**复测**，第一个"缺口真的缩小了"的剂量就用它。
    #   不许只看"补了没有" —— 补了但没变好等于白补（铁律 24d）。
    dose0 = min(d * DOSE_GAIN, cap)
    for dose in (dose0, dose0 * 0.7, dose0 * 0.45):
        if dose < 0.3:
            break
        y = _boost(x, sr, float(dose))
        tmp = out_wav + ".try.wav"
        try:
            sf.write(tmp, y.astype(np.float32), sr, subtype="PCM_16")
            m2 = measure(tmp, src)
        finally:
            with open(os.devnull, "w"):
                pass
            try:
                os.remove(tmp)
            except OSError:
                pass
        after = float(m2["deficit_db"]) if m2 else None
        res["tried"].append({"dose_db": round(float(dose), 2),
                             "after_deficit_db": after})
        if m2 and after < d - 0.2:          # 真的补进去了才算数
            sf.write(out_wav, y.astype(np.float32), sr, subtype="PCM_16")
            res.update({"ok": True, "applied": True,
                        "dose_db": round(float(dose), 2),
                        "after_deficit_db": after,
                        "reason": "补回 %.1f dB 临场感（缺口 %.1f → %.1f）"
                                  % (dose, d, after)})
            _dump(out_json, res)
            sys.stdout.write("PRES_AUTO applied=1 dose=%.1f deficit=%.1f->%.1f\n"
                             % (dose, d, after))
            return 0

    res["ok"] = True
    res["reason"] = ("补了但缺口没缩小（试了 %d 档）⇒ 不改动成品"
                     % len(res["tried"]))
    _dump(out_json, res)
    sys.stdout.write("PRES_SKIP no_gain tried=%d\n" % len(res["tried"]))
    return 0


def _opts(argv):
    o = {}
    i = 0
    while i < len(argv):
        if argv[i] == "--max-db" and i + 1 < len(argv):
            o["max_db"] = argv[i + 1]
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
            sys.stdout.write("PRES_FAIL 量不出有效频谱\n")
            return 1
        sys.stdout.write("PRES_MEASURE deficit=%.1f dst=%.1f src=%.1f\n"
                         % (m["deficit_db"], m["dst_db"], m["src_db"]))
        return 0
    if len(a) >= 5 and a[0] == "auto":
        return cmd_auto(a[1], a[2], a[3], a[4], **_opts(a[5:]))
    sys.stdout.write("usage: presence_cli.py measure <成品> <原唱> <out.json>\n"
                     "       presence_cli.py auto <成品> <原唱> <out.wav> "
                     "<out.json> [--max-db N]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
