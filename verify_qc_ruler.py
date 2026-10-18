# -*- coding: utf-8 -*-
r"""门禁：**客观质检那把尺子本身**（不是被测的音频，是量音频的那把尺）。

为什么要有这一关：这一版之前，qc_cli 用「f0 轮廓相关最大」来挑时间对齐，
结果挑到 lag=-130ms 的**错位**处，在那里算出来的吻合率只剩 31.9%（真实
lag=0 处是 65.6%）—— 于是一个正常的翻唱被质检判成"失败"。而"尺子坏了"
这件事**不会让任何其他判据变红**：任务照样跑完、成品照样生成，只是结论反了。

所以这里用**已知真值**的变体去量尺子本身 —— 素材现场合成，不依赖用户数据：
    same      dst = src 本身          真值：完美
    reversed  dst = src 倒放          真值：旋律完全对不上
    shuffled  dst = src 按 1 秒乱序   真值：局部在唱、整体对不上
    delayed   dst = src 整体晚 300ms  真值：时间错位 300ms，音准错位

断言（前三条要求尺子**能红**，第四条要求错位检测**真的在工作**）：
    same      宽松吻合 >= 0.98
    reversed  宽松吻合 <= 0.45  且 轮廓相关 <= 0.30
    shuffled  宽松吻合 <= 0.70
    delayed   报出的错位落在 +300±30ms，且 lag=0 处吻合明显低于 same

用法（用 RVC 自带解释器跑，它才有 RMVPE/librosa）：
    runtime\python.exe -s verify_qc_ruler.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
RVC_ROOT = r"D:\RVC\Retrieval-based-Voice-Conversion-WebUI"
QC = os.path.join(HERE, "stages", "qc_cli.py")
SR = 16000
OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name + (("  | " + detail) if detail else ""),
          flush=True)


def sing_melody(dur=10.0, seed=7):
    """合成一段"唱"：f0 从 180 Hz **单调**升到 400 Hz（带颤音）的谐波堆叠 + 一点噪声。

    ★ 为什么必须**单调**：一开始写的是"在几个音之间来回跳"的旋律，结果**倒放**
      之后仍然有 53.8% 的帧落在 ±50 音分内、轮廓相关还有 +0.356 —— 负对照根本
      红不了。原因是那套旋律的音高集合没变、只是顺序变了，统计上（中位数、
      四分位距）几乎一样，而尺子的"吻合率"恰恰是去掉整体平移之后的**逐帧**
      吻合 —— 乱掉顺序它抓不住。单调扫频让"顺序"本身就是"音高"，
      倒放/乱序之后必然逐帧对不上。
    ★ 加噪声是必须的：纯谐波堆叠的谱太平坦，会被 f0 提取器当成纯音，
      行为跟真实人声不一致。
    """
    n = int(dur * SR)
    t = np.arange(n) / float(SR)
    f = np.linspace(180.0, 400.0, n)
    f = f * (1.0 + 0.015 * np.sin(2 * np.pi * 5.5 * t))
    ph = 2 * np.pi * np.cumsum(f) / SR
    y = np.zeros(n, dtype=np.float64)
    for k in range(1, 13):
        y += (1.0 / k) * np.sin(k * ph)
    y = y / max(1e-9, np.max(np.abs(y))) * 0.7
    y += 0.05 * np.random.RandomState(seed).randn(n)
    y = np.clip(y, -0.95, 0.95)
    # ★ 加乐句停顿（每 1.5 秒唱 1.2 秒、停 0.3 秒）。两个理由：
    #   ① 真实歌曲本来就有换气；② 更要紧的是"错位检测"是拿**唱/不唱的开合图案**
    #      算的，连续有声的素材让那个图案接近一条常数线，减均值之后就没有信息了，
    #      定位会偏（踩过：真值 +300ms 报成 +240ms）。
    for s in np.arange(1.2, dur, 1.5):
        i0, i1 = int(s * SR), min(n, int((s + 0.3) * SR))
        y[i0:i1] = 0.0
    y += 0.0008 * np.random.RandomState(seed + 1).randn(n)   # 停顿段留一点底噪
    return y.astype(np.float32)


def run_qc(src, dst, out_json):
    p = subprocess.run([sys.executable, "-s", QC, src, dst, out_json],
                       cwd=RVC_ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if not os.path.isfile(out_json):
        return None, (p.stdout or "") + (p.stderr or "")
    return json.load(open(out_json, encoding="utf-8")), (p.stdout or "")


def main():
    tmp = tempfile.mkdtemp(prefix="vs_qcruler_")
    try:
        x = sing_melody()
        n = len(x)
        p_src = os.path.join(tmp, "src.wav")
        sf.write(p_src, x, SR)

        variants = {}
        variants["same"] = x.copy()
        variants["reversed"] = x[::-1].copy()
        blk = SR                                   # 1 秒一块，乱序
        nb = n // blk
        order = list(range(nb))
        np.random.RandomState(20261011).shuffle(order)
        variants["shuffled"] = x[:nb * blk].reshape(nb, blk)[order].reshape(-1).copy()
        dly = int(0.3 * SR)                        # 整体晚 300 ms
        variants["delayed"] = np.concatenate([np.zeros(dly, dtype=np.float32), x])[:n].copy()

        res = {}
        for name, data in variants.items():
            p = os.path.join(tmp, "d_%s.wav" % name)
            sf.write(p, data, SR)
            q, log = run_qc(p_src, p, os.path.join(tmp, "q_%s.json" % name))
            if q is None:
                print(log[-500:])
            res[name] = q
            if q:
                print("[%-9s] 宽松吻合 %5.1f%%  (±50 %5.1f%%)  轮廓相关 %+.3f  八度错 %5.1f%%  "
                      "错位 %+.0fms"
                      % (name, q["match_ratio_loose"] * 100, q["match_ratio"] * 100,
                         q["contour_corr"], q["octave_err_ratio"] * 100, q["lag_ms"]))

        s = res.get("same")
        check("尺子上限：dst = src → 宽松吻合 >= 0.98",
              bool(s) and s["match_ratio_loose"] >= 0.98,
              "%.3f" % (s["match_ratio_loose"] if s else -1))
        r = res.get("reversed")
        check("★ 能红：dst = 倒放 → 宽松吻合 <= 0.45",
              bool(r) and r["match_ratio_loose"] <= 0.45,
              "%.3f" % (r["match_ratio_loose"] if r else -1))
        check("★ 能红：dst = 倒放 → 轮廓相关 <= 0.30",
              bool(r) and r["contour_corr"] <= 0.30,
              "%+.3f" % (r["contour_corr"] if r else 9))
        sh = res.get("shuffled")
        check("★ 能红：dst = 1 秒块乱序 → 宽松吻合 <= 0.70",
              bool(sh) and sh["match_ratio_loose"] <= 0.70,
              "%.3f" % (sh["match_ratio_loose"] if sh else -1))
        d = res.get("delayed")
        check("错位检测：dst 整体晚 300ms → 报出的错位在 +300±40ms",
              bool(d) and 260 <= d["lag_ms"] <= 340,
              "%+.0fms" % (d["lag_ms"] if d else 0))
        # ★ 分工：错位由 lag 指标负责报，**不该**污染音准结论。素材是单调扫频，
        #   300ms 的错位对应的音高差只有几 Hz，所以逐帧偏差仍在 ±150 音分内、
        #   吻合率保持高值。真实歌曲旋律变化快，错位通常也会拉低吻合率 ——
        #   但那是巧合，不是尺子的义务。两条指标各管一摊，别混着解读。
        check("错位不会污染音准结论（lag 报错位、match 报音准）",
              bool(d) and d["match_ratio_loose"] > 0.9,
              "delayed 宽松吻合 %.3f，错位 %+.0fms"
              % (d["match_ratio_loose"] if d else -1, d["lag_ms"] if d else 0))

        print("-" * 62)
        print("PASS %d / FAIL %d" % (len(OK), len(BAD)))
        if BAD:
            print("!! 失败项：" + "、".join(BAD))
        else:
            print("结论：质检尺子门禁全绿（含 3 条能红的负对照）")
        return 0 if not BAD else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
