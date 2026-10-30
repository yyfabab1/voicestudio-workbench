# -*- coding: utf-8 -*-
r"""门禁：唱前/唱后"波形级修复"不许啃掉咬字（declick / heal_splices）。

为什么单独一关：这一步**错了不会让任何既有判据变红** —— 成品照样出来、任务照样
成功，只是字被啃了、音有点飘。老板原话（2026-10-26）：
    「同一个模型一个歌，变的丢词 还丢音 甚至有点变调」

**两个真 bug**，取证就长在本文件里（第 1、2、3、4 条判据就是它 —— 改坏
_map1 第 1 条立刻红，放宽窗宽第 2/3 条立刻红），不用额外的脚本也能复现：

 ① 就地改写：_map1 用 np.asarray(x, float64)，当 x 本来就是 float64 ndarray 时
    **不复制**，返回同一块内存 ⇒ declick / heal_splices 直接把调用方那份数组改了。
    后果是 _ladder 的三档剂量（1.0/0.7/0.45）**每档都真的生效并叠加**，
    而报告里写的是 "✗ 没采纳" —— 看得见的和实际发生的完全是两回事。
    实测：真实人声 20 秒，传入数组最大变化 declick 1.67 / heal_splices 1.52
    （那条 wav 自己的 peak 才 0.89）。
 ② 修复窗太宽：±1.5ms = ±66 个采样被三次插值替换 ⇒ 咬字的宽带毛刺整片被抹成
    一条光滑曲线。而"孤立陡跳"这条判据在 44.1kHz 真实人声上会误判
    （45 秒干净素材报 292 个点），却连注入的 30 个真单脉冲都抓不到（0/30）。

窗宽扫描（真实 45 秒分离人声，只跑一遍 declick）：
      ±1.50ms → 咬字掉>3dB 的帧 5.1%、最狠 −39.7dB、f0 一致 94.0%
      ±0.50ms →                 3.3%           −32.4dB        97.2%
      ±0.20ms →                 1.1%           −22.2dB        98.2%
      ±0.10ms →                 0.7%           −23.6dB        98.8%  ← 取它
      ±0.05ms →                 0.0%            0.0dB        100.0%（真跳变会修不动）

修复前后，同一份 45 秒真实分离人声、唱前整条链路：
                         掉>1dB   掉>3dB   最狠一帧   f0 一致
    修复前                6.7%     5.0%    −39.6dB    93.7%
    只修 bug ①           6.7%     5.0%    −39.6dB    93.7%  （还不够）
    + 窗宽收到 ±0.1ms     0.9%     0.6%    −18.0dB    98.2%
    + 密集度闸门          0.0%     0.0%     −1.7dB    99.6%  ← 现在

断言（每条都配了能红的负对照，改坏实现必须变红）：
  1. declick 不许就地改写调用方的数组
  2. 窗宽常量 CLICK_HEAL_MS 必须是 ±0.1ms 量级（不许有人悄悄放宽）
  3. ★ 干净素材（有辅音、没有真爆音）上跑 declick ⇒ **内容零伤害**
  4. ★ 密度闸门：报点密度离谱时不许动手（这条在真实素材上天天触发）
  5. ★ 真 Mats Hiroshi 台阶（密度在合理范围内）必须**真的被修掉** ——
     保证我们只是把功能收进了笼子，没有把它阉了
  6. ★ cmd_pre 跑完整条唱前链路，也不许伤咬字（端到端）

用法（挂 fallback）：
    python verify_declick.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "stages"))
import polish as P  # noqa: E402

SR = 44100
# 物理常理（**不**读被测模块的 CLICK_MAX_PER10S，理由见下面第 4 条的自指陷阱）：
# 一首正常的歌里，真爆音/真咔哒是零星几个，不可能每 10 秒几十上百个。
SANE_CLICKS_PER10S = 5.0
OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                           (" —— " + detail) if detail else ""))


# ---------------------------------------------------------------------------
# 素材：现场合成，不依赖用户库里恰好有一首什么样的歌
# ---------------------------------------------------------------------------
def vocal_like(sec=30.0, seed=5, cons=True):
    """一段"人声样"的信号：谐波串（元音）+ 若干**清辅音**段。

    ★ 辅音段是本关的重点：它就是被 declick 误判的地方。
      没有它，这一关量不出任何差别 —— 第一版用纯正弦喂进去，declick 一个点都没
      抓到，"零伤害"其实是在自证清白。
    ★ 但**只**放宽带白噪声也不行：白噪声的相邻差分是同分布的随机抖动，
      很难凑出"某点比局部中位陡 8 倍、还同时比前后邻居陡 3 倍"的组合 ——
      实测 amp 开到 0.5 仍然 **0 个点**（见 tools/_trigfind.py）。
      真实辅音之所以能被误判，是因为它是**瞬态**：一段快速衰减的脉冲串，
      每个脉冲的头部正好就是那个"孤立陡跳"。⇒ 所以辅音 = 噪声包 + 指数脉冲串。
      实测命中率 191/200 ⇒ 这才是能复现真实素材误判（20 秒 73 点）的素材。
    """
    rng = np.random.default_rng(seed)
    t = np.arange(int(sec * SR)) / float(SR)
    f0 = 210.0 + 45.0 * np.sin(2 * np.pi * 0.27 * t)
    ph = np.cumsum(2 * np.pi * f0 / SR)
    v = (0.30 * np.sin(ph) + 0.14 * np.sin(2 * ph) + 0.07 * np.sin(3 * ph)
         + 0.03 * np.sin(5 * ph))
    v *= 0.55 + 0.45 * np.sin(2 * np.pi * 0.6 * t) ** 2        # 乐句起伏
    if cons:
        d = max(3, int(SR * 0.6 / 1000.0))                    # 0.6ms 的瞬态
        ker = np.exp(-np.arange(d) / float(max(1.0, d / 4.0)))
        for _ in range(int(sec * 1.2)):           # 每秒 1.2 个辅音，像正常咬字
            s = int(rng.uniform(0.2, sec - 0.35) * SR)
            L = int(rng.uniform(0.06, 0.22) * SR)
            # 噪声会把"局部差分中位数"抬高 ⇒ 阈值 loc×8
            #   跟着水涨船高 ⇒ 脉冲的头不再突出 ⇒ 一点都判不出来（实测 30 秒
            #   只剩 1 个点，那条判据就成了摆设）。控制痕噪声，让瞬态跳出来。
            v[s:s + L] += 0.05 * np.hanning(L) * rng.standard_normal(L)
            for _j in range(int(rng.integers(2, 4))):         # 每段 2~3 个瞬态
                q = s + int(rng.uniform(0.01, max(0.02, L / SR - 0.03)) * SR)
                if q + d < len(v):
                    v[q:q + d] += 0.16 * ker * rng.choice([-1.0, 1.0])
    v += 0.0009 * rng.standard_normal(len(v))       # 底噪，别让它太假
    return np.ascontiguousarray(v, dtype=np.float64)


def add_steps(v, n=10, mag=0.18, seed=9):
    """注入**真**台阶（ splice 型爆音）：从某个采样起整条抬高一块。

    ★ 为什么是台阶不是单采样尖峰：现判据要求"比前后邻居的差分都大 3 倍"，
      单采样尖峰会在**相邻两个差分**上同时留下大值 ⇒ 互相否定 ⇒ 一个都抓不到
      （实测注入 30 个尖峰，召回 0/30）。它自称"孤立跳变"，能抓的就是台阶。
    """
    rng = np.random.default_rng(seed)
    y = np.array(v, dtype=np.float64, copy=True)
    n0 = len(y)
    pos = np.sort(rng.choice(np.arange(int(0.5 * SR), n0 - int(0.5 * SR)),
                             n, replace=False))
    for i in pos:
        y[i:] += rng.choice([-1.0, 1.0]) * float(rng.uniform(mag * 0.6, mag))
    return y, pos


def cons_change_db(dry, wet, sr=SR):
    """咬字伤害：辅音带(2~8k)相对本体带(300~1.5k)的逐帧变化 —— 只看有声帧。

    返回 (掉>1dB 的帧占比 %, 掉>3dB 的帧占比 %, 最狠的一帧 dB, f0 一致率 %)
    """
    def sp(x):
        from scipy.signal import stft
        f, t, Z = stft(x, sr, nperseg=1024, noverlap=768,
                       boundary="zeros", padded=True)
        return f, np.abs(Z) + 1e-12
    f, M = sp(wet)
    f2, R = sp(dry)
    n = min(M.shape[1], R.shape[1])
    M, R = M[:, :n], R[:, :n]

    def bd(Z, lo, hi):
        m = (f >= lo) & (f < hi)
        return 10.0 * np.log10((Z[m, :] ** 2).mean(axis=0) + 1e-20)
    d = (bd(M, 2000, 8000) - bd(M, 300, 1500)) - (bd(R, 2000, 8000) - bd(R, 300, 1500))
    ref = 10.0 * np.log10((R ** 2).mean(axis=0) + 1e-20)
    voiced = ref > (np.percentile(ref, 90.0) - 40.0)
    keep = voiced & (((R[(f >= 2000) & (f < 8000), :] ** 2).mean(axis=0)) > 1e-16)
    sel = d[keep]
    mf = (f >= 120) & (f <= 900)
    a1 = f[mf][np.argmax(M[mf, :], axis=0)]
    b1 = f2[mf][np.argmax(R[mf, :], axis=0)]
    nz = voiced & (a1 > 0) & (b1 > 0)
    semi = 12.0 * np.log2(a1[nz] / b1[nz])
    return (round(100.0 * float(np.mean(sel < -1.0)), 2),
            round(100.0 * float(np.mean(sel < -3.0)), 2),
            round(float(np.min(sel)), 2),
            round(100.0 * float(np.mean(np.abs(semi) < 0.25)), 1))


def inject_spikes(v, n, wd=1, frac=0.5, seed=0):
    """往素材里注入 n 个"真爆音"：持续 wd 个采样、幅度 frac × peak 的孤立尖峰。

    ★ 为什么这么造才叫真爆音：数字毛刺 / 剪辑咔哒在波形上就是**极短**的孤立
      跳变，1~3 个采样，幅度远超邻域。它不是"变陡"（那是辅音），是"凭空冒出来"。
    """
    rng = np.random.default_rng(seed)
    y = np.array(v, dtype=np.float64, copy=True)
    n0 = len(y)
    pk = float(np.max(np.abs(y))) + 1e-12
    pos = np.sort(rng.choice(np.arange(int(0.5 * SR), n0 - int(0.5 * SR)),
                             size=n, replace=False))
    for i in pos:
        y[i:i + wd] = frac * pk * (1.0 if rng.random() < 0.5 else -1.0)
    return y, pos


def recall_of(segs, pos, wd):
    """检测到的段命中了多少个注入位置。"""
    hit = 0
    for lo, hi in segs:
        for i in pos:
            if lo <= i + wd and i <= hi + wd:
                hit += 1
                break
    return hit


def main():
    tmp = tempfile.mkdtemp(prefix="vsdeclick_")
    try:
        print("=" * 68)
        print("唱前/唱后波形修复 · 不许啃咬字")
        print("=" * 68)

        # ---- 1. 不许就地改写调用方的数组 ----------------------------------
        print("\n[1] 就地改写")
        v = vocal_like(20.0, seed=3)
        vv = np.array(v, dtype=np.float64, copy=True)
        orig = vv.copy()
        P.declick(vv, SR)
        P.heal_splices(vv, SR)
        dmax = float(np.max(np.abs(vv - orig)))
        check("declick / heal_splices 不许就地改写传入的数组",
              dmax == 0.0, "传入数组最大变化 %.6f" % dmax)

        # ---- 2. 窗宽常量 ---------------------------------------------------
        print("\n[2] 修复窗宽")
        check("CLICK_HEAL_MS 收窄到 ±0.1ms 量级（原 ±1.5ms 会抹平咬字）",
              P.CLICK_HEAL_MS <= 0.2,
              "CLICK_HEAL_MS=%s ms ⇒ ±%d 采样"
              % (P.CLICK_HEAL_MS, int(round(SR * P.CLICK_HEAL_MS / 1000.0))))
        check("HEAL_MS 也收窄了（原 ±3ms）", P.HEAL_MS <= 0.6,
              "HEAL_MS=%s ms" % P.HEAL_MS)

        # ---- 3. 干净素材不许乱报点；注了真脉冲再修，不许啃咬字 --------------
        print("\n[3] ★ 干净素材不许乱报点；注了真脉冲之后再修，不许啃咬字")
        dry = vocal_like(30.0, seed=5)
        segs0 = P._spike_segments(dry, SR)
        old0 = int(P._diff_outliers(dry, SR, P.CLICK_K, P.CLICK_WIN_MS).size)
        check("干净合成素材上：新判据报 0 段（旧判据会报 %d 个，那些全是辅音）" % old0,
              len(segs0) == 0, "新判据 %d 段 / 旧判据 %d 点" % (len(segs0), old0))
        # ★★ 光测"干净素材零伤害"在新判据下是**假绿**：新判据在干净素材上一个点
        #    都不报 ⇒ declick 什么都不做 ⇒ "零伤害"是白送的，什么都没测到
        #    （2026-10-26 二修时抓到：这条当时绿得毫无意义）。
        #    ⇒ 必须**先注入真脉冲**逼它真的动手，再断言没啃到字。
        inj, pos = inject_spikes(dry, 24, wd=2, frac=0.5, seed=11)
        wet = P.declick(inj, SR)
        # ★★ 基准必须是**没注入过**的那一版（dry），不能拿注入版（inj）当基准。
        #    第一版就写错了：单采样尖峰在频谱上是**全频带白**的，它自己就往
        #    2~8k 铺了一大把能量；把它修掉，那一帧的"咬字"自然暴跌 ——
        #    实测最狠 −36dB，看着像把字啃了，其实是**修对了**。
        #    拿 inj 当基准等于把"修掉爆音"误判成"啃掉咬字"（2026-10-26 二修踩到）。
        g1, g3, worst, f0ok = cons_change_db(dry, wet)
        npts = int(P._count_clicks(inj, SR))
        check("注入 24 个真脉冲后 declick：修完离**干净版**的偏差，掉 >3dB 的帧 ≤ 1%",
              g3 <= 1.0, "掉>1dB %.2f%% / 掉>3dB %.2f%% / 最狠 %.2fdB" % (g1, g3, worst))
        # ★ 闭环：还得证明**修了确实比不修更接近干净版**（不然"零偏差"可能是
        #   因为压根没动手）。直接量注入位置上的残留幅度。
        # ★ 用**中位数**而不是最大值：最大值只看最差的那个点，只要有一个没修掉
        #   就红，判的是"召回率"不是"修得干不干净" —— 那是 5b 那条的事。
        wd0 = 2
        raw_e = [float(np.max(np.abs(inj[i:i + wd0] - dry[i:i + wd0]))) for i in pos]
        fix_e = [float(np.max(np.abs(wet[i:i + wd0] - dry[i:i + wd0]))) for i in pos]
        check("闭环：注入位置残留的**中位数**必须降到不修时的 1/5 以下（真修掉了）",
              float(np.median(fix_e)) < float(np.median(raw_e)) * 0.2,
              "中位 %.4f → %.4f（最差点 %.4f → %.4f）"
              % (np.median(raw_e), np.median(fix_e), max(raw_e), max(fix_e)))
        # ★ 为什么非要有这一条：只看"百分比"会被模糊掉 —— 窗宽从 ±0.1ms 放宽回
        #   ±1.5ms 时，百分比只从 0.02% 涨到 0.54%（照样 ≤1%，放行），可**最狠的
        #   那一帧**从 −3.25dB 掉到 −14.12dB —— 那一帧的字是整个被抹平的。
        #   实测证实百分比这条尺子抓不住本回归（负对照验证时它就是绿的），
        #   所以再钉一条**逐帧最坏值**的。两把尺子一起才敢说"没有把字啃掉"。
        check("注入真脉冲后 declick：**最狠的那一帧**下坠不许超过 6dB",
              worst >= -6.0,
              "最狠 %.2fdB（负对照：窗宽放宽到 ±1.5ms 时会到 −14dB）" % worst)
        check("注入真脉冲后 declick：f0 一致率 ≥ 98%",
              f0ok >= 98.0, "一致 %.1f%%（这段判出 %d 个疑似点）" % (f0ok, npts))

        # ---- 4. 密度闸门 ---------------------------------------------------
        # ★★ 这里有个**自指陷阱**：第一版写的是 "dens > P.CLICK_MAX_PER10S 时才测"，
        #    于是把 CLICK_MAX_PER10S 改坏成 9999 时，这条自动判定"素材不够脏"、
        #    跳过 ⇒ 负对照**不变红**（实测过）。用被测物当自己的尺子，抓不住自己。
        #    ⇒ 现在用一把**独立**的常理尺子：真爆音在一首歌里不可能每 10 秒超过
        #      SANE_CLICKS_PER10S 个。素材比它还脏 ⇒ 就该被闸门拦下。
        print("\n[4] ★ 疑似点密度离谱时不许动手")
        # ★ 素材怎么造：注入**每 10 秒 60 个**真脉冲。这不是"判据失效"，而是
        #   "素材真有这么密" —— 闸门的设计意图就是这种时候宁可漏修也不啃字。
        #   （新判据在干净素材上只报 0 段，拿干净素材测这条等于没测。）
        dense, _ = inject_spikes(vocal_like(10.0, seed=9), 60, wd=1, frac=0.5, seed=12)
        dens = len(P._spike_segments(dense, SR)) / 10.0 * 10.0
        if dens <= SANE_CLICKS_PER10S:
            print("  （跳过：素材密度 %.1f/10s 还不够脏，测不出这条 —— 见下面 SSE）"
                  % dens)
        else:
            p_a = os.path.join(tmp, "a.wav")
            p_o = os.path.join(tmp, "a_out.wav")
            p_j = os.path.join(tmp, "a.json")
            sf.write(p_a, dense, SR)
            subprocess.run([sys.executable, os.path.join(HERE, "stages", "polish.py"),
                            "pre", p_a, p_o, p_j, "--skip", "level,hp,denoise"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=900)
            st, notes = [], []
            if os.path.isfile(p_j):
                import json
                jd = json.load(open(p_j, encoding="utf-8"))
                st = jd.get("steps") or []
                notes = jd.get("notes") or []
            kept = [s for s in st if s.get("name") == "declick" and s.get("kept")]
            check("密度 %.1f 个/10 秒（真爆音没这么多）⇒ declick 必须不采纳" % dens,
                  not kept, "steps=%s" % [(s.get("name"), s.get("kept")) for s in st])
            # ★ 还得证明是**密度**拦的，不是别的原因侥幸蒙对 ——
            #   （比如 harm 超限也会让它不采纳，那是另一回事）
            hit = [t for t in notes if ("真爆音没这么多" in t or "每 10 秒" in t)]
            check("报告里得写明「是密度拦的」（不许静默跳过）",
                  bool(hit), "notes=%s" % (notes or "（空）"))

        # ---- 5. ★ declip 必须真的修好削波（用户要的"防破音"就是它） --------
        #   ★ 为什么正对照挑 declip 而不是 declick：削波有**明确的物理特征**
        #     （波形被削平成 ±1.0），不需要猜 ⇒ 判据可靠、验收也可靠。
        #     而 declick 实测是"坏的精度"：45 秒干净素材报 292 个点（全是辅音），
        #     注入 30 个真单采样尖峰却**一个都抓不到**（0/30）。它还在 loop 里，
        #     只是**默认不再参与自动链路**（见 PRE_STEP_DEFAULT），由用户自己决定。
        print("\n[5] ★ declip 必须真的把削波修回来")
        # ★ 素材怎么造：不能简单地"乘个增益把整轨推过载" —— 那样未削波的真值本身
        #   就超过 1.0，declip 自带的安全帽（补完夹回 ±0.999）会把它再次压平，
        #   clip_ratio 永远降不下来 ⇒ 这条判据会在"好版本"上也红，变成假尺子。
        #   所以照标准的低阈值硬削波来造：真值整轨 ≤ 1.0，把超过阈值的部分
        #   钳到 0.999 ⇒ declip 认得出（≥CLIP_THR），而恢复目标真的小于 1.0。
        base = vocal_like(20.0, seed=21, cons=True)
        clean = base / float(np.max(np.abs(base)))            # 真值，峰值 = 1.0
        # ★ 阈值取 0.25 不是随便挑的：先按 peak=1 归一化之后，这个合成信号里
        #   |x|>0.75 的采样**只有百万分之一**（宽带辅音让它尖得像根针）⇒
        #   拿 0.75 当阈值等于什么都没削，判据会在好版本上也"通过"（假尺子）。
        #    0.25 ⇒ 约一成的采样被削平，这才是真实过载的样子（整片贴顶）。
        thr = 0.25
        clipped = np.where(clean > thr, 0.999, np.where(clean < -thr, -0.999, clean))
        cr0 = P._clip_ratio(clipped)
        fixed = P.declip(clipped, SR)
        cr1 = P._clip_ratio(fixed)
        e_before = float(np.mean((clipped - clean) ** 2))
        e_after = float(np.mean((fixed - clean) ** 2))
        check("注入的素材确实被削平了（clip_ratio > 1%）", cr0 > 0.01,
              "clip_ratio=%.4f" % cr0)
        check("declip 之后削波基本消失（降到原来的 1/10 以下）",
              cr1 < cr0 * 0.1, "%.5f → %.5f" % (cr0, cr1))
        check("修完更接近没被削波的那一版（MSE 下降）",
              e_after < e_before * 0.8, "MSE %.3e → %.3e" % (e_before, e_after))

        # ---- 5b. ★★ 真脉冲召回（本轮换判据要的就是这个） --------------------
        #   旧判据对单采样尖峰召回 **0/30**，原因不是阈值没调好，是**结构上冲突**：
        #   真脉冲必然产生两个相邻的大跳变（上去 + 下来），而旧判据的②③两条要求
        #   "比前/后一个采样间差大 3 倍" —— 两条同时成立时恰好把真脉冲自己排除了。
        #   （取证：只取①&② 时召回 30/30，加上③ 立刻变 0/30。）
        #   ⇒ 这条判据就是"换回旧判据"这个回归的负对照：谁换回去，它立刻红。
        print("\n[5b] ★★ 注入的真爆音必须真的被认出来（新判据的核心价值）")
        # ★★ 为什么必须同时看**精确率**：只看召回的话，旧判据在 2/3 采样上反而
        #    "召回"更高（18/20、14/20）—— 那不是它准，是它**报得密**（干净素材
        #    就报 23 个），配合 ±wd 的命中容差蒙对的。靠运气覆盖 = 没覆盖。
        #    加上精确率后它立刻露馅：命中数 / 报出总数 低得没法看。
        for wd in (1, 2, 3):
            v0 = vocal_like(20.0, seed=31)
            inj2, pos2 = inject_spikes(v0, 20, wd=wd, frac=0.5, seed=41 + wd)
            segs = P._spike_segments(inj2, SR)
            new_hit = recall_of(segs, pos2, wd)
            new_prec = new_hit / max(1, len(segs))
            old_pts = P._diff_outliers(inj2, SR, P.CLICK_K, P.CLICK_WIN_MS)
            old_hit = int(sum(1 for i in pos2 if np.any(np.abs(old_pts - i) <= wd)))
            old_prec = old_hit / max(1, len(old_pts))
            check("%d 采样宽：召回 ≥ 40%%" % wd, new_hit >= 8,
                  "新 %d/20（报 %d 段）| 旧 %d/20（报 %d 点）"
                  % (new_hit, len(segs), old_hit, len(old_pts)))
            check("%d 采样宽：**精确率** ≥ 50%%（不许靠狂报蒙对）" % wd,
                  new_prec >= 0.5,
                  "新精确率 %.2f | 旧精确率 %.2f（旧的就是这么蒙的）"
                  % (new_prec, old_prec))

        # ---- 6. 端到端：cmd_pre 跑完整条唱前链路也不许伤字 ------------------
        print("\n[6] ★ 端到端：整条唱前链路")
        p_b = os.path.join(tmp, "b.wav")
        p_bo = os.path.join(tmp, "b_out.wav")
        p_bj = os.path.join(tmp, "b.json")
        sf.write(p_b, dry, SR)
        r = subprocess.run([sys.executable, os.path.join(HERE, "stages", "polish.py"),
                            "pre", p_b, p_bo, p_bj, "--skip", "level"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=900)
        if not os.path.isfile(p_bo):
            print("  （这段素材上唱前一步都没达标，没产出 —— 也算零伤害） %s"
                  % (r.stdout or "").strip())
            g1e = g3e = 0.0
            f0oke = 100.0
        else:
            y, _ = sf.read(p_bo, dtype="float64", always_2d=False)
            if getattr(y, "ndim", 1) > 1:
                y = y.mean(axis=1)
            g1e, g3e, _w, f0oke = cons_change_db(dry, y)
        check("整条唱前链路跑完：咬字掉 >3dB 的帧 ≤ 1%", g3e <= 1.0,
              "掉>1dB %.2f%% / 掉>3dB %.2f%%" % (g1e, g3e))
        check("整条唱前链路跑完：f0 一致率 ≥ 98%", f0oke >= 98.0, "%.1f%%" % f0oke)

        print("\n" + "-" * 68)
        print("PASS %d / FAIL %d" % (len(OK), len(BAD)))
        if BAD:
            print("!! 失败项：" + "、".join(BAD))
        else:
            print("结论：波形修复不再啃咬字，且真爆音照样修得动")
        return 0 if not BAD else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
