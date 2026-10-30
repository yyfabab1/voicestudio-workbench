# -*- coding: utf-8 -*-
"""段边界接缝探针 v2：分段会不会在切点处留下可听的响度台阶。

★ v1 为什么作废
----------------
v1 直接数「相邻 50 ms 窗响度差 > 3 dB」的个数，结果是 分段 1497 处 vs
未分段 1489 处 —— 两个数几乎一样，看着像"没问题"，但这个尺子**测的是素材
本身的动态**（辅音、换气、起音在 50 ms 尺度上本来就有 ±10 dB 的跳变），
跟分段毫无关系。1489 处里随便波动 8 处就把结论淹了 ⇒ **尺子没牙**，
v1 的"没有额外接缝"不能算数。

★ v2 的做法：做差，把素材消掉
----------------
同一首歌的「分段产物」和「未分段产物」用的是**同一条素材**，所以素材引起的
响度起伏在两条曲线里是一样的。逐窗响度相减：

    δ(t) = dB_seg(t) - dB_noseg(t)      （再去掉整体常数差）

剩下的就只有「两次推理的差异」。如果分段在切点处留了台阶，δ 会在那些位置
出现**台阶状跳变**；如果分段没引入接缝，δ 应该是一条平缓的噪声带。

判据（只认台阶，不认绝对波动）：
    P1  δ 的逐点跳变 |Δδ| 的最大值 < 阈值（2.0 dB）
    P2  且这些跳变**不集中**在 30 秒整数倍附近（切点预期位置）

★ 尺子自检（必须能红）
----------------
脚本会自己造一份「在第 2 段乘 1.5 倍」的假分段产物再跑一遍 —— 那是一个
明摆着的 +3.5 dB 台阶。尺子认不出它就不配叫尺子。

用法：
    <py> -s tools\\probe_seam.py <分段产物.wav> <未分段产物.wav>
"""

import os
import sys

import numpy as np
import soundfile as sf

WIN = 1.0            # 响度窗（秒）：太短测到的是素材动态，1 秒才看得见"台阶"
HOP = 0.25
STEP_DB = 2.0        # δ（两轨响度差）的整体跨度阈值
JUMP_DB = 1.5        # δ 在 0.2 秒窗上的一步跳变阈值
NEAR = 3.5           # 「切点附近」的容差（外层切点是在 ±3 秒窗里挑能量最低处）


def db_curve(path, win=WIN, hop=HOP):
    y, sr = sf.read(path, always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)
    y = np.asarray(y, dtype="float64")
    w = int(win * sr)
    h = int(hop * sr)
    n = (len(y) - w) // h
    if n <= 1:
        raise SystemExit("音频太短：%s" % path)
    seg = np.stack([y[i * h: i * h + w] for i in range(n)])
    return 20.0 * np.log10(np.sqrt((seg ** 2).mean(axis=1)) + 1e-12), sr, hop, len(y)


