#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁：成品必须被挪回与源同一条时间轴（段级相位对齐）。

为什么需要这一关
----------------
RVC 的分片输出比应有长度多 `window/sr` = 160/16000 = **10 ms**，而
`align_duration` 只把**总长**拉回，**不修成品与源之间的相对偏移**。后果是成品
整体比源晚 10~20 ms。别小看这 10 ms —— f0 是一帧 10 ms 算的，错开一帧，在音高
变化快的地方逐帧偏差直接翻几倍：

    同一段 26 秒素材   未对齐 ⇒ 吻合 89.3%  偏差 IQR 17.1 音分  轮廓 0.925
                      已对齐 ⇒ 吻合 96.4%  偏差 IQR  9.4 音分  轮廓 0.970

它还会让质检数字**抖**：输入长度差 0.01 秒（160 个样本）就能让同一段内容从
93.6% 掉到 90.0%。上一轮长测「第 20 段 30.050 秒掉到 86.3%、裁成 30.000 秒
回到 93.2%」也是这一个东西，当时误判成「末尾多了一点」。

这一关锁住三件事：① 真的会挪，且挪得准；② 测不准时**不许乱挪**；
③ 别再踩「延迟加载 + except 吞异常」那个坑（功能装了但从不生效）。
"""
import os
import sys

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
STAGE = os.path.join(HERE, "stages", "rvc_infer.py")
SR = 16000

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
    spec = importlib.util.spec_from_file_location("rvc_infer_align", STAGE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def fake_voice(sec=8.0, sr=SR, seed=3):
    """合成一段「像人声」的信号：基频在 200~320 Hz 之间滑动 + 几个谐波。

    为什么不用白噪声：真实的偏移是在**周期信号**上测的，而周期信号的互相关有
    一堆等间隔的假峰（间隔 = 基频周期），误配正是这么来的。用白噪声测出来的
    「尺子很准」是假象，一上真歌就跳。
    """
    rs = np.random.RandomState(seed)
    n = int(sec * sr)
    t = np.arange(n) / float(sr)
    f0 = 260.0 + 60.0 * np.sin(2 * np.pi * 0.37 * t)
    ph = 2 * np.pi * np.cumsum(f0) / sr
    y = np.zeros(n)
    for k, amp in ((1, 1.0), (2, 0.5), (3, 0.28), (4, 0.15)):
        y += amp * np.sin(k * ph + rs.rand())
    # 加一点音节起伏（能量包络），更接近真歌
    env = 0.35 + 0.65 * (0.5 + 0.5 * np.sin(2 * np.pi * 0.8 * t))
    return (y * env).astype("float32")


def a_ruler():
    print("\nA. 尺子准不准（合成人声：基频 200~320 Hz 滑动 + 谐波 + 音节起伏）")
    mod = load_mod()
    src = fake_voice()

    # 人为延后 12 ms（整样本移位；真实的偏移量本来就是 10 ms 量级，够用了）
    d_ms = 12.0
    shift = int(round(d_ms / 1000.0 * SR))
    late = np.concatenate([np.zeros(shift, dtype="float32"), src])[:len(src)]

    got = mod.align_phase  # 单纯确认函数可调用
    tmp_src = os.path.join(HERE, "data", "_align_tmp_src.wav")
    os.makedirs(os.path.dirname(tmp_src), exist_ok=True)
    sf.write(tmp_src, src, SR)

    _, lag = mod.align_phase(tmp_src, late.astype("float32"), SR)
    check("人为延后 %.1f ms ⇒ 读出 %.2f ms（±1.5 ms）" % (d_ms, lag),
          abs(lag - d_ms) < 1.5, "实际 %.2f ms" % lag)

    _, lag0 = mod.align_phase(tmp_src, src.astype("float32"), SR)
    check("源 vs 自己 ⇒ 读出 ≈0（|lag| < 0.5 ms）", abs(lag0) < 0.5,
          "实际 %.2f ms" % lag0)

    # 校正后应该挪回去：把 late 校正后，与源再做一次，lag 应接近 0
    fixed, lag1 = mod.align_phase(tmp_src, late.astype("float32"), SR)
    _, lag2 = mod.align_phase(tmp_src, fixed, SR)
    check("校正后残余偏移 ≈0（|lag| < 1.5 ms）", abs(lag2) < 1.5,
          "残余 %.2f ms" % lag2)
    try:
        os.remove(tmp_src)
    except OSError:
        pass


def b_safety():
    print("\nB. 测不准时不许乱挪")
    mod = load_mod()
    src = fake_voice(sec=4.0, seed=11)
    tmp_src = os.path.join(HERE, "data", "_align_tmp_src2.wav")
    sf.write(tmp_src, src, SR)

    # 拿一段**完全不相干**的音频当成品（噪声），各频带必然读不到一致的偏移
    noise = (np.random.RandomState(99).randn(len(src)) * 0.3).astype("float32")
    out, lag = mod.align_phase(tmp_src, noise, SR)
    check("成品与源毫不相干 ⇒ 不校正（lag 报 0，且原样返回）",
          lag == 0.0 and np.array_equal(np.asarray(out), noise),
          "lag=%.2f" % lag)

    # 长度不足（< 0.5 秒）也不许动
    tiny = noise[:int(0.2 * SR)]
    out2, lag2 = mod.align_phase(tmp_src, tiny, SR)
    check("音频太短（0.2 秒）⇒ 不校正", lag2 == 0.0 and len(out2) == len(tiny))

    # ★★ 负对照：成品比源**早** 10 ms。RVC 只会让输出变晚（前后 pad + 每片多
    #   10 ms），输出现成"提前"只有一种可能 —— 误配到基频隔壁的假峰上。
    #   实测某段读出 −12.7 ms，照它挪过去吻合度从 96.7% 掉到 89.4%。
    #   ⇒ 这条必须被挡下来；挡不住的话，上面那些安全性判据就是摆设。
    early = np.concatenate([src[int(0.010 * SR):], np.zeros(int(0.010 * SR))]).astype("float32")
    out3, lag3 = mod.align_phase(tmp_src, early, SR)
    check("★ 负对照：成品比源早 10 ms（物理上不可能）⇒ 拒绝校正",
          lag3 == 0.0 and np.array_equal(np.asarray(out3), early),
          "lag=%.2f" % lag3)
    try:
        os.remove(tmp_src)
    except OSError:
        pass


def c_wiring():
    print("\nC. 接线与那个「装了但从不生效」的坑")
    src_txt = open(STAGE, encoding="utf-8").read()
    i = src_txt.index("def align_phase(")
    body = src_txt[i:src_txt.index("def convert_one(")]
    env = src_txt[src_txt.index("def _env_lag("):src_txt.index("def align_phase(")]

    # ★★ 2026-10-23 真踩过：这个模块顶层刻意不 import numpy / soundfile
    #   （延迟加载，main 里才引）。漏了局部 import ⇒ 抛 NameError，
    #   而外面套着 except ⇒ 静默返回 (0,0)：功能装了，日志里却什么都没有，
    #   端到端跑出来的质量跟没改一模一样，极难发现。
    check("★ align_phase 里自己 import 了 numpy",
          "import numpy as np" in body)
    check("★ align_phase 里自己 import 了 soundfile",
          "import soundfile as sf" in body)
    check("★ _env_lag 里也自己 import 了 numpy", "import numpy as np" in env)
    check("失败时打印原因（不许静默吞掉）",
          body.count("print(") >= 3, "print 次数 %d" % body.count("print("))

    # ★ 频带必须卡在「基频 + 前几次谐波」：太低（贴基频）全是周期假峰，
    #   太高（2 kHz 以上）测的是共振峰延迟 —— 而 RVC 改的正是共振峰，
    #   两边根本不同形。实测 2000/2800/3600 Hz 一致读出 18.5 ms，
    #   而 1000/1400 Hz 读 8~9 ms，且不是整数倍（不是周期误配，是频率相关失真）。
    check("频带卡在基频谐波区（900/1200/1500，不含 ≥2k 的共振峰区）",
          "for c in (900.0, 1200.0, 1500.0)" in body)
    check("取相关峰最高的频带当基准", "sort(key=lambda t: -t[1])" in body)
    # ★ 判据测的是「实质约束在不在」，不是某一行的字面量 ——
    #   2026-10-23 换投票规则（先剔负值、单频带降级靠闭环兜）之后，
    #   老判据因为找不到 `len(agree) < 2` 这个串而假红，那是尺子跟着实现漂了。
    check("多频带一致时取中位数（≥2 个咬合才算强读数）",
          "if len(agree) >= 2:" in body and "np.median(agree)" in body)
    check("只剩一个正读数时降级为弱（不直接扔，交给闭环兜）",
          "｜单频带" in body and "return float(np.median([l for l, p, c in pos])), 1" in body)
    check("偏移量有上限（防测飞了乱挪）", "max_ms=40.0" in src_txt)
    # ★ 物理约束：RVC 只会让成品变晚，负值必是误配。
    #   现在这一步前移到**投票之前**（老实现留到排序后才挡，实测会让
    #   「1200/1500Hz 都读 11.0、900Hz 读 −5.6」的段整段白扔）。
    check("★ 负读数在投票前就被剔掉（物理上 RVC 只会变晚）",
          "pos = [t for t in reads if t[0] >= -0.5]" in body)
    check("★ 偏移必须有上限（cap = min(max_ms, 35)）",
          "cap = min(float(max_ms), 35.0)" in body)
    check("★ 幅度太小的偏移不折腾（lag < 1.0 直接放行）",
          "if lag < 1.0 or lag > cap:" in body)
    # ★★ 闭环自验证：挪完再测一次。这是唯一能抓住「数值看着合理但其实是假峰」
    #   的手段 —— 实测某段读出 15.04 ms 挪过去，吻合度反而从 94% 掉到 89%。
    check("★★ 闭环自验证：挪过去之后重测，残余超阈值就回滚",
          "measure(out)" in body and "abs(again) > need" in body
          and "回滚不校正" in body)
    check("★ 闭环阈值跟着第一次的可信度走，且不比老版宽（1.2 / 0.8 ≤ 2.0）",
          "need = 1.2 if level >= 2 else 0.8" in body)
    check("自验证失败时原样返回（不许返回挪坏的版本）",
          body.count("return y, 0.0") >= 4,
          "提前返回的分支数 %d" % body.count("return y, 0.0"))
    check("有 --no-align 开关", '"--no-align"' in src_txt or "'--no-align'" in src_txt)

    # 顺序：必须先 align_duration 再 align_phase —— 长度不对时互相关会被
    # 拉伸搅浑，测出来的偏移不可信。
    co = src_txt[src_txt.index("def convert_one("):]
    ci = co.index("def ", 10)
    co = co[:ci] if ci > 0 else co
    kd = co.index("align_duration(")
    kp = co.index("align_phase(")
    check("★ 先对齐长度、再对齐相位（顺序反了测出来的偏移不可信）", 0 < kd < kp,
          "align_duration@%d < align_phase@%d" % (kd, kp))
    check("只在 align 开关打开时做", "getattr(a, \"align\", True)" in co)


# ---------------------------------------------------------------- D 真指标闸门

class _StubF0(object):
    """假的 RMVPE：按调用顺序吐预先编好的 f0 曲线。

    用它才能在**没有 torch 的环境**里真跑一遍闸门逻辑（门禁跑在 PY_VENV 下）。
    `_f0_tight` 只用到 `infer_from_audio`，桩掉它就够了 —— 判的是
    「两条 f0 曲线谁更贴」，跟 f0 是怎么来的没关系。
    """

    def __init__(self, curves):
        self.curves = curves
        self.n = 0

    def infer_from_audio(self, v, thred=0.03):
        i = self.n
        self.n += 1
        return np.asarray(self.curves[min(i, len(self.curves) - 1)], dtype="float32")


def _ramp(n=400):
    """一条干净的基频曲线：200 → 400 Hz 线性滑动。"""
    return np.linspace(200.0, 400.0, n).astype("float64")


def d_f0guard():
    print("\nD. 最后那道闸：挪了到底涨没涨（用真指标判，不再用代理量）")
    mod = load_mod()

    # ① 结构：函数在对的地方、被对的地方调用
    txt = open(STAGE, encoding="utf-8").read()
    body = txt[txt.index("def align_phase("):txt.index("def convert_one(")]
    check("定义了 _f0_tight（RMVPE 量音准吻合率）", "def _f0_tight(" in txt)
    check("align_phase 里真的调了 _f0_tight", "_f0_tight(" in body)
    check("★ 校正后更差 ⇒ 回滚（return y, 0.0）",
          "elif t_out < t_raw:" in body and "return y, 0.0" in body)
    check("闸门量不出来时**打印原因**并照挪（不许静默跳过）",
          "f0 闸门量不出来" in body)
    check("★ RMVPE 只加载一次（模块级 _F0_MODEL 缓存）",
          "_F0_MODEL = None" in txt and "if _F0_MODEL is None:" in txt)
    check("有 --no-f0guard 开关", '"--no-f0guard"' in txt)
    # ★ 开关类参数不往下传 = 静默失效（--no-align 就是这个毛病，本轮一并修）
    cc = txt[txt.index("def child_cmd("):txt.index("def convert_segmented(")]
    check("★ child_cmd 把 --no-align 传下去（分段模式下这个开关以前是失效的）",
          '"--no-align"' in cc)
    check("★ child_cmd 把 --no-f0guard 传下去", '"--no-f0guard"' in cc)

    # ② ★ 真跑：桩掉 RMVPE，验证这把尺子**判得出好坏**
    base = _ramp()
    mod._F0_ERR = ""
    mod._F0_MODEL = _StubF0([base,                      # 源
                             base,                      # 不校正：完全吻合
                             np.roll(base, 1) * 1.02])  # 校正后：错一帧 + 跑调
    t_raw, t_out, why = mod._f0_tight(base, base, np.roll(base, 1) * 1.02, SR)
    if t_raw is None or t_out is None:
        check("闸门能量出分数", False, why)
    else:
        check("★ 校正后更差 ⇒ 分数更低（闸门会回滚）", t_out < t_raw,
              "不校正 %.1f%% vs 校正后 %.1f%%" % (t_raw * 100, t_out * 100))

    mod._F0_MODEL = _StubF0([base, np.roll(base, 2) * 1.05, base])
    t2_raw, t2_out, _ = mod._f0_tight(base, np.roll(base, 2) * 1.05, base, SR)
    check("★ 校正后更好 ⇒ 分数更高（闸门会采纳）",
          (t2_out is not None and t2_raw is not None and t2_out > t2_raw),
          "不校正 %.1f%% vs 校正后 %.1f%%"
          % ((t2_raw or 0) * 100, (t2_out or 0) * 100))

    # ②b ★ 量级坑：RVC 的 vc_single 返回 **int16 量级**的 PCM（peak 24715），
    #     而源是 soundfile 读出来的 [-1,1] 浮点。RMVPE 按浮点接口吃进去 ⇒
    #     成品一条基频都提不出来 ⇒ 闸门静默失效（日志只留一句"量不出来"）。
    #     ⇒ 用一个**对幅度敏感**的桩模型来测：幅度 >1.5 就吐全 0。
    class _AmpAware(_StubF0):
        def infer_from_audio(self, v, thred=0.03):
            if float(np.abs(np.asarray(v, dtype="float64")).max()) > 1.5:
                return np.zeros(300, dtype="float32")   # 模拟 RMVPE 提不出基频
            return _StubF0.infer_from_audio(self, v, thred)

    base2 = _ramp(300)
    mod._F0_ERR = ""
    mod._F0_MODEL = _AmpAware([base2, base2, base2])
    ok_s = np.full(3000, 0.3, dtype="float64")        # 源：[-1,1] 浮点
    int16ish = np.full(3000, 20000.0, dtype="float64")  # 成品：int16 量级
    t5, t6, why5 = mod._f0_tight(ok_s, int16ish, int16ish, SR)
    check("★ 成品是 int16 量级时也要量得出（归一化到 [-1,1]）",
          t5 is not None and t6 is not None, why5 or "量不出来 ⇒ 闸门会静默失效")

    # ②c ★ 越界坑：索引上界必须是「源与成品的重合长度 m」，不是成品长度 n。
    #     源比成品短一点点时 m < n ⇒ IndexError 崩掉整段（60 秒素材实测撞到）。
    check("★ 索引按重合长度 m 收敛（不是按成品长度 n）",
          "top = max(0, m - 1)" in body and "0, top)" in body)

    # ③ 负对照：模型加载失败时不能装作"校正没问题"
    mod._F0_MODEL = None
    mod._F0_ERR = "（测试）找不到权重"
    t3, t4, why3 = mod._f0_tight(base, base, base, SR)
    check("★ 负对照：模型不可用时返回 None 并带原因（不给假分数）",
          t3 is None and t4 is None and bool(why3), why3)
    mod._F0_ERR = ""


def main():
    print("门禁：段级相位对齐（把成品挪回源的时间轴）")
    a_ruler()
    b_safety()
    c_wiring()
    d_f0guard()
    print("\n通过 %d 项，失败 %d 项" % (N_PASS, len(FAILS)))
    for f in FAILS:
        print("  失败：%s" % f)
    print("EXIT=%d" % (1 if FAILS else 0))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
