# -*- coding: utf-8 -*-
r"""第 ? 关：RVC 输出时长对齐（「越唱越差」的根因防线）。

由头：2026-10-22 老板说「现在有了之前越唱越差的问题，特别是破音后」。
追下去拿到一条完整的因果链（每一步都有数据，不是推测）：

  1. 同一段素材（170-204s）**单独**跑 RVC：devIQR 7.1 cents、与源对齐 0.0 ms；
     同一段放进 204 秒的**整首**里跑：devIQR **54.0** cents、对齐 **-70 ms**。
     ⇒ 素材无罪，是"整首模式"引入的。7.6 倍。
  2. tools/probe_drift.py 逐窗量对齐：0s 处 +4.1ms → 180s 处 -57.0ms，
     位置 vs 滞后 rho = **-0.969**，斜率 -0.311 ms/秒，相邻窗跳变中位仅 2.9 ms
     ⇒ **均匀累积**（时长比例错了），不是分片边界跳变。
  3. 源 204.080 秒 / 成品 204.000 秒 —— 成品整体短了 **80 ms**，按比例铺在整条轴上。
  4. 它不动音色（八度错、谐波性、削波全正常）⇒ 常规质检**发现不了**，
     所以要有一条专门守住它的判据。

修法：stages/rvc_infer.py 的 align_duration() —— 写盘前把输出按比例校正到与输入等长。
     缩放比 ~1.0004 ⇒ 音高只变约 0.7 音分（人耳可辨约 5~10 音分），代价可忽略。
     实测：同一段 90-120s，devIQR 40.5 → **24.8**、吻合 90.1% → **94.4%**。

本关不碰显卡（纯数值 + 静态检查），判据分四段：
  A. align_duration 的基本行为与保真度
  B. **负对照**：换成"截断/零填充"这种偷懒写法必须变红（证明 A 的保真度判据有牙）
  C. 静态：主流程确实在 sf.write **之前**调用了它
  D. **负对照**：把调用注释掉，C 必须变红
退出码：0 全绿；1 有判据红；2 环境问题（import 不到模块）。
"""
import importlib.util
import inspect
import os
import re
import sys

import numpy as np

N_PASS = 0
FAILS = []


def check(name, ok, extra=""):
    global N_PASS
    if ok:
        N_PASS += 1
        print("  [ok] %s%s" % (name, ("  %s" % extra) if extra else ""))
    else:
        FAILS.append(name)
        print("  [!!] %s%s" % (name, ("  %s" % extra) if extra else ""))
    return bool(ok)


ROOT = os.path.dirname(os.path.abspath(__file__))
STAGE = os.path.join(ROOT, "stages", "rvc_infer.py")

SR = 48000
# 测试素材统一用 20 秒：0.04% 的真实比例在它上面是 8ms，超过 align_duration 的
# 2ms 免动阈值；用 2 秒素材的话只有 0.8ms，函数会正确地"不动手"，判据就假红了。
TEST_SECS = 20.0


# ---------------------------------------------------------------- 加载被测函数
def load_align():
    """直接从 stages/rvc_infer.py 里把 align_duration 抠出来——不 import 整个模块。

    为什么不用 import：rvc_infer.py 顶部刻意只 import 标准库（numpy/soundfile 延迟到
    main() 里），它是 RVC 自带解释器跑的脚本，在 venv 里 import 会炸。
    """
    with open(STAGE, encoding="utf-8") as f:
        src = f.read()
    m = re.search(r"^def align_duration\(.*?(?=^\S)", src, re.S | re.M)
    if not m:
        return None
    ns = {"np": np}
    exec(compile(m.group(0), "align_duration", "exec"), ns)  # noqa: S102
    return ns.get("align_duration")


def sine(freq=440.0, secs=2.0, sr=SR, amp=0.6):
    t = np.arange(int(secs * sr), dtype=np.float64) / sr
    return amp * np.sin(2 * np.pi * freq * t)


def chirp(f0=200.0, f1=800.0, secs=2.0, sr=SR, amp=0.6):
    """线性扫频。★ 负对照必须用它，不能用正弦 —— 见 B 段注释。"""
    t = np.arange(int(secs * sr), dtype=np.float64) / sr
    phase = 2 * np.pi * (f0 * t + (f1 - f0) / (2.0 * max(secs, 1e-9)) * t ** 2)
    return amp * np.sin(phase)


def compressed(x, factor):
    """把信号按 factor 做时间压缩/拉伸，得到 another 长度 —— 模拟「RVC 输出偏短」。

    ★ 刻意用**线性插值**造它，与被测函数内部的 FFT 重采样不是同一套算法，
      免得变成"拿同一套东西造再还原"的循环论证。
    """
    n_out = int(round(len(x) * factor))
    return np.interp(np.linspace(0.0, len(x) - 1.0, n_out),
                     np.arange(len(x), dtype=np.float64), np.asarray(x, dtype=np.float64))


