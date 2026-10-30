# -*- coding: utf-8 -*-
r"""探针：超采样到底贵多少（每帧毫秒），以及成片画幅怎么配最划算。

为什么要单独量
------------------------------------------------------------------
超采样把几何绘制放大了 ss² 倍像素。理论上很便宜（PIL 的 polygon/ellipse
是 C 实现），但 `_glow_layer` 和整帧 alpha 合成是逐帧大头，一旦被带进
超采样就翻 4 倍。所以必须实测，不能拍脑袋。

同时量一组「绘制分辨率 × 超采样」的组合，找性价比最高的那一档：
  0.5 倍绘制 + ss2  = 几何画在 1.0 倍，缩到 0.5，再让 ffmpeg 拉到 1.0
  0.75 倍绘制 + ss2 = 几何画在 1.5 倍（过度采样，最贵最好）
  1.0 倍绘制 + ss1  = 老的"全分辨率直画"，没有超采样
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FFMPEG = r"D:\BaiduNetdiskDownload\GPT-SoVITS(1)\v5最新版\GPT-SoVITS-v5-20261005nvidia50x0\GPT-SoVITS-v2pro-20261006nvidia50x0\runtime\ffmpeg.exe"
OUT = os.path.join(ROOT, "data", "_probe_smooth")


def main():
    import vizfx

    audio = None
    mus = os.path.join(ROOT, "data", "music")
    for f in sorted(os.listdir(mus)):
        if f.lower().endswith((".mp3", ".wav", ".flac")):
            p = os.path.join(mus, f)
            if os.path.getsize(p) > 200 * 1024:
                audio = p
                break
    if not audio:
        print("  ✗ 没有测试素材")
        return 1

    x = vizfx.decode(FFMPEG, audio, None, 8.0)
    an = vizfx.analyze(x, 30, nb=72)
    print("  %d 帧素材，样式取 orbit / aurora（一个偏几何、一个偏线框）\n" % an["spec"].shape[0])

    combos = [(540, 960, 1), (540, 960, 2), (540, 960, 3), (810, 1440, 2), (1080, 1920, 1)]
    N = 24
    print("  %-10s %-6s %-8s %-9s %-9s" % ("绘制尺寸", "ss", "单帧ms", "3分钟估时", "相对"))
    base = None
    for (w, h, ss) in combos:
        for key in ("orbit", "aurora"):
            st = vizfx.style_by_key(key)
            sc = vizfx.Scene(w, h, st, an["nb"])
            sc.fps = 30
            sc.dur = an["spec"].shape[0] / 30.0
            sc.set_supersample(ss)
            state = {"peak": True}
            for i in range(3):
                st["draw"](sc, an, i, state)
            t0 = time.time()
            for i in range(N):
                st["draw"](sc, an, i * 3 % (an["spec"].shape[0] - 1), state)
            ms = (time.time() - t0) / N * 1000.0
            est = ms * 30 * 180 / 1000.0 / 60.0
            if key == "orbit" and base is None:
                base = ms
            print("  %-10s %-6s %-8.1f %-9s %-9s" % (
                "%dx%d" % (w, h), ss, ms,
                "%.1f 分" % est, "%.2fx" % (ms / base) if base else "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
