#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""为什么写它
-----------
「越唱越差」落到体检表上就是 devIQR 变大，但 IQR 只是一个数字，看不出
**坏帧长什么样**。而修法完全取决于它：
    · 坏帧是整体偏了半个八度  ⇒ 是错认八度，可以**后处理修正**（>±550 音分翻转回来）
    · 坏帧是随机散布在 ±50~200 ⇒ 是真抖动，后处理救不了，得改生成
    · 坏帧集中在少数几帧       ⇒ 可能是 unvoiced 边界误判，改提取/平滑即可
本探针把逐帧偏差分桶，并沿时间铺开，让上面三条能一次分开。

用法（cwd 必须是 RVC 根目录，RMVPE 权重按相对路径找）：
    ./runtime/python.exe -s <本脚本> <源.wav> <成品.wav> [pitch_semis] [win] [json]
"""
import json
import os
import sys

import numpy as np

os.environ.setdefault("rmvpe_root", os.path.join(os.getcwd(), "assets", "rmvpe"))
if os.getcwd() not in sys.path:
    sys.path.insert(0, os.getcwd())

import librosa  # noqa: E402
import torch  # noqa: E402
from infer.rmvpe import RMVPE  # noqa: E402

SR = 16000


def cents_hist(dev):
    """偏差绝对值 → 分桶占比（%）。单位是音分。"""
    v = np.abs(np.asarray(dev, dtype="float64"))
    if v.size == 0:
        return {}
    tot = v.size
    bins = [("  <25c", v < 25), ("25-50c", (v >= 25) & (v < 50)),
            ("50-100c", (v >= 50) & (v < 100)), ("100-200c", (v >= 100) & (v < 200)),
            ("200-550c", (v >= 200) & (v < 550)), (">=550c(八度)", v >= 550)]
    return {k: 100.0 * float(np.count_nonzero(m)) / tot for k, m in bins}


def best_align(cents, max_lag_frames=20):
    """挑一个整段平移让 |偏差| 中位最小 —— 和 qc_cli 同一个口径。

    ★ 只用 **发声帧**（有限值）来挑，且平移用**截断**不是 np.roll：
      1) roll 是循环移位，会把末尾那一 20 帧搬到开头，偏差瞬间变成几百音分，
         整段统计被这几帧拖着走（实测因此硬生生多出 2% 的假"八度错"）；
      2) unvoiced 帧的值是 nan，roll 会把 nan 挪进来污染中位数。
    """
    seq = np.asarray(cents, dtype="float64")
    best, bl = np.nanmedian(np.abs(seq)), 0
    for lg in range(-max_lag_frames, max_lag_frames + 1):
        if lg < 0:
            c = seq[-lg:]
        elif lg > 0:
            c = seq[:len(seq) - lg]
        else:
            c = seq
        v = np.nanmedian(np.abs(c))
        if np.isfinite(v) and v < best:
            best, bl = v, lg
    if bl < 0:
        return np.concatenate([seq[-bl:], np.full(-bl, np.nan)]), bl
    if bl > 0:
        return np.concatenate([np.full(bl, np.nan), seq[:len(seq) - bl]]), bl
    return seq, bl


def main():
    src, dst = sys.argv[1], sys.argv[2]
    semis = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
    win = float(sys.argv[4]) if len(sys.argv) > 4 else 30.0
    out = sys.argv[5] if len(sys.argv) > 5 else ""

    dev = "cuda:0" if torch.cuda.is_available() else "cpu"
    ft = os.path.join(os.environ["rmvpe_root"], "rmvpe.pt")
    print("设备 %s | 加载 RMVPE…" % dev)
    sys.stdout.flush()
    m = RMVPE(ft, is_half=(dev != "cpu"), device=dev)

    ys, _ = librosa.load(src, sr=SR, mono=True)
    yd, _ = librosa.load(dst, sr=SR, mono=True)
    ys = np.asarray(ys, dtype=np.float32)
    yd = np.asarray(yd, dtype=np.float32)
    n = min(len(ys), len(yd))
    ys, yd = ys[:n], yd[:n]
    total = n / float(SR)
    print("比对 %.1f 秒（pitch=%+g ⇒ 预期恒定偏差 %+.0f 音分）"
          % (total, semis, semis * 100))
    sys.stdout.flush()

    fs = np.asarray(m.infer_from_audio(ys, thred=0.03), dtype="float64").reshape(-1)
    fd = np.asarray(m.infer_from_audio(yd, thred=0.03), dtype="float64").reshape(-1)
    nf = min(len(fs), len(fd))
    fs, fd = fs[:nf], fd[:nf]
    hop = total / max(nf, 1)

    ok = (fs > 20) & (fd > 20)
    # ★ unvoiced 帧必须是 nan，**不能赋 0**：赋 0 的话后面减掉 pitch 的 300 音分，
    #   这些帧会整整齐齐落在 -300 音分那一档，把分布图完全带偏。
    cents = np.where(ok, 1200.0 * np.log2(np.where(ok, fd, 1) / np.where(ok, fs, 1)),
                     np.nan)

    print("\n%s" % ("=" * 92))
    print("逐窗偏差分布（%%）；「>=550c」= 认错八度。")
    print("=" * 92)
    hdr = ("%-16s" % "at(s)") + "".join(["%13s" % k for k in
           ("  <25c", "25-50c", "50-100c", "100-200c", "200-550c", ">=550c(八度)")])
    print(hdr)
    print("-" * 92)
    rows = []
    t0 = 0.0
    while t0 + win <= total:
        sl = slice(int(t0 / hop), int((t0 + win) / hop))
        c = cents[sl]
        # ★ 去中心用**实测中位数**，不要信命令行传进来的 pitch：
        #   一键唱最后用的 pitch 是「音高体检」选出来的档，常常和 rvc_infer
        #   的入参不是一回事（实测这首歌 RVC 传的是 +3，成品相对源却是 0 半音）。
        #   按 pitch 硬扣会凭空制造一个恒定偏置，把所有帧推进同一只桶里。
        raw_med = float(np.nanmedian(c)) if np.isfinite(np.nanmedian(c)) else float("nan")
        resid = c - raw_med
        resid = resid[np.isfinite(resid)]  # ★ 丢掉 unvoiced，别让它混进分布
        resid, _ = best_align(resid)
        resid = resid[np.isfinite(resid)]
        if resid.size < 30:
            print("%-16s" % ("%.0f-%.0f" % (t0, t0 + win)) + "%72s" % "（发声帧不足）")
            t0 += win
            continue
        h = cents_hist(resid)
        rows.append({"at": round(t0, 1), "raw_med": raw_med,
                     "resid_med": float(np.nanmedian(resid)), "n": int(resid.size), **h})
        print("%-16s" % ("%.0f-%.0f" % (t0, t0 + win))
              + "".join(["%12.1f%%" % h.get(k, 0.0) for k in
                         ("  <25c", "25-50c", "50-100c", "100-200c",
                          "200-550c", ">=550c(八度)")])
              + "   |原始中位 %+7.1fc / 残差中位 %+7.1fc / n=%d"
              % (raw_med, float(np.nanmedian(resid)), resid.size))
        t0 += win

    if out:
        json.dump({"rows": rows, "win": win}, open(out, "w", encoding="utf-8"),
                  ensure_ascii=False)
        print("\n写到 %s" % out)
    print("DONE")


if __name__ == "__main__":
    main()
