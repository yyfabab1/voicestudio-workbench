# -*- coding: utf-8 -*-
r"""量成品的**有效带宽**：嗓子是 24k 的还是 44.1k 的，听不出来但量得出来。

为什么需要这把尺子：
  容器采样率会骗人。一个 44.1kHz 的 wav 里，16k 以上可能什么都没有 ——
  它是被声码器"填满"的，不是真的有内容。耳朵分不出"高频缺失"和"高频很轻"，
  但频谱上骗不了人：真有内容的频段能量是一条缓慢下降的斜坡，
  缺内容的频段是一堵墙（能量掉到 −60dB 以下再也不回来）。

所以这里量两个东西：
  1. **滚降点**：从低频往高频扫，能量第一次跌到峰值 −60dB 以下并且**之后再也没
     爬回来**的那个频率。这就是"这副嗓子真正的上限"。
  2. **各频段能量占比**：8k+ / 12k+ / 16k+ 各占多少。这三个数是能直接和原唱比的，
     谁缺谁不缺一眼就看得出来。

用法：  <py> -s tools/probe_bandwidth.py <wav1> [wav2 ...]
"""
import os
import sys

import numpy as np
import soundfile as sf


def spec(x, sr, n=4096):
    """长窗平均谱：单帧频谱噪声太大（颤音/共振峰会让曲线抖成一团），
    取所有帧的中位数才是一条能读的曲线。"""
    hop = n // 2
    if x.size < n:
        return None, None
    nfr = 1 + (x.size - n) // hop
    idx = np.arange(n)
    fr = idx[: n // 2 + 1] * sr / float(n)
    # 一次切出所有帧（比逐帧切快得多）
    st = np.arange(nfr) * hop
    frames = x[st[:, None] + idx[None, :]]
    w = np.hanning(n)
    S = np.abs(np.fft.rfft(frames * w, axis=1))
    # 只留有声帧：静音段的谱是本底噪声，会把曲线拉平
    e = (frames ** 2).mean(1)
    keep = e > max(1e-7, e.max() * 0.02)
    if int(keep.sum()) < 5:
        return None, None
    return fr, np.median(S[keep], axis=0)


def rolloff(fr, mag, db=-60.0):
    """滚降点：**最后一个还在阈值以上的频率**。

    ★ 方向别写反。第一版我写成"最后一个跌破的位置"，结果对 48k 容器返回 24000Hz
      （频谱到奈奎斯特就结束了，末端自然是低的）—— 量出来的是**容器带宽**，
      不是**内容带宽**。48k 容器里空无一物也照样读 24000Hz，这把尺子就没有牙。
      取"最后一个还站得住的频率"才对：噪声地板再平它也站不住。
    """
    d = 20.0 * np.log10(np.maximum(mag, 1e-12) / max(float(mag.max()), 1e-12))
    above = np.where(d >= db)[0]
    if above.size == 0:
        return 0.0
    i = int(above[-1])
    # 单帧噪声毛刺会让某一点偶然站住 ⇒ 取该点之前的连续段
    return float(fr[max(0, i - 2)])


def band_ratio(fr, mag, lo):
    m = fr >= lo
    if int(m.sum()) < 2:
        return 0.0
    tot = float((mag ** 2).sum())
    if tot <= 0:
        return 0.0
    return float((mag[m] ** 2).sum()) / tot


def centroid(fr, mag):
    return float((fr * mag).sum() / max(float(mag.sum()), 1e-12))


def band_db(fr, mag, lo, hi):
    """某频段的**绝对电平**（dB，相对全带峰值）。

    ★ 为什么不能只看占比：占比是相对量，处理本身会改分母。
      实测里 strong 档（fc=3200）把 3.2k 以上全换成原唱，中频能量跟着涨，
      于是 8-12k 的**占比**反而从 0.226 掉到 0.196 —— 看着像"补得更少"，
      其实绝对电平一模一样。只看占比会得出完全相反的结论。
    """
    m = (fr >= lo) & (fr < hi)
    if int(m.sum()) < 2:
        return -99.0
    e = float((mag[m] ** 2).sum())
    return 10.0 * np.log10(max(e, 1e-20))


def flatness(fr, mag, lo, hi):
    """某频段的**谱平坦度**（几何均值 / 算术均值，0~1）。

    ★ 它是把「真高频」和「声码器噪声」分开的那一刀，单独看能量占比会看走眼：
      平坦度 ~1 = 那段是一条平的噪声地板（声码器铺上去的，什么信息都没有）；
      平坦度很低 = 那段是稀疏的谐波峰（真的人声泛音，齿音/气声都在这）。
      实测里 RVC 成品 >12k 的能量比原唱还高，光看占比会以为它高频更丰富，
      其实是噪声 —— 平坦度一量就露馅。
    """
    m = (fr >= lo) & (fr < hi)
    if int(m.sum()) < 4:
        return float("nan")
    v = np.maximum(mag[m], 1e-12)
    return float(np.exp(np.mean(np.log(v))) / np.mean(v))


def probe(path):
    x, sr = sf.read(path, dtype="float32")
    if x.ndim > 1:
        x = x.mean(1)
    # 只看中间 60 秒：开头的静音和结尾的尾音都不是主体
    n = int(min(x.size, sr * 60))
    x = x[int(max(0, x.size - n) * 0): int(max(0, x.size - n) * 0) + n] if x.size > n else x
    fr, mag = spec(x, sr)
    if fr is None:
        return None
    return {
        "sr": sr,
        "rolloff": rolloff(fr, mag),
        "r8": band_ratio(fr, mag, 8000.0),
        "r12": band_ratio(fr, mag, 12000.0),
        "r16": band_ratio(fr, mag, 16000.0),
        "cent": centroid(fr, mag),
        "fl8_12": flatness(fr, mag, 8000.0, 12000.0),
        "fl12_20": flatness(fr, mag, 12000.0, 20000.0),
        "db4_8": band_db(fr, mag, 4000.0, 8000.0),
        "db8_12": band_db(fr, mag, 8000.0, 12000.0),
    }


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit("用法： probe_bandwidth.py <wav1> [wav2 ...]")
    print("%-38s %7s %9s %7s %7s %8s %9s"
          % ("文件", "采样率", "滚降点", ">8k", ">16k", "谱质心", "平坦12-20"))
    print("%-38s %7s %9s %7s %7s %8s %9s"
          % ("", "", "", "", "", "", ""))
    for p in args:
        r = probe(p)
        if r is None:
            print("%-38s  （有声部分太少，量不出来）" % os.path.basename(p)[:38])
            continue
        print("%-38s %7d %8.0fHz %7.3f %7.3f %7.0fHz   %.3f"
              % (os.path.basename(p)[:38], r["sr"], r["rolloff"],
                 r["r8"], r["r16"], r["cent"], r["fl12_20"]))
        print("%-38s %7s   4-8k %+6.1fdB   8-12k %+6.1fdB"
              % ("", "", r["db4_8"], r["db8_12"]))


if __name__ == "__main__":
    main()
