# -*- coding: utf-8 -*-
"""tools/probe_degrade.py —— 回答「唱歌越唱越差、特别是破音后」。

为什么要有这个东西：
    老板的听感是"前面还行，破音之后就崩了"。这句话有三个可能的技术来源，
    光靠耳朵分不出来，必须把**同一时间点**的源和成品摆在一起量：

      A. 源（分离出来的干声）在那段本身就有漏电/残留伴奏 ⇒ 不是 RVC 的锅；
      B. RVC 在那段产生了 artifacts（谐波性崩掉/噪声底升高）⇒ RVC 的锅；
      C. 分片（RVC_CHUNK_SEC=20）边界处的拼接缺陷 ⇒ 会呈现 20 秒周期性。

    本探针不猜，只量：逐窗出一套「源 / 成品 / 差值」的客观指标，让人自己看。

量什么（每个指标都为上面某一个假设服务）：
    rms_db   —— 能量。突然掉到很低 = 漏的句子；突然抬高 = 爆。
    clip     —— 削波率（|x| >= 0.99 的样本占比）。>0 就是数字过载，听感"劈"。
    crest    —— 波峰因数 peak/rms（dB）。**失真会把峰值压平 ⇒ crest 掉**，
                这是"糊/闷/像被限幅"的直接证据，且不受音量归一化影响。
    hnr_db   —— 谐波噪声比（自相关法，只统计有声帧）。**越低 = 沙哑/撕裂/电音感**。
                这是本探针的主指标：破音在声学上就是谐波结构解体。
    flat     —— 谱平坦度均值。越接近 1 越像白噪声 ⇒ 底噪糊、齿音发毛。

用法：
    python tools/probe_degrade.py <源vocal.wav> <成品voice.wav> [--win 5] [--csv out.csv]

输出：逐窗表格 + 结论段（自动判断 A/B/C 哪个更可能）。
"""

import argparse
import csv
import os
import sys

import numpy as np
import soundfile as sf


# ---------------------------------------------------------------- 读音频
def load_mono(path):
    x, sr = sf.read(path, dtype="float32", always_2d=False)
    if x.ndim > 1:
        x = x.mean(axis=1)
    return np.asarray(x, dtype=np.float64), int(sr)


def _frames(x, sr, win_ms=25.0, hop_ms=10.0):
    n = int(sr * win_ms / 1000.0)
    h = int(sr * hop_ms / 1000.0)
    if len(x) < n:
        return np.zeros((0, n))
    idx = np.arange(0, len(x) - n, h)
    return np.stack([x[i:i + n] for i in idx])


def hnr_db(x, sr, fmin=50.0, fmax=500.0):
    """自相关法谐波噪声比（dB）。只算有声帧，返回 (均值, 有声帧数, 总帧数)。

    公式：HNR = 10*log10( r(t0) / (1 - r(t0)) )，r(t0) 是 lag 落在 f0 区间内的
    归一化自相关峰值。这个近似在多篇嗓音分析文献里用，够分辨"干净 vs 撕裂"。
    """
    fr = _frames(x, sr)
    if fr.shape[0] == 0:
        return None, 0, 0
    lag_min = max(2, int(sr / fmax))
    lag_max = min(fr.shape[1] - 1, int(sr / fmin))
    if lag_max <= lag_min:
        return None, 0, 0
    vals = []
    n_voiced = 0
    for f in fr:
        f = f - f.mean()
        e = float(np.dot(f, f))
        if e <= 1e-12:
            continue
        # 只有能量够的帧才算（静音帧的 HNR 没有意义，会把均值拉飞）
        if np.sqrt(e / len(f)) < 1e-3:
            continue
        n_voiced += 1
        ac = np.correlate(f, f, mode="full")[len(f) - 1:]
        seg = ac[lag_min:lag_max + 1] / e
        r = float(seg.max())
        # r 必须落在 (0,1) 内才算数；>=1 说明窗口退化，跳过
        if 0.0 < r < 1.0:
            vals.append(10.0 * np.log10(r / (1.0 - r)))
    if not vals:
        return None, n_voiced, fr.shape[0]
    return float(np.mean(vals)), n_voiced, fr.shape[0]


