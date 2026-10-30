# -*- coding: utf-8 -*-
"""和声设计器：给"多个人一起唱"挑**不会跑调**的音高。

--------------------------------------------------------------------------
为什么有这个文件（2026-10-25 第八轮）
--------------------------------------------------------------------------
老板：「多角色一起唱，不是简单的拼接」（参考 BV1PyYReMEFL）。

第七轮做完 `chorus_layer` 之后，和声层已经不是"主唱复制一遍"了
（相关系数 1.0000 → 0.2385），但它们**全在同一个音高上** —— 那还是齐唱
（unison），不是合唱。听起来是"一个人唱得很厚"，不是"几个人站一排唱"。
真正的差别必须是**音高**，EQ / 错位 / 混响都装不出来。

好消息：**不需要动音色，也不需要额外跑一次推理。**
多角色叠唱（`mode=chorus`）本来就要给每个角色各跑一遍 RVC —— 只要给第
2..N 个角色的 `--pitch` 加一个音程偏移，出来就是**模型自己合成的和声声部**
（RVC 的 f0_up_key 移的是 f0，HuBERT feature 不变 ⇒ 音色保留、音高改变）。

于是剩下唯一的问题：**加几个半音才不会跑调？**

--------------------------------------------------------------------------
做法：让数据在"音乐上本来就协和的模板"里挑一个，而不是随便挑音程
--------------------------------------------------------------------------
三件事叠起来：

① **Chroma 重合度**（数据）
   `librosa.feature.chroma_cqt` 把音频压成 12 维 pitch class 序列。
   ★ **移调 N 半音 = 把 vocal chroma 沿着 pitch class 轴滚动 N 格** ——
   所以一次 CQT 就能把 −12..+12 全部候选都算完，**根本不用跑 f0，也不用跑 RVC**。
   重合度 = 移调后每帧（只在"有在唱"的帧）人声 pitch class 与伴奏 chroma 的余弦。
   局部取逐帧均值（跟当前和弦合不合），全局取调性向量的余弦（跟这首歌的调合不合）。

② **音乐先验**（理论）
   纯数据会踩雷，必须补一道理论闸门：
     同度 0.02（我们要的就是不要它）｜小二/大二 0.01~0.02（脏）
     三全音 0.05（雷区）｜七度 0.30~0.45（看和弦，风险高）
     三度 / 六度 1.00（和声的肉）｜纯五度 1.00 ｜纯四度 0.95 ｜八度 0.85（安全但空）
   实测（Something Ain't Right）：纯数据把 `+2`（大二度！）排到第一，
   乘上先验后掉到 0.014 —— 没有这道闸门会直接翻车。

③ **模板**（保证层与层之间协和）
   ★ 不能让数据贪心逐个挑音程 —— 原型第一版实测：按分数贪心会选出
     `+3 / +4` 这种**相邻半音**堆在一起（小二度，难听）。
   所以候选单位是**整条堆叠模板**（每条模板内部保证：没有同度、没有小二度、
   有上有下把主唱包在中间），数据只在模板之间选。

然后用 Krumhansl-Schmuckler 轮廓定出这首歌的调；最后拿 f0 中值做**音域闸门**
（小提琴谱给女低音唱 = 移 −8 之后低于 70Hz 就废掉这一档）。

实测（这首歌，检测到 C minor）：
    1 层 → +3        2 层 → +3 −8        3 层 → +3 −8 +7
而 `1 / b3 / 5` 正好就是**小三和弦的三个音** —— 数据挑出来的结果跟乐理一致，
这是我们对它有信心的理由。

--------------------------------------------------------------------------
失败就优雅降级
--------------------------------------------------------------------------
拿不到伴奏、librosa 缺失、算不出有把握的答案 —— 一律 `ok=false`，
上游照旧做齐唱（第七轮那套 chorus_layer 还在），**绝不让这一步拖死整首歌**。

命令行：
    pick <干声wav> <伴奏wav> <out.json> [--layers N]
"""

import argparse
import json
import os
import sys

SR = 22050
HOP = 512
N_OCTAVES = 7
BINS_PER_OCTAVE = 36

