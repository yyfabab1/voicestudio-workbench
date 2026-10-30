# -*- coding: utf-8 -*-
r"""tools/probe_leak_timeline.py —— 逐窗量「分离出来的干声里混进了多少伴奏」。

为什么会有这个探针：
    「越唱越差」查了两轮，两个热闹的假设都被否掉了 ——
      · 不是音高、` argparse.Namespace(); proibe_degrade 也已证明不是失真 artifacts；
      · 同一首歌 pitch=0 和 pitch=+3 两个版本，偏差曲线**逐窗重合** ⇒ 与 pitch 无关，
        而且两条共享同一个源 ⇒ 差异是**源驱动**的，不是模型跑坏了。
    剩下最合理的嫌疑人：**漏电**。分离模型不可能完美，总有伴奏残留在人声轨里。
    而「漏电」在一首歌里从来不是均匀的 —— 副歌鼓贝斯一上来，残留必然变多。
    RVC 把残留的乐器当人声处理 ⇒ 基频被拽偏、抽出不该有的音 ⇒ 听感就是"飘/破音"。

怎么量（不需要重跑分离，只需要它已經產出的 vocal / inst 两条轨）：
    逐窗（默认 10 秒）算 vocal 与 inst 的**归一化相关**：
        rho = <v, i>^2 / (<v,v> * <i,i>)
    这就是「inst 能线性解释 vocal 的那部分能量占比」，值域 [0,1]，越大越漏电。
    为了不被"整窗都安静"污染，额外报两者的 rms。

用法（纯 CPU）：
    python tools/probe_leak_timeline.py <vocal.wav> <inst.wav> [--win 10] [--csv out.csv]

退出码：0 成功；2 参数错误。
"""
import argparse
import csv
import os
import sys

import numpy as np
import soundfile as sf


def load_mono(path):
    x, sr = sf.read(path, dtype="float32", always_2d=False)
    if x.ndim > 1:
        x = x.mean(axis=1)
    return np.asarray(x, dtype=np.float64), int(sr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vocal")
    ap.add_argument("inst")
    ap.add_argument("--win", type=float, default=10.0)
    ap.add_argument("--csv", default="")
    a = ap.parse_args()
    for p in (a.vocal, a.inst):
        if not os.path.isfile(p):
            print("不存在：%s" % p)
            return 2

    v, srv = load_mono(a.vocal)
    i, sri = load_mono(a.inst)
    n = min(len(v), len(i))
    v, i = v[:n], i[:n]
    sr = min(srv, sri)
    total = n / sr
    print(" vocal %.1f 秒 @%d | inst %.1f 秒 @%d" % (len(v) / srv, srv, len(i) / sri, sri))
    print(" 窗长 %.0f 秒\n" % a.win)

    w = int(sr * a.win)
    rows = []
    t = 0.0
    while t + a.win <= total:
        i0 = int(t * sr)
        a1 = v[i0:i0 + w]
        b1 = i[i0:i0 + w]
        if len(a1) < sr:
            break
        ev = float(np.dot(a1, a1))
        ei = float(np.dot(b1, b1))
        if ev > 1e-12 and ei > 1e-12:
            cross = float(np.dot(a1, b1))
            rho = (cross * cross) / (ev * ei)
        else:
            rho = 0.0
        rows.append({"at": round(t, 1),
                     "leak": rho,
                     "v_rms_db": 20.0 * np.log10(np.sqrt(ev / len(a1)) + 1e-12),
                     "i_rms_db": 20.0 * np.log10(np.sqrt(ei / len(b1)) + 1e-12)})
        t += a.win

    print("%-8s %10s %12s %12s" % ("at(s)", "漏电rho", "vocal rms", "inst rms"))
    print("-" * 46)
    for r in rows:
        print("%-8s %9.4f%% %11.1f %11.1f" % (r["at"], r["leak"] * 100, r["v_rms_db"], r["i_rms_db"]))

    lk = [r["leak"] for r in rows]
    half = len(rows) // 2
    print("\n前半漏电均值 %.3f%% → 后半 %.3f%%（×%.2f）"
          % (np.mean(lk[:half]) * 100, np.mean(lk[half:]) * 100,
             np.mean(lk[half:]) / max(np.mean(lk[:half]), 1e-12)))
    worst = max(rows, key=lambda r: r["leak"])
    print("最脏的一窗 @%.0fs：%.3f%%（vocal %.1f dB / inst %.1f dB）"
          % (worst["at"], worst["leak"] * 100, worst["v_rms_db"], worst["i_rms_db"]))

    # ★ 把漏电曲线和已知的「偏差变飘」曲线摆在一起看，才知道是不是同一个形状
    print("\n（对照：这首歌的成品在第 100 秒开始破音率翻倍，")
    print(" 偏差 IQR 从 ~10 cents 爬到 ~88 cents。漏电若也在同一段抬头，就对上了。）")

    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8-sig") as fp:
            wr = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
            wr.writeheader()
            for r in rows:
                wr.writerow(r)
        print("\nCSV 已写：%s" % a.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