def spectral_flatness(x, sr):
    """谱平坦度均值（几何均值/算术均值）。越接近 1 越噪声化。"""
    fr = _frames(x, sr, 50.0, 25.0)
    if fr.shape[0] == 0:
        return None
    w = np.hanning(fr.shape[1]).astype(np.float64)
    out = []
    for f in fr:
        sp = np.abs(np.fft.rfft(f * w)) + 1e-12
        gm = float(np.exp(np.mean(np.log(sp))))
        am = float(np.mean(sp))
        if am > 0:
            out.append(gm / am)
    return float(np.mean(out)) if out else None


def metrics(x, sr):
    """一窗音频的全部指标。"""
    if len(x) < sr * 0.05:
        return None
    rms = float(np.sqrt(np.mean(x * x)))
    peak = float(np.max(np.abs(x)))
    clip = float(np.mean(np.abs(x) >= 0.99)) if len(x) else 0.0
    crest = (20.0 * np.log10(peak / rms)) if (rms > 1e-9 and peak > 1e-9) else None
    h, nv, nf = hnr_db(x, sr)
    return {
        "rms_db": (20.0 * np.log10(rms + 1e-12)),
        "peak": peak,
        "clip": clip,
        "crest_db": crest,
        "hnr_db": h,
        "flat": spectral_flatness(x, sr),
        "voiced": nv,
        "frames": nf,
    }


# ---------------------------------------------------------------- 主流程
def scan(path, sr_target, win, hop):
    x, sr = load_mono(path)
    rows = []
    n = len(x)
    step = int(sr * hop)
    w = int(sr * win)
    if n < w:
        return rows, n, sr
    for i in range(0, n - w + 1, step):
        m = metrics(x[i:i + w], sr)
        if m:
            m["at"] = round(i / float(sr), 2)
            rows.append(m)
    return rows, n, sr


