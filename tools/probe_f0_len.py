#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""为什么写它
-----------
修完「输出比输入短 80ms」之后，老板试听说**还是**越唱越差。那条已经不是时长问题了
（新成品时长已经和源逐样本对齐）。

剩下的最大嫌疑是 **RMVPE 在长音频上提不准 f0**：
  单独跑 tail(170-204s) devIQR 7.1  |  同一段在整首里 54.0
  单独跑 head(0-34s)    devIQR 10.0 |  同一段在整首里 10.3   ← 前段几乎没差！
「位置越靠后、差异越大」+「前面的不受影响」 ⇒ 像是**逐帧/逐窗累积**的东西，
而 RMVPE 最后一步是 Viterbi 解码，长序列上本来就会越走越偏。

本探针做的事（纯 CPU，不占显卡）：
  对同一份干声，分别在「整首」和「切成若干短段」两种喂法下提 f0，
  比较重叠区里两条 f0 曲线的差异如何随位置变化。

用法：在 RVC 根目录下用它的解释器跑（RMVPE 权重按相对路径找）
  cd /d/RVC/Retrieval-based-Voice-Conversion-WebUI
  ./runtime/python.exe -s D:/AHC/VoiceStudio/tools/probe_f0_len.py <vocal.wav> [json输出]