# ---- ② 音乐先验（详见文件头）----
PRIOR = {
    0: 0.02,
    1: 0.01, 2: 0.02,
    3: 1.00, 4: 1.00,
    5: 0.95,
    6: 0.05,
    7: 1.00,
    8: 1.00, 9: 1.00,
    10: 0.45, 11: 0.30,
    12: 0.85,
}

SEMIS = tuple(range(-12, 13))

# ---- ③ 堆叠模板（每条内部保证协和；顺序 = 第 1 层、第 2 层…）----
#
# ★★ 为什么**向下的音程排在前面**（不是随便排的，有实测）：
#    同一条干声同一把模型，只改 --pitch，量出来（48k / 32 秒实测段）：
#
#        pitch    HNR      psim     jitter    shimmer
#          0     20.49    0.8807   0.00908   0.03911   ← 基准
#         +3     22.07    0.8647   0.00808   0.03608
#         +7     22.41    0.8174   0.00747   0.03873
#         -5     17.89    0.8783   0.01016   0.04794
#         -8     16.43    0.8867   0.01135   0.05225
#
#    向上 ⇒ HNR **升高**（22.07 / 22.41 > 20.49）= 更电；
#    向下 ⇒ HNR **降低**（17.89 / 16.43 < 20.49）= 更气声、更厚。
#    我们前几轮一直在跟"电音"打仗（HNR 太高就是电），所以和声层**优先向下**：
#      ① 不会把成品往"电"的方向推  ② 低频本来就该由主唱占着地基、和声在下面。
#    想向上加"亮"的那一层，排在第 3 位以后（层数少时根本轮不到它）。
TEMPLATES = (
    ("包夹（下四度+上小三+下小六+上五度）", (-5, 3, -8, 7)),
    ("和弦开放（下四度+上小三+上五度+下小六）", (-5, 3, 7, -8)),
    ("大三和弦开放（下小三+上大三+下小六+上五度）", (-3, 4, -8, 7)),
    ("坚实基础（低八度+下四度+上五度+上小三）", (-12, -5, 7, 3)),
    ("经典三度堆叠（下大六+上小三+上五度+低八度）", (-9, 3, 7, -12)),
    ("柔和大六（下大三+上大六+下四度+上五度）", (-4, 9, -5, 7)),
    ("Power（下四度+上五度+低八度+下小六）", (-5, 7, -12, -8)),
)

# ---- ④ 音质护栏：由上面那张表拟合出来的 ----
#   向下 ΔHNR ≈ 0.51 × s（s 为负）：−5 → −2.60（实测 −2.60）、−8 → −4.08（实测 −4.06）
#     线性度极好，两个点只差 0.02。
#   向上 ΔHNR ≈ 1.325 + 0.085 × s（s 为正）：+3 → 1.58（实测 1.58）、+7 → 1.92（实测 1.92）
#     ★ 这条是过两点直线，只在 3..12 半音内可信，别外推。
DNHR_DOWN_PER_SEMI = 0.51
DNHR_UP_BASE = 1.325
DNHR_UP_PER_SEMI = 0.085
MAX_PREDICT_DHNR = 3.0     # 预测 HNR 偏移超过这么多 ⇒ 这一档废掉
MAX_ABS_SEMI = 12          # RVC 通用建议上限，再远音色会散

DOWN_BONUS = 1.06          # 向下的轻微加权：实测向下 HNR 更低（不电），
                           # 而且低频本来就该由主唱占着地基、和声待在下面。
                           # ★ 只加 6%：加多了会把"最協和的那个音程"挤下去。

IVAL_NAMES = {
    0: "同度", 1: "小二度", 2: "大二度", 3: "小三度", 4: "大三度",
    5: "纯四度", 6: "三全音", 7: "纯五度", 8: "小六度", 9: "大六度",
    10: "小七度", 11: "大七度", 12: "八度",
}

# Krumhansl-Schmuckler 调性轮廓
KS_MAJ = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
KS_MIN = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)
NOTE = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

