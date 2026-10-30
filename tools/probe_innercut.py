# -*- coding: utf-8 -*-
"""把 RVC **自己那一层**的切点算出来看（零显卡，纯数值模拟）。

★ 为什么要看它
--------------
外层分段（--maxseg 30）之后，每段还会被 RVC 的 Pipeline 再切一刀：
    t_max    = chunk + 5 = 25 秒   ← 超过这个长度才进分片分支
    t_center = chunk     = 20 秒   ← 每隔这么远放一个候选切点
    t_query  = chunk/3   = 6.67 秒 ← 在候选点前后这么宽的窗里找能量最低处
于是「30 秒的段」内部其实还是 20 秒 + 10 秒两片。

问题在 opt_ts 的搜索窗会被音频末尾截断：
    for t in range(t_center, len(audio), t_center):
        opt_ts.append(t - t_query + argmin(audio_sum[t-t_query : t+t_query]))
当 `t + t_query > len(audio)` 时，窗口只有左半边 + 一点点右边，
argmin 很容易被**末尾的静音/弱音**吸走 ⇒ 切点跑偏，最后一片可能只剩零点几秒。
极短的最后一片意味着模型看到的是一小截上下文不够的碎片，质量无法保证 ——
这正好能解释「段 1 最短（26.15 秒）却最差（IQR 25.9）」。

判据：
    I1 每一片的时长 ≥ 3.0 秒（短于它的碎片没有足够上下文，不该出现）
    I2 内部片数 ≥ 2 时，最短片 / 最长片 ≥ 0.25（不许出现「一大片拖一根针」）

★ 自检（--selfcheck）：喂一段「前 20 秒满能量 + 末尾 6 秒静音」的合成音频，
  切点必然被吸到静音区，I1/I2 必须判红。尺子没红 ⇒ 判据是摆设。

用法：
    <py> -s tools\\probe_innercut.py <源人声> [--maxseg 30] [--chunk 20]
"""

import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RVC_ROOT = r"D:\RVC\Retrieval-based-Voice-Conversion-WebUI"
MIN_PIECE = 3.0
MIN_RATIO = 0.25


def inner_pieces(y, sr, chunk, tail=5.0):
    """照抄 Pipeline.pipeline() 里的 opt_ts 算法，返回各片长度（秒）。"""
    window = 160
    t_pad_sr = 3.0                       # x_pad 默认 3 秒
    t_pad = int(sr * t_pad_sr)
    t_max = int(sr * (chunk + tail))
    t_center = int(sr * chunk)
    t_query = int(sr * min(10.0, chunk / 3.0))

    audio = np.asarray(y, dtype="float64")
    n = len(audio)
    if n + window <= t_max:
        return [n / float(sr)], 1

    audio_pad = np.pad(audio, (window // 2, window // 2), mode="reflect")
    audio_sum = np.zeros_like(audio)
    for i in range(window):
        audio_sum += np.abs(audio_pad[i:i - window])   # ★ 负索引：滑窗和
    opt_ts = []
    for t in range(t_center, n, t_center):
        lo, hi = t - t_query, t + t_query
        seg = audio_sum[max(0, lo):hi]                 # ★ numpy 自动截断 ⇒ 窗口变窄
        if len(seg) == 0:
            continue
        k = int(np.argmin(seg))
        opt_ts.append(max(0, lo) + k)                  # 绝对样本位
    opt_ts = sorted(set(int(t) for t in opt_ts if 0 < t < n))

    bounds = [0] + opt_ts + [n]
    return [(bounds[i + 1] - bounds[i]) / float(sr) for i in range(len(bounds) - 1)], len(bounds) - 1


def report(y, sr, chunk, tag, quiet=False):
    pieces, npc = inner_pieces(y, sr, chunk)
    if not pieces:
        return True
    mn, mx = min(pieces), max(pieces)
    ok1 = mn >= MIN_PIECE
    ok2 = (mn / mx >= MIN_RATIO) if npc >= 2 and mx > 0 else True
    if not quiet:
        print("  %-10s %5.2f 秒 ⇒ %d 片 [%s]  最短 %.2f 秒 %s | 最短/最长 %.2f %s"
              % (tag, len(y) / float(sr), npc,
                 " ".join("%.2f" % p for p in pieces), mn,
                 "OK" if ok1 else "★ 碎片太短", mn / mx if mx else 0.0,
                 "OK" if ok2 else "★ 长短悬殊"))
    return ok1 and ok2


def selfcheck(sr=16000):
    """末尾静音 ⇒ argmin 被吸走 ⇒ 切点跑偏，判据必须红。"""
    n = int(sr * 26.0)
    y = np.random.RandomState(0).randn(n) * 0.2
    y[int(sr * 20.0):] *= 0.02                    # 后 6 秒近乎静音
    print("  === 自检：26 秒音频，前 20 秒有内容 + 末尾 6 秒近乎静音 ===")
    ok = report(y, sr, 20.0, "自检")
    print("  自检结果：%s" % ("★ 尺子没牙" if ok else "尺子有牙（正确判红）"))
    return not ok


def main():
    args = [a for a in sys.argv[1:]]
    ruler = selfcheck()
    if not args:
        print("  尺子自检通过；加 <源人声> 参数查看真实歌曲。")
        return 0 if ruler else 1
    src = args[0]
    maxseg = 30.0
    chunk = 20.0
    for i, a in enumerate(args):
        if a == "--maxseg":
            maxseg = float(args[i + 1])
        if a == "--chunk":
            chunk = float(args[i + 1])

    src = os.path.abspath(src)          # ★ 必须在 chdir 之前转绝对路径
    os.chdir(RVC_ROOT)
    sys.path.insert(0, os.path.join(HERE, "stages"))
    from rvc_infer import plan_cuts
    import librosa
    y, sr = librosa.load(src, sr=16000, mono=True)
    y = np.asarray(y, dtype="float32")
    cuts = plan_cuts(y, maxseg)
    bounds = [0.0] + cuts + [len(y) / float(sr)]
    print("\n外层 maxseg=%.1f ⇒ %d 段；内部 chunk=%.1f" % (maxseg, len(bounds) - 1, chunk))
    allok = True
    for i in range(len(bounds) - 1):
        a, b = int(bounds[i] * sr), int(bounds[i + 1] * sr)
        allok = report(y[a:b], sr, chunk, "段%d" % (i + 1)) and allok
    print("\n结论：%s" % ("各段内部切片正常" if (allok and ruler)
                          else ("★ 内部切片有问题" if ruler else "★ 尺子不可信")))
    return 0 if (allok and ruler) else 1


if __name__ == "__main__":
    sys.exit(main())
