# -*- coding: utf-8 -*-
r"""探针：把每种可视化样式渲一帧出来，肉眼判「不光滑」在哪。

为什么要有这个探针
------------------------------------------------------------------
「看起来不光滑」是个视觉判断，靠读代码猜不出来 —— 必须渲出来看。
这个脚本做两件事：

  1. **整帧**：每种样式渲同一帧，存成 png，直接看整体；
  2. **局部放大**：从每帧里裁一块**斜边最密集**的区域（圆环 / 锥形柱的边缘），
     用 NEAREST 放大 3 倍 —— 有没有锯齿台阶一眼就见。

★ 判「光滑」不看主观感觉，看一个数：**边缘台阶率**。
   沿一条穿过边缘的扫描线走，统计相邻像素亮度跳变 > 阈值的次数。
   光滑的边缘是"缓坡"，跳变次数少；锯齿边缘是"楼梯"，跳变次数多。
   这个数同时给出**负对照**：把超采样关掉再跑一遍，数必须变大。

用法
------------------------------------------------------------------
    python tools/probe_smooth.py                 # 渲全部样式
    python tools/probe_smooth.py orbit tunnel    # 只渲这两样
    python tools/probe_smooth.py --ss 0          # 关掉超采样（负对照）
"""
import math
import os
import sys
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FFMPEG = r"D:\BaiduNetdiskDownload\GPT-SoVITS(1)\v5最新版\GPT-SoVITS-v5-20261005nvidia50x0\GPT-SoVITS-v2pro-20261006nvidia50x0\runtime\ffmpeg.exe"
OUT = os.path.join(ROOT, "data", "_probe_smooth")


def pick_audio():
    """挑一段测试音频：优先本地曲库，没有就合成一段。"""
    mus = os.path.join(ROOT, "data", "music")
    if os.path.isdir(mus):
        for f in sorted(os.listdir(mus)):
            if f.lower().endswith((".mp3", ".wav", ".flac", ".m4a")):
                p = os.path.join(mus, f)
                if os.path.getsize(p) > 200 * 1024:
                    return p
    return None


def synth(path, sec=8.0, sr=44100):
    """合成一段有低音有旋律的素材（没有曲库时的兜底）。"""
    import wave
    import struct
    n = int(sr * sec)
    buf = np.zeros(n, dtype=np.float32)
    t = np.arange(n) / float(sr)
    # 低音鼓：每 0.5 秒一击
    for k in range(int(sec / 0.5)):
        i0 = int(k * 0.5 * sr)
        i1 = min(n, i0 + int(0.18 * sr))
        env = np.exp(-np.linspace(0, 9, i1 - i0))
        buf[i0:i1] += 0.62 * env * np.sin(2 * np.pi * 62.0 * t[i0:i1])
    # 旋律：几个音轮换，带颤音
    notes = [261.6, 293.7, 329.6, 392.0, 349.2, 311.1]
    for k in range(int(sec / 0.75)):
        i0 = int(k * 0.75 * sr)
        i1 = min(n, i0 + int(0.75 * sr))
        f = notes[k % len(notes)]
        env = np.exp(-np.linspace(0, 2.2, i1 - i0)) * (1 - np.exp(-np.linspace(0, 60, i1 - i0)))
        tt = t[i0:i1]
        vib = 1.0 + 0.006 * np.sin(2 * np.pi * 5.2 * tt)
        buf[i0:i1] += 0.30 * env * (np.sin(2 * np.pi * f * vib * tt)
                                    + 0.34 * np.sin(2 * np.pi * f * 2 * vib * tt))

    # 一点点气声，让高频不空
    buf += 0.012 * np.random.RandomState(7).randn(n).astype(np.float32)
    buf = np.clip(buf, -0.98, 0.98)
    data = (buf * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data.tobytes())
    return path


def stair_rate(arr, thr=10.0):
    r"""★ 废弃的尺子，留着当反面教材：数「相邻亮度跳变 > thr」的次数。

    它**方向是反的**：一条硬边是「0 → 100」一次大跳变，抗锯齿之后变成
    「0 → 33 → 66 → 100」三次小跳变 —— 于是超采样打开后这个数**变大了**
    （实测 orbit 0.0356 → 0.0449，aurora 0.0955 → 0.1095），
    照着它判就会把"修好了"当成"更糟了"。

    ⇒ 真正的判据必须是「边缘有多陡」，不是「跳变有多少次」：
      超采样做的是**把跳变摊薄**，所以要看**每次跳变的幅度**（grad_p95）
      和**二阶差分能量**（lap_l1），这两个都会**变小**。
    """
    a = arr.astype(np.float32)
    gx = np.abs(np.diff(a, axis=1))
    gy = np.abs(np.diff(a, axis=0))
    n = (gx > thr).sum() + (gy > thr).sum()
    return float(n) / float(a.size)