def analyse(a_path, b_path, tag):
    A, sr, h, n = db_curve(a_path)
    B, _, _, _ = db_curve(b_path)
    m = min(len(A), len(B))
    A, B = A[:m], B[:m]
    d = A - B
    d = d - np.median(d)                 # 去掉整体常数差
    t = (np.arange(m) + 1) * (h / float(sr))

    # ★ 只留有声音的窗。开头/结尾的静音会让 dB 掉到 -120，两轨相减出来
    #   上百 dB 的"跳变"，那纯粹是 1e-12 底噪在打架，跟接缝毫无关系
    #   （v2 第一版就被它坑了：最大跳变 146 dB @ 0.00 秒，把真信号全盖住）。
    gate = max(A.max(), B.max()) - 40.0
    keep = (A > gate) & (B > gate)
    if keep.sum() < 20:
        raise SystemExit("有效窗太少（%d），多半是音频几乎全静音" % keep.sum())
    dv = d[keep]
    tv = t[keep]

    p5, p95 = np.percentile(dv, [5, 95])
    span = float(p95 - p5)
    off = np.where(np.abs(dv - np.median(dv)) > 1.0)[0]
    near = [float(tv[i]) for i in off
            if min(abs(tv[i] - k * 30.0) for k in range(1, 12)) < NEAR]

    print("\n=== %s ===" % tag)
    print("  有效窗 %d / %d（滤掉静音后）" % (keep.sum(), m))
    print("  δ 分布：5%%分位 %+.2f dB，中位 %+.2f，95%%分位 %+.2f ⇒ 跨度 %.2f dB"
          % (p5, np.median(dv), p95, span))
    print("  偏离中位数 > 1.0 dB 的窗：%d 处；其中在 30 秒整数倍 ±%.1f 秒内：%d 处 %s"
          % (len(off), NEAR, len(near), [round(x, 2) for x in near][:10]))
    # ★ P2 换把尺子：看 **δ 的跳变位置**，不是"δ 偏离最大的位置"。
    #   人为台阶在 30~60 秒这段里，δ 在**段中间**最稳（+3.5 dB），
    #   而切点（30.0 / 60.0）处才是跳变发生的地方 —— 上一版按"偏离最大"
    #   找位置，找到的全是段中间，于是自检也判 0 处 ⇒ P2 没牙。
    #   这里用 0.2 秒短窗：窗要么整个在改动区外、要么整个在内，δ 就会
    #   **一步**跳过去，位置精确到 0.1 秒。（短窗之所以能用，是因为素材
    #   动态已经在 δ=A-B 里被抵消了。）
    A2, sr2, h2, _ = db_curve(a_path, win=0.2, hop=0.1)
    B2, _, _, _ = db_curve(b_path, win=0.2, hop=0.1)
    m2 = min(len(A2), len(B2))
    d2 = (A2 - B2)[:m2]
    t2 = (np.arange(m2) + 1) * (h2 / float(sr2))
    gate2 = max(A2.max(), B2.max()) - 40.0
    k2 = (A2[:m2] > gate2) & (B2[:m2] > gate2)
    dv2, tv2 = d2[k2], t2[k2]
    dv2 = dv2 - np.median(dv2)
    jd = np.abs(np.diff(dv2))
    hit = np.where(jd > JUMP_DB)[0]
    hit_t = [float(tv2[i]) for i in hit]
    in_seam = [x for x in hit_t
               if min(abs(x - k * 30.0) for k in range(1, 12)) < 1.0]
    print("  δ 跳变（0.2 秒窗，相邻差 > %.1f dB）：%d 处；"
          "落在 30 秒整数倍 ±1.0 秒内：%d 处 %s"
          % (JUMP_DB, len(hit), len(in_seam),
             [round(x, 2) for x in in_seam][:8]))
    ok_p1 = span < STEP_DB
    print("  P1 两轨响度差跨度 < %.1f dB：%s" % (STEP_DB, "OK" if ok_p1 else "★ 超标"))
    # ★「跳变是否落在切点」只作参考，**不参与判定**：
    #   0.2 秒窗上 δ 本身的抖动就有 200+ 处超过阈值（两次推理的细粒度差异），
    #   人为造一个 3.5 dB 台阶时，它也只是这 200 多处里的两三个 ——
    #   自检里这一条**没有变红**，说明它分不出台阶和噪声。
    #   没有牙的尺子不能挂"判据"的名头（会误导后来人），降级为参考信息。
    print("  （参考，不作判定）切点附近跳变 %d 处 —— 噪声底约 %d 处，两者同量级"
          % (len(in_seam), int(len(hit) * 2.0 / max(1.0, tv2[-1]))))
    return ok_p1


def selftest(a_path, b_path):
    """尺子自检：人为在某个切点处造一个 +3.5 dB 的台阶，尺子必须变红。"""
    y, sr = sf.read(a_path, always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)
    y = np.asarray(y, dtype="float64")
    k = int(30.0 * sr)
    if len(y) > 2 * k:
        y[k: 2 * k] *= 1.5               # 第 2 段整体抬高 3.5 dB
    p = os.path.join(os.path.dirname(a_path), "_seam_selftest.wav")
    sf.write(p, y.astype("float32"), sr)
    ok = analyse(p, b_path, "自检：第 2 段人为抬高 3.5 dB（尺子应当判红）")
    return (not ok), p


def main():
    if len(sys.argv) < 3:
        print("用法：probe_seam.py <分段产物> <未分段产物>")
        return 2
    ok = analyse(sys.argv[1], sys.argv[2], "真实产物")
    red, p = selftest(sys.argv[1], sys.argv[2])
    try:
        os.remove(p)
    except Exception:
        pass
    print("\n---- 结论 ----")
    if not red:
        print("★ 尺子自检没红 ⇒ 这把尺子不可信，上面的结论不作数")
        return 3
    print("尺子自检：能红（认得出人为台阶）")
    print("真实产物：%s" % ("没有段边界台阶" if ok else "★ 存在段边界台阶"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