"""
import json
import os
import sys

import numpy as np

# ---- RMVPE（和 stages/qc_cli.py 完全同一套加载姿势）----
os.environ.setdefault("rmvpe_root", os.path.join(os.getcwd(), "assets", "rmvpe"))
if os.getcwd() not in sys.path:
    sys.path.insert(0, os.getcwd())

import librosa  # noqa: E402
import torch  # noqa: E402
from infer.rmvpe import RMVPE  # noqa: E402

SR = 16000
SEG = 30.0        # 「短段」喂法的段长（秒）
STEP = 3.0        # 统计步长
WIN = 6.0         # 统计窗口


def f0_np(f0):
    """把 f0 序列里 <=0 的帧当静音（RMVPE 用 0 表示 unvoiced）。"""
    a = np.asarray(f0, dtype="float64").reshape(-1)
    return np.where(a > 0, a, 0.0)


def to_cents(a, b):
    """两条同长 f0 序列 → 逐帧音分差（只比都发声的帧）。"""
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    ok = (a > 20) & (b > 20)
    if int(ok.sum()) < 30:
        return None
    c = 1200.0 * np.log2(np.where(ok, b, 1.0) / np.where(ok, a, 1.0))
    v = np.abs(c[ok])
    # 「崩掉」的帧 = 偏了一个半音以上（认错附近半音 / 八度）
    return {"n": int(ok.sum()), "med": float(np.median(v)),
            "p75": float(np.percentile(v, 75)),
            "oct": float(np.mean(v > 550)), "cent_p90": float(np.percentile(v, 90))}


def jit_local(f0):
    """相邻帧的音分跳幅中位数 —— 衡量「抖得厉不厉害」，不需要参考真值。"""
    a = np.asarray(f0, dtype="float64").reshape(-1)
    ok = a > 20
    if int(ok.sum()) < 50:
        return float("nan")
    idx = np.where(ok)[0]
    # 只统计连续发声的相邻帧跨步
    step = np.diff(idx)
    pairs = (a[idx[:-1]] > 20) & (a[idx[1:]] > 20) & (step <= 2)
    if int(pairs.sum()) < 30:
        return float("nan")
    d = np.abs(1200.0 * np.log2(a[idx[1:]][pairs] / a[idx[:-1]][pairs]))
    return float(np.median(d))


def main():
    src = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else ""

    y, _ = librosa.load(src, sr=SR, mono=True)
    y = np.asarray(y, dtype=np.float32)
    total = len(y) / float(SR)
    print("音频 %.3f 秒 / %d 样本" % (total, len(y)))

    dev = "cuda:0" if torch.cuda.is_available() else "cpu"
    ft = os.path.join(os.environ["rmvpe_root"], "rmvpe.pt")
    print("设备 %s | 加载 RMVPE…" % dev)
    sys.stdout.flush()
    m = RMVPE(ft, is_half=(dev != "cpu"), device=dev)

    print("① 整首一次提 f0（%.1f 秒一次喂进去）…" % total)
    sys.stdout.flush()
    f0_full = f0_np(m.infer_from_audio(y, thred=0.03))
    hop_sec = total / max(len(f0_full), 1)
    print("   得到 %d 帧，帧长 %.2f ms" % (len(f0_full), hop_sec * 1000))

    print("② 分段提 f0（每 %.0f 秒独立提，再拼起来）…" % SEG)
    sys.stdout.flush()
    pieces = []
    t = 0.0
    while t < total - 1e-6:
        seg = y[int(t * SR):int(min(total, t + SEG) * SR)]
        if len(seg) < SR:
            break
        pieces.append(f0_np(m.infer_from_audio(seg, thred=0.03)))
        t += SEG
    f0_seg = np.concatenate(pieces) if pieces else np.zeros(1)
    print("   %d 段拼起来 %d 帧" % (len(pieces), len(f0_seg)))

    n = min(len(f0_full), len(f0_seg))
    f0_full, f0_seg = f0_full[:n], f0_seg[:n]
    t_axis = np.arange(n) * hop_sec

    print("\n=== 两种喂法的 f0 差异，沿时间走 ===")
    print("%-8s %6s %10s %10s %9s %11s %11s"
          % ("at(s)", "帧数", "|Δ|中位", "|Δ|p90", "崩帧%", "整首抖动", "分段抖动"))
    print("-" * 66)
    rows = []
    t0 = 0.0
    while t0 + WIN <= total:
        sl = (t_axis >= t0) & (t_axis < t0 + WIN)
        r = to_cents(f0_full[sl], f0_seg[sl])
        if r:
            jf = jit_local(f0_full[sl])
            js = jit_local(f0_seg[sl])
            rows.append({"at": t0, **r, "jit_full": jf, "jit_seg": js})
            print("%-8.1f %6d %10.1f %10.1f %8.1f%% %11.1f %11.1f"
                  % (t0, r["n"], r["med"], r["cent_p90"], r["oct"] * 100, jf, js))
        t0 += STEP

    if rows:
        meds = [r["med"] for r in rows]
        jf = [r["jit_full"] for r in rows if not np.isnan(r["jit_full"])]
        ats = [r["at"] for r in rows]
        rho = float(np.corrcoef(ats, meds)[0, 1])
        print("\n★ 位置 vs |Δ| 相关 rho = %+.3f" % rho)
        print("  首窗 %.1f 音分 → 末窗 %.1f 音分" % (meds[0], meds[-1]))
        print("  抖动（不需要参考真值、不会被错位骗）整首喂 vs 分段喂：")
        jv = [abs(r["jit_full"] - r["jit_seg"]) for r in rows
              if not np.isnan(r["jit_full"]) and not np.isnan(r["jit_seg"])]
        if jv:
            print("    逐窗差值 最大 %.2f / 中位 %.2f 音分/帧"
                  % (max(jv), float(np.median(jv))))

        # ★★ 别拿 |Δ| 当结论 —— 它是陷阱。
        #   两条 f0 序列帧数不同（分段提时每段自带 pad），按索引硬对齐会
        #   **随时间累积错位** ⇒ |Δ| 必然单调上涨，跟 f0 准不准无关。
        #   真正不受错位影响的是「局部抖动」（相邻帧跳幅），它不需要任何对齐。
        print("\n  ⚠ |Δ| 这一列**不可信**：分段拼接后帧数与整首不一致，")
        print("    直接按下标对齐会越往后错得越多，rho>0 是几何假象。")
        print("    判断 f0 有没有随位置劣化，只看「抖动」两列是否分岔。")
        if jv and max(jv) < 1.0:
            print("  ⇒ **f0 提取没有位置效应**（两种喂法抖动一致）⇒ 不是 RMVPE 的锅，")
            print("     越唱越差要往别处找：分片拼接 / 合成阶段 / 能量对齐。")
        elif jv:
            print("  ⇒ 两种喂法的抖动有明显分岔，值得继续挖 f0 这一层。")
        else:
            print("  ⇒ 抖动数据不足，无法判断。")

    if out:
        json.dump({"rows": rows, "hop_sec": hop_sec,
                   "total": total, "seg": SEG}, open(out, "w", encoding="utf-8"),
                  ensure_ascii=False)
        print("\n写到 %s" % out)
    print("DONE")


if __name__ == "__main__":
    main()