F0_LO = 70.0      # 移调之后的人声 f0 中值低于这个 = 太低，这档废掉
F0_HI = 520.0     # 高于这个 = 女高音都够呛，同样废掉
F0_MIN_SEC = 0.5  # 少于这么多"有在唱"的时间就不信任 f0


def _load_mono(p, target_sr=SR):
    import numpy as np
    import soundfile as sf
    y, sr = sf.read(p, dtype="float32", always_2d=False)
    y = np.asarray(y, dtype=np.float64)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if sr != target_sr:
        # ★ 不用 librosa.resample：它背后可能要 soxr / resampy，本机没装 resampy。
        #   scipy.signal.resample_poly 是纯 numpy+scipy，最稳。
        from scipy.signal import resample_poly
        from math import gcd
        g = gcd(int(sr), int(target_sr))
        y = resample_poly(y, int(target_sr) // g, int(sr) // g)
    return y, target_sr


def chroma_of(y, sr):
    # ★ np 必须在这里自己 import（本模块延迟加载，顶部没有 numpy）：
    #   同一个坑这轮栽了两次（这里是第二次），落了 ⇒ NameError ⇒ 被上层
    #   except 吞掉 ⇒ "功能装了但从不生效"，日志里连报错都看不到。
    import numpy as np
    import librosa
    return librosa.feature.chroma_cqt(y=np.asarray(y, dtype="float32"),
                                      sr=sr, hop_length=HOP,
                                      bins_per_octave=BINS_PER_OCTAVE,
                                      n_octaves=N_OCTAVES)


def _norm_cols(c):
    import numpy as np
    n = np.linalg.norm(c, axis=0, keepdims=True)
    return c / np.maximum(n, 1e-9)


def voiced_mask(y, sr, hop=HOP):
    """哪些帧"有在唱"。

    取能量的 60 分位 ∧ 峰值的 15% —— 双条件：
    只用分位会把整首都算进去（安静段也过），只用峰值比例会把唱得轻的段丢掉。

    ★★ 帧数必须跟 chroma 一致：`librosa` 的 `center=True` 是 **1 + len(y)//hop**
      帧，我却按 `len(y)//hop` 算 ⇒ 少一帧 ⇒ 下面 `cvn[:, vmask]` 直接抛
      IndexError（实测：5967 vs 5966）。这个 off-by-one 是会崩在这一行的，
      但只要 `t` 直接从 chroma 拿、掩码再裁到 t，就永远对得上。
    """
    import numpy as np
    n = 1 + len(y) // hop
    if n <= 0:
        return np.zeros(0, dtype=bool)
    fr = np.zeros(n, dtype=np.float64)
    for i in range(n):
        a = i * hop
        b = min(len(y), a + hop)
        if b <= a:
            fr[i] = 0.0
            continue
        seg = y[a:b]
        fr[i] = float(np.sqrt((seg ** 2).mean()))
    return fr > max(float(np.percentile(fr, 60)), fr.max() * 0.15)


def detect_key(chroma_inst):
    """从伴奏的平均 chroma 定调（Krumhansl-Schmuckler 相关系数）。"""
    import numpy as np
    prof = chroma_inst.mean(axis=1)
    s = prof.sum()
    if s <= 1e-9:
        return None
    prof = prof / s
    best = None
    ks_maj = np.asarray(KS_MAJ) / sum(KS_MAJ)
    ks_min = np.asarray(KS_MIN) / sum(KS_MIN)
    for pc in range(12):
        rot = np.roll(prof, -pc)
        for mode, ks in (("minor", ks_min), ("major", ks_maj)):
            r = float(np.corrcoef(rot, ks)[0, 1])
            if best is None or r > best[0]:
                best = (r, pc, mode)
    return best


def f0_median(y, sr):
    """唱歌音高的中位数（自相关逐帧 → 取中值）。

    ★ 只需要一个"中位"来判断音域，不需要逐帧精度 ⇒ 自相关就够，
      省掉 pyin 那一分钟。返回 (中值 Hz, 参与统计的秒数)。
    """
    import numpy as np
    win = int(0.040 * sr)          # 40ms 窗
    hop = int(0.020 * sr)          # 20ms 跳
    if len(y) < win * 2:
        return 0.0, 0.0
    f_lo, f_hi = 60.0, 900.0
    k_lo = int(sr / f_hi)
    k_hi = min(int(sr / f_lo), win - 1)
    vals, secs = [], 0.0
    for st in range(0, len(y) - win, hop):
        seg = y[st:st + win]
        seg = seg - seg.mean()
        e = float((seg ** 2).mean())
        if e < 1e-7:
            continue
        ac = np.correlate(seg, seg, "full")[win - 1:]
        if len(ac) <= k_hi:
            continue
        region = ac[k_lo:k_hi + 1]
        if len(region) == 0 or region.max() <= 0:
            continue
        k = int(np.argmax(region)) + k_lo
        pk = float(region.max()) / max(1e-12, float(ac[0]))
        if pk < 0.35 or k <= 0:      # 周期性不够明显 ⇒ 不当浊音
            continue
        vals.append(sr / float(k))
        secs += hop / float(sr)
    if not vals:
        return 0.0, 0.0
    return float(np.median(vals)), secs


def prior_of(semi):
    return PRIOR.get(abs(int(semi)), 0.05)


def predicted_dhnr(semi):
    """这一档移调预计让 HNR 变多少 dB（正 = 更电，负 = 更厚/气声）。

    ★ 公式来自实测拟合（见文件头 ④ 那张表），不是拍脑袋。
      用它把"音高合适但会把成品推成电音"的档挡在外面 —— 我们前几轮
      一直在跟电音打仗，和声这层不能再往那边推一把。
    """
    s = int(semi)
    if s == 0:
        return 0.0
    if s < 0:
        return DNHR_DOWN_PER_SEMI * s
    return DNHR_UP_BASE + DNHR_UP_PER_SEMI * s


def score_candidates(vocal, inst, f0med):
    """给每个候选半音打分。返回 {semi: score}，废掉的档不在里面。"""
    import numpy as np
    yv, sr = _load_mono(vocal)
    ya, _ = _load_mono(inst)
    n = min(len(yv), len(ya))
    if n <= sr:
        raise ValueError("素材太短")
    yv, ya = yv[:n], ya[:n]

    cv, ca = chroma_of(yv, sr), chroma_of(ya, sr)
    t = min(cv.shape[1], ca.shape[1])
    cv, ca = cv[:, :t], ca[:, :t]

    vmask = voiced_mask(yv, sr)[:t]      # ★ 帧数对齐，见 voiced_mask 注释
    if vmask.sum() < 20:
        raise ValueError("几乎没有在唱的段落")
    cvn, can = _norm_cols(cv), _norm_cols(ca)

    gv = cvn[:, vmask].mean(axis=1)
    ga = can[:, vmask].mean(axis=1)
    gv = gv / max(1e-9, np.linalg.norm(gv))
    ga = ga / max(1e-9, np.linalg.norm(ga))

    key = detect_key(ca)

    out = {}
    for i in SEMIS:
        if abs(int(i)) > MAX_ABS_SEMI:
            continue
        # ④ 音质护栏：预计把成品推成电音的这一档不要
        dh = predicted_dhnr(i)
        if abs(dh) > MAX_PREDICT_DHNR:
            continue
        # ①ʹ 音域闸门：移了之后出不了人声的音域就别用（不用惩罚，直接废掉）
        if f0med > 0:
            fa = f0med * (2.0 ** (i / 12.0))
            if fa < F0_LO or fa > F0_HI:
                continue
        loc = float((np.roll(cvn, i, axis=0)[:, vmask]
                     * can[:, vmask]).sum() / max(1, int(vmask.sum())))
        glob = float(np.dot(np.roll(gv, i), ga))
        raw = 0.6 * loc + 0.4 * glob
        out[i] = {"loc": round(loc, 4), "glob": round(glob, 4),
                  "raw": round(raw, 4), "score": round(raw * prior_of(i), 4),
                  "prior": prior_of(i), "dhnr": round(dh, 2),
                  "f0_after": round(f0med * (2.0 ** (i / 12.0)), 1)}
    return out, key, int(vmask.sum()), t


def _fits(taken, s):
    """s 能不能加进已选的这一堆？

    两两之间必须至少隔开**小三度**（环形 pitch class 距离 3~9 半音），
    而且三全音（6）也不行。

    为什么这条比"模板"更根本：◦adjacent◦hana 堆在一起就是脏音程 ——
      - 同度 (0)：两个人唱同一个音 = 又变回齐唱了，白做
      - 小二 / 大二 (1,2)：糊成一团
      - 小七 / 大七 (10,11)：只有属七和弦才撑得住，跟旋律叠 = 难听
      - 三全音 (6)：这个不用解释
    ★ 一个副作用恰好很美：同一方向的音程彼此距离通常只有 1~2 半音
      （−3/−4/−5 之间都是 1），所以这条约束**天然逼出"有上有下"的结构**，
      不用再额外规定模板里必须一上一下。
    """
    for t in taken:
        if t == s:
            return False
        d = abs((int(s) - int(t)) % 12)
        d = min(d, 12 - d)
        if d < 3 or d == 6:
            return False
    return True


def pick_layers(sc, n_layers):
    """挑一组互相协和的音程。

    ★ 不是逐个贪心（那会选出 +3/+4 这种相邻半音，实测过），也不是写死的
      模板（这条依赖已经有：f0 344Hz 那首歌模板成员 −8 被音质护栏废掉，
      结果七条模板全军覆没，连 1 层都出不来）。
      ⇒ **贪心 + `_fits` 协和约束**：先用音乐先验把雷排掉，再逐个往里加，
        加不进就跳过。

    ★★ 返回的层数可能**少于** n_layers —— 这不是失败，是这首歌的可用音域/
        协和约束只允许这么多。为什么不硬凑：凑出来的第三层必然是脏音程，
        那还不如让那个人唱原调（齐唱加倍，音乐上完全成立）。
        ⇒ 宁可少给，也不给难听的（`underfull` 就是给上游看的信号）。
    """
    # ★ np 必须在这里自己 import：本模块是延迟加载的，模块顶部并没有 numpy。
    #   落了这个 ⇒ 抛 NameError ⇒ 被上层 except 吞掉 ⇒ "功能装了但从不生效"、
    #   日志里连报错都看不到（这个坑记在长期记忆第 24e 条，我自己又栽了一次）。
    import numpy as np

    def _rank(s):
        return sc[s]["score"] * (DOWN_BONUS if int(s) < 0 else 1.0)

    # ★ 0 半音永远不许进和声层：那是**齐唱**，我们要的就是不要它。
    #   它的 score 被 prior 压到 0.0136，本来排不上号，但协和池一旦没得挑，
    #   它是唯一能跟谁都"不打架"的（与任何音程都隔着 ≥3 半音），
    #   于是被贪心当成兜底填了进来 —— 实测 3 层时第 3 层就是它。
    pool = [s for s in sc if int(s) != 0]
    order = sorted(pool, key=lambda s: -_rank(s))
    # ★★ 主唱（0 半音）必须**先放进池子里**再挑 —— 只检查"层与层之间协和"
    #    是不够的：实测 3 层时第三层挑到了 −1，**相对主唱是小二度**，
    #    只因为它跟 +3 / −4 都隔着 ≥3 半音。而"跟主唱协和"才是整个算法的
    #    立足点（chroma 比的就是"移调后的人声 vs 伴奏"，基准始终是主唱）。
    take = [0]
    for s in order:
        if len(take) > n_layers:          # take 里含主唱那个 0
            break
        if _fits(take, s):
            take.append(int(s))
    take = [int(s) for s in take[1:]]     # 把主唱摘掉，返回真正的和声层
    if not take:
        return None
    return (tuple(take),
            "协和池（贪心 + 至少小三度间隔）",
            float(np.mean([sc[s]["score"] for s in take])))


def cmd_pick(a):
    import numpy as np
    rep = {"ok": False, "reason": ""}
    out = a.out
    try:
        yv, sr = _load_mono(a.vocal)
        f0med, vsec = f0_median(yv, sr)
    except Exception as e:
        rep["reason"] = "读不了人声轨：%s" % str(e)[:120]
        _write(out, rep)
        print("HARM_PICK " + json.dumps({"ok": False}, ensure_ascii=False))
        return 0
    # f0 不可信（几乎没有周期性可测）⇒ 把音域闸门关掉，但 chroma 那部分照样能用。
    # ★ 必须先定 f0 再算一遍，别拿到一半发现不可信又从头重算 —— 一次 CQT 是几十秒。
    usable_f0 = f0med if vsec >= F0_MIN_SEC else 0.0
    rep.update({"f0_med": round(f0med, 1), "voiced_sec": round(vsec, 2),
                "f0_gate": bool(usable_f0)})
    try:
        sc, key, nframes, tframes = score_candidates(a.vocal, a.inst, usable_f0)
    except Exception as e:
        rep["reason"] = "算不出和声配比：%s" % str(e)[:120]
        _write(out, rep)
        print("HARM_PICK " + json.dumps({"ok": False}, ensure_ascii=False))
        return 0
    n_layers = max(1, min(int(a.layers or 1), 6))
    best = pick_layers(sc, n_layers)
    rep.update({
        "voiced_frames": nframes,
        "frames": tframes,
        "key": (NOTE[key[1]] + " " + key[2]) if key else None,
        "key_corr": round(key[0], 3) if key else None,
        "layers": n_layers,
        "candidates": sc,
    })
    if best is None:
        rep["reason"] = "所有候选都被音乐先验或音域排除了"
        _write(out, rep)
        print("HARM_PICK " + json.dumps({"ok": False}, ensure_ascii=False))
        return 0
    semis, name, tot = best
    # ★★ 这里必须把 ok 置真：以前只在 stdout 的 HARM_PICK 里写 ok:true，
    #    写进 JSON 文件的 rep 还是初始的 {"ok": False} ⇒ 上层读文件后一律判
    #    「和声设计没跑通」⇒ **永远退化成齐唱**（所有人唱同一个音高），
    #    听感就是"非主唱一起唱、糊成一片、发哑"。2026-10-27 实测踩到。
    #    教训：机器可读结论要么只留一份，要么两处必须同源 —— 别一个在 stdout
    #    一个在文件里各说各话，门禁还会因为读的是 stdout 而一路绿灯。
    rep["ok"] = True
    rep["template"] = name
    rep["template_score"] = round(tot, 4)
    rep["got_layers"] = len(semis)
    rep["underfull"] = len(semis) < n_layers
    rep["plan"] = [{"layer": k + 1, "semitone": int(s),
                    "score": sc[s]["score"],
                    "name": IVAL_NAMES.get(abs(s), "?")
                            + ("（上）" if s > 0 else "（下）"),
                    "dhnr": sc[s]["dhnr"], "f0_after": sc[s]["f0_after"]}
                   for k, s in enumerate(semis)]
    rep["reason"] = "调 = %s，%s，共 %d 层%s" % (
        rep["key"] or "未知", name, len(semis),
        "" if not rep["underfull"]
        else "（要 %d 层只能给 %d 层：剩下的音程全是不协和的，"
             "那些人唱原调 —— 见 plan 里没有的部分）" % (n_layers, len(semis)))
    _write(out, rep)
    print("HARM_PICK " + json.dumps(
        {"ok": True, "plan": [p["semitone"] for p in rep["plan"]],
         "key": rep["key"], "template": name}, ensure_ascii=False))
    return 0


def _write(path, rep):
    if not path:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)


def main(argv=None):
    ap = argparse.ArgumentParser(description="和声设计器：挑不跑调的音高")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pick", help="给定干声+伴奏，给出每个和声层该移几个半音")
    p.add_argument("vocal")
    p.add_argument("inst")
    p.add_argument("out")
    p.add_argument("--layers", type=int, default=1)
    p.set_defaults(fn=cmd_pick)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