def grad_p95(arr, floor=6.0):
    """边缘陡度：只统计"算得上边缘"的梯度（>floor），取 95 分位。

    硬边的梯度接近整段对比度（比如 100），抗锯齿后每个像素只承担 1/3
    （33）⇒ **这个数越小越光滑**。95 分位而不是最大值/均值：
    最大值被一两个过曝点绑住，均值被大片平坦背景稀释。
    """
    a = arr.astype(np.float32)
    g = np.concatenate([np.abs(np.diff(a, axis=1)).ravel(),
                        np.abs(np.diff(a, axis=0)).ravel()])
    v = g[g > floor]
    return float(np.percentile(v, 95)) if v.size else 0.0


def lap_l1(arr):
    """二阶差分（拉普拉斯）绝对值的均值 —— 台阶/锯齿的能量。

    一维上：硬边 [...0,100,100...] 的 lap 是 [100,-100]，
    三级过渡 [...0,33,66,100...] 的 lap 是 [33,0,1,-33]。
    平方和差 4 倍，这里取 L1 也差 2 倍以上。**越小越光滑**。
    """
    a = arr.astype(np.float32)
    lap = (4.0 * a[1:-1, 1:-1] - a[:-2, 1:-1] - a[2:, 1:-1]
           - a[1:-1, :-2] - a[1:-1, 2:])
    return float(np.abs(lap).mean())


def main():
    import vizfx

    # 选项解析：带值的长选项要连它的值一起跳过，剩下的才是"只渲这几个样式"
    args = []
    argv = sys.argv[1:]
    k = 0
    while k < len(argv):
        a = argv[k]
        if a in ("--ss", "--tag"):
            k += 2
            continue
        if a.startswith("--"):
            k += 1
            continue
        args.append(a)
        k += 1
    ss_override = None
    if "--ss" in sys.argv:
        i = sys.argv.index("--ss")
        if i + 1 < len(sys.argv):
            ss_override = float(sys.argv[i + 1])
    tag = ""
    if "--tag" in sys.argv:
        i = sys.argv.index("--tag")
        if i + 1 < len(sys.argv):
            tag = "_" + sys.argv[i + 1]

    os.makedirs(OUT, exist_ok=True)
    audio = pick_audio()
    if not audio:
        audio = synth(os.path.join(OUT, "_synth.wav"))
        print("  ⚠ 本地曲库为空，用合成素材：%s" % audio)
    print("  素材：%s" % audio)

    keys = args or [s["key"] for s in vizfx.STYLES]
    fps = 30
    size = {"w": 1080, "h": 1920}

    t0 = time.time()
    x = vizfx.decode(FFMPEG, audio, None, 12.0)
    if x is None:
        print("  ✗ 解码失败")
        return 1
    print("  解码 %.1f 秒，用时 %.1f 秒" % (len(x) / 44100.0, time.time() - t0))

    t0 = time.time()
    an = vizfx.analyze(x, fps, nb=72)
    print("  分析 %d 帧，用时 %.1f 秒" % (an["spec"].shape[0], time.time() - t0))

    # 挑一帧低音比较足的，画面才热闹（安静帧看着"没东西"，判断不了光滑度）
    bi = int(np.argmax(an["bass"][: int(len(an["bass"]) * 0.7)]))
    print("  取第 %d 帧（低音最强附近）" % bi)

    rows = []
    for key in keys:
        st = vizfx.style_by_key(key)
        if not st:
            print("  ✗ 没有这个样式：%s" % key)
            continue
        sc = vizfx.Scene(540, 960, st, an["nb"], cover=None)
        sc.fps = fps
        sc.dur = an["spec"].shape[0] / float(fps)
        if ss_override is not None and hasattr(sc, "set_supersample"):
            sc.set_supersample(ss_override)
        state = {"peak": True}
        # 先跑两帧把脉冲环之类的状态养出来
        for i in range(max(0, bi - 3), bi + 1):
            img = st["draw"](sc, an, i, state)
        p = os.path.join(OUT, "frame_%s%s.png" % (key, tag))
        img.save(p)
        # 局部放大：取中心圆盘外圈那一带（斜边最密）
        w_, h_ = img.size
        box = (int(w_ * 0.16), int(h_ * 0.28), int(w_ * 0.84), int(h_ * 0.62))
        crop = img.crop(box).resize(
            ((box[2] - box[0]) * 3, (box[3] - box[1]) * 3), Image.NEAREST)
        crop.save(os.path.join(OUT, "zoom_%s%s.png" % (key, tag)))
        gp = grad_p95(np.asarray(img.convert("L")))
        ll = lap_l1(np.asarray(img.convert("L")))
        rows.append((key, st["name"], gp, ll, p))
        print("  ✓ %-8s %-14s 陡度 %.2f  锯齿能量 %.3f  →  %s"
              % (key, st["name"], gp, ll, p))

    if rows:
        gp = [r[2] for r in rows]
        ll = [r[3] for r in rows]
        print("\n  边缘陡度：均值 %.2f  最小 %.2f  最大 %.2f" % (
            sum(gp) / len(gp), min(gp), max(gp)))
        print("  锯齿能量：均值 %.3f  最小 %.3f  最大 %.3f" % (
            sum(ll) / len(ll), min(ll), max(ll)))
    print("\n  产物目录：%s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
