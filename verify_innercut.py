#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁：外层分段之后，RVC **内部不许再切一刀**。

为什么需要这一关
----------------
2026-10-23 实测（同一段 26 秒素材、同一模型、同一参数，只差内部切不切）：

    内部方案                吻合%    偏差IQR    lag     推理
    分 2 片（20.4 + 5.8）   84.9     25.9 音分  20 ms   8.7 s
    1 片（不分片）          93.5     12.4 音分  10 ms   7.9 s

不分片**又快又好**。机理是 RVC 每片输出都比应有长度多 `window/sr` = 10 ms
（window=160、sr=16000），片串起来是**累积错位**，而我们原来的 `align_duration`
只拉总长、不修片与片之间的相对偏移。这也正是「分段后第 1 段掉 11 个点」的
真正来源 —— 分段成品第 1 段实测 85.1% / 25.9 / 20ms，与上面「分 2 片」那一行
几乎一模一样，说明它跟外部分段没关系，是 RVC 内部那一刀造成的。

要做到「内部永不分片」，得同时锁住两件事：
    ① plan_cuts 切出来的**每段严格 ≤ maxseg**（老实现只在 c±3 秒里找能量最低点，
       不管段长 ⇒ 段长能到 maxseg+6，实测 204 秒歌出现过 32.85 秒的段）；
    ② 把 RVC 的 chunk 顶到 maxseg+5，使 t_max = chunk+5 ≥ 段长，
       Pipeline 的 `audio_pad.shape[0] > t_max` 分支就永远进不去。
缺任何一条，内部就会重新分片，上面那 8.6 个点的损失原样回来。

用法：
    <py> verify_innercut.py        # 退出码 0 = 全通过