def peak_freq(x, sr=SR):
    n = len(x)
    w = np.hanning(n)
    sp = np.abs(np.fft.rfft(x * w))
    k = int(np.argmax(sp[1:])) + 1
    return float(k) * sr / n


def snr_db(ref, got):
    num = float(np.sum(ref ** 2))
    den = float(np.sum((ref - got) ** 2))
    if den <= 1e-18:
        return 120.0
    return float(10.0 * np.log10(num / den))


# ---------------------------------------------------------------- A. 基本行为
def a_basic(f):
    print("\nA. align_duration 的基本行为")
    y = sine(440.0, 2.0)                     # 96000 样本
    check("能拿到 align_duration", f is not None)

    # A1 一样长就不动手
    out, d = f(y, len(y), SR)
    check("长度不需要改时原样返回（且零漂移）",
          len(out) == len(y) and abs(d) < 1e-9, "drift=%s" % d)

    # A2 小差异（2ms 内）也不动手 —— 避免为了 0.5ms 做一次无谓重采样
    small = int(SR * 0.001)
    out2, d2 = f(y, len(y) - small, SR)
    check("小于 2ms 的差异不动手（阈值保护）",
          len(out2) == len(y) and abs(d2) < 1e-9, "len=%d" % len(out2))

    # A3 真实场景：短了 80ms
    want = int(round(len(y) * SR / SR)) - int(0.08 * SR)
    yshort = y[:len(y) - int(0.08 * SR)]
    out3, d3 = f(yshort, len(y), SR)
    check("短了 80ms 时按目标长度补回", len(out3) == len(y), "got=%d" % len(out3))
    check("报告的漂移量与真实差一致（约 -80ms）",
          abs(d3 - (-80.0)) < 1.0, "%.1f ms" % d3)

    # A4 保真度：拉伸 1% 后能原样还原（用 1% 是为了让"主频有没有被拉伸带走"这条
    #   判据**真的有分辨力** —— 真实偏差只有 0.04%（≈0.7 音分），2 秒窗的 FFT 频率
    #   分辨率是 0.5 Hz，压根测不出来，写进去就是一条永远绿的假判据。
    ref_s = sine(440.0, 2.0)
    bad_s = compressed(ref_s, 0.99)
    out4, _ = f(bad_s, len(ref_s), SR)
    f0_bad, f0_out = peak_freq(bad_s), peak_freq(out4)
    cents_out = abs(1200.0 * np.log2(f0_out / 440.0))
    cents_bad = abs(1200.0 * np.log2(f0_bad / 440.0))
    check("未校正时被拉伸带走的音高确实存在（%d 音分量级）——证明这条判据有分辨力"
          % 15, cents_bad > 15.0, "%.1f cents" % cents_bad)
    check("校正后主频回到原位（<%d 音分）—— 音高没被拉伸带走" % 15,
          cents_out < 15.0, "%.1f cents (%s→%s Hz)"
          % (cents_out, round(f0_bad, 2), round(f0_out, 2)))

    # A5 真实场景量级（0.04%）：重采样还原后与目标逐样本对得上。
    #   ★ 素材必须用**长信号**（20 秒）：0.04% 在 2 秒上只有 0.8ms，会落在
    #     align_duration 的 2ms 免动阈值里而根本不触发 —— 第一版就是这么假红的。
    ref_c = chirp(200.0, 800.0, TEST_SECS)
    bad_c = compressed(ref_c, 0.9996)
    out5, _ = f(bad_c, len(ref_c), SR)
    s = snr_db(ref_c[:len(out5)], np.asarray(out5, dtype=np.float64))
    check("0.04% 量级（真实偏差）：还原后 SNR > 20 dB（拉伸没破坏音质）",
          s > 20.0, "%.1f dB（%d→%d 样本）" % (s, len(bad_c), len(out5)))

    # A6 int16 保持整型 + 不溢出（★ 这段 getattr 过一次：RVC 出来的是 int16）
    yi = np.asarray(y * 30000, dtype=np.int16)
    outi, _ = f(yi, len(yi) - int(0.05 * SR), SR)
    ok_dtype = np.issubdtype(np.asarray(outi).dtype, np.integer)
    peak_ok = True
    if ok_dtype:
        info = np.iinfo(np.asarray(outi).dtype)
        peak_ok = np.all(np.asarray(outi) <= info.max) and np.all(np.asarray(outi) >= info.min)
    check("int16 进去仍是整型（不会偷偷变成 float 写坏 PCM）", ok_dtype,
          str(np.asarray(outi).dtype))
    check("整型输出不溢出", peak_ok)
    return s, cents_out