def f(v, nd=2, dash="-"):
    return dash if v is None else ("%.*f" % (nd, v))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="源干声 vocal.wav")
    ap.add_argument("dst", help="成品 voice.wav")
    ap.add_argument("--win", type=float, default=5.0, help="窗长秒（默认 5）")
    ap.add_argument("--hop", type=float, default=5.0, help="步进秒（默认 5）")
    ap.add_argument("--csv", default="", help="可选：导出 CSV")
    a = ap.parse_args()

    for p in (a.src, a.dst):
        if not os.path.isfile(p):
            print("文件不存在：%s" % p)
            return 2

    srows, sn, ssr = scan(a.src, None, a.win, a.hop)
    drows, dn, dsr = scan(a.dst, None, a.win, a.hop)
    print("源   %s：%.1f 秒 @%d Hz，%d 窗" % (os.path.basename(a.src), sn / ssr, ssr, len(srows)))
    print("成品 %s：%.1f 秒 @%d Hz，%d 窗" % (os.path.basename(a.dst), dn / dsr, dsr, len(drows)))
    if not srows or not drows:
        print("窗数为 0 —— 音频太短，换个 --win")
        return 2

    n = min(len(srows), len(drows))
    print("\n%-7s | %-26s | %-26s | %s" % ("at(s)", "源   rms/clip/crest/hnr", "成品 rms/clip/crest/hnr", "Δhnr"))
    print("-" * 96)

    table = []
    for i in range(n):
        s, d = srows[i], drows[i]
        dh = (d["hnr_db"] - s["hnr_db"]) if (d["hnr_db"] is not None and s["hnr_db"] is not None) else None
        table.append({"at": s["at"], "s_rms": s["rms_db"], "d_rms": d["rms_db"],
                      "s_clip": s["clip"], "d_clip": d["clip"],
                      "s_crest": s["crest_db"], "d_crest": d["crest_db"],
                      "s_hnr": s["hnr_db"], "d_hnr": d["hnr_db"],
                      "s_flat": s["flat"], "d_flat": d["flat"], "d_hnr": dh})
        print("%-7s | %6s %5s %6s %7s | %6s %5s %6s %7s | %6s" % (
            f(s["at"], 1),
            f(s["rms_db"], 1), "%.3f" % s["clip"], f(s["crest_db"], 1), f(s["hnr_db"], 1),
            f(d["rms_db"], 1), "%.3f" % d["clip"], f(d["crest_db"], 1), f(d["hnr_db"], 1),
            f(dh, 1)))

    # ---------------------------------------------------------- 结论
    print("\n" + "=" * 96)
    print("结论（自动判断，都是可复核的算术）")
    print("=" * 96)

    def col(k, rng=None):
        return [r[k] for r in (rng or table) if r.get(k) is not None]

    # 1) 整体：成品相对源损失了多少谐波性
    dhs = col("d_hnr")
    if dhs:
        print("1. 谐波性损失 Δhnr：均值 %.1f dB，最差 %.1f dB @%.0fs，最好 %.1f dB"
              % (np.mean(dhs), min(dhs),
                 table[[i for i, r in enumerate(table) if r.get("d_hnr") is not None][int(np.argmin(dhs))]]["at"],
                 max(dhs)))
        bad = [r for r in table if (r.get("d_hnr") is not None and r["d_hnr"] < -6.0)]
        print("   Δhnr < -6 dB 的窗：%d / %d" % (len(bad), len(dhs)))

    # 2) 时间趋势：后半 vs 前半
    half = len(table) // 2
    for name, key in (("成品 hnr", "d_hnr"), ("源 hnr", "s_hnr"), ("成品 crest", "d_crest")):
        vals = col(key)
        if len(vals) < 4:
            continue
        A = [r[key] for r in table[:half] if r.get(key) is not None]
        B = [r[key] for r in table[half:] if r.get(key) is not None]
        if not A or not B:
            continue
        print("2. %s：前半均值 %.1f → 后半均值 %.1f（差 %.1f）"
              % (name, np.mean(A), np.mean(B), np.mean(B) - np.mean(A)))

    # 3) 有没有「劣化拐点」—— 找第一个明显变差的窗，看它之后是不是回不来
    if dhs:
        idx = None
        for i, r in enumerate(table):
            if r.get("d_hnr") is not None and r["d_hnr"] < -6.0:
                idx = i
                break
        if idx is not None:
            before = [r["d_hnr"] for r in table[:idx] if r.get("d_hnr") is not None]
            after = [r["d_hnr"] for r in table[idx:] if r.get("d_hnr") is not None]
            print("3. 第一个 Δhnr<-6 的窗在 %.0fs：此前 %d 窗均值 %.1f，此后 %d 窗均值 %.1f"
                  % (table[idx]["at"], len(before), np.mean(before) if before else float("nan"),
                     len(after), np.mean(after) if after else float("nan")))
        else:
            print("3. 没有出现 Δhnr < -6 dB 的窗 ⇒ 成品没有明显「撕裂/电音」 artifacts")

    # 4) 是否随 20 秒分片周期性反复（分片的锅会有这个形状）
    if len(table) >= 8 and a.hop <= 5.0:
        by20 = {}
        for r in table:
            k = int(r["at"]) % 20
            if r.get("d_hnr") is not None:
                by20.setdefault(k // 5, []).append(r["d_hnr"])
        if len(by20) >= 3:
            print("4. 按「片内第几个 5 秒」分组的 Δhnr 均值（若分片有问题，会呈现单调下降再复位）：")
            for k in sorted(by20):
                print("     片内第 %d 个 5 秒：%.1f dB（n=%d）" % (k + 1, np.mean(by20[k]), len(by20[k])))

    # 5) 削波
    cl = [r for r in table if r["d_clip"] > 0.0005]
    print("5. 成品削波 > 0.05%% 的窗：%d 个%s"
          % (len(cl), ("，最重 %.3f%% @%.0fs" % (
              max(r["d_clip"] for r in cl) * 100,
              max(cl, key=lambda r: r["d_clip"])["at"])) if cl else ""))

    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8-sig") as fp:
            wcsv = csv.DictWriter(fp, fieldnames=list(table[0].keys()))
            wcsv.writeheader()
            for r in table:
                wcsv.writerow(r)
        print("\nCSV 已写：%s" % a.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