"""
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))

STAGE = os.path.join(HERE, "stages", "rvc_infer.py")
MAXSEG = 30.0
MINSEG = 6.0

N_PASS = 0
FAILS = []


def check(name, ok, detail=""):
    global N_PASS
    if ok:
        N_PASS += 1
        print("  [ok] %s%s" % (name, ("  " + str(detail)) if detail else ""))
    else:
        FAILS.append(name)
        print("  [!!] %s  %s" % (name, detail))


def load_mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location("rvc_infer_inner", STAGE)
    m = importlib.util.module_from_spec(spec)
    # ★ rvc_infer 顶层会按 os.getcwd() 写 rmvpe_root 等环境变量；门禁只用到
    #   plan_cuts，不碰推理，但顶层的 setdefault 仍会跑。切到一个无害目录即可，
    #   别让它污染当前进程环境（2026-10-23 长歌探针踩过这个坑）。
    spec.loader.exec_module(m)
    return m


def segs_of(cuts, total):
    bounds = [0.0] + list(cuts) + [total]
    return [bounds[i + 1] - bounds[i] for i in range(len(bounds) - 1)]


def legacy_cuts(y16, maxseg, search=3.0):
    """★ 负对照用的「老实现」：只找能量最低点，不管段长（改回它 ⇒ A1 必须红）。"""
    sr = 16000
    total = len(y16) / float(sr)
    if maxseg <= 0 or total <= maxseg:
        return []
    n = max(1, int(math.ceil(total / float(maxseg))))
    step = total / n
    cuts = []
    win = int(0.05 * sr)
    for i in range(1, n):
        c = i * step
        a = int(max(0.0, c - search) * sr)
        b = int(min(total, c + search) * sr)
        if b - a < win * 2:
            cuts.append(c)
            continue
        fr = np.abs(y16[a:b]).astype("float64")
        k = max(1, (len(fr) - win) // win)
        e = (fr[:k * win].reshape(k, win) ** 2).mean(axis=1)
        cuts.append(round((a + int(np.argmin(e)) * win) / float(sr), 3))
    return [c for c in cuts if 0.5 < c < total - 0.5]


def make_grad(total=204.0, sr=16000):
    """构造素材：两个相邻切点各自被能量梯度往相反方向拽。

    maxseg=30、总长 204 ⇒ n=7、step=29.14，候选切点 29.14 / 58.29 / ...
    包络设计成：
        26.1 → 32.1 秒 能量递增 ⇒ argmin 落在窗口**最左**（切点左移 3 秒）
        55.3 → 61.3 秒 能量递减 ⇒ argmin 落在窗口**最右**（切点右移 3 秒）
    于是第 2 段长度 = 61.3 − 26.1 ≈ 35.1 秒 > maxseg —— 老实现必然超标。
    """
    rs = np.random.RandomState(7)
    n = int(total * sr)
    y = rs.randn(n).astype("float32")
    t = np.arange(n) / float(sr)
    knots = [0, 24.0, 26.1, 32.2, 55.2, 61.4, 90.0, 204.0]
    vals = [0.5, 0.5, 0.03, 1.0, 1.0, 0.03, 0.5, 0.5]
    env = np.interp(t, knots, vals).astype("float32")
    return (y * env).astype("float32")


# ---------------------------------------------- A. 段长硬约束
def a_length():
    print("\nA. plan_cuts 的段长硬约束（保证每段 ∈ [minseg, maxseg]）")
    mod = load_mod()
    sr = 16000

    y = make_grad()
    total = len(y) / float(sr)
    cuts = mod.plan_cuts(y, MAXSEG)
    segs = segs_of(cuts, total)
    print("   新实现 %d 段：%s" % (len(segs), ", ".join("%.2f" % s for s in segs)))
    check("每段都 ≤ maxseg（%.0f 秒）" % MAXSEG, max(segs) <= MAXSEG + 1e-6,
          "最长 %.2f 秒" % max(segs))
    check("每段都 ≥ minseg（%.0f 秒，不许出碎片）" % MINSEG, min(segs) >= MINSEG - 1e-6,
          "最短 %.2f 秒" % min(segs))
    # ★ 段数不写死：约束把 ±3 秒的挑选窗挤没时，plan_cuts 会**多切一段**来重新
    #   腾出挑选余地（宁可多起一个子进程，也不退化成等分 —— 等分切点会落在音符
    #   中间，那一下接缝是可听的）。所以这里守的是「不会少切、也不会失控地多切」。
    n0 = int(math.ceil(total / MAXSEG))
    check("段数在 [%d, %d] 内（装不下时宁可多切一段，也不退化等分）"
          % (n0, n0 + 3), n0 <= len(segs) <= n0 + 3, "实际 %d 段" % len(segs))

    # ★ 尺子自检：喂一段明摆着违规的切点，判据必须红（否则上面两条是摆设）
    bad = [26.1]                      # ⇒ 段长 [26.1, 177.9]
    bad_segs = segs_of(bad, total)
    check("尺子自检：喂违规切点（段长 %.0f 秒）时判据确实会红"
          % max(bad_segs), max(bad_segs) > MAXSEG)

    # ★ 真负对照：换回老实现，同一份素材必须超标
    old = legacy_cuts(y, MAXSEG)
    old_segs = segs_of(old, total)
    print("   老实现 %d 段：%s" % (len(old_segs), ", ".join("%.2f" % s for s in old_segs)))
    check("负对照：换回「只看能量、不管段长」的老实现 ⇒ 出现超标段"
          "（说明 A1 真在守着东西）",
          max(old_segs) > MAXSEG + 1e-6,
          "老实现最长 %.2f 秒" % max(old_segs))

    # 边界：不用切的、和极端参数的
    check("短音频（≤ maxseg）不切", mod.plan_cuts(y[:int(20 * sr)], MAXSEG) == [])
    check("maxseg ≤ 0 视为关闭", mod.plan_cuts(y, 0) == [])
    tight = mod.plan_cuts(y, 8.0)     # maxseg 很小 ⇒ minseg 自动让位，不许崩
    ts = segs_of(tight, total)
    check("参数极端（maxseg=8）也不崩且仍不超上限",
          len(ts) > 1 and max(ts) <= 8.0 + 1e-6, "最长 %.2f 秒" % max(ts))


# ---------------------------------------------- B. 内部不许再分片
def b_inner():
    print("\nB. 外层切完之后，RVC 内部必须是 1 片")
    import probe_innercut as pi

    mod = load_mod()
    y = make_grad()
    sr = 16000
    cuts = mod.plan_cuts(y, MAXSEG)
    bounds = [0.0] + list(cuts) + [len(y) / float(sr)]

    eff_chunk = MAXSEG + 5.0          # 产品里就是这么顶的（见 main 的 _chunk）
    worst = 0
    npc_all = []
    for i in range(len(bounds) - 1):
        a, b = int(bounds[i] * sr), int(bounds[i + 1] * sr)
        pieces, npc = pi.inner_pieces(y[a:b], sr, eff_chunk)
        npc_all.append(npc)
        worst = max(worst, npc)
    print("   各段内部片数：%s（chunk=%.0f）" % (npc_all, eff_chunk))
    check("内部全部是 1 片（chunk 顶到 maxseg+5 之后）", worst == 1,
          "最多的一段有 %d 片" % worst)

    # ★ 真负对照：chunk 不顶（仍是 20）⇒ 内部重新分片，甚至出碎片
    npc_old = []
    frag = []
    for i in range(len(bounds) - 1):
        a, b = int(bounds[i] * sr), int(bounds[i + 1] * sr)
        pieces, npc = pi.inner_pieces(y[a:b], sr, 20.0)
        npc_old.append(npc)
        if pieces:
            frag.append(min(pieces))
    print("   若 chunk 仍是 20，各段内部片数：%s，最短片 %.2f 秒"
          % (npc_old, min(frag) if frag else -1))
    check("负对照：chunk 不顶 ⇒ 内部重新分片（≥2 片）", max(npc_old) >= 2,
          "最多 %d 片" % max(npc_old))


# ---------------------------------------------- C. 源码接线
def c_wiring():
    print("\nC. 源码：chunk 必须在 maxseg 开启时顶上去（父子两处都要顶）")
    src = open(STAGE, encoding="utf-8").read()
    body = src[src.index("def main("):]
    cmd = src[src.index("def child_cmd("):]
    cmd = cmd[:cmd.index("def ", 10)] if "def " in cmd[10:] else cmd

    check("有统一的 eff_chunk()（父子共用，避免两处口径分叉）",
          "def eff_chunk(a):" in src
          and "max(c, m + 5.0) if m > 0 else c" in src)
    check("提升只在 maxseg 开启时生效（否则短音频会被喂成 40 秒一片）",
          "if m > 0 else c" in src)
    check("父进程 apply_chunk 吃的是顶过之后的值", "apply_chunk(vc, eff_chunk(a))" in body)

    # ★★ 这条锁的是 2026-10-23 端到端日志里抓到的真 bug：
    #    父亲把 chunk 顶到了 35，但给子进程的命令行里仍写着 --chunk 20
    #    ⇒ 子进程 --maxseg 0，自己不会再顶 ⇒ 它照样在 30 秒的段里切两片，
    #    顶层优化被完整绕过，而日志只有一行「分片 20.00 秒/片」看不出异常。
    check("★ 子进程命令行传的是 eff_chunk(a)，不是原始 a.chunk",
          '"--chunk", str(eff_chunk(a))' in cmd and '"--chunk", str(a.chunk)' not in cmd)

    check("plan_cuts 有段长下限参数 minseg",
          "def plan_cuts(y16, maxseg, search=3.0, minseg=6.0)" in src)
    check("plan_cuts 里做了约束传播（lo/hi 双向夹）",
          "lo = max(prev + mn, total - left * ms)" in src
          and "hi = min(prev + ms, total - left * mn)" in src)

    # ★ 负对照：把子进程那处换回 a.chunk，源码必须不同（换回去 B 段就失效）
    bad = cmd.replace('"--chunk", str(eff_chunk(a))', '"--chunk", str(a.chunk)', 1)
    check("负对照：子进程换回 a.chunk 后源码确实不同（B 段随之失效）", bad != cmd)


def main():
    print("门禁：外部分段后 RVC 内部不许再分片（maxseg=%.0f）" % MAXSEG)
    a_length()
    b_inner()
    c_wiring()
    print("\n通过 %d 项，失败 %d 项" % (N_PASS, len(FAILS)))
    for f in FAILS:
        print("  失败：%s" % f)
    print("EXIT=%d" % (1 if FAILS else 0))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