# ---------------------------------------------------------------- B. 负对照
def b_negative(f):
    # ★ 第一版这里用**正弦**做负对照，结果偷懒写法也算出来 120 dB（说服力 0）：
    #   正弦是周期信号，截断/补零后把窗对一对，波形几乎重合 —— 尺子根本没有牙。
    #   ⇒ 必须换成**扫频**：频率随时间一直变，长度一错就整段相位失配，补零还会
    #     在末尾留下一段人为静音，正确重采样则完全对得上。这是能吃住这个 bug 的判据。
    print("\nB. 负对照：换掉实现，A 的保真度判据必须变红（证明它不是摆设）")
    ref = chirp(200.0, 800.0, TEST_SECS)
    bad = compressed(ref, 0.9996)      # 模拟"RVC 输出比理想偏短"
    want = len(ref)

    def truncate(it, n, sr):
        it = np.asarray(it)
        if len(it) >= n:
            return it[:n], (len(it) - n) / float(sr) * 1000.0
        pad = np.zeros(n - len(it), dtype=it.dtype)
        return np.concatenate([it, pad]), (len(it) - n) / float(sr) * 1000.0

    out_good, _ = f(bad, want, SR)
    out_cheat, _ = truncate(bad, want, SR)
    # ★ 不做事后对齐：校正后的输出本来就该与目标逐样本对得上，允许平移就又没牙了
    s_good = snr_db(ref[:len(out_good)], np.asarray(out_good, dtype=np.float64))
    s_cheat = snr_db(ref[:len(out_cheat)], np.asarray(out_cheat, dtype=np.float64))
    check("正确实现还原出目标扫频 SNR > 20 dB", s_good > 20.0, "%.1f dB" % s_good)
    check("偷懒写法（截断补零）明显失配 ⇒ A 的保真度判据有牙",
          s_cheat < s_good - 15.0, "%.1f vs %.1f dB" % (s_cheat, s_good))

    def noop(it, n, sr):
        return it, 0.0
    out_noop, _ = noop(bad, want, SR)
    check("完全不管长度的实现通不过 A3（长度判据有牙）",
          len(out_noop) != want, "len=%d want=%d" % (len(out_noop), want))


# ---------------------------------------------------------------- C. 静态接线
def read_stage():
    with open(STAGE, encoding="utf-8") as fh:
        return fh.read()


def c_wiring():
    print("\nC. 静态：主流程是不是真的在写盘前调用了它")
    src = read_stage()
    check("stages/rvc_infer.py 里定义了 align_duration", "def align_duration(" in src)
    call = "audio, drift_ms = align_duration("
    w_point = "sf.write(a.output"
    ok_order = (call in src and w_point in src
                and src.index(call) < src.index(w_point))
    check(" sf.write 之前调用了 align_duration", ok_order)
    # 必须是"按输入对齐"，不能写死常量。
    # ★ 别按"谁在第几个字符"来查：抽函数把 want 挪到调用**上一行**之后，
    #   原来那条 `src.index("align_duration(audio") 之后 400 字符内要有 want`
    #   就立刻失配红掉 —— 而其实产品是对的。改成查两件真正要说的事：
    #     ① align_duration 的第二实参得是 want（不是别的什么变量/常量）；
    #     ② want 本身得是从输入文件的帧数算出来的。
    #   ★ 还得搜**全部**调用点：现在代码里 align_duration 被调用两次 ——
    #     convert_one 里按段长校正（实参是 int(round(expect_sec*sr))，按需算出来
    #     的，合理），finalize 里按整个输入校正（实参是 want）。只抓第一处会
    #     抓到 convert_one 那个，然后就永远红。
    m_call = re.findall(r"align_duration\(\s*audio\s*,\s*(\w+)", src)
    m_want = re.search(r"want\s*=\s*int\(\s*round\(\s*n_in\b", src)
    check("目标长度是按**输入**算出来的（不是拍脑袋的常量）",
          "want" in m_call and bool(m_want),
          "实参=%s, want←n_in=%s" % (",".join(m_call) or "?", bool(m_want)))


# ---------------------------------------------------------------- D. 负对照
def d_negative():
    # ★ 第一版把替换串写成了 "        # audio, drift_ms = align_duration(" —— 原调用
    #   文本原样留在注释里，`in muted` 依旧为真 ⇒ 负对照永远不会红。要连文本一起抹掉。
    print("\nD. 负对照：把调用注释掉，C 必须变红")
    src = read_stage()
    call = "audio, drift_ms = align_duration("
    muted = re.sub(r"^\s*" + re.escape(call),
                   "        # <negative-control: call removed>", src, flags=re.M)
    ok_neg = (call in src) and (call not in muted)
    check("注释掉调用后原文里再找不到它 ⇒ C 的判据确实会失效（=尺子有牙）", ok_neg,
          "muted_contains=%s" % (call in muted))


def main():
    print("=" * 72)
    print("第 ? 关 RVC 输出时长对齐（「越唱越差」的根因防线）")
    print("=" * 72)
    if not os.path.isfile(STAGE):
        print("找不到 %s" % STAGE)
        return 2
    f = load_align()
    if f is None:
        print("stages/rvc_infer.py 里没有 align_duration")
        return 1
    a_basic(f)
    b_negative(f)
    c_wiring()
    d_negative()

    print("\n" + "=" * 72)
    print("通过 %d / 失败 %d" % (N_PASS, len(FAILS)))
    if FAILS:
        for n in FAILS:
            print("  红：%s" % n)
    print("=" * 72)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
