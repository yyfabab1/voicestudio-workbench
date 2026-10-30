# -*- coding: utf-8 -*-
r"""逐帧手绘的音频可视化（比 ffmpeg 内置滤镜好看一个档次）。

为什么不用 ffmpeg 的 show* 滤镜
------------------------------------------------------------------
`showcqt` / `showwaves` / `showfreqs` 出来的东西只有三种命运：
黑底上一条条直线、颜色是固定彩虹、没有层次没有光。用户的原话是「太难看了」。
它们的极限就在那里 —— 滤镜只能画"一维数据的一维投影"。

要做到「好看」，得有四样东西，而这几样 ffmpeg 滤镜都给不了：
  1. **背景**（渐变 / 光晕 / 暗角），让画面不是一个死黑的洞；
  2. **发光**（glow）—— 亮的东西要往外洇一点，眼睛才觉得"亮"；
  3. **形状**（圆环、锥形柱、同心圆、隧道框）而不是清一色的竖线；
  4. **跟音乐的呼吸**（低音来了中心圆要涨、要亮、要往外扩一圈）。

第二版（照着老板给的参考片重做）
------------------------------------------------------------------
第一版做出来老板还是一句「太难看了」。把参考包（AE 模板：Audio Spectrum
Music Visualizer / Audio React Tunnel / SoundVisible）的预览片抽帧看了一遍，
问题不在"有没有动"，而在**画面是空的** —— 主体只占中间一小块，上下两大片
纯渐变。参考片之所以"满"，靠的是这四层：

  · **环绕**：频谱绕中心一圈（不是只有上半圈），低音在正上方、高音绕到下方；
  · **纵深**：几层发光的多边形框一圈比一圈小、各自缓慢旋转（`tunnel`）；
  · **粒子**：一层缓慢上浮的光点/三角碎片，主体周围一直在动；
  · **光芒**：从中心射出的柔光扇，缓慢旋转（**沿长度分段降 alpha**，
    否则外端硬切，排一圈出来是一台彩色风车）。

所以现在有五种：光环律动 / 光之隧道 / 对称声柱 / 极光同心 / 声波脉冲。

架构（这一条没变）
------------------------------------------------------------------
反过来走：**Python 逐帧画好 RGB，管道喂给 ffmpeg，ffmpeg 只负责编码和烧字幕**。
画面完全可控，而 ffmpeg 最擅长的（编码、libass 字幕）一点没浪费。

依赖：只用 numpy + PIL（运行时的 GPT-SoVITS python 和门禁 venv 里都有，
不新增任何东西）。**不要用 cv2** —— 门禁 venv 里没有，用了门禁就跑不起来了。

性能（实测，1080×1920）
------------------------------------------------------------------
约 25~40 ms/帧 ⇒ 3 分钟的歌 @30fps 要渲 2~3 分钟。所以：
  · 进度按"已画多少帧"上报，用户知道在动；
  · 支持「只做一段」，做小样几秒就好；
  · 每帧都问一次 `should_stop()`，取消能立刻停。

★ 别为了好看无脑加"层"。每多一层 `_glow_layer` 就多一次"缩→模糊→放大"，
  一帧多 5~7 ms。现在的做法是：光芒 / 粒子 / 脉冲环 / 频谱**全画在同一张
  图层上**，只做一次发光模糊。
"""
import math
import os
import random
import subprocess
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ---------------------------------------------------------------- 调色板

PALETTES = {
    # 青 → 蓝 → 紫 → 粉，最通用的"电子感"
    "neon": (0.52, 0.94, 0.86, 1.00),
    # 绿 → 青 → 蓝 → 紫，极光
    "aurora": (0.32, 0.80, 0.78, 0.95),
    # 橙 → 粉 → 紫，日落
    # ★ h1 故意给到 1.00 以上：色相是**环形**的，0.93→1.16 走的是
    #   粉→红→橙这一小段暖色；写成 0.02→0.90 会从红"绕远路"穿过青蓝回到紫，
    #   画出来是一整条彩虹 —— 那是"光谱"不是"日落"（脉动样式的光晕就被它
    #   染成了青色，像两盏车灯）。
    "sunset": (0.93, 1.16, 0.90, 1.00),
    # 冰蓝 → 白蓝，干净
    "ice": (0.50, 0.60, 0.55, 1.00),
    # 红→橙→黄→绿→青→蓝，整条彩虹。均衡器式的柱状图配它最有"频率"的感觉
    "spectrum": (0.00, 0.74, 0.88, 1.00),
}


def palette(n, name="neon"):
    """取 n 个从色相 h0 均匀走到 h1 的 RGB。用 colorsys 只为省依赖。"""
    import colorsys
    h0, h1, sat, val = PALETTES.get(name, PALETTES["neon"])
    out = []
    for i in range(max(1, n)):
        h = h0 + (h1 - h0) * (i / float(max(1, n - 1))) if n > 1 else h0
        r, g, b = colorsys.hsv_to_rgb(h % 1.0, sat, val)
        out.append((int(r * 255), int(g * 255), int(b * 255)))
    return out


def tint(rgb, k):
    """把颜色按系数调亮/调暗，k>1 变亮（不超 255）。"""
    return tuple(min(255, max(0, int(c * k))) for c in rgb)


def wave_body(xs, ys, cy, n):
    """把上下两条曲线合成一个**封闭**多边形（声波样式的实心波身）。

    ★ 这里踩过一个很隐蔽的坑：`bot` 一开始写成
      `[(xs[n-1-j], cy - ys[n-1-j]) for j in range(n)]`，等于把 top 原路再走
      一遍 —— polygon 收到的是一个**零面积的退化环**，什么都填不上。
      画面本身"有东西"（还有两条亮线、还有光晕），所以整整一版都没人发现
      "实心波身"其实从没画出来。正确做法：下边 = 上边跨过轴线镜像（cy + ys），
      并且**倒序**接回起点。门禁里用 `poly_area` 钉死这一点。
    """
    top = [(float(xs[j]), cy - float(ys[j])) for j in range(n)]
    bot = [(float(xs[j]), cy + float(ys[j])) for j in range(n - 1, -1, -1)]
    return top, bot


def poly_area(pts):
    """鞋带公式求多边形面积。门禁用它判"这个填充环不是退化的"。"""
    a = 0.0
    m = len(pts)
    for j in range(m):
        x0, y0 = pts[j]
        x1, y1 = pts[(j + 1) % m]
        a += x0 * y1 - x1 * y0
    return abs(a) * 0.5


# ---------------------------------------------------------------- 音频分析

SR = 44100


def decode(ffmpeg, path, start=None, duration=None, sr=SR):
    """把音频解成 float32 的 (样本, 2) 数组。解不出来返回 None。"""
    cmd = [ffmpeg, "-v", "error", "-nostdin"]
    if start:
        cmd += ["-ss", "%.3f" % start]
    if duration:
        cmd += ["-t", "%.3f" % duration]
    cmd += ["-i", path, "-ac", "2", "-ar", str(sr), "-f", "f32le", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=600)
    except Exception:                                        # noqa: BLE001
        return None
    if p.returncode != 0 or not p.stdout:
        return None
    x = np.frombuffer(p.stdout, dtype=np.float32)
    n = len(x) // 2
    if n < 1024:
        return None
    return x[:n * 2].reshape(n, 2)


def _band_edges(nfft, sr, nb, lo=32.0, hi=16000.0):
    """把线性频点分成 nb 个**对数等宽**的频带。

    ★ 必须对数。线性分组的话 20Hz~20kHz 里前 1% 才到 200Hz，
      整首歌的低音会全挤进第 1 根柱子 —— 人耳听得出"低音在跳"，
      眼睛却什么也看不见，等于白做。
    """
    freqs = np.fft.rfftfreq(nfft, 1.0 / sr)
    e = np.geomspace(lo, hi, nb + 1)
    return np.clip(np.searchsorted(freqs, e), 0, len(freqs) - 1)


def analyze(x, fps, nb=72, win=2048, sr=SR, nwave=192):
    """把整段音频算成每帧的频谱带 + 低音包络 + 波形。

    返回 dict：
      spec  (帧, nb) 0..1   —— 每帧每个频带的能量
      peak  (帧, nb) 0..1   —— 峰值保持（会慢慢掉），画"顶点小帽"
      bass  (帧,)    0..1   —— 低音包络，驱动中心圆呼吸
      mid   (帧,)    0..1
      high  (帧,)    0..1
      wave  (帧, nwave) -1..1 —— 该帧的波形（给脉冲样式画线条）
    """
    mono = x.mean(axis=1).astype(np.float32)
    hop = max(1, int(round(sr / float(fps))))
    nf = max(1, 1 + max(0, (len(mono) - win) // hop))

    idx = np.arange(win)[None, :] + hop * np.arange(nf)[:, None]
    seg = mono[np.clip(idx, 0, len(mono) - 1)] * np.hanning(win).astype(np.float32)
    amp = np.abs(np.fft.rfft(seg, axis=1)).astype(np.float32)

    be = _band_edges(win, sr, nb)
    raw = np.zeros((nf, nb), dtype=np.float32)
    for b in range(nb):
        a, c = int(be[b]), int(be[b + 1])
        if c > a:
            raw[:, b] = amp[:, a:c].mean(axis=1)

    # ★ 归一化用 99 分位而不是最大值：一个爆音就能把最大值抬到天上，
    #   之后整首歌的柱子都只有一格高。99 分位对爆音不敏感。
    ref = float(np.percentile(raw, 99.0)) or 1.0
    v = np.clip(raw / ref, 0.0, 1.0) ** 0.62      # 幂次 <1：把小信号提起来
    # 高频天生弱，给一条随频带上升的补偿，否则右半边永远趴着
    v = np.clip(v * (1.0 + 0.55 * np.linspace(0, 1, nb)[None, :]), 0.0, 1.0)

    # ★ 时间平滑：起得快、落得慢。"看着像在跟着音乐跳"全靠这一句。
    #   没有它，柱子会以帧率抖动，像坏了的示波器。
    spec = np.zeros_like(v)
    prev = np.zeros(nb, dtype=np.float32)
    for i in range(nf):
        a = np.where(v[i] > prev, 0.55, 0.13)
        prev = prev + (v[i] - prev) * a
        spec[i] = prev

    # 峰值保持：一眼看出"刚才最响到哪"
    peak = np.zeros_like(spec)
    hold = np.zeros(nb, dtype=np.float32)
    for i in range(nf):
        hold = np.maximum(spec[i], hold - 0.010)
        peak[i] = hold

    nbass = max(1, int(round(nb * math.log(150.0 / 32.0) / math.log(16000.0 / 32.0))))
    bass = spec[:, :nbass].mean(axis=1)
    mid = spec[:, nbass:nbass * 3].mean(axis=1)
    high = spec[:, int(nb * 0.6):].mean(axis=1)

    # 波形：每帧取窗内 nwave 个点的极值包络（不是抽样，抽样会漏掉尖峰）
    step = max(1, win // nwave)
    usable = (win // step) * step
    w = seg[:, :usable].reshape(nf, usable // step, step)
    wave = w[np.arange(nf)[:, None], np.arange(usable // step)[None, :],
             np.abs(w).argmax(axis=2)]
    wave = np.clip(wave / (np.percentile(np.abs(wave), 99.0) or 1.0), -1.0, 1.0)

    return {"spec": spec, "peak": peak, "bass": bass, "mid": mid,
            "high": high, "wave": wave.astype(np.float32), "nb": nb}


# ---------------------------------------------------------------- 静态素材

def _gradient_bg(w, h, top, bot, scrim=0.0, haze=None):
    """竖向渐变 + 底部压暗（给歌词留可读性）+ 一层静态雾光。

    ★ 雾光是**烤进背景**的。频谱/光环都集中在画面上半部分，下半幅容易死成
      一块黑板 —— 观众会觉得"没做完"。加一层很淡的径向雾，下半幅就有呼吸了。
      关键是它一次算好、之后每帧直接复用，等于不要钱。
    """
    yy = np.linspace(0, 1, h, dtype=np.float32)[:, None]
    t = np.array(top, dtype=np.float32)
    b = np.array(bot, dtype=np.float32)
    g = t[None, :] + (b - t)[None, :] * yy
    g = np.repeat(g[:, None, :], w, axis=1)
    if haze is not None:
        hx, hy, hstr, hcol = haze
        Y, X = np.mgrid[0:h, 0:w].astype(np.float32)
        d = np.sqrt(((X - w * hx) / (w * 0.78)) ** 2 + ((Y - h * hy) / (h * 0.46)) ** 2)
        # ★ hstr 是 0~1 的**系数**，不是加到像素上的绝对值。
        #   第一版按"直接乘颜色"传了 46 进来，结果 R 通道加了 4400，
        #   整个画面白成一片（实测：四种样式全过曝）。
        k = np.exp(-(d ** 2) * 2.1)[:, :, None] * float(hstr)
        g = g + k * np.array(hcol, dtype=np.float32)[None, None, :]
    if scrim > 0:
        # (h,1,1) 才能和已经铺成 (h,w,3) 的 g 广播；写成 (h,1) 会直接报形状错
        s = np.clip((yy[:, 0] - 0.74) / 0.26, 0, 1)[:, None, None] ** 1.5
        g = g * (1.0 - scrim * s)
    return np.ascontiguousarray(np.clip(g, 0, 255).astype(np.uint8))


def _vignette_overlay(w, h, strength=0.60, cy=0.44):
    """暗角做成一层"黑 + alpha"贴图。

    ★ 别用 numpy 逐像素乘：1080×1920 转 float32 再乘，一条就是 20~30 ms，
      比画频谱本身还贵。做成贴图后每帧一次 `alpha_composite` 就完事。
    """
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    d = np.sqrt(((xx - w / 2.0) / (w * 0.70)) ** 2 + ((yy - h * cy) / (h * 0.56)) ** 2)
    m = np.clip(1.0 - strength * np.clip(d - 0.5, 0, None) ** 1.7, 0, 1)
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[:, :, 3] = ((1.0 - m) * 255).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def _glow_sprite(n, color):
    """径向光斑精灵。每帧只缩放 + 叠加，不重算。"""
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32)
    d = np.sqrt((xx - n / 2) ** 2 + (yy - n / 2) ** 2) / (n / 2.0)
    a = np.clip(1.0 - d, 0, 1) ** 2.4
    arr = np.zeros((n, n, 4), dtype=np.uint8)
    arr[:, :, 0], arr[:, :, 1], arr[:, :, 2] = color
    arr[:, :, 3] = (a * 255).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def _disc_sprite(r, color, cover=None):
    """中心的圆盘：有封面就用封面，没有就画一个渐变球。"""
    n = max(4, int(r) * 2)
    if cover is not None:
        try:
            im = cover.copy()
            s = min(im.size)
            im = im.crop(((im.width - s) // 2, (im.height - s) // 2,
                          (im.width + s) // 2, (im.height + s) // 2)).resize(
                (n, n), Image.LANCZOS).convert("RGBA")
        except Exception:                                    # noqa: BLE001
            im = None
        if im is not None:
            mask = Image.new("L", (n, n), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, n - 1, n - 1], fill=255)
            out = Image.new("RGBA", (n, n), (0, 0, 0, 0))
            out.paste(im, (0, 0), mask)
            return out, mask
    # 没封面：从深到亮的径向渐变球，中心偏色 + 边缘一圈轮廓光。
    # 一个纯黑的圆会显得像个洞；有中心辉光和轮廓光才有"实体"的感觉。
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32)
    d = np.clip(np.sqrt((xx - n / 2) ** 2 + (yy - n / 2) ** 2) / (n / 2.0), 0, 1)
    base = np.array([10, 12, 26], dtype=np.float32)
    hot = np.array(tint(color, 1.15), dtype=np.float32)
    rim = np.array(tint(color, 0.85), dtype=np.float32)
    g = (base * (1.0 - 0.30 * (1 - d))[..., None]
         + hot[None, None, :] * ((1 - d) ** 1.8)[..., None] * 1.05
         + rim[None, None, :] * np.exp(-((d - 0.90) ** 2) * 110.0)[..., None] * 0.75)
    arr = np.zeros((n, n, 4), dtype=np.uint8)
    arr[:, :, :3] = np.clip(g, 0, 255).astype(np.uint8)
    # 边缘只留 6% 的过渡带：太软会像糊了，和外面那圈描边叠在一起更糊
    arr[:, :, 3] = (np.clip((1.0 - d) / 0.06, 0, 1) * 255).astype(np.uint8)
    return Image.fromarray(arr, "RGBA"), None


# ---------------------------------------------------------------- 场景

def _make_particles(sc, dust=1.0):
    """预生成一层灰尘/碎片粒子表（归一化坐标，跟分辨率无关）。

    粒子要么是小圆点、要么是小三角 —— 参考片里那种"空气里有东西"的感觉。
    位置随时间只做两件很便宜的事：缓慢上浮 + 左右轻摆，逐帧算一次 sin 就够。
    """
    n = int(round(96 * float(dust)))
    rnd = random.Random(20261014)
    out = []
    for _ in range(max(0, n)):
        out.append({
            "x": rnd.random(), "y": rnd.random(),
            "r": sc.mind * (0.0018 + 0.0075 * rnd.random() ** 2),
            "c": rnd.randrange(max(1, sc.nb)),
            "ph": rnd.random() * 6.2832,
            "sw": 0.010 + 0.030 * rnd.random(),          # 左右摆幅（占画宽比例）
            "vy": 0.004 + 0.028 * rnd.random(),          # 上浮速度（占画高/秒）
            "spin": (rnd.random() - 0.5) * 0.9,          # 三角自转
            "tri": rnd.random() < 0.42,
            "a": 34 + int(104 * rnd.random()),
        })
    return out


def _make_rays(sc, n=44):
    """预生成从中心射出的柔光扇（角度、长度、色号）。逐帧只加一个旋转量。"""
    n = int(n)
    if n <= 0:
        return []
    rnd = random.Random(4321)
    out = []
    for k in range(n):
        a = (k + 0.35 * rnd.random()) * (2.0 * math.pi / n)
        out.append((a, sc.diag * (0.42 + 0.52 * rnd.random()), rnd.random()))
    return out


# ---------------------------------------------------------------- 超采样抗锯齿


class _SSDraw(object):
    """把绘制坐标按 ss 倍放大的 ImageDraw 代理 —— 超采样抗锯齿的芯。

    为什么必须有它（这是「看起来不光滑」的正解）
    ------------------------------------------------------------------
    PIL 的 ImageDraw 画多边形 / 椭圆 / 直线是**硬边**的：一个像素要么算
    "在里面"、要么算"在外面"，没有部分覆盖。所以斜边在像素级就是台阶。
    而渲染管线本来就是「半分辨率画 → ffmpeg lanczos 拉到成片」，
    台阶被再放大 2 倍 —— 把 orbit 的圆内示波环、aurora 的同心环放大 3 倍看，
    全是清清楚楚的楼梯（tools/probe_smooth.py 的局部放大图）。

    正统解法是**超采样**：按 ss 倍分辨率画，再 LANCZOS 缩回目标尺寸。
    ss=2 时每个目标像素由 4 个样本平均，台阶被摊成 2~3 级过渡带，
    眼睛看到的就是一条光滑曲线。

    ★ 只包**几何**，不包位图合成。发光层（缩→模糊→放大）和整帧 alpha
      合成仍在 1× 画布上做 —— 那两样才是逐帧的大头，一起放大的话
      渲染时间要再翻一倍，换来的差别肉眼看不见。
      位图素材（头像贴图、圆盘、光斑、渐变背景）本来就用 numpy/lanczos
      生成，天生带过渡带，不走这条路。

    ★ width 必须跟着放大。不放大就等于"在超采样空间画细一半、缩回来变成
      一条半透明灰线"，越描越淡（第一版就是这么翻车的）。
    """

    def __init__(self, draw, ss):
        self._d = draw
        self._ss = float(ss)
        self._fc = {}

    # 没有包装的方法（如 setink / flush）原样透传
    def __getattr__(self, k):
        return getattr(object.__getattribute__(self, "_d"), k)

    # ---- 坐标 / 线宽 ----

    def _p(self, xy):
        """缩放一组坐标。三种形状都要吃：扁平 [x0,y0,x1,y1] / 点表 / 单点。"""
        s = self._ss
        if not xy:
            return xy
        first = xy[0]
        if isinstance(first, (tuple, list)):
            return [(float(p[0]) * s, float(p[1]) * s) for p in xy]
        return tuple(float(v) * s for v in xy)

    def _wd(self, w):
        if w is None:
            return 1
        return max(1, int(round(float(w) * self._ss)))

    # ---- 几何（逐个显式定义，别靠 __getattr__ 兜底 —— 兜底会把坐标原样传下去，
    #      表现出来是"开了超采样但画面缩到左上角四分之一"，排查很久）----

    def line(self, xy, fill=None, width=1, joint=None):
        return self._d.line(self._p(xy), fill=fill, width=self._wd(width), joint=joint)

    def polygon(self, xy, fill=None, outline=None, width=1):
        return self._d.polygon(self._p(xy), fill=fill, outline=outline,
                               width=self._wd(width))

    def ellipse(self, xy, fill=None, outline=None, width=1):
        return self._d.ellipse(self._p(xy), fill=fill, outline=outline,
                               width=self._wd(width))

    def rectangle(self, xy, fill=None, outline=None, width=1):
        return self._d.rectangle(self._p(xy), fill=fill, outline=outline,
                                 width=self._wd(width))

    def rounded_rectangle(self, xy, radius=0, fill=None, outline=None, width=1):
        return self._d.rounded_rectangle(self._p(xy),
                                         radius=float(radius) * self._ss,
                                         fill=fill, outline=outline,
                                         width=self._wd(width))

    def arc(self, xy, start, end, fill=None, width=1):
        return self._d.arc(self._p(xy), start, end, fill=fill, width=self._wd(width))

    def chord(self, xy, start, end, fill=None, outline=None, width=1):
        return self._d.chord(self._p(xy), start, end, fill=fill,
                             outline=outline, width=self._wd(width))

    def pieslice(self, xy, start, end, fill=None, outline=None, width=1):
        return self._d.pieslice(self._p(xy), start, end, fill=fill,
                                outline=outline, width=self._wd(width))

    def point(self, xy, fill=None):
        s = self._ss
        if xy and isinstance(xy[0], (tuple, list)):
            pts = [(float(p[0]) * s, float(p[1]) * s) for p in xy]
        else:
            pts = tuple(float(v) * s for v in xy)
        return self._d.point(pts, fill=fill)

    def text(self, xy, text, *a, **kw):
        if kw.get("font") is not None:
            kw["font"] = self.font(kw["font"])
        if kw.get("stroke_width"):
            kw["stroke_width"] = int(round(float(kw["stroke_width"]) * self._ss))
        return self._d.text(self._p(xy), text, *a, **kw)

    def multiline_text(self, xy, text, *a, **kw):
        if kw.get("font") is not None:
            kw["font"] = self.font(kw["font"])
        if kw.get("stroke_width"):
            kw["stroke_width"] = int(round(float(kw["stroke_width"]) * self._ss))
        return self._d.multiline_text(self._p(xy), text, *a, **kw)

    # ---- 字体：按 ss 放大一份（带缓存，同一字号只加载一次）----

    def font(self, fnt):
        """把字体放大 ss 倍。拿不到 path/size（位图默认字体）就原样返回。

        ★ 排版用的 `getlength()` 必须继续用**原字号**的字体算 —— 宽度是
          线性量，原字号量出来的 x 坐标乘 ss 之后位置是对的。所以这里
          只在真正落笔时换字体，不动调用方的度量。
        """
        if fnt is None or self._ss <= 1.0:
            return fnt
        path = getattr(fnt, "path", None)
        size = getattr(fnt, "size", None)
        if not path or not size:
            return fnt
        px = max(6, int(round(float(size) * self._ss)))
        key = (path, px)
        f = self._fc.get(key)
        if f is None:
            try:
                f = ImageFont.truetype(path, px)
            except Exception:                                    # noqa: BLE001
                return fnt
            self._fc[key] = f
        return f


class Scene(object):
    """一次渲染里"每帧都不变"的东西全在这里算好，逐帧只做绘制。

    ★ 这里的 (w, h) 是**绘制分辨率**，不是最终画幅。默认只有最终画幅的一半：
      逐像素的活（渐变背景、发光层、暗角、alpha 合成）在 1080×1920 上要
      60~200 ms/帧，砍一半边长就是 1/4 的像素、差不多 1/4 的时间；
      最后交给 ffmpeg 用 lanczos 拉大到成片画幅。
      代价是柱子边缘会有一点点软 —— 但那反而比 PIL 全分辨率画出来的
      硬锯齿边好看。**字幕不走这条路**，它由 libass 在拉大之后烧，始终是清晰的。
    """

    def __init__(self, w, h, style, nb, cover=None):
        self.w, self.h, self.nb = w, h, nb
        self.style = style
        self.pal = palette(nb, style.get("palette", "neon"))
        self.cover = cover
        self.mind = float(min(w, h))
        self.diag = math.hypot(w, h)
        self.fps = 30
        self.dur = 0.0                 # 总时长（render_fx 会填）—— 片头片尾要用
        self.bg = _make_bg_array(w, h, style)
        self.bg_img = Image.fromarray(self.bg, "RGB")
        self.vig = _vignette_overlay(w, h, style.get("vig", 0.60))
        self.glow = _glow_sprite(256, self.pal[int(nb * float(style.get("glow_k", 0.55)))])
        self.small = (max(4, w // 4), max(4, h // 4))
        self._disc_cache = {}
        # ★ 粒子与光芒是**一次算好、逐帧只换位置**的静态表。
        #   参考片（AE 模板）里画面之所以"满"，靠的就是这两层：
        #   一层缓慢上浮的灰尘/三角碎片，一层从中心射出的柔光。
        #   没有它们，主体周围就是一大片纯渐变 —— 那正是"太难看"的来源。
        self.parts = _make_particles(self, style.get("dust", 1.0))
        self.rays = _make_rays(self, style.get("rays", 44))
        # 超采样倍率。1.0 = 关（老行为，探针的负对照用它）。
        # ★ 2.0 不是"更高的画质"，而是**把几何画在成片分辨率上**：
        #   本来 0.5 倍绘制再拉大 2 倍，超采样 2 倍正好把它抵消掉，
        #   同时多赚一次 4 点平均的抗锯齿。
        self.ss = 2.0

    # ---- 超采样 ----

    def set_supersample(self, ss):
        """改超采样倍率，返回实际生效值。1.0 = 关。"""
        self.ss = max(1.0, float(ss or 1.0))
        return self.ss

    def ss_canvas(self, ss=None):
        """开一张**超采样**透明图层。返回 `(draw, finish)`。

        坐标照**绘制分辨率**（self.w / self.h）给就行，`_SSDraw` 自己放大；
        `finish()` 无参调用，用 LANCZOS 缩回 1×，返回可直接
        `alpha_composite` 的 RGBA。

        ★ 为什么返回闭包而不是让调用方自己缩：缩的那一步必须用 **BOX**
          （盒式平均），不能想当然写 LANCZOS ——
          ① 超采样在数学上就是"面积平均"，整数倍时 BOX 精确等于 ss×ss 平均，
             这才是正解，不是妥协；
          ② LANCZOS 带振铃（边缘会冒一圈亮边），在纯色柱子上很显眼；
          ③ 实测 LANCZOS 缩 1080×1920 RGBA 要 60+ ms，BOX 只要几毫秒 ——
             用错这一个参数，3 分钟的歌从 2.2 分钟变 8.2 分钟。
        """
        s = self.ss if ss is None else max(1.0, float(ss))
        if s <= 1.001:
            lay = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
            return ImageDraw.Draw(lay), (lambda: lay)
        bw = max(2, int(round(self.w * s)))
        bh = max(2, int(round(self.h * s)))
        big = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        wh = (self.w, self.h)
        return _SSDraw(ImageDraw.Draw(big), s), (lambda: big.resize(wh, Image.BOX))

    def ss_patch(self, cx, cy, rw, rh, fn, ss=None):
        """在一小块超采样画布上画，缩回 1× 后贴到 (cx,cy, rw,rh) 这块地方。

        给「只有一个局部需要抗锯齿」的场合用（合唱台的头像圆/描边）——
        整帧超采样太贵，只在那一圈圆环上多采几个样，成本可以忽略。
        `fn(d, ox, oy)` 里用的是**这块补丁自己的坐标**（左上角为 0,0）。
        """
        s = self.ss if ss is None else max(1.0, float(ss))
        rw, rh = max(2, int(rw)), max(2, int(rh))
        if s <= 1.001:
            lay = Image.new("RGBA", (rw, rh), (0, 0, 0, 0))
            fn(ImageDraw.Draw(lay), 0, 0)
            return lay
        big = Image.new("RGBA", (max(2, int(rw * s)), max(2, int(rh * s))), (0, 0, 0, 0))
        fn(_SSDraw(ImageDraw.Draw(big), s), 0, 0)
        return big.resize((rw, rh), Image.BOX)

    # ---- 通用零件 ----
    def _glow_layer(self, lay, k=0.60):
        """缩→模糊→放大的发光层。全画幅模糊太慢，这样快一个量级。"""
        g = lay.resize(self.small, Image.BILINEAR).filter(ImageFilter.GaussianBlur(6))
        g = g.resize((self.w, self.h), Image.BILINEAR)
        g.putalpha(Image.eval(g.split()[3], lambda v: int(v * k)))
        return g

    def _disc(self, r, color):
        key = int(r)
        if key not in self._disc_cache:
            self._disc_cache = {key: _disc_sprite(r, color, self.cover)}
        return self._disc_cache[key]

    # ---- 氛围层（参考片里"画面很满"靠的就是这两样） ----

    def dust(self, ld, i, cx=None, cy=None):
        """一层缓慢上浮的灰尘/三角碎片。"""
        if not self.parts:
            return
        t = i / float(max(1, self.fps))
        w, h = self.w, self.h
        for p in self.parts:
            y = (p["y"] - p["vy"] * t) % 1.0
            x = (p["x"] + p["sw"] * math.sin(t * 0.45 + p["ph"])) % 1.0
            X, Y = x * w, y * h
            c = self.pal[p["c"]]
            if p["tri"]:
                R = p["r"] * 2.3
                a = p["ph"] + t * p["spin"]
                pts = [(X + math.cos(a + k * 2.0944) * R, Y + math.sin(a + k * 2.0944) * R)
                       for k in range(3)]
                ld.polygon(pts, fill=c + (p["a"],))
            else:
                r = p["r"]
                ld.ellipse([X - r, Y - r, X + r, Y + r],
                           fill=tint(c, 1.35) + (min(255, p["a"] + 46),))

    def beam(self, ld, i, cx, cy, alpha=24, scale=1.0):
        """从中心射出的柔光扇，缓慢旋转。

        ★ 别把整张光芒做成一张大图每帧 `rotate()` —— 1200×1200 的 RGBA 转一次
          要 15~25 ms，比画频谱还贵。这里就是一堆多边形，一帧 1~2 ms。

        ★ 关键在于**沿长度分段降 alpha**。第一版一根光只有一个多边形，
          外端在半径 L 处戛然而止，44 根排一圈出来就是一台"彩色风车"，
          硬边一眼就看见。切成三段（1 / 0.5 / 0.18 倍 alpha）之后，
          光自然淡出，才像光而不像扇叶。
        """
        t = i / float(max(1, self.fps))
        rot = t * 0.055
        segs = ((0.00, 0.42, 1.00), (0.42, 0.76, 0.50), (0.76, 1.00, 0.18))
        for (a, ln, k) in self.rays:
            aa = a + rot
            ln = ln * scale
            c = self.pal[int(k * (self.nb - 1))]
            # 颜色本身也要带一点色相宽度感：外段偏亮，内段偏原色
            for (f0, f1, fa) in segs:
                r0, r1 = ln * f0, ln * f1
                sp = 0.014 + 0.030 * k
                A = int(alpha * fa)
                if A <= 1:
                    continue
                ca0, sa0 = math.cos(aa - sp), math.sin(aa - sp)
                ca1, sa1 = math.cos(aa + sp), math.sin(aa + sp)
                ld.polygon([(cx + ca0 * r0, cy + sa0 * r0),
                            (cx + ca1 * r0, cy + sa1 * r0),
                            (cx + ca1 * r1, cy + sa1 * r1),
                            (cx + ca0 * r1, cy + sa0 * r1)],
                           fill=(tint(c, 1.0 + 0.5 * f1) if fa > 0.4 else c) + (A,))

    def pulses(self, ld, state, i, cx, cy, r0, bass):
        """低音一来就往外交一圈脉冲环。

        环的位置/透明度**必须跨帧保存**（在 state 里），否则每帧重算就成了一张
        静止的同心圆 —— 那是"没有动"的意思，恰恰不是我们要的。
        半径按 mind 归一，换分辨率不用改参数。
        """
        if state is None:
            return
        rings = state.setdefault("rings", [])
        t = i / float(max(1, self.fps))
        prev = state.get("last_ring", -9.0)
        if bass > 0.55 and (t - prev) > 0.26:
            rings.append({"t": t, "k": r0 * 0.12 + 0.5 * bass})
            state["last_ring"] = t
            if len(rings) > 6:
                del rings[0]
        for rg in rings:
            age = t - rg["t"]
            if age > 1.5:
                continue
            r = r0 + self.mind * (0.34 * age + rg["k"] * age)
            a = int(150 * max(0.0, 1.0 - age / 1.5) ** 1.35)
            if a <= 3 or r < 3:
                continue
            lw = max(1, int(self.mind * 0.0042 * (1.0 - 0.5 * age / 1.5)))
            ld.ellipse([cx - r, cy - r, cx + r, cy + r],
                       outline=tint(self.pal[int(self.nb * 0.6)], 1.3) + (a,), width=lw)

    def blank(self):
        """每帧的底：背景铺满。返回 RGBA。

        ★ 用 `paste(Image)` 而不是 `Image.fromarray(ndarray)` —— 后者每帧都要
          重新包装一遍 ndarray，白白多花几毫秒。
        """
        img = Image.new("RGBA", (self.w, self.h))
        img.paste(self.bg_img, (0, 0))
        return img

    def _finish(self, img):
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        img.alpha_composite(self.vig)
        return img.convert("RGB")


# ---------------------------------------------------------------- 五种画法


def draw_orbit(sc, an, i, state):
    """光环律动：中心圆盘 + 一整圈径向频谱 + 低音脉冲环 + 漂浮粒子。

    这是照着老板给的参考片（AE 模板那套环形音频可视化）重做的版本。第一版只有
    "上半圆一扇形柱 + 峰值白点"，在 1080×1920 上就是中间一只眼睛、上下两大片空
    渐变 —— 原话「太难看了」。这一版把三样东西补上：

      1. 频谱**绕满一圈**（低音正上方、高音正下方、左右镜像），画面立刻有包围感；
      2. 低音触发**向外扩散的脉冲环**，耳朵听到的"咚"眼睛能看见；
      3. 一层**缓慢上浮的粒子**和从中心射出的**柔光扇**，主体周围不再是空白。
    """
    w, h, nb = sc.w, sc.h, sc.nb
    spec, peak, bass, wave = an["spec"], an["peak"], an["bass"], an["wave"]
    mind = sc.mind
    cx, cy = w / 2.0, h * 0.452
    b = float(bass[i])
    r0 = mind * 0.172                       # 中心圆盘半径
    hub = r0 * 1.20                         # 频谱柱根部半径
    maxlen = mind * 0.252                   # 柱子最长能伸多远
    img = sc.blank()

    # 背景大光晕：跟着低音呼吸。比第一版大一圈，把主体周围整片点亮。
    gr = int(mind * (0.60 + 0.32 * b))
    img.alpha_composite(sc.glow.resize((gr * 2, gr * 2), Image.BILINEAR),
                        (int(cx - gr), int(cy - gr)))

    # ★ 超采样画布：柱子的斜边、脉冲环、粒子三角全靠它才不是台阶。
    #   `ld` 收的是 1× 坐标，内部按 ss 倍放大画，done() 用 LANCZOS 缩回来。
    ld, _done = sc.ss_canvas()

    # 柔光扇 + 粒子 + 脉冲环：全部画在同一层，最后只做**一次**发光模糊。
    # ★ 每多一层 `_glow_layer` 就多一次"缩→模糊→放大"，一帧多 5~7 ms。
    #   四样东西各来一遍的话 3 分钟的歌要多渲两三分钟，不值。
    sc.beam(ld, i, cx, cy, alpha=20)
    sc.dust(ld, i)

    # 频谱：低音在正上方（-90°），沿两侧向下铺到高音，左右镜像。
    half = max(2, nb // 2)
    halfw = max(2.0, mind * 0.0074)
    cs = [sc.pal[int(j * (nb - 1) / float(max(1, half - 1)))] for j in range(half)]
    for j in range(half):
        v = float(spec[i, j])
        # ★ 指数 0.92 而不是 1.15：高音本来就比低音弱，指数 >1 会把下半圈压成
        #   一排看不见的小点。再给 7% 的底长，安静段落也不会整圈消失。
        L = hub + maxlen * (0.07 + 0.93 * (v ** 0.92))
        ang = math.pi * (j + 0.5) / float(half)
        c = cs[j]
        for sgn in (1.0, -1.0):
            a = -math.pi / 2.0 + sgn * ang
            ca, sa = math.cos(a), math.sin(a)
            pa, pb = -sa, ca                 # 柱子的宽度方向
            tip = 0.30
            ld.polygon([(cx + ca * hub + pa * halfw, cy + sa * hub + pb * halfw),
                        (cx + ca * hub - pa * halfw, cy + sa * hub - pb * halfw),
                        (cx + ca * L - pa * halfw * tip, cy + sa * L - pb * halfw * tip),
                        (cx + ca * L + pa * halfw * tip, cy + sa * L + pb * halfw * tip)],
                       fill=c + (255,))
            # 峰值小帽：一眼看出"刚才最响到哪"。
            if state is not None:
                Lp = hub + maxlen * (0.07 + 0.93 * (float(peak[i, j]) ** 0.92))
                x, y = cx + ca * Lp, cy + sa * Lp
                r = max(1.3, halfw * 0.42)
                ld.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, 120))

    # 脉冲环放在频谱**下面**画（先画环再画柱？不 —— 环要压住柱子尾部才像"穿过"）。
    # 这里刻意后画：环扫过柱阵时把柱子切一下，动感比被柱子挡住强得多。
    sc.pulses(ld, state, i, cx, cy, hub, b)

    # 两道很淡的结构环：给整圈柱子一个"轨道"，不然柱子像浮在空中
    for k, rr in enumerate((hub, hub + maxlen * 0.62)):
        ln = max(1, int(mind * 0.0028))
        ld.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                   outline=tint(cs[int(half * 0.5)], 1.1) + (34 - 10 * k,), width=ln)

    lay = _done()                       # 超采样层缩回 1×
    img.alpha_composite(sc._glow_layer(lay, 0.62))
    img.alpha_composite(lay)

    # 中心圆盘（封面或渐变球）
    rr = max(10, int(r0 * (1.0 + 0.055 * b)))
    disc, has_cover = sc._disc(rr, sc.pal[nb // 2])
    img.alpha_composite(disc, (int(cx - rr), int(cy - rr)))
    d, _done2 = sc.ss_canvas()          # 描边和示波环同样走超采样
    ring = sc.pal[nb // 2]
    d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
              outline=ring + (240,), width=max(2, int(rr * 0.045)))
    # ★ 外面那圈**别用比背景暗的颜色**：画在亮光晕上会变成两道显眼的深色弧
    #   （第一版用 tint(ring, 0.7)，斜对角上就出现了两条莫名其妙的黑弧）。
    d.ellipse([cx - rr * 1.08, cy - rr * 1.08, cx + rr * 1.08, cy + rr * 1.08],
              outline=tint(ring, 1.25) + (85,), width=max(1, int(rr * 0.018)))

    # 圆内示波环：把这一帧的波形盘成一个小圈，像是"里面在转"
    # ★ 先做 3 点滑动平均再画。原始波形逐点相连会画成一团毛线（第一版就是），
    #   抹一下立刻变成一条有起伏的曲线，好看得多。
    if not has_cover:
        wr = rr * 0.72
        npts = int(wave.shape[1])
        wv = wave[i].astype(np.float32)
        sm = (np.roll(wv, 1) + 2.0 * wv + np.roll(wv, -1)) / 4.0
        pts = []
        for j in range(npts):
            a = (j / float(npts)) * 2 * math.pi - math.pi / 2.0
            rad = wr * (0.58 + 0.42 * abs(float(sm[j])))
            pts.append((cx + math.cos(a) * rad, cy + math.sin(a) * rad))
        pts.append(pts[0])
        d.line(pts, fill=tint(sc.pal[int(nb * 0.75)], 1.35) + (215,),
               width=max(1, int(sc.mind * 0.0034)))
    img.alpha_composite(_done2())
    return sc._finish(img)


def draw_mirror(sc, an, i, state):
    """对称声柱：横向一排锥形柱，从中央轴线上下对称展开，带倒影。"""
    w, h, nb = sc.w, sc.h, sc.nb
    spec, peak = an["spec"], an["peak"]
    axis = h * 0.46
    img = sc.blank()
    b = float(an["bass"][i])

    # 三处光晕（中间大、两侧小）拼出一条横向的光带 —— 单一个中心光斑会让
    # 画面左右两半看着像"没画完"。
    # ★ 但是三处一起加会叠成一片糊掉的绿色（第一版就是，整幅发绿）。
    #   光晕是"垫底"，不是主角：半径和数量都要收着给。
    for fx, fs in ((0.5, 0.62), (0.16, 0.36), (0.84, 0.36)):
        gr = int(sc.mind * (fs * (0.60 + 0.30 * b)))
        img.alpha_composite(sc.glow.resize((gr * 2, gr * 2), Image.BILINEAR),
                            (int(w * fx - gr), int(axis - gr)))

    ld, _done = sc.ss_canvas()
    sc.dust(ld, i)
    m = w * 0.055
    step = (w - 2 * m) / float(nb)
    bw = step * 0.62
    maxh = h * 0.225
    for k in range(nb):
        v = float(spec[i, k])
        H = maxh * (0.05 + 0.95 * (v ** 0.95))
        x0 = m + k * step + (step - bw) / 2.0
        x1 = x0 + bw
        c = sc.pal[k]
        # 柱身：上亮下暗的竖向渐变。平板一块色会显得很"PPT"。
        if H >= 2:
            ld.rectangle([x0, axis - H, x1, axis - H * 0.45], fill=tint(c, 0.72) + (255,))
            ld.rectangle([x0, axis - H * 0.45, x1, axis], fill=c + (255,))
            ld.rectangle([x0, axis - H * 0.05, x1, axis], fill=tint(c, 1.45) + (255,))
        # 倒影：同形状压暗 + 变短，像水面
        if H >= 2:
            rh = H * 0.55
            ld.rectangle([x0, axis, x1, axis + rh * 0.45], fill=tint(c, 0.60) + (95,))
            ld.rectangle([x0, axis + rh * 0.45, x1, axis + rh], fill=tint(c, 0.45) + (55,))
        # 峰值小帽
        if state is not None:
            ph = maxh * (0.05 + 0.95 * (float(peak[i, k]) ** 0.95))
            ld.rectangle([x0, axis - ph - 4, x1, axis - ph], fill=(255, 255, 255, 225))

    lay = _done()
    img.alpha_composite(sc._glow_layer(lay, 0.55))
    img.alpha_composite(lay)

    # 轴线：中间那条细亮线，把上下两半分开
    d, _done2 = sc.ss_canvas()
    lw = max(1, int(sc.mind * 0.0022))
    d.line([(m * 0.5, axis), (w - m * 0.5, axis)], fill=(255, 255, 255, 90), width=lw)
    img.alpha_composite(_done2())
    return sc._finish(img)


def draw_aurora(sc, an, i, state):
    """极光同心：一圈圈同心椭圆的半径/透明度跟着频带走，像极光在呼吸。"""
    w, h, nb = sc.w, sc.h, sc.nb
    spec, bass = an["spec"], an["bass"]
    cx, cy = w / 2.0, h * 0.45
    b = float(bass[i])
    img = sc.blank()

    # ★ 光晕要克制。第一版给了 1.5 倍半径，结果整个画面变成一团过曝的蓝，
    #   同心环全被淹了 —— 极光样式的主角是**环**，光晕只是垫底。
    gr = int(sc.mind * (0.62 + 0.30 * b))
    img.alpha_composite(sc.glow.resize((gr * 2, gr * 2), Image.BILINEAR),
                        (int(cx - gr), int(cy - gr)))

    ld, _done = sc.ss_canvas()
    sc.dust(ld, i)
    base = sc.mind * 0.050
    gap = sc.mind * 0.0235
    lw = max(2, int(sc.mind * 0.0062))
    npts = 72
    # ★ 让环缓慢向外"漂"：8 秒一轮。静止的同心环像是在看靶子，
    #   有了整体外移就有"极光在流动"的意思了。
    drift = ((i / float(max(1, sc.fps))) % 8.0) / 8.0
    # 步进 2：64 个环挤在一起会糊成一团灰，隔一个画一个有"层次"。
    for k in range(0, nb, 2):
        v = float(spec[i, k])
        r = base + (k + drift * 2.0) * gap * 0.5 + v * gap * 3.2
        if r < 3:
            continue
        # 越靠外越淡，边缘自然消失（不然最外圈会硬生生被画幅切断）
        edge = max(0.0, min(1.0, (1.0 - r / (sc.mind * 0.80))))
        a = int((26 + 190 * (v ** 1.15)) * (0.44 + 0.56 * edge))
        if a <= 4:
            continue
        col = sc.pal[k]
        # ★ 关键：环**不能是正圆**。正圆一圈圈同心出来就是一张靶纸。
        #   把半径按 θ 做一点低频起伏，立刻变成极光那种"帘幕"的形状。
        #   ★ 但起伏必须**小**、而且相邻环之间要**差不多**（相位随 k 缓变）：
        #     第一版起伏给到 ±17% 且相位按 0.85k 跳，相邻环大面积交叉，
        #     整幅变成一团乱麻（实测）。±5% + 0.22k 才是"流动的帘"。
        amp = 0.018 + 0.048 * v
        ph = k * 0.22 + drift * 6.283
        pts = []
        for j in range(npts):
            th = j * 2.0 * math.pi / npts
            rr = r * (1.0 + amp * math.sin(5.0 * th + ph)
                      + amp * 0.55 * math.sin(8.0 * th - ph * 0.7))
            pts.append((cx + math.cos(th) * rr, cy + math.sin(th) * rr * 0.88))
        pts.append(pts[0])
        ld.line(pts, fill=col + (a,), width=lw, joint="curve")
    # 低音脉冲环压在极光上面：让"咚"有一个明确的时间点
    sc.pulses(ld, state, i, cx, cy, sc.mind * 0.10, b)

    lay = _done()
    img.alpha_composite(sc._glow_layer(lay, 0.62))
    img.alpha_composite(lay)

    rr = max(10, int(base * 2.3 * (1.0 + 0.12 * b)))
    disc, _ = sc._disc(rr, sc.pal[int(nb * 0.35)])
    img.alpha_composite(disc, (int(cx - rr), int(cy - rr)))
    d, _done2 = sc.ss_canvas()
    d.ellipse([cx - rr * 1.10, cy - rr * 1.10, cx + rr * 1.10, cy + rr * 1.10],
              outline=tint(sc.pal[int(nb * 0.35)], 1.25) + (70,),
              width=max(1, int(rr * 0.045)))
    img.alpha_composite(_done2())
    return sc._finish(img)


def draw_pulse(sc, an, i, state):
    """声波脉冲：一条填了渐变的横向声波，上下对称，背景一圈大光晕。"""
    w, h, nb = sc.w, sc.h, sc.nb
    wave, bass = an["wave"], an["bass"]
    b = float(bass[i])
    cy = h * 0.455
    img = sc.blank()

    # 三处光晕叠成一片**竖向**的光，把一条横波包在中间。
    # ★ 位置要挨近、颜色要暖：第一版三处摊到 0.32/0.47/0.66 且用的是
    #   "日落"色板的中段（那里其实是青色），画出来是上下两盏青色车灯。
    #   光晕颜色现在由 style["glow_k"] 指定，日落色板取暖色段。
    for fy, fs in ((0.44, 0.66), (0.30, 0.44), (0.62, 0.44)):
        gr = int(sc.mind * (fs * (0.62 + 0.34 * b)))
        img.alpha_composite(sc.glow.resize((gr * 2, gr * 2), Image.BILINEAR),
                            (int(w / 2 - gr), int(h * fy - gr)))

    n = int(wave.shape[1])
    amp = h * 0.155
    m = w * 0.05
    xs = np.linspace(m, w - m, n)
    # ★ 极值包络是"取窗口内最大绝对值"，直接连起来会画成一节节的方波
    #   （实测看着像坏了）。抹两道 3 点平均就顺了。
    wv = wave[i].astype(np.float32)
    for _ in range(2):
        wv = (np.roll(wv, 1) + 2.0 * wv + np.roll(wv, -1)) / 4.0
    ys = wv * amp * (0.62 + 0.58 * b)

    ld, _done = sc.ss_canvas()
    sc.dust(ld, i)
    # ★ 这个样式**不画光芒扇**。第一版给它加了 28 根彩色光扇，结果整幅是一台
    #   风车，声波反而看不见了 —— 光芒和声波都是"横贯画面"的构图，互抢。
    # 上下各一条曲线，中间填成一个**实心波身**（比两条线有分量得多）
    top, bot = wave_body(xs, ys, cy, n)
    mid = sc.pal[nb // 2]
    ld.polygon(top + bot, fill=mid + (120,))
    lw = max(2, int(sc.mind * 0.0045))
    for j in range(n - 1):
        t = j / float(max(1, n - 1))
        c = sc.pal[int(t * (nb - 1))]
        ld.line([top[j], top[j + 1]], fill=c + (255,), width=lw)
        ld.line([(top[j][0], 2 * cy - top[j][1]), (top[j + 1][0], 2 * cy - top[j + 1][1])],
                fill=c + (74,), width=lw)
    sc.pulses(ld, state, i, w / 2.0, cy, h * 0.06, b)

    lay = _done()
    img.alpha_composite(sc._glow_layer(lay, 0.85))
    img.alpha_composite(lay)

    # ★ 这里原本**连着做了两遍** glow + composite（复制粘贴留下的）：
    #   画面被叠亮一层，而且 `_glow_layer` 是逐帧最贵的一步（缩→模糊→放大），
    #   白扔掉这一样式接近一半的渲染时间。删掉第二遍 —— 视觉上几乎无差
    #   （第二层的 alpha 打 0.85 折再叠上去，只是把已经很亮的波身再推亮 8%）。
    #   verify_viz 的 G 段钉住"每帧只做一次 glow"。

    d, _done2 = sc.ss_canvas()
    lw2 = max(1, int(sc.mind * 0.0020))
    d.line([(m * 0.6, cy), (w - m * 0.6, cy)], fill=(255, 255, 255, 70), width=lw2)
    img.alpha_composite(_done2())
    return sc._finish(img)


def draw_tunnel(sc, an, i, state):
    """光之隧道：一层层往里推进的六边形框架 + 中心圆盘 + 环绕频谱。

    照着老板给的参考包里的 `Audio React Tunnel` 那套做的 —— 那套之所以"高级"，
    是因为它给了画面**纵深**：几层发光的框一圈比一圈小、还各自慢慢转，
    眼睛会自动把"越来越小"读成"往里去"。平面的一排柱子永远给不了这个。
    """
    w, h, nb = sc.w, sc.h, sc.nb
    spec, bass = an["spec"], an["bass"]
    mind = sc.mind
    cx, cy = w / 2.0, h * 0.452
    b = float(bass[i])
    img = sc.blank()

    gr = int(mind * (0.72 + 0.30 * b))
    img.alpha_composite(sc.glow.resize((gr * 2, gr * 2), Image.BILINEAR),
                        (int(cx - gr), int(cy - gr)))

    rmax = mind * 0.66
    r0 = mind * 0.150
    t = i / float(max(1, sc.fps))

    ld, _done = sc.ss_canvas()
    sc.beam(ld, i, cx, cy, alpha=18, scale=1.25)
    sc.dust(ld, i)

    # 隧道壁：从外往里画，越里面越亮越粗。
    # 每层用相邻频带的能量决定亮度 —— 于是"往里冲"的速度是跟着音乐走的。
    depth = 5
    for d in range(depth):
        k = int(nb * (0.75 - 0.13 * d))
        v = float(spec[i, max(0, min(nb - 1, k))])
        R = rmax * (1.0 - 0.155 * d) * (1.0 + 0.022 * b)
        rot = math.radians(d * 7.5 - 90.0 + t * (3.2 + 5.0 * v))
        pts = [(cx + math.cos(rot + m * math.pi / 3.0) * R,
                cy + math.sin(rot + m * math.pi / 3.0) * R) for m in range(6)]
        a = int(60 + 150 * (d / float(depth - 1)) + 45 * v)
        lw = max(2, int(mind * (0.0022 + 0.0028 * d)))
        col = sc.pal[int((d / float(depth - 1)) * (nb - 1))]
        ld.polygon(pts, outline=tint(col, 1.15) + (min(255, a),), width=lw)
        # 顶点上往外挑一小段，读起来像"支架"而不是一个孤零零的六边形
        for m in range(6):
            a0 = rot + m * math.pi / 3.0
            ex, ey = cx + math.cos(a0) * R, cy + math.sin(a0) * R
            ln = mind * 0.030 * (1.0 + 0.6 * v)
            ld.line([(ex, ey), (ex + math.cos(a0) * ln, ey + math.sin(a0) * ln)],
                    fill=tint(col, 1.3) + (min(255, a),), width=max(1, lw - 1))

    # 环绕中心圆盘的频谱
    half = max(2, nb // 2)
    hub = r0 * 1.24
    maxlen = mind * 0.155
    halfw = max(2.0, mind * 0.0068)
    cs = [sc.pal[int(j * (nb - 1) / float(max(1, half - 1)))] for j in range(half)]
    for j in range(half):
        v = float(spec[i, j])
        L = hub + maxlen * (0.07 + 0.93 * (v ** 0.92))
        ang = math.pi * (j + 0.5) / float(half)
        c = cs[j]
        for sgn in (1.0, -1.0):
            a = -math.pi / 2.0 + sgn * ang
            ca, sa = math.cos(a), math.sin(a)
            pa, pb = -sa, ca
            tip = 0.32
            ld.polygon([(cx + ca * hub + pa * halfw, cy + sa * hub + pb * halfw),
                        (cx + ca * hub - pa * halfw, cy + sa * hub - pb * halfw),
                        (cx + ca * L - pa * halfw * tip, cy + sa * L - pb * halfw * tip),
                        (cx + ca * L + pa * halfw * tip, cy + sa * L + pb * halfw * tip)],
                       fill=c + (255,))
    sc.pulses(ld, state, i, cx, cy, hub, b)

    lay = _done()
    img.alpha_composite(sc._glow_layer(lay, 0.66))
    img.alpha_composite(lay)

    rr = max(10, int(r0 * (1.0 + 0.055 * b)))
    disc, _ = sc._disc(rr, sc.pal[nb // 2])
    img.alpha_composite(disc, (int(cx - rr), int(cy - rr)))
    dd, _done2 = sc.ss_canvas()
    ring = sc.pal[nb // 2]
    dd.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
               outline=ring + (240,), width=max(2, int(rr * 0.045)))
    dd.ellipse([cx - rr * 1.09, cy - rr * 1.09, cx + rr * 1.09, cy + rr * 1.09],
               outline=tint(ring, 1.3) + (80,), width=max(1, int(rr * 0.018)))
    img.alpha_composite(_done2())
    return sc._finish(img)


# ---------------------------------------------------------------- 合唱台（多角色）

# 头像贴图 / 字体都按"路径+尺寸+明暗"缓存：逐帧重开图片是这类渲染最贵的浪费，
# 一幅 8 人 × 5400 帧的片子会因此多花几分钟。
_AV_CACHE = {}
_CAST_FONTS = {}


def _hex_rgb(s, fallback=(0x6C, 0x8C, 0xFF)):
    s = str(s or "").strip()
    if s.startswith("#") and len(s) >= 7:
        try:
            return (int(s[1:3], 16), int(s[3:5], 16), int(s[5:7], 16))
        except Exception:                      # noqa: BLE001
            return fallback
    return fallback


def _cast_avatar(path, size, dim=False):
    """圆形头像贴图。没有图返回 None（调用方画带首字母的圆，别留一个洞）。"""
    key = (path or "", int(size), bool(dim))
    if key in _AV_CACHE:
        return _AV_CACHE[key]
    im = None
    if path and os.path.isfile(path):
        try:
            im = Image.open(path).convert("RGB")
            w, h = im.size
            s = min(w, h)
            im = im.crop(((w - s) // 2, (h - s) // 2,
                          (w - s) // 2 + s, (h - s) // 2 + s))
            im = im.resize((int(size), int(size)), Image.LANCZOS)
        except Exception:                      # noqa: BLE001
            im = None
    if im is not None and dim:
        # 没在唱的人：**去饱和 + 压暗一起上**。
        #   只压暗 → 变成一坨黑；只去饱和 → 还是抢眼睛。两样一起才叫"退到背景"。
        g = im.convert("L").convert("RGB")
        im = Image.blend(im, g, 0.70)
        im = Image.eval(im, lambda v: int(v * 0.50))
    if im is None:
        _AV_CACHE[key] = None
        return None
    # 圆形遮罩按 4 倍画再缩回 —— 这是**缓存**的贴图，超采样一次不花钱，
    # 而硬边 ellipse 会在头发丝、下巴这些斜边上留一整圈台阶
    # （放大看就是"头像边上有一圈锯齿毛边"，比柱子上的台阶更难看，因为眼睛
    #   对人脸边缘特别敏感）。
    n = int(size)
    big = max(8, n * 4)
    mask_big = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask_big).ellipse([0, 0, big - 1, big - 1], fill=255)
    mask = mask_big.resize((n, n), Image.BOX)
    out = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    _AV_CACHE[key] = out
    return out


def _cast_font(size, bold=False):
    key = (int(size), bool(bold))
    if key in _CAST_FONTS:
        return _CAST_FONTS[key]
    names = (r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
             r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc",
             r"C:\Windows\Fonts\simhei.ttf")
    f = None
    for p in names:
        if not os.path.exists(p):
            continue
        try:
            f = ImageFont.truetype(p, max(6, int(size)))
            break
        except Exception:                      # noqa: BLE001
            continue
    if f is None:
        f = ImageFont.load_default()
    _CAST_FONTS[key] = f
    return f


def _make_bg_array(w, h, style):
    """背景：渐变（默认）/ 纯色 / 用户自己的图片。

    ★ 为什么放这层而不是每种画法各写一遍：背景是"每帧不变"的东西，六种画法
      都要它。放这里改一次，六种同时生效。
    ★ 用户图片一定要**模糊 + 压暗**：原图直接铺底下，歌词和头像会糊在一起看不清。
      模糊用"缩到 1/8 再放大"，比全画幅高斯快一个量级（一首歌 5000 帧，差得出来）。
    ★ 图坏了就退回渐变，不许因为一张背景图让整条渲染崩掉。
    """
    img_path = style.get("bg_image")
    if img_path and os.path.isfile(img_path):
        try:
            im = Image.open(img_path).convert("RGB")
            iw, ih = im.size
            k = max(w / float(iw), h / float(ih))
            im = im.resize((max(1, int(iw * k + 0.5)), max(1, int(ih * k + 0.5))),
                           Image.LANCZOS)
            ox, oy = (im.size[0] - w) // 2, (im.size[1] - h) // 2
            im = im.crop((ox, oy, ox + w, oy + h))
            im = im.resize((max(2, w // 8), max(2, h // 8)), Image.BILINEAR)
            im = im.filter(ImageFilter.GaussianBlur(3)).resize((w, h), Image.BILINEAR)
            im = Image.eval(im, lambda v: int(v * 0.46 + 6))
            return np.asarray(im, dtype=np.uint8)
        except Exception:                                  # noqa: BLE001
            pass
    c = style.get("bg_color")
    if c:
        top = tuple(float(v) for v in c)
        bot = tuple(v * 0.42 for v in top)
        return _gradient_bg(w, h, top, bot, style.get("scrim", 0.72),
                            haze=style.get("haze"))
    return _gradient_bg(w, h, style.get("bg_top", (13, 15, 32)),
                        style.get("bg_bot", (30, 12, 40)),
                        style.get("scrim", 0.72), haze=style.get("haze"))


def _fit_font(text, size, bold, max_px):
    """把字号缩到这一行放得下。返回 (字体, 实际字号)。"""
    fnt = _cast_font(size, bold)
    while max_px and fnt.getlength(text) > max_px and size > 14:
        size = int(size * 0.92)
        fnt = _cast_font(size, bold)
    return fnt, size


def _draw_line(ld, cx, text, yy, size, fill, bold, stroke=0,
               prog=None, done_fill=None, max_w=None, align="center"):
    """居中（或左对齐）画一行；给了 `prog`（0~1）就做**逐句扫色**。

    ★ 扫色必须**逐字画**。PIL 没有"文字裁剪"，一把画两遍会出现两层字叠加：
      交界那个字会被后画的那层压掉半条边。逐字画虽然多几次绘制调用，
      但它只画一次、颜色按进度切，边界是干净的。
    ★ 进度按**字数**均分（不按词级时间戳）—— 老板要的就是"大概跟着唱"，
      而词级时间戳我们手上没有，也不值得为它多跑一遍识别。
    """
    if not text:
        return size
    fnt, size = _fit_font(text, size, bold, max_w)
    total = fnt.getlength(text)
    x = cx - total / 2.0 if align == "center" else cx
    if prog is None:
        ld.text((x, yy), text, font=fnt, fill=fill,
                stroke_width=stroke, stroke_fill=(0, 0, 0, 150))
        return size
    n_done = int(round(len(text) * max(0.0, min(1.0, prog))))
    for k, ch in enumerate(text):
        ld.text((x, yy), ch, font=fnt,
                fill=(done_fill if k < n_done else fill),
                stroke_width=stroke, stroke_fill=(0, 0, 0, 150))
        x += fnt.getlength(ch)
    return size


# ★ 合唱句（分配表里 role = -1，见 multising.ROLE_ALL）在画面上的写法。
#   值必须与前端 MULTI_CHORUS / 后端 ROLE_ALL 同一个约定 —— 三处写死同一个 -1，
#   任一一处改了另外两处不动 ⇒ "标了合唱但画面/声音还是独唱"，且不报错。
CHORUS_LABEL = "合唱："
CHORUS_RGBA = (255, 212, 96, 252)      # 与"已唱到"的金色一致：一眼看出是合唱

CAST_OPT_DEFAULT = {
    "karaoke": True,      # 逐句扫色（当前这句从白到黄）
    "motion": True,       # 谁唱谁动（起伏 + 电平条）
    "singer": True,       # 当前句前面带上演唱者的名字（用他自己的颜色）
    "layout": "bottom",   # 歌词排版：bottom / center / side
    "intro": 0.0,         # 片头秒数
    "outro": 0.0,         # 片尾秒数（淡出）
}


def _draw_named(ld, cx, name, text, yy, size, fill, name_fill, bold,
                stroke=0, prog=None, done_fill=(255, 212, 96, 252),
                max_w=None, align="center"):
    """一行里画「演唱者名字 + 歌词」，名字用他自己的颜色。

    ★ 名字为什么不参与扫色：扫色是"唱到哪个字哪个字变黄"，而名字根本没被
      唱出来 —— 让它跟着扫，画面上就是开头的几个字先黄，看着像那几个字是歌词。

    ★ 为什么是**同行前缀**而不是上方单独一行：上下两行之間塞不进去。
      实测过：横屏 bottom 排版，上一句和当前句之间只有 4.6px 空隙（按字形
      实际墨迹高算），而名字行要 11.7px —— 抬上一句又会压到头像块。
      同行前缀一行不动，代价只是正文可用宽度少了一截（_fit_font 会自己缩）。
    """
    if not text:
        return _draw_line(ld, cx, name, yy, size, name_fill, bold,
                          stroke=stroke, max_w=max_w, align=align)
    fnt, size = _fit_font(name + text, size, bold, max_w)
    total = fnt.getlength(name + text)
    x = cx - total / 2.0 if align == "center" else cx
    # ★ 名字**不描边**。正文用 2px 黑描边是为了压在画面上还看得清，
    #   但名字是彩色小字，2px 描边会把笔画内部整个吃掉 —— 实测渲出来
    #   整行只剩白色正文和一圈黑边，角色色一个像素都找不到。
    ld.text((x, yy), name, font=fnt, fill=name_fill)
    x += fnt.getlength(name)
    if prog is None:
        ld.text((x, yy), text, font=fnt, fill=fill,
                stroke_width=stroke, stroke_fill=(0, 0, 0, 150))
        return size
    n_done = int(round(len(text) * max(0.0, min(1.0, prog))))
    for k, ch in enumerate(text):
        ld.text((x, yy), ch, font=fnt,
                fill=(done_fill if k < n_done else fill),
                stroke_width=stroke, stroke_fill=(0, 0, 0, 150))
        x += fnt.getlength(ch)
    return size


def cast_opt_of(style):
    """把界面上给的选项收敛成一份可用的配置（缺省 = 今天的样子 + 扫色 + 动效）。"""
    o = dict(CAST_OPT_DEFAULT)
    for k, v in (style.get("cast_opt") or {}).items():
        if k in o:
            o[k] = v
    for k in ("intro", "outro"):
        try:
            o[k] = max(0.0, min(float(o.get(k) or 0), 8.0))
        except (TypeError, ValueError):
            o[k] = 0.0
    if o.get("layout") not in ("bottom", "center", "side"):
        o["layout"] = "bottom"
    o["karaoke"] = bool(o.get("karaoke"))
    o["motion"] = bool(o.get("motion"))
    o["singer"] = bool(o.get("singer"))
    return o


def _cast_layout(w, h, n, mind):
    """头像摆哪：横屏一排（保持原样）；竖屏按人数分 2/3/4 列。

    返回 ([(cx, cy), ...], r, 块底边 y)。

    ★ 竖屏必须重排：竖屏只有 1080 宽，8 个人排一排每人直径 ~120px，
      脸看不清。分列之后直径能翻倍 —— 这才是"投稿能用"和"内部预览"的差别。
    ★ 还要返回**块底边**：歌词的起点必须由它决定，否则"画面中段"那套排版
      会直接压在最后一行头像上（竖屏 8 人实测就是这样，名字被歌词盖住）。
    ★ 横屏那段**一个数都不许动**：门禁是按这套几何量"谁亮"的，
      改了等于把尺子和产品一起挪，红绿都不可信。
    """
    if h <= w * 1.15:
        r = int(min(mind * 0.115, (w * 0.86) / max(1.0, n * 2 + (n - 1) * 0.38)))
        r = max(18, r)
        gap = r * 0.38
        total = n * 2 * r + (n - 1) * gap
        x0 = (w - total) / 2.0 + r
        pos = [(x0 + k * (2 * r + gap), h * 0.38) for k in range(n)]
        return pos, r, h * 0.38 + r * 1.55

    if n <= 3:
        cols = max(1, n)
    elif n <= 4:
        cols = 2
    elif n <= 6:
        cols = 3
    else:
        cols = 4
    rows = int(math.ceil(n / float(cols)))
    r_w = (w * 0.82) / (cols * 2 + (cols - 1) * 0.45)
    # 纵向预算：头像块落在 0.15~0.54 之间（下面要留给歌词）
    r_h = (h * 0.39) / max(1.0, rows * 2.55 + (rows - 1) * 0.35)
    r = max(18, int(min(mind * 0.14, r_w, r_h)))
    gap = r * 0.45
    rh = r * 3.05                     # 一行占的高度（含名字）
    block = rows * rh
    top = h * 0.345 - block / 2.0
    pos = []
    for k in range(n):
        c = k % cols
        ri = k // cols
        in_row = min(cols, n - ri * cols)
        total = in_row * 2 * r + (in_row - 1) * gap
        x = (w - total) / 2.0 + r + c * (2 * r + gap)
        pos.append((x, top + r + ri * rh))
    return pos, r, top + block


def _cast_who_at(takes, t):
    """第 t 秒是谁在唱。

    ★ 段与段之间的空隙归**上一段**那个人 —— 否则句间换气的那 0.3 秒里
      所有头像一起暗下去，看起来像"卡了一帧"。
    """
    if not takes:
        return -1
    for a, b, k in takes:
        if float(a) <= t < float(b):
            return int(k)
    prev = [x for x in takes if float(x[1]) <= t]
    return int(prev[-1][2]) if prev else int(takes[0][2])


def _cast_chorus_at(takes, t):
    """第 t 秒是不是**合唱段**（分配表里 role < 0，见 multising.ROLE_ALL）。

    ★ 为什么要单独一个函数：`_cast_who_at` 只能回答"是谁"，而合唱的答案是
      **"所有人"** —— 它返回 -1，看的人以为是"没人"。没有这个判据的症状是
      "合唱那一句谁的头像都不亮、整块画面看着像卡住了"，而且不报错。
    ★ 空 takes（没有分配表）必须返回 False：那是"谁都没在唱"的基线帧，
      和合唱是两件事，不能混。
    """
    if not takes:
        return False
    for a, b, k in takes:
        if float(a) <= t < float(b):
            return int(k) < 0
    prev = [x for x in takes if float(x[1]) <= t]
    return bool(prev) and int(prev[-1][2]) < 0


def _cast_hot(c, k):
    """这一帧第 k 个人是不是**在唱**（会亮、会动、名字用彩色）。

    合唱段里所有人都在唱 —— 这与"主角是谁"是两件事，别用 `k == who` 一把量。
    """
    return bool(c.get("cho")) or int(k) == int(c.get("who", -1))


def _cast_lyric_ys(block_bottom, h, lay, zh):
    """歌词那几行放在哪。返回 (主句 y, 上一句 y)。

    ★ 抽成纯函数是为了能被单独验：这个 bug 的形状是"上一句正好落在头像块里"，
      画出来只是几个字压在脸上，肉眼一眼看不出来是谁的错 —— 而这里只要
      断言 `上一句 y >= 头像块底边` 就钉死了。
    """
    prev_gap = {"center": 0.050, "side": 0.055}.get(lay, 0.036)
    top_min = block_bottom + h * (0.045 + prev_gap)
    base = {"center": (0.505, 0.520),
            "side": (0.500, 0.520)}.get(lay, (0.688, 0.705))
    y_en = max(h * (base[0] if zh else base[1]), top_min)
    return y_en, y_en - h * prev_gap


def draw_cast(sc, an, i, state):
    """合唱台：一排角色头像，**谁在唱谁亮**，下面滚歌词。

    ★ 数据不是"从音频猜出来的"，而是成品自己记下的分配表：
        cast["members"] [{name, avatar, color}, ...]
        cast["takes"]   [[起, 止, 第几个人], ...]   ← 就是 multi_takes
        cast["lines"]   [{start, end, text, role}, ...]
      谁唱哪句在出片之前就已经定死了，这里只是把它画出来 —— 所以
      画面上亮的人和听到的声音**必然是同一个**，不会出现"对不上"。

    选项（`style["cast_opt"]`，见 cast_opt_of）：
        karaoke  当前这句从白到黄"跟着唱"（按字数均分，不需要词级时间戳）
        motion   谁唱谁轻微起伏 + 头像下面一排电平条（音量驱动，不是装饰动画）
        layout   歌词排版：bottom（默认）/ center / side
        intro    片头秒数（歌名 + 全员名单逐个淡入），outro 淡出到黑
    """
    w, h, nb = sc.w, sc.h, sc.nb
    spec, bass = an["spec"], an["bass"]
    cast = sc.style.get("cast") or {}
    members = cast.get("members") or []
    takes = cast.get("takes") or []
    lines = cast.get("lines") or []
    opt = cast_opt_of(sc.style)
    fps = float(sc.fps or 30)
    t = i / fps
    b = float(bass[i])

    # ★ 分派：duet / ring / stage 各有独立绘制函数，`row` 这条老路**一行不动**。
    #   老路是门禁量过的（"谁亮"的几何按这套坐标判），改它等于把尺子和产品
    #   一起挪 —— 绿红都不再可信。
    _kind = sc.style.get("cast_lay") or "row"
    if _kind != "row":
        _out = _draw_cast_variant(sc, an, i, state, _kind)
        if _out is not None:
            return _out

    img = sc.blank()
    ld = ImageDraw.Draw(img, "RGBA")

    # ★ 动效的驱动量要**归一化**。原始 bass/spec 是绝对能量：安静的歌只有 0.002，
    #   换算成位移不到半个像素 ⇒ 头像一动不动，用户以为"这个开关没用"。
    #   按整段素材的峰值归一之后，任何歌都是同样的"看得见"。
    _bmax = float(bass.max()) if len(bass) else 1.0
    _smax = float(spec.max()) if spec.size else 1.0
    _bn = b / (_bmax or 1.0)
    _sn = [_bn]

    who = _cast_who_at(takes, t)
    cho = _cast_chorus_at(takes, t)
    accent = CHORUS_RGBA[:3] if cho else (
        _hex_rgb(members[who].get("color")) if 0 <= who < len(members)
        else (0x6C, 0x8C, 0xFF))

    # ---- 片头 / 片尾 ------------------------------------------------------
    # ★ 只在"片头片尾加起来还不到整首的一半"时才真的插：用户把片头填成 60 秒、
    #   歌只有 20 秒的话，画面会变成一张海报 —— 那时候宁可不做，也不能把歌吞掉。
    dur = float(getattr(sc, "dur", 0) or 0)
    intro = opt["intro"] if (dur > 0 and opt["intro"] + opt["outro"] < dur * 0.5) else 0.0
    outro = opt["outro"] if intro else 0.0

    if intro and t < intro:
        fade = max(0.0, min(1.0, t / min(0.6, intro * 0.5)))
        title = (cast.get("title") or "").strip()
        names = [m.get("name") for m in members if m.get("name")]
        _draw_line(ld, w / 2.0, title, h * (0.40 if names else 0.46),
                   sc.mind * 0.105, (255, 255, 255, int(250 * fade)), True,
                   stroke=2, max_w=w * 0.86)
        if names:
            # ★ 名单**每行最多 4 个、最多两行**：8 个人挤一行会被裁掉两边
            #   （实测：缩到最小字号还是溢出，字被画到画面外）。
            rows_t = [" · ".join(names[i:i + 4]) for i in range(0, len(names), 4)]
            if len(rows_t) > 2:
                rows_t = rows_t[:2]
                rows_t[1] += " …（共 %d 人）" % len(names)
            for j, row in enumerate(rows_t):
                _draw_line(ld, w / 2.0, row, h * (0.515 + j * 0.048),
                           sc.mind * 0.036, (236, 226, 255, int(220 * fade)), False,
                           max_w=w * 0.86)
        bar_w = int(w * 0.24 * fade)
        if bar_w > 0:
            ld.rectangle([w / 2.0 - bar_w, h * 0.615, w / 2.0 + bar_w, h * 0.615 + 3],
                         fill=accent + (200,))
        return sc._finish(img)

    # ---- 一层随低音呼吸的背光，颜色跟着"正在唱的那个人"走 ----
    gr = int(sc.mind * (0.78 + 0.22 * b))
    glow = Image.new("RGBA", (gr * 2, gr * 2), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([0, 0, gr * 2 - 1, gr * 2 - 1], fill=accent + (46,))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(gr * 0.28)),
                        (int(w / 2 - gr), int(h * 0.40 - gr)))

    # ---- 底部一条很低的频谱：画面不能只有头像和字，否则像 PPT ----
    bar_n = 56
    bw = w * 0.62 / bar_n
    bx = w * 0.19
    by = h * 0.965
    for k in range(bar_n):
        v = float(spec[i, min(nb - 1, int(k * nb / bar_n))])
        bh = max(1.0, v * sc.mind * 0.11)
        a = int(26 + 70 * v)
        ld.rectangle([bx + k * bw, by - bh, bx + k * bw + bw * 0.62, by],
                     fill=accent + (a,))

    # ---- 标题（这条成品叫什么）----
    title = (cast.get("title") or "").strip()
    if title:
        fnt = _cast_font(int(sc.mind * 0.046), True)
        bb = ld.textbbox((0, 0), title, font=fnt)
        ld.text((w / 2.0 - (bb[2] - bb[0]) / 2.0 - bb[0], h * 0.052),
                title, font=fnt, fill=(255, 255, 255, 150))

    # ---- 头像：横屏一排 / 竖屏分列（布局在 _cast_layout 里）----
    n = max(1, len(members))
    pos, r, block_bottom = _cast_layout(w, h, n, sc.mind)
    for k, m in enumerate(members):
        cx, cy = pos[k]
        # ★ 合唱段**所有人**都在唱（都亮、都跟着动）—— 这条老路以前只认"一个人"，
        #   合唱句会出现"谁都没亮"的画面，看着像卡住。
        on = cho or (k == who)
        # ★ 动效只给**正在唱的那个人**：别人一动不动，"谁在唱"才一眼看得出来。
        #   起伏跟着低音走（不是匀速正弦）：匀速那种看两秒就腻，而且和歌没关系。
        bob, sc_k = 0.0, 1.0
        if on and opt["motion"]:
            lvl = min(1.0, 0.35 + 0.65 * _bn)
            amp = max(3.0, r * 0.065) * (0.55 + 0.45 * lvl)
            bob = math.sin(t * 3.4 + k * 0.9) * amp
            sc_k = 1.0 + 0.05 * lvl
        rr = int(r * (1.12 if on else 0.92) * sc_k)
        cy = cy + bob
        col = _hex_rgb(m.get("color"))
        av = _cast_avatar(m.get("avatar"), rr * 2, dim=not on)
        if av is not None:
            img.alpha_composite(av, (int(cx - rr), int(cy - rr)))
        else:
            c = col if on else tuple(int(v * 0.42) for v in col)
            # 圆脸同样走一小块超采样补丁（圆的斜边最容易看出台阶）
            _pr = max(4, int(rr) + 2)

            def _face(dd, _ox, _oy, _c=c, _rr=rr, _p=_pr):
                dd.ellipse([_p - _rr, _p - _rr, _p + _rr, _p + _rr], fill=_c + (255,))

            img.alpha_composite(sc.ss_patch(cx - _pr, cy - _pr, _pr * 2, _pr * 2, _face),
                                (int(cx - _pr), int(cy - _pr)))
            ini = (m.get("name") or "?")[:1]
            fnt = _cast_font(int(rr * 0.92), True)
            bb = ld.textbbox((0, 0), ini, font=fnt)
            ld.text((cx - (bb[2] - bb[0]) / 2.0 - bb[0],
                     cy - (bb[3] - bb[1]) / 2.0 - bb[1]),
                    ini, font=fnt, fill=(255, 255, 255, 235))
        if on:
            # 亮的人：两圈半透明外晕 + 一圈实描边（"发光"是这三层叠出来的）
            # ★ 圆环是斜边最密的地方。整帧超采样太贵，只在这一圈上开一块
            #   小补丁多采几个样 —— 8 个人一帧也就多 3~4 ms。
            _pr2 = max(4, int(rr * 1.20) + 3)

            def _rings(dd, _ox, _oy, _rr=rr, _r=r, _p=_pr2):
                for jj, aa in ((1.20, 30), (1.09, 66)):
                    r2 = int(_rr * jj)
                    dd.ellipse([_p - r2, _p - r2, _p + r2, _p + r2],
                               outline=accent + (aa,), width=max(2, int(_r * 0.085)))
                dd.ellipse([_p - _rr, _p - _rr, _p + _rr, _p + _rr],
                           outline=tint(accent, 1.45) + (236,),
                           width=max(2, int(_r * 0.07)))

            img.alpha_composite(
                sc.ss_patch(cx - _pr2, cy - _pr2, _pr2 * 2, _pr2 * 2, _rings),
                (int(cx - _pr2), int(cy - _pr2)))
        nm = (m.get("name") or "")[:8]
        fnt = _cast_font(r * (0.40 if on else 0.34), on)
        bb = ld.textbbox((0, 0), nm, font=fnt)
        ny = cy + rr + r * 0.18
        ld.text((cx - (bb[2] - bb[0]) / 2.0 - bb[0], ny), nm, font=fnt,
                fill=(255, 255, 255, 246) if on else (188, 190, 203, 122))
        # ---- 电平条：声音多大就多高（这颗是"麦克风在响"的那点意思）----
        if on and opt["motion"]:
            bn = 5
            bw2 = max(2.0, r * 0.072)
            gp2 = bw2 * 0.9
            tw = bn * bw2 + (bn - 1) * gp2
            bx2 = cx - tw / 2.0
            # ★★ 电平条**从上往下长**，起点压到名字下沿之外。
            #   以前是 `by2 = ny + r*0.60` 且向上长 `r*0.62` ⇒ 正好盖住名字
            #   （名字画在 ny，高度约 r*0.40）。一个人唱时只盖住一半、不明显；
            #   一进合唱所有人都亮，两个名字一起被竖条压住，一眼就看出来了。
            nh = float(bb[3] - bb[1])                 # 名字实际高度（按字体量，不猜）
            by2 = ny + nh + r * 0.10
            for j in range(bn):
                v = float(spec[i, min(nb - 1, int((0.12 + 0.72 * j / bn) * nb))]) / (_smax or 1.0)
                hh = max(bw2 * 0.7, v * r * 0.62)
                ld.rectangle([bx2 + j * (bw2 + gp2), by2,
                              bx2 + j * (bw2 + gp2) + bw2, by2 + hh],
                             fill=tint(accent, 1.25) + (205,))

    # ---- 歌词：当前句大字 + 上下各一句做上下文 ----
    cur = -1
    for idx, ln in enumerate(lines):
        if float(ln.get("start") or 0) <= t < float(ln.get("end") or 0):
            cur = idx
            break

    # 三套排版：bottom（默认，底部居中）/ center（画面中段，像 MV）/ side（靠左）
    # ★ 起点还要避开头像块：竖屏人多时块会一直排到 0.5h 以下，
    #   照原来的固定比例画，歌词就压在最后一行头像上（实测过，名字被盖住）。
    # ★ 而且**连"上一句"那一行也要算进去**：只把主句往下推的话，上一句
    #   正好落在头像块里 —— 横屏 8 人那版实测就是上一句横穿过头像。
    lay = opt["layout"]
    if lay == "side":
        base_x, align, max_w = w * 0.075, "left", w * 0.74
    else:
        base_x, align, max_w = w / 2.0, "center", w * 0.90

    def _txt(text, yy, size, fill, bold, stroke=0, prog=None):
        _draw_line(ld, base_x, text, yy, size, fill, bold, stroke=stroke,
                   prog=prog, done_fill=(255, 212, 96, 252), max_w=max_w,
                   align=align)

    if lines:
        if cur >= 0:
            ln = lines[cur]
            en = (ln.get("text") or "").strip()
            zh = (ln.get("zh") or "").strip()
            # 扫色进度：这句唱到哪儿了。按**字数**均分。
            prog = None
            if opt["karaoke"] and en:
                _a = float(ln.get("start") or 0)
                _b = float(ln.get("end") or 0)
                prog = (t - _a) / max(0.25, _b - _a)
            if lay == "center":
                y_en, y_prev = _cast_lyric_ys(block_bottom, h, "center", bool(zh))
            elif lay == "side":
                y_en, y_prev = _cast_lyric_ys(block_bottom, h, "side", bool(zh))
            else:
                y_en, y_prev = _cast_lyric_ys(block_bottom, h, "bottom", bool(zh))
            if cur - 1 >= 0:
                _txt((lines[cur - 1].get("text") or "").strip(),
                     y_prev, sc.mind * 0.040, (255, 255, 255, 96), False)
            # ★ 英文大字在上、中文小字在下面 —— 老板的原话「英语的下面是小的中文」。
            #   两行都画时**整块上移一点**、下一句往下压，否则中文会压到下一句上。
            # ★ 这一条 `row` 老路是**自己画歌词**的（没走 _cast_lyric），所以
            #   名字前缀得在这儿再写一遍 —— 漏了的话症状是"另外三款有名字、
            #   最常用的横排没有"，而且不报错，只是少了一个信息。
            _nm, _ncol = "", None
            if opt["singer"] and members:
                _k = int(ln.get("role") or 0)
                # ★ row 老路同样要认合唱句（不然最常用的横排把合唱写成别人独唱）
                if _k < 0:
                    _nm, _ncol = CHORUS_LABEL, CHORUS_RGBA
                elif 0 <= _k < len(members):
                    _s = (members[_k].get("name") or "").strip()
                    if _s:
                        _nm = _s + "："
                        _ncol = _hex_rgb(members[_k].get("color")) + (250,)
            if _nm:
                _draw_named(ld, base_x, _nm, en, y_en, sc.mind * 0.078,
                            (255, 255, 255, 250), _ncol, True, stroke=2,
                            prog=prog, max_w=max_w, align=align)
            else:
                _txt(en, y_en, sc.mind * 0.078, (255, 255, 255, 250), True,
                     stroke=2, prog=prog)
            if zh:
                y_zh = y_en + sc.mind * 0.078 * 1.16
                _txt(zh, y_zh, sc.mind * 0.046, (236, 226, 255, 226), False, stroke=1)
                y_next = y_zh + sc.mind * 0.046 * 1.55
            else:
                y_next = y_en + h * 0.120
            if cur + 1 < len(lines):
                _txt((lines[cur + 1].get("text") or "").strip(),
                     y_next, sc.mind * 0.040, (255, 255, 255, 84), False)
        else:
            # 前奏 / 间奏：写一句"谁在等"，别让画面空着
            _txt("· · ·", h * (0.62 if lay != "bottom" else 0.735),
                 sc.mind * 0.060, (255, 255, 255, 70), False)

    # ---- 片尾：整幅淡出到黑 ----
    if outro and t > dur - outro:
        a = max(0.0, min(1.0, (t - (dur - outro)) / max(0.2, outro)))
        veil = Image.new("RGBA", (w, h), (0, 0, 0, int(255 * a)))
        img.alpha_composite(veil)

    return sc._finish(img)


def draw_grid(sc, an, i, state):
    """网格矩阵：一列一个频带、一行一个时间切片，像一块会跳动的音阶矩阵。

    风格上和其它几种刻意相反 —— 那几种都在画"圆"和"光"，这一种只画**点**：
    极简、冷、科技感。适合电子、Glitch、赛博朋克那类片子，也适合当背景板
    （信息密度低，字幕压上去最清楚）。

    ★ 时间轴不用另外存历史：`an["spec"]` 本来就是**整首歌每一帧**的频谱，
      第 r 行直接取第 `i - r*step` 帧就行 —— 零成本拿到 0.9 秒的回溯。
      （第一版想着"逐帧往数组里 unshift"，等于每帧复制一遍历史，白花时间。）
    """
    w, h, nb = sc.w, sc.h, sc.nb
    spec, bass = an["spec"], an["bass"]
    nfr = spec.shape[0]
    b = float(bass[i])
    img = sc.blank()

    cols, rows = 9, 9
    cx, cy = w / 2.0, h * 0.435
    span_x = w * 0.86
    step_f = 3                                    # 每行往前推 3 帧（0.1 秒）
    # ★ 点距不能太小：第一版按 0.036h 给，整块矩阵只有中间一小片，
    #   上下两大截死黑 —— "极简"和"没画完"在竖屏里只差一个尺寸。
    gy = min(h * 0.058, span_x / (cols - 1))
    span_y = gy * (rows - 1)
    gx = span_x / (cols - 1)

    gr = int(sc.mind * (0.72 + 0.28 * b))
    img.alpha_composite(sc.glow.resize((gr * 2, gr * 2), Image.BILINEAR),
                        (int(cx - gr), int(cy - gr)))

    ld, _done = sc.ss_canvas()
    # 网格：很淡的十字线，给点阵一个"坐标系"
    for cc in range(cols):
        x = cx - span_x / 2.0 + cc * gx
        ld.line([(x, cy - span_y / 2.0), (x, cy + span_y / 2.0)],
                fill=sc.pal[int(cc * (nb - 1) / float(cols - 1))] + (16,), width=1)
    for r in range(rows):
        y = cy - span_y / 2.0 + r * gy
        ld.line([(cx - span_x / 2.0, y), (cx + span_x / 2.0, y)],
                fill=(210, 220, 255, 14), width=1)

    # 点阵：越靠下越新（最新一行在最下面），衰减 = 时间衰减
    for r in range(rows):
        j = max(0, i - (rows - 1 - r) * step_f)
        fade = 0.30 + 0.70 * math.pow(r / float(rows - 1), 1.35)
        for cc in range(cols):
            band = int(round(cc * (nb - 1) / float(cols - 1)))
            v = float(spec[j, band])
            x = cx - span_x / 2.0 + cc * gx
            y = cy - span_y / 2.0 + r * gy
            rr = max(1.2, gy * (0.12 + 0.34 * v))
            col = sc.pal[band]
            a = int(255 * fade * (0.26 + 0.74 * v))
            ld.ellipse([x - rr, y - rr, x + rr, y + rr],
                       fill=tint(col, 1.0 + 0.45 * v) + (a,))
            # 最响的那几个点给一个白心：整块矩阵立刻有"峰值在跑"的感觉
            if v > 0.72:
                r2 = max(0.9, rr * 0.38)
                ld.ellipse([x - r2, y - r2, x + r2, y + r2],
                           fill=(255, 255, 255, int(190 * fade)))

    # 底边一条低音指示条：让"咚"有一个明确的位置
    bar = max(1.0, sc.mind * 0.0035)
    y0 = cy + span_y / 2.0 + sc.mind * 0.035
    ld.line([(cx - span_x / 2.0, y0), (cx + span_x / 2.0, y0)],
            fill=(210, 220, 255, 40), width=max(1, int(bar)))
    lw = span_x * (0.10 + 0.90 * b)
    ld.line([(cx - lw / 2.0, y0), (cx + lw / 2.0, y0)],
            fill=tint(sc.pal[nb // 3], 1.35) + (235,), width=max(2, int(bar * 2.4)))

    # ★ `_done()` 只能调一次：它每次都真的跑一遍 BOX 下采样，
    #   调两次就是白扔一倍的缩放开销（这个坑在上面几个样式里也踩过）。
    lay = _done()
    img.alpha_composite(sc._glow_layer(lay, 0.45))
    img.alpha_composite(lay)
    return sc._finish(img)


# ---------------------------------------------------------------- 合唱台·构型
#
# 一个「谁唱谁亮」的构型 = ①头像摆在哪 ②"亮起来"长什么样 ③背景怎么托。
# 三样都换才叫另一种风格；只挪位置那是换汤不换药。
#
#   row    一排 / 竖屏分列（默认，最稳，人多也不乱）
#   duet   左右对垒：两张脸占满左右半屏，中间一道随唱者变色的能量线
#   ring   围坐一圈：头像绕中心成环，谁唱谁那一段弧亮起来
#   stage  舞台追光：一排人在台上，谁唱谁被一道光柱打到，其余压成剪影
#
# ★ 这四条**共用**同一份"谁在唱"数据（cast["takes"]），所以画面上亮的人
#   和听到的声音必然是同一个 —— 这是合唱台的地基，不能靠音频去猜。

_GLOW_CACHE = {}


def _glow_of(color, n=256):
    """按颜色缓存径向光斑。

    ★ 不要每帧现做：`Image.new + ellipse + GaussianBlur` 在 540×960 上要
      20~30 ms，一首歌 5000 帧就是两分钟。光斑是**形状固定、只有颜色变**
      的东西，按颜色缓存一份，逐帧只做一次 resize 叠加。
    """
    key = tuple(int(v) for v in (color or (140, 160, 255))[:3])
    g = _GLOW_CACHE.get(key)
    if g is None:
        g = _glow_sprite(n, key)
        _GLOW_CACHE[key] = g
    return g


def _cast_ctx(sc, an, i):
    """把合唱台所有构型都要用的量一次算齐。"""
    cast = sc.style.get("cast") or {}
    members = cast.get("members") or []
    lines = cast.get("lines") or []
    opt = cast_opt_of(sc.style)
    fps = float(sc.fps or 30)
    t = i / fps
    spec = an["spec"]
    bass = an["bass"]
    b = float(bass[i])
    bmax = float(bass.max()) if len(bass) else 1.0
    smax = float(spec.max()) if spec.size else 1.0
    who = _cast_who_at(cast.get("takes") or [], t)
    cho = _cast_chorus_at(cast.get("takes") or [], t)
    # 合唱段用中性金（和句前「合唱：」同一个色），而不是某个人的角色色 ——
    # 用角色色会让观众以为那一句是主角一个人唱的。
    accent = CHORUS_RGBA[:3] if cho else (
        _hex_rgb(members[who].get("color"))
        if 0 <= who < len(members) else (0x6C, 0x8C, 0xFF))
    cur = -1
    for idx, ln in enumerate(lines):
        if float(ln.get("start") or 0) <= t < float(ln.get("end") or 0):
            cur = idx
            break
    dur = float(getattr(sc, "dur", 0) or 0)
    intro = opt["intro"] if (dur > 0 and opt["intro"] + opt["outro"] < dur * 0.5) else 0.0
    outro = opt["outro"] if intro else 0.0
    return {"cast": cast, "members": members, "lines": lines, "opt": opt,
            "fps": fps, "t": t, "b": b, "bn": b / (bmax or 1.0), "smax": smax,
            "who": who, "cho": cho, "accent": accent, "cur": cur, "dur": dur,
            "intro": intro, "outro": outro, "spec": spec, "an": an, "i": i}


def _cast_intro(sc, img, c):
    """片头卡（标题 + 名单）。返回 True = 这一帧已经画完，别再画正片。"""
    if not c["intro"] or c["t"] >= c["intro"]:
        return False
    w, h = sc.w, sc.h
    fade = max(0.0, min(1.0, c["t"] / min(0.6, c["intro"] * 0.5)))
    ld = ImageDraw.Draw(img, "RGBA")
    title = (c["cast"].get("title") or "").strip()
    names = [m.get("name") for m in c["members"] if m.get("name")]
    _draw_line(ld, w / 2.0, title, h * (0.40 if names else 0.46),
               sc.mind * 0.105, (255, 255, 255, int(250 * fade)), True,
               stroke=2, max_w=w * 0.86)
    if names:
        rows_t = [" · ".join(names[i2:i2 + 4]) for i2 in range(0, len(names), 4)]
        if len(rows_t) > 2:
            rows_t = rows_t[:2]
            rows_t[1] += " …（共 %d 人）" % len(names)
        for j, row in enumerate(rows_t):
            _draw_line(ld, w / 2.0, row, h * (0.515 + j * 0.048),
                       sc.mind * 0.036, (236, 226, 255, int(220 * fade)), False,
                       max_w=w * 0.86)
    bar_w = int(w * 0.24 * fade)
    if bar_w > 0:
        ld.rectangle([w / 2.0 - bar_w, h * 0.615, w / 2.0 + bar_w, h * 0.615 + 3],
                     fill=(c["accent"] or (120, 140, 255)) + (200,))
    return True


def _cast_lyric(sc, ld, c, y_from, max_w=None, align="center", base_x=None):
    """画「上一句 / 当前句（扫色）/ 下一句」。四个构型共用这一份排版。"""
    w, h = sc.w, sc.h
    lines = c["lines"]
    if not lines:
        return
    max_w = max_w if max_w is not None else w * 0.90
    base_x = w / 2.0 if base_x is None else base_x
    t = c["t"]
    cur = c["cur"]

    def _txt(text, yy, size, fill, bold, stroke=0, prog=None):
        _draw_line(ld, base_x, text, yy, size, fill, bold, stroke=stroke,
                   prog=prog, done_fill=(255, 212, 96, 252), max_w=max_w,
                   align=align)

    if cur < 0:
        _txt("· · ·", y_from + sc.mind * 0.02, sc.mind * 0.058,
             (255, 255, 255, 70), False)
        return
    ln = lines[cur]
    en = (ln.get("text") or "").strip()
    zh = (ln.get("zh") or "").strip()
    prog = None
    if c["opt"]["karaoke"] and en:
        a = float(ln.get("start") or 0)
        bb = float(ln.get("end") or 0)
        prog = (t - a) / max(0.25, bb - a)
    if cur - 1 >= 0:
        _txt((lines[cur - 1].get("text") or "").strip(), y_from,
             sc.mind * 0.038, (255, 255, 255, 96), False)

    # 「谁唱的」标在当前句前面：画名字要有人才行（没有分配表就别画个空冒号）
    nm, ncol = "", None
    if c["opt"]["singer"] and c.get("members"):
        k = int(ln.get("role") or 0)
        # ★ 合唱句：role = -1（multising.ROLE_ALL）⇒ 写「合唱」并用中性色，
        #   不写某个人的名字（那句不是他一个人唱的）。
        #   ⚠ 别写成 `0 <= k`：-1 会被 `or 0` 之外的判断漏掉吗不会，
        #   但 `k < 0` 必须显式处理 —— 否则 COLORS[-1] 取到最后一个人，
        #   画面上合唱句被标成"最后那个人独唱"，而且不报错。
        if k < 0:
            nm = CHORUS_LABEL
            ncol = CHORUS_RGBA
        elif 0 <= k < len(c["members"]):
            _m = c["members"][k]
            _s = (_m.get("name") or "").strip()
            if _s:
                nm = _s + "："
                ncol = _hex_rgb(_m.get("color")) + (250,)
    y_en = y_from + sc.mind * 0.058
    if nm:
        _draw_named(ld, base_x, nm, en, y_en, sc.mind * 0.076,
                    (255, 255, 255, 250), ncol, True, stroke=2, prog=prog,
                    max_w=max_w, align=align)
    else:
        _txt(en, y_en, sc.mind * 0.076,
             (255, 255, 255, 250), True, stroke=2, prog=prog)
    if zh:
        y_zh = y_from + sc.mind * 0.058 + sc.mind * 0.076 * 1.16
        _txt(zh, y_zh, sc.mind * 0.045, (236, 226, 255, 226), False, stroke=1)
        y_next = y_zh + sc.mind * 0.045 * 1.55
    else:
        y_next = y_from + sc.mind * 0.058 + h * 0.118
    if cur + 1 < len(lines):
        _txt((lines[cur + 1].get("text") or "").strip(), y_next,
             sc.mind * 0.038, (255, 255, 255, 84), False)


def _cast_face(sc, img, ld, m, cx, cy, rr, on, accent, r_anim=1.0):
    """贴一张圆头像：位图贴图 + （没图就）首字母圆 + 亮的人描三圈光。

    ★ 圆的描边走 `ss_patch` 小补丁 —— 整帧超采样太贵，只在这一圈上
      多采几个样即可；头像贴图的圆形遮罩已在 `_cast_avatar` 里超采样过。
    """
    col = _hex_rgb(m.get("color"))
    av = _cast_avatar(m.get("avatar"), max(8, int(rr * 2)), dim=not on)
    if av is not None:
        img.alpha_composite(av, (int(cx - rr), int(cy - rr)))
    else:
        c = col if on else tuple(int(v * 0.42) for v in col)
        _p = max(4, int(rr) + 2)

        def _face(dd, _ox, _oy, _c=c, _rr=rr, _q=_p):
            dd.ellipse([_q - _rr, _q - _rr, _q + _rr, _q + _rr], fill=_c + (255,))

        img.alpha_composite(sc.ss_patch(cx - _p, cy - _p, _p * 2, _p * 2, _face),
                            (int(cx - _p), int(cy - _p)))
        ini = (m.get("name") or "?")[:1]
        fnt = _cast_font(int(rr * 0.92), True)
        bb = ld.textbbox((0, 0), ini, font=fnt)
        ld.text((cx - (bb[2] - bb[0]) / 2.0 - bb[0],
                 cy - (bb[3] - bb[1]) / 2.0 - bb[1]),
                ini, font=fnt, fill=(255, 255, 255, 235))
    if on:
        _p2 = max(4, int(rr * 1.20 * r_anim) + 3)

        def _rings(dd, _ox, _oy, _rr=rr, _q=_p2):
            for jj, aa in ((1.20, 30), (1.09, 66)):
                r2 = int(_rr * jj)
                dd.ellipse([_q - r2, _q - r2, _q + r2, _q + r2],
                           outline=accent + (aa,), width=max(2, int(_rr * 0.085)))
            dd.ellipse([_q - _rr, _q - _rr, _q + _rr, _q + _rr],
                       outline=tint(accent, 1.45) + (236,),
                       width=max(2, int(_rr * 0.07)))

        img.alpha_composite(
            sc.ss_patch(cx - _p2, cy - _p2, _p2 * 2, _p2 * 2, _rings),
            (int(cx - _p2), int(cy - _p2)))


def draw_cast_duet(sc, an, i, state):
    """对唱擂台：两张脸各占左右半屏，中间一道随唱者变色的能量线。

    为什么另做一个：`row` 把人排成一排，"对唱"的那种**对峙感**出不来 ——
    两人歌最想看到的画面是"A 唱完 B 接上"的来回拉扯，那就得让两个人
    面对面占住画面。人数超过 2 个也照画（取前两位轮流，其余缩小排在下面），
    不报错、不吞人。
    """
    c = _cast_ctx(sc, an, i)
    w, h = sc.w, sc.h
    mind = sc.mind
    members = c["members"]
    if not members:
        return sc._finish(sc.blank())
    img = sc.blank()
    if _cast_intro(sc, img, c):
        return sc._finish(img)

    n = len(members)
    duo = members[:2]
    ld = ImageDraw.Draw(img, "RGBA")
    t, b = c["t"], c["b"]

    # 背景：两团对撞的光，颜色各取各自的角色色 —— 谁唱谁那团涨起来，
    # 于是"现在轮到谁"在没有头像的部位也能读出来。
    for k, m in enumerate(duo):
        col = _hex_rgb(m.get("color"))
        hot = _cast_hot(c, k)
        rad = int(mind * ((0.66 if hot else 0.50) + 0.16 * b))
        g = _glow_of(col)
        img.alpha_composite(g.resize((rad * 2, rad * 2), Image.BILINEAR),
                            (int(w * (0.22 + 0.56 * k) - rad),
                             int(h * 0.335 - rad)))

    # 中间那道能量线：粗细跟着低音跳，颜色跟着当前演唱者变。
    # ★ 线**不要顶到画面上下边缘**。顶到头就成了一道把画面切成两半的隔板
    #   （第一版就是这样，看着像"卷轴"而不是"两人之间的能量"）。
    #   上下各留一截，中间放一颗脉动的球。
    xm = w / 2.0
    y0, y1 = h * 0.170, h * 0.520
    lw = max(2, int(mind * (0.0030 + 0.0046 * c["bn"])))
    ld.line([(xm, y0), (xm, y1)], fill=c["accent"] + (140,), width=lw)
    ld.line([(xm, y0), (xm, y1)], fill=tint(c["accent"], 1.5) + (95,),
            width=max(1, lw // 3))
    my = (y0 + y1) / 2.0
    br = int(mind * (0.011 + 0.014 * c["bn"]))
    img.alpha_composite(
        _glow_of(c["accent"]).resize((br * 7, br * 7), Image.BILINEAR),
        (int(xm - br * 3.5), int(my - br * 3.5)))
    ld.ellipse([xm - br, my - br, xm + br, my + br],
               fill=tint(c["accent"], 1.55) + (240,))
    # 两个**左右指向**的箭头：这一眼就能读出"现在轮到哪边"
    side = -1.0 if c["who"] % 2 == 0 else 1.0
    for dy in (-1.0, 1.0):
        yy = my + dy * mind * 0.066
        tri = [(xm + side * mind * 0.040, yy),
               (xm + side * mind * 0.013, yy - mind * 0.018),
               (xm + side * mind * 0.013, yy + mind * 0.018)]
        ld.polygon(tri, fill=c["accent"] + (228,))

    # 两个人
    r_base = min(mind * 0.185, w * 0.30)
    for k, m in enumerate(duo):
        hot = _cast_hot(c, k)
        rr = int(r_base * ((1.16 if hot else 0.92) + 0.05 * c["bn"]))
        cx = w * (0.235 + 0.53 * k)
        cy = h * 0.335 - (mind * 0.010 * c["bn"] if hot else 0)
        _cast_face(sc, img, ld, m, cx, cy, rr, hot, c["accent"])
        nm = (m.get("name") or "")[:8]
        fnt = _cast_font(rr * (0.30 if hot else 0.26), hot)
        bb = ld.textbbox((0, 0), nm, font=fnt)
        ld.text((cx - (bb[2] - bb[0]) / 2.0 - bb[0], cy + rr + mind * 0.030),
                nm, font=fnt,
                fill=(255, 255, 255, 246) if hot else (188, 190, 203, 118))

    # 3 人以上：其余人缩成一排小头像，谁也不落下。
    # ★ 位置要压在主头像名字**下方**、歌词**上方**那一段 —— 第一版给到 0.525h，
    #   正好压在"云宝"两个字上（用户名被自己挡住，比没有还难看）。
    if n > 2:
        rest = members[2:]
        r2 = int(r_base * 0.30)
        step = (w * 0.54) / max(1, len(rest))
        for k, m in enumerate(rest):
            cx = w * 0.23 + step * (k + 0.5)
            hot = _cast_hot(c, k + 2)
            _cast_face(sc, img, ld, m, cx, h * 0.578, r2, hot, c["accent"])

    # 底部一条低频谱 + 歌词
    _cast_strip(sc, img, c)
    ld2 = ImageDraw.Draw(img, "RGBA")
    _cast_lyric(sc, ld2, c, h * 0.700)
    _cast_outro(sc, img, c)
    return sc._finish(img)


def draw_cast_ring(sc, an, i, state):
    """环列声场：头像围成一圈，谁唱谁那一段弧亮起来。

    适合 3~8 人的"合唱团"感：每个人固定在一圈上的一个方位，轮到谁，
    他那一段弧就从暗转亮、头像放大。中心放一颗随低音呼吸的球 ——
    不然圆中间是个洞，整幅像个甜甜圈。
    """
    c = _cast_ctx(sc, an, i)
    w, h = sc.w, sc.h
    mind = sc.mind
    members = c["members"]
    if not members:
        return sc._finish(sc.blank())
    img = sc.blank()
    if _cast_intro(sc, img, c):
        return sc._finish(img)

    n = len(members)
    ld = ImageDraw.Draw(img, "RGBA")
    b = c["b"]
    cx, cy = w / 2.0, h * 0.415
    R = min(mind * 0.245, w * 0.295)
    rr0 = max(14, int(min(R * 0.42, (2 * math.pi * R) / (n * 2.9))))

    # 中心球：**不能只是一颗暗球**。第一版半径只给头像的 1.3 倍、颜色又被
    # disc 的深底压住，画出来是一团几乎看不见的灰蓝 —— 圆中间看着是个洞，
    # 整幅像一只甜甜圈。现在给到 1.8 倍 + 外发光 + 一个亮核。
    hub = int(rr0 * (1.80 + 0.20 * b))
    img.alpha_composite(
        _glow_of(c["accent"]).resize((hub * 5, hub * 5), Image.BILINEAR),
        (int(cx - hub * 2.5), int(cy - hub * 2.5)))
    disc, _ = sc._disc(hub, tint(c["accent"], 1.15))
    img.alpha_composite(disc, (int(cx - hub), int(cy - hub)))
    cr = max(3, int(hub * 0.34))
    img.alpha_composite(
        _glow_of((255, 255, 255)).resize((cr * 5, cr * 5), Image.BILINEAR),
        (int(cx - cr * 2.5), int(cy - cr * 2.5)))

    # 刻度环：一圈细弧当"底盘"
    for rw_, aw in ((R + rr0 * 1.62, 26), (R + rr0 * 1.72, 14)):
        _arc(sc, img, cx, cy, rw_, 0, 360, c["accent"] + (aw,),
             max(1, int(mind * 0.0022)))

    for k, m in enumerate(members):
        hot = _cast_hot(c, k)
        ang = -math.pi / 2.0 + k * 2.0 * math.pi / n
        px, py = cx + math.cos(ang) * R, cy + math.sin(ang) * R
        rr = int(rr0 * ((1.26 if hot else 0.94) + 0.05 * b))
        py = py - (mind * 0.010 * c["bn"] if hot else 0)

        # 谁唱：他那个方位的弧段加粗高亮。
        # ★ 半径必须**在头像外圈之外**（R + rr0*1.5）。画在 R 上正好穿过
        #   头像中心，被自己的脸盖掉 80%，等于没画。
        if hot:
            a0 = math.degrees(ang) - 360.0 / n * 0.42
            a1 = math.degrees(ang) + 360.0 / n * 0.42
            _arc(sc, img, cx, cy, R + rr0 * 1.48, a0, a1, c["accent"] + (245,),
                 max(3, int(mind * 0.0090)))
            _arc(sc, img, cx, cy, R + rr0 * 1.62, a0, a1,
                 tint(c["accent"], 1.45) + (150,), max(2, int(mind * 0.0048)))

        _cast_face(sc, img, ld, m, px, py, rr, hot, c["accent"])
        # 名字：简短标在头像外侧（带描边，压在光晕上也看得清）
        nm = (m.get("name") or "")[:6]
        fnt = _cast_font(rr * 0.40, hot)
        bb = ld.textbbox((0, 0), nm, font=fnt)
        ny = py + rr + mind * 0.014
        ld.text((px - (bb[2] - bb[0]) / 2.0 - bb[0], ny), nm, font=fnt,
                fill=(255, 255, 255, 244) if hot else (196, 198, 212, 128),
                stroke_width=2, stroke_fill=(0, 0, 0, 140))

    _cast_strip(sc, img, c)
    ld2 = ImageDraw.Draw(img, "RGBA")
    _cast_lyric(sc, ld2, c, h * 0.700)
    _cast_outro(sc, img, c)
    return sc._finish(img)


def draw_cast_stage(sc, an, i, state):
    """舞台追光：一排人站在台上，谁唱谁被一道光柱打到，其余压成剪影。

    和 `row` 的区别不是"位置"而是**光**：这里"亮"是靠一道从顶上打下来的
    锥形光柱，所以即使头像很小、人很多，也能一眼看出谁在唱。舞台前沿还有
    一排脚灯，随频带闪 —— 这是"现场"和"贴纸"的差别。
    """
    c = _cast_ctx(sc, an, i)
    w, h = sc.w, sc.h
    mind = sc.mind
    members = c["members"]
    if not members:
        return sc._finish(sc.blank())
    img = sc.blank()
    if _cast_intro(sc, img, c):
        return sc._finish(img)

    n = len(members)
    ld = ImageDraw.Draw(img, "RGBA")
    b = c["b"]
    t = c["t"]

    # 台面：一条渐变暗板（上沿微亮、往下迅速沉黑）+ 一条边缘高光，
    # 人才像"站在上面"而不是"浮在黑里"。
    # ★ 逐帧填 580 行矩形太贵，整块台板一次性算好存进 Scene（同一场渲染里
    #   尺寸不变），之后每帧只做一次 alpha_composite。
    stage_y = h * 0.615
    plate = getattr(sc, "_stage_plate", None)
    if plate is None:
        ph = max(4, int(h - stage_y))
        yy = np.linspace(0.0, 1.0, ph, dtype=np.float32)[:, None, None]
        base = np.array([48, 42, 72], dtype=np.float32)[None, None, :]
        arr = np.zeros((ph, w, 4), dtype=np.uint8)
        arr[:, :, :3] = np.clip(base * (1.0 - 0.93 * yy), 0, 255).astype(np.uint8)
        arr[:, :, 3] = 238
        plate = Image.fromarray(arr, "RGBA")
        sc._stage_plate = plate
    img.alpha_composite(plate, (0, int(stage_y)))
    ld.line([(0, stage_y), (w, stage_y)], fill=(160, 170, 220, 85),
            width=max(1, int(mind * 0.0028)))

    # 灯架：顶部一条横杆 + 三个灯位。★ 没有它，那道光柱是从"画面外"凭空
    # 打下来的，顶上那一大片空就成了"没画完"；有了灯架，"光"才有来源。
    rig_y = h * 0.098
    ld.line([(w * 0.07, rig_y), (w * 0.93, rig_y)],
            fill=(146, 152, 184, 95), width=max(1, int(mind * 0.0024)))
    for lx in (w * 0.20, w * 0.50, w * 0.80):
        lr0 = int(mind * 0.0085)
        ld.ellipse([lx - lr0, rig_y - lr0, lx + lr0, rig_y + lr0],
                   fill=(150, 155, 186) + (150,))
        ld.ellipse([lx - lr0 * 0.5, rig_y - lr0 * 0.5,
                    lx + lr0 * 0.5, rig_y + lr0 * 0.5],
                   fill=(226, 230, 244) + (190,))

    ncol = min(n, 6)
    r = int(min(mind * 0.088, (w * 0.86) / max(1.0, ncol * 2.35)))
    r = max(14, r)
    gap = r * 0.42
    total = ncol * 2 * r + (ncol - 1) * gap
    x0 = (w - total) / 2.0 + r
    rows = int(math.ceil(n / float(ncol)))

    for k, m in enumerate(members):
        col_i = k % ncol
        row_i = k // ncol
        cx = x0 + col_i * (2 * r + gap)
        if rows == 1:
            cx = x0 + col_i * (2 * r + gap)
        else:
            in_row = min(ncol, n - row_i * ncol)
            tw = in_row * 2 * r + (in_row - 1) * gap
            cx = (w - tw) / 2.0 + r + col_i * (2 * r + gap)
        cy = stage_y - r * 1.06 - row_i * r * 2.35
        hot = _cast_hot(c, k)

        if hot:
            # 追光：从画面顶部打到头顶的锥形光柱。
            # ★ 必须画在**单独一层**上，再走一次 `_glow_layer` 模糊。
            #   直接拿硬梯形往画面上怼（第一版）得到的是"一根蓝色塑料柱压在
            #   头上"—— 光是有体积、有边缘衰减的，硬多边形给不了。
            #   所以 alpha 也给得很低（30~34），亮度交给模糊和脚边光斑去堆。
            bl = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            bd = ImageDraw.Draw(bl)
            # 从**灯架下沿**起，不从画面最顶起 —— 从最顶起就是"光从画外打进来"，
            # 顶上那盏灯和灯架脱节，看着像一颗浮着的圆点。
            top_y = h * 0.105
            head_y = cy - r * 0.72
            half_top = r * 0.30
            half_bot = r * 1.55 * (0.96 + 0.10 * b)
            bd.polygon([(cx - half_top, top_y), (cx + half_top, top_y),
                        (cx + half_bot, head_y), (cx - half_bot, head_y)],
                       fill=c["accent"] + (34,))
            bd.polygon([(cx - half_top * 0.50, top_y), (cx + half_top * 0.50, top_y),
                        (cx + half_bot * 0.50, head_y),
                        (cx - half_bot * 0.50, head_y)],
                       fill=tint(c["accent"], 1.5) + (30,))
            lr = int(r * 0.34)
            bd.ellipse([cx - lr, top_y - lr * 0.7, cx + lr, top_y + lr * 1.1],
                       fill=tint(c["accent"], 1.7) + (210,))
            img.alpha_composite(sc._glow_layer(bl, 0.95))
            img.alpha_composite(bl)
            # 脚下的一圈光斑：人不是"贴"在台面上
            rad = int(r * 2.6)
            img.alpha_composite(
                _glow_of(c["accent"]).resize((rad * 2, rad * 2), Image.BILINEAR),
                (int(cx - rad), int(stage_y - rad * 0.72)))

        rr = int(r * ((1.16 if hot else 0.90) + 0.05 * b))
        _cast_face(sc, img, ld, m, cx, cy, rr, hot, c["accent"])
        nm = (m.get("name") or "")[:6]
        fnt = _cast_font(rr * 0.40, hot)
        bb = ld.textbbox((0, 0), nm, font=fnt)
        ld.text((cx - (bb[2] - bb[0]) / 2.0 - bb[0], cy + rr + mind * 0.012),
                nm, font=fnt,
                fill=(255, 255, 255, 246) if hot else (150, 154, 172, 105),
                stroke_width=2, stroke_fill=(0, 0, 0, 150))

    # 脚灯：台沿上一排小灯，随频带走。★ 灯要够大，太小在成片里看不见。
    lamp_n = 20
    nb_ = c["spec"].shape[1]
    for k in range(lamp_n):
        v = float(c["spec"][i, min(nb_ - 1, int(k * nb_ / lamp_n))])
        lx = w * (0.055 + 0.89 * k / float(lamp_n - 1))
        lr = max(1.5, mind * (0.0034 + 0.0075 * v))
        a = int(60 + 195 * min(1.0, v / (c["smax"] or 1.0)))
        ld.ellipse([lx - lr, stage_y - lr, lx + lr, stage_y + lr],
                   fill=tint(c["accent"], 1.30) + (a,))

    ld2 = ImageDraw.Draw(img, "RGBA")
    _cast_lyric(sc, ld2, c, h * 0.688)
    _cast_strip(sc, img, c)
    _cast_outro(sc, img, c)
    return sc._finish(img)


def _arc(sc, img, cx, cy, r, a0, a1, fill, width):
    """画一段圆弧（走超采样补丁，圆是最容易看出台阶的形状）。"""
    r = float(r)
    p = max(4, int(r) + width + 3)

    def _d0(dd, _ox, _oy, _p=p, _r=r):
        dd.arc([_p - _r, _p - _r, _p + _r, _p + _r], a0, a1,
               fill=fill, width=width)

    img.alpha_composite(sc.ss_patch(cx - p, cy - p, p * 2, p * 2, _d0),
                        (int(cx - p), int(cy - p)))


def _cast_strip(sc, img, c, y_base=0.995, hmax=0.130):
    """底部一条低频谱。画面不能只有头像和字，否则像 PPT。

    ★ 宽度给到 94%、高度给到 0.13×短边 —— 第一版只有 62% 宽、0.085 高，
      在竖屏里就是贴着底边一小撮，底下 1/5 幅面整片死黑，像"没渲染完"。
      这一条同时也是**安全区**：歌词再长也不会掉到它下面去。
    """
    w, h = sc.w, sc.h
    ld = ImageDraw.Draw(img, "RGBA")
    bar_n = 56
    bw = w * 0.94 / bar_n
    bx = w * 0.03
    by = h * float(y_base)
    top = sc.mind * float(hmax)
    # ★ 频带数必须从**这一帧的数据**里取（spec.shape[1]），不能写 72。
    #   合唱族样式声明的是 nb=56，写死 72 会直接 IndexError —— 而且只在
    #   真实渲染（nb=56）时炸，用 72 造的探针数据一切正常，非常容易漏。
    nb = c["spec"].shape[1]
    for k in range(bar_n):
        v = float(c["spec"][c["i"], min(nb - 1, int(k * nb / bar_n))])
        bh = max(1.0, v * top)
        a = int(34 + 92 * v)
        ld.rectangle([bx + k * bw, by - bh, bx + k * bw + bw * 0.68, by],
                     fill=c["accent"] + (a,))


def _cast_outro(sc, img, c):
    """片尾整幅淡出到黑。"""
    if not c["outro"] or c["t"] <= c["dur"] - c["outro"]:
        return
    a = max(0.0, min(1.0, (c["t"] - (c["dur"] - c["outro"])) / max(0.2, c["outro"])))
    img.alpha_composite(Image.new("RGBA", (sc.w, sc.h), (0, 0, 0, int(255 * a))))


def _draw_cast_variant(sc, an, i, state, kind):
    if kind == "duet":
        return draw_cast_duet(sc, an, i, state)
    if kind == "ring":
        return draw_cast_ring(sc, an, i, state)
    if kind == "stage":
        return draw_cast_stage(sc, an, i, state)
    return None


# ---------------------------------------------------------------- 样式表

# ============================================================================
# MV 风（AE 感）：黑底 + 顶部角色格 + 大号粗斜体歌词 + RGB 色差 + 故障切片
# ----------------------------------------------------------------------------
# 参考片（B 站《Something Ain't Right》MLP 二创）的视觉语言其实只有四条：
#   ① 纯黑底，只留一点角色色的辉光；
#   ② 顶部一排角色格，**谁唱谁亮**；
#   ③ 大号**粗斜体**字，带红/青**色差**（RGB split）；
#   ④ 偶发横向切片错位（glitch）+ 细扫描线 + 极淡噪点。
# 这四条 PIL 全画得出来，不需要 AE。关键是 ③ 的那两笔 ——
# 少了色差和倾斜，它还是"频谱壁纸"；加上去立刻就有 MV 片头那股劲。
# ============================================================================

_MV_CACHE = {}


def _mv_overlays(w, h):
    """扫描线 + 噪点：一次生成、逐帧复用。

    ★ 每帧现算噪点（随机数填满一帧）在 1080×1920 上要 20~40 ms，
      比画歌词本身贵好几倍，而肉眼看到的只是"一点点颗粒"。
      做成贴图后每帧只是一次 `alpha_composite`。
    """
    key = (w, h)
    if key in _MV_CACHE:
        return _MV_CACHE[key]
    import numpy as np
    scan = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ds = ImageDraw.Draw(scan)
    for y in range(0, h, 3):
        ds.line([(0, y), (w, y)], fill=(0, 0, 0, 30))
    rng = np.random.RandomState(17)
    grain = Image.fromarray(rng.randint(0, 255, (h, w), dtype=np.uint8), "L")
    grain = grain.convert("RGBA")
    grain.putalpha(9)
    _MV_CACHE[key] = (scan, grain)
    return scan, grain


def _mv_text(text, size, bold, max_px, rgb=(255, 255, 255),
             dx_k=0.055, shear=0.27, alpha=235):
    """画一行**粗斜体 + RGB 色差**的字，返回已倾斜的 RGBA 贴图。

    ★ 色差怎么才有"AE 味"：不是把整帧拆 RGB 通道（那要 numpy 分通道，
      贵且会连背景一起晕），而是把**这一层文字**画三遍 ——
      红往左偏、青往右偏、白压在最上面。白的不透明，所以中间是干净的白字，
      只有边缘漏出红青两色 ⇒ 正是参考片里那种"信号发散"。
    ★ 斜体：PIL 没有 fake-italic，用仿射 shear 自己拧。
      `x' = x + shear*(H - y)` —— 顶部往右偏才叫斜体（y 向下为正）。
      写成 +y 就是倒着的，看着像漏了墨。
    """
    fnt, size = _fit_font(text, size, bold, max_px)
    tw = max(1, int(fnt.getlength(text)))
    th = int(size * 1.34)
    pad = int(size * 0.52)
    W, H = tw + pad * 2, th + pad * 2
    lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    # ★ `max(2, ...)` 会让 dx_k=0 也至少偏 2px —— 干净版"关掉色差"就关不干净
    #   （实测第一版干净样片里字边仍有红青）。所以 0 就是 0，一遍都不画。
    o = max(2, int(size * dx_k)) if dx_k > 0 else 0
    if o:
        d.text((pad - o, pad), text, font=fnt, fill=(255, 48, 96, alpha))
        d.text((pad + o, pad), text, font=fnt, fill=(0, 226, 255, alpha))
    d.text((pad, pad), text, font=fnt, fill=tuple(rgb) + (255,))
    return lay.transform((W + int(shear * H), H), Image.AFFINE,
                         (1, -shear, shear * H, 0, 1, 0),
                         resample=Image.BICUBIC)


def _mv_wrap(text, size, max_px, max_rows=2):
    """长句按词折成 2 行（只给竖屏用）。

    ★ 为什么必须折：竖屏 540 宽下 "Something ain't right" 挤在一行要缩到
      88% 宽才放得下，字高只剩屏宽的 1/10，两边还贴死边框；拆两行后每行
      只占六成宽，字号能回到接近原定尺寸，中间那块大黑也一并填掉了。
    ★ 断点不按字数、按**墨迹宽度**挑：取"两行里较长那行最短"的切法，
      这样两行长度接近，不会出现"一行三个词一行一个词"。
    """
    fnt = _cast_font(max(12, int(size)), True)
    if fnt.getlength(text) <= max_px:
        return [text]
    words = text.split()
    if len(words) < 2:
        return [text]
    best, best_w = None, None
    for k in range(1, len(words)):
        a = " ".join(words[:k])
        b = " ".join(words[k:])
        if not a or not b:
            continue
        wmax = max(fnt.getlength(a), fnt.getlength(b))
        if best_w is None or wmax < best_w:
            best, best_w = [a, b], wmax
    return best or [text]


def _mv_cast_bar(img, sc, c, y0, bar_h, dash=True, k_hot=0.38,
                 k_cold=0.10):
    """顶部角色格：一格一个人，谁在唱谁亮（其余去饱和压暗）。

    `dash=False` 去掉下沿那条黄色点线 —— 干净版不要装饰。
    """
    members = c["members"]
    n = max(1, len(members))
    bw = sc.w / float(n)
    d = ImageDraw.Draw(img, "RGBA")
    for k, m in enumerate(members):
        x0 = int(k * bw)
        hot = c["cho"] or (k == c["who"])
        col = _hex_rgb(m.get("color"))
        # 底色：角色色由上往下渐隐到黑（参考片每格都是一块"打光"的底）
        k1 = k_hot if hot else k_cold
        # ★ 原先用 `d.line(..., y0+j*2)` 逐 2 行画：奇数行空着 ⇒ 底下透出黑，
        #   画面是一层**横条纹**。片头版有扫描线盖着看不出来，干净版一眼就露。
        #   改成 2px 高的矩形，行行相接才是一块连续的底色。
        steps = max(1, bar_h // 2)
        if k1 > 0:
            for j in range(steps):
                f = 1.0 - j / float(steps)
                yy = y0 + j * 2
                d.rectangle([x0, yy, x0 + int(bw) - 2, min(y0 + bar_h, yy + 1)],
                            fill=col + (int(255 * k1 * f * 0.55),))
        cx = x0 + bw / 2.0
        cy = y0 + bar_h * 0.46
        # ★ 头像尺寸要同时受"格宽"和"格高"约束：只按格高算的话，
        #   2 个人时每格半屏、头像却只有 100px，两边一大片空。
        av = _cast_avatar(m.get("avatar"),
                          int(min(bw * 0.58, bar_h * 0.80)), dim=not hot)
        if av:
            aw = av.size[0]
            # 在唱的放大一点点 + 一圈角色色描边（"被聚光灯点到"）
            if hot:
                halo = Image.new("RGBA", (aw + 14, aw + 14), (0, 0, 0, 0))
                ImageDraw.Draw(halo).ellipse([0, 0, aw + 13, aw + 13],
                                             outline=col + (200,), width=3)
                img.alpha_composite(halo, (int(cx - aw / 2) - 7,
                                          int(cy - aw / 2) - 7))
            img.alpha_composite(av, (int(cx - aw / 2), int(cy - aw / 2)))
        else:
            rr = int(min(bw, bar_h) * 0.28)
            d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                      fill=col + (210 if hot else 80,))
            d.text((cx, cy), (m.get("name") or "?")[:1],
                   font=_cast_font(max(10, int(rr * 1.15)), True),
                   fill=(255, 255, 255, 255), anchor="mm")
        nm = (m.get("name") or "").strip()
        if nm:
            fnt = _cast_font(max(9, int(bar_h * 0.155)), hot)
            d.text((cx, y0 + bar_h * 0.875), nm, font=fnt,
                   fill=(255, 255, 255, 245) if hot
                   else (168, 168, 178, 175), anchor="mm")
        if hot:
            d.rectangle([x0 + 6, y0 + bar_h - 3, x0 + int(bw) - 8, y0 + bar_h],
                        fill=c["accent"] + (255,))
    for k in range(1, n):
        x = int(k * bw)
        d.line([(x, y0), (x, y0 + bar_h)], fill=(255, 255, 255, 28))
    # 角色格下沿一条点线（参考片同款装饰）—— 有它才像"版式"，没有就是"贴了排图"
    if dash:
        gap = max(10, int(bw * 0.10))
        r0 = max(1, int(bar_h * 0.012))
        for x in range(int(gap * 0.6), sc.w, gap):
            d.ellipse([x - r0, y0 + bar_h + 8 - r0, x + r0, y0 + bar_h + 8 + r0],
                      fill=(255, 214, 110, 185))


def _mv_glitch(img, i, amp):
    """横向切片错位。`amp` = 0 就不动（安静段不该抖）。

    ★ 种子按 `i // 2` 取：每 2 帧换一次。按 `i` 取是**每帧都变** ——
      那看起来是满屏雪花，不是故障。
    """
    if amp <= 0:
        return img
    import random
    import numpy as np
    # ★ 必须 `np.array`（拷贝），不能 `np.asarray`：后者在 PIL 图上拿到的是
    #   只读视图，`a[y:y+bh] = ...` 当场 `ValueError: assignment destination
    #   is read-only`。只在"低音重/到点触发"时才走到，所以安静的歌不会报出来。
    a = np.array(img)
    if a.ndim != 3:
        return img
    hh = a.shape[0]
    rng = random.Random(int(i) // 2)
    for _ in range(rng.randint(1, 3)):
        y = rng.randrange(0, max(1, hh - 8))
        bh = rng.randint(3, max(4, hh // 26))
        dx = rng.randint(-amp, amp)
        if dx:
            a[y:y + bh] = np.roll(a[y:y + bh], dx, axis=1)
    return Image.fromarray(a)


# 两款 MV 共用同一份排版代码，靠这几个开关决定"花不花"。
# ★ 为什么不做成两个独立函数：折行、行距、角色格位置这些**排版逻辑**只能有一份，
#   分两份一定会在下次调行距时只改一边（一个产物两种形状，已经栽过 5 次）。
DECO_FULL = {"glow": True, "dxk": 0.055, "shear": 0.27, "alpha": 235,
             "grain": True, "glitch": True, "dash": True, "spec_a": 110,
             "bg": (7, 7, 10), "k_hot": 0.38, "k_cold": 0.10}
# 干净版把"每人一块色"也压下去：只有正在唱的那个人有淡淡一层，
# 其余全黑 —— 六个人六块颜色本身就是"花"的一大半。
DECO_CLEAN = {"glow": False, "dxk": 0.0, "shear": 0.0, "alpha": 255,
              "grain": False, "glitch": False, "dash": False, "spec_a": 70,
              "bg": (10, 10, 12), "k_hot": 0.20, "k_cold": 0.0}


def draw_mv(sc, an, i, state):
    return _mv_body(sc, an, i, state, DECO_FULL)


def draw_mvclean(sc, an, i, state):
    """干净版：黑底、白字、不打光不抖不噪、没有扫描线。

    老板的原话是「太花哨」—— 故障切片、色差、扫描线、噪点这四样全关掉，
    只留"黑底 + 谁是歌手 + 大字歌词"。版式与 mv 完全同源（同一个 `_mv_body`）。
    """
    return _mv_body(sc, an, i, state, DECO_CLEAN)


def _mv_body(sc, an, i, state, deco):
    """MV 版式本体：黑底 + 顶部角色格 + 大号粗字歌词（可选色差/故障/噪点）。

    位置全部按比例算，横屏竖屏都能用（影片默认横屏，手机端是竖屏）。
    """
    c = _cast_ctx(sc, an, i)
    w, h = sc.w, sc.h
    portrait = h > w
    img = Image.new("RGBA", (w, h), tuple(deco["bg"]) + (255,))
    d = ImageDraw.Draw(img, "RGBA")

    # ---- 角色色的一点辉光 ----
    # ★ alpha 必须很小（实测 44 会把整屏染成墨绿/暖棕，"纯黑底"直接没了；
    #   12 才是"黑底上一团气"）。而且范围只盖歌词区，不铺满全屏。
    if deco["glow"]:
        gs = Image.new("RGBA", (w // 4, h // 4), (0, 0, 0, 0))
        ImageDraw.Draw(gs).ellipse([0, 0, w // 4 - 1, h // 4 - 1],
                                   fill=c["accent"] + (12,))
        gs = gs.resize((max(8, int(w * 0.92)), max(8, int(h * 0.52))),
                       Image.BILINEAR)
        # ★ 必须模糊：不模糊的话硬边椭圆会**看得见轮廓**，像"贴了一块圆形色纸"。
        #   半径给到画面高度的 9%，边缘才化开成"气"。
        gs = gs.filter(ImageFilter.GaussianBlur(max(4, int(h * 0.09))))
        img.alpha_composite(gs, (int(w * 0.04), int(h * 0.31)))

    # ---- 顶部角色格 ----
    bar_h = int(h * (0.130 if portrait else 0.300))
    if c["members"]:
        _mv_cast_bar(img, sc, c, 0, bar_h, dash=deco["dash"],
                      k_hot=deco["k_hot"], k_cold=deco["k_cold"])
    else:
        # 没有角色数据（单角色成品）时留一条极简横线，别空一块
        d.line([(0, bar_h), (w, bar_h)], fill=(255, 255, 255, 40))

    # 竖屏的量词是"大片黑"：中间不放东西会显得像没渲染完。
    # 放一行很小的歌名，既有信息量又不会抢歌词。
    if portrait:
        _song = (c["cast"].get("song") or "").strip()
        if _song:
            sf = _cast_font(max(10, int(sc.mind * 0.030)), False)
            d.text((w / 2.0, h * 0.345), _song, font=sf,
                   fill=(188, 194, 210, 125), anchor="mm")
            d.line([(w * 0.34, h * 0.378), (w * 0.66, h * 0.378)],
                   fill=(255, 255, 255, 34))

    # ---- 歌词 ----
    cur = c["cur"]
    if cur >= 0:
        ln = c["lines"][cur]
        text = (ln.get("text") or "").strip()
        zh = (ln.get("zh") or "").strip()
        t0 = float(ln.get("start") or 0)
        t1 = float(ln.get("end") or 0)
        prog = 0.0 if t1 <= t0 else max(0.0, min(1.0, (c["t"] - t0) / (t1 - t0)))
        # 入场动画：前 0.28 秒从下方 3% 高度弹上来 + 淡入
        age = max(0.0, c["t"] - t0)
        rise = int(h * 0.030 * max(0.0, 1.0 - age / 0.28))
        base = h * (0.50 if portrait else 0.63)
        # 低音重的时候整块轻微上顶（跟参考片一样"被推一下"）
        base -= h * 0.006 * c["bn"]
        size = int(sc.mind * (0.098 if portrait else 0.125))
        maxw = int(w * 0.88)
        lay = None
        glyph_top = glyph_bot = base
        if text:
            # 已唱过的字用角色色，未唱的纯白（扫色）+ 色差
            # 竖屏长句折两行；横屏 88% 宽一行放得下，不折
            rows = _mv_wrap(text, size, int(maxw * 0.94), 2) if portrait else [text]
            usz = size
            for _r in rows:
                _, _s2 = _fit_font(_r, size, True, maxw)
                usz = min(usz, _s2)
            gap = usz * 1.04
            lays, ys = [], []
            for _k, _r in enumerate(rows):
                _l = _mv_text(_r, usz, True, maxw, (255, 255, 255),
                              dx_k=deco["dxk"], shear=deco["shear"],
                              alpha=deco["alpha"])
                _lx = int((w - _l.size[0]) / 2)
                _cy = base + (_k - (len(rows) - 1) / 2.0) * gap + rise
                _ly = int(_cy - _l.size[1] / 2)
                img.alpha_composite(_l, (_lx, _ly))
                lays.append(_l)
                ys.append(_ly)
            # ★ 行距不能用 lay.size[1] 算：字体留白（ascent/descent 之外的空）占了大头，
            #   按 0.36 倍高度放中文，中文中心落在**大字的下半截**里 ⇒ 两行直接叠在一起
            #   （实测 solo 帧"有点不对劲"压住 "Something ain't right" 的 g 尾）。
            #   改成量图层真实墨迹边界 getbbox()，再按字号给固定间距。
            tops, bots = [], []
            for _l, _ly in zip(lays, ys):
                _bb = _l.getbbox()
                tops.append(_ly + (_bb[1] if _bb else 0))
                bots.append(_ly + (_bb[3] if _bb else _l.size[1]))
            if tops:
                glyph_top, glyph_bot = min(tops), max(bots)
            # ★ 关掉 karaoke 就是**纯白字 + 色差**（更像参考片的片头字）；
            #   开着才有"唱到哪染到哪"。两种都有人要，所以跟着开关走。
            #   多行时进度按**墨迹总宽**累计，逐行裁（单行取整句即可）。
            if prog > 0.01 and c["opt"]["karaoke"]:
                _f = _cast_font(max(12, int(usz)), True)
                _tot = sum(max(1.0, _f.getlength(_r)) for _r in rows) or 1.0
                _need = _tot * prog
                _acc = 0.0
                for _k, _r in enumerate(rows):
                    _wr = max(1.0, _f.getlength(_r))
                    _take = max(0.0, min(1.0, (_need - _acc) / _wr))
                    _acc += _wr
                    if _take <= 0.01:
                        break
                    done = _mv_text(_r, usz, True, maxw, c["accent"],
                                    dx_k=min(0.02, deco["dxk"]),
                                    shear=deco["shear"], alpha=deco["alpha"])
                    cw = int(done.size[0] * _take)
                    if cw > 2:
                        img.alpha_composite(done.crop((0, 0, cw, done.size[1])),
                                            (int((w - done.size[0]) / 2), ys[_k]))
        _zh_bot = None
        if zh:
            zf, zs = _fit_font(zh, int(size * 0.42), False, int(maxw * 0.9))
            # 贴着大字**真实**墨迹下沿放（glyph_bot），不再按图层高度估。
            # 0.52 是量出来的：字形底到中文墨迹顶留出约 0.16 字号的空气，
            # 0.42 时只剩 4px，肉眼看还是"贴"在一起。
            _zy = (glyph_bot + size * 0.52) if text else base
            d.text((w / 2.0, _zy), zh, font=zf, fill=(226, 232, 246, 210), anchor="mm")
            _zh_bot = _zy + zs * 0.5
        # 竖屏：长句折两行后块底离频谱还有一大截，紧贴块下方预告下一句，
        # 视线不断线；底部大片黑本身就是这款的风格（配扫描线不显空）。
        # ★ y 不能写死比例：有中文副行时它会跟中文撞在一起（实测差 15px）。
        #   按"中文墨迹下沿 + 一个行高"往下排，没中文才退回固定比例。
        if portrait and cur + 1 < len(c["lines"]):
            _nx = (c["lines"][cur + 1].get("text") or "").strip()
            if _nx:
                _nyy = max(h * 0.635, (_zh_bot + size * 0.92) if _zh_bot else h * 0.635)
                d.text((w / 2.0, _nyy), _nx,
                       font=_cast_font(max(10, int(sc.mind * 0.038)), False),
                       fill=(196, 203, 220, 74), anchor="mm")
        # 谁在唱：名字 + 一条角色色的短横线
        nm = "合唱" if c["cho"] else (
            (c["members"][c["who"]].get("name") or "")
            if 0 <= c["who"] < len(c["members"]) else "")
        if nm:
            nf = _cast_font(max(11, int(size * 0.30)), True)
            ny = (glyph_top - size * 0.46) if text else (base - size * 0.5)
            d.text((w / 2.0, ny), nm, font=nf, fill=c["accent"] + (255,), anchor="mm")
            hw = int(size * 0.34)
            d.line([(w / 2.0 - hw, ny + size * 0.26),
                    (w / 2.0 + hw, ny + size * 0.26)],
                   fill=c["accent"] + (180,), width=max(1, int(size * 0.045)))
    elif c["lines"]:
        # 间奏：黑底上一行小字，别让画面完全空着
        d.text((w / 2.0, h * 0.55), "···", font=_cast_font(int(sc.mind * 0.05), True),
               fill=(150, 155, 170, 120), anchor="mm")

    # ---- 底部：一条极低的频谱（只做"有声音在动"的提示，不抢主体）----
    spec = c["spec"]
    nb = sc.nb
    smax = c["smax"] or 1.0
    base_y = h - int(h * (0.075 if portrait else 0.085))
    bars = min(nb, 64)
    bwid = w / float(bars)
    for j in range(bars):
        v = float(spec[i, min(nb - 1, int((j + 0.5) / bars * nb))]) / smax
        bh = max(1, int(v * h * 0.045))
        x = int(j * bwid)
        # ★ 右边界必须 >= 左边界：小画幅（门禁用 180 宽）时 bwid 只有 2~3 px，
        #   `x + int(bwid) - 2` 会落到 `x + 1` 左边 ⇒ PIL 当场
        #   `ValueError: x1 must be greater than or equal to x0`。
        #   这个错只在"画幅很窄"时出现，正是门禁才会跑到的尺寸。
        d.rectangle([x + 1, base_y - bh, max(x + 1, x + int(bwid) - 2), base_y],
                    fill=c["accent"] + (deco["spec_a"],))
    # 进度线（贴着最底）
    dur = float(c["dur"] or 0)
    if dur > 0:
        pw = int(w * min(1.0, c["t"] / dur))
        d.rectangle([0, h - 4, w, h], fill=(255, 255, 255, 26))
        d.rectangle([0, h - 4, pw, h], fill=c["accent"] + (230,))

    # ---- 扫描线 + 噪点 + 故障（干净版这三样全不画）----
    if deco["grain"]:
        scan, grain = _mv_overlays(w, h)
        img.alpha_composite(scan)
        img.alpha_composite(grain)
    amp = 0
    if deco["glitch"]:
        if c["bn"] > 0.52:
            amp = 1 + int(c["bn"] * 3)
        if dur > 0 and (i % max(1, int(c["fps"] * 6)) < 2):
            amp = max(amp, 2)                  # 每 6 秒来一次小故障
    if amp:
        img = _mv_glitch(img.convert("RGB"), i, amp)
        img = img.convert("RGBA")
    return sc._finish(img)


STYLES = [
    {
        "key": "mvclean", "name": "MV 简洁（黑底白字）", "tag": "最不花",
        "draw": draw_mvclean, "palette": "neon", "nb": 64,
        "bg_top": (10, 10, 12), "bg_bot": (10, 10, 12),
        "scrim": 0.0, "vig": 0.22, "dust": 0, "rays": 0,
        "needs_cast": True, "cast_lay": "mv",
        "desc": "和「MV 片头」同一版式，但**去掉全部电子味**：没有故障切片、"
                "没有红青色差、没有扫描线、没有噪点、不打光，字是正的白色粗体。"
                "只留黑底、顶部角色格（谁唱谁亮）、一行大字歌词。"
                "嫌片头版花哨就选这个。",
    },
    {
        "key": "mv", "name": "MV 片头（色差故障）", "tag": "最像 AE",
        "draw": draw_mv, "palette": "neon", "nb": 64,
        "bg_top": (7, 7, 10), "bg_bot": (7, 7, 10), "scrim": 0.20, "vig": 0.42,
        "dust": 0, "rays": 0,
        # 它的底由 draw_mv 自己铺（纯黑 + 角色色辉光），不走 blank()；
        # 这里的 bg_* 只是 Scene 构造时要有个值，画面上看不到。
        "needs_cast": True, "cast_lay": "mv",
        "desc": "参考 B 站 MLP 二创 MV 的视觉语言：**纯黑底**、顶部一排角色格"
                "（谁唱谁亮）、大号**粗斜体**歌词带红/青**色差**、偶发横向切片"
                "故障 + 细扫描线。片头感最强 —— 想要「像 AE 做的」就选它。",
    },
    {
        "key": "orbit", "name": "光环律动", "tag": "最出片",
        "draw": draw_orbit, "palette": "neon", "nb": 72,
        "bg_top": (14, 15, 34), "bg_bot": (38, 12, 46), "scrim": 0.70, "vig": 0.62,
        "haze": (0.50, 0.72, 0.34, (96, 56, 150)),
        "desc": "中心一个圆盘，一整圈频谱柱绕着它跳动、左右镜像，低音一来就往外"
                "扩一圈光环，周围有缓慢上浮的光点。适合流行、摇滚、国风 —— "
                "最像正经音乐视频的一种。",
    },
    {
        "key": "tunnel", "name": "光之隧道", "tag": "最有纵深",
        "draw": draw_tunnel, "palette": "neon", "nb": 72,
        "bg_top": (10, 10, 28), "bg_bot": (30, 10, 44), "scrim": 0.72, "vig": 0.64,
        "haze": (0.50, 0.70, 0.32, (88, 52, 146)),
        "desc": "五层六边形光框一层层往画面中心推进、各自缓慢旋转，中心是圆盘和"
                "环绕频谱。纵深感最强，适合电子、舞曲、游戏/科技向的片子。",
    },
    {
        "key": "mirror", "name": "对称声柱", "tag": "节奏最明显",
        "draw": draw_mirror, "palette": "spectrum", "nb": 80,
        "bg_top": (12, 12, 22), "bg_bot": (10, 18, 30), "scrim": 0.76, "vig": 0.58,
        "haze": (0.50, 0.70, 0.26, (40, 70, 130)),
        "desc": "一整排锥形声柱从画面中央向上下对称展开，带水面倒影和峰值小帽。"
                "低音在左、高音在右，节奏看得最清楚。",
    },
    {
        "key": "aurora", "name": "极光同心", "tag": "氛围感最强",
        "draw": draw_aurora, "palette": "aurora", "nb": 64,
        "bg_top": (5, 12, 20), "bg_bot": (8, 20, 26), "scrim": 0.70, "vig": 0.66,
        "haze": (0.50, 0.66, 0.22, (30, 110, 110)),
        "dust": 0.7, "rays": 0,
        "desc": "一圈圈带波浪的同心光帘随频带涨落、并缓慢向外漂，低音来了再叠一层"
                "往外扩的脉冲环，像极光在呼吸。安静但有存在感，适合抒情、"
                "纯音乐、氛围电子。",
    },
    {
        "key": "pulse", "name": "声波脉冲", "tag": "干净利落",
        "draw": draw_pulse, "palette": "sunset", "nb": 48,
        "bg_top": (18, 10, 22), "bg_bot": (34, 14, 18), "scrim": 0.74, "vig": 0.60,
        "haze": (0.50, 0.70, 0.26, (150, 60, 80)),
        "dust": 0.75, "rays": 0, "glow_k": 0.30,
        "desc": "一条填了渐变的声波横贯画面，上下镜像、背后三处大光晕随低音呼吸，"
                "周围有缓慢上浮的光点。适合说唱、口播、抒情慢歌。",
    },
    {
        "key": "grid", "name": "网格矩阵", "tag": "极简 · 科技感",
        "draw": draw_grid, "palette": "neon", "nb": 64,
        "bg_top": (6, 8, 16), "bg_bot": (8, 12, 24), "scrim": 0.74, "vig": 0.62,
        "haze": (0.50, 0.44, 0.20, (40, 70, 120)),
        "dust": 0, "rays": 0, "glow_k": 0.30,
        "desc": "九乘九的点阵：一列一个频带、一行一个时间切片，最新的在最下面，"
                "越旧越暗。只有点和极淡的网格线，信息密度最低 —— 适合电子、"
                "赛博朋克那种片子，也最适合压字幕（背景再花就抢字了）。",
    },
    {
        "key": "cast", "name": "合唱台（谁唱谁亮）", "tag": "多角色专用",
        "draw": draw_cast, "palette": "neon", "nb": 56,
        "bg_top": (10, 11, 24), "bg_bot": (22, 12, 34), "scrim": 0.80, "vig": 0.56,
        "haze": (0.50, 0.72, 0.30, (72, 46, 130)),
        "dust": 0.55, "rays": 0,
        # ★ needs_cast 是「这条样式必须拿到分配表才能画」的标记。
        #   studio 那边靠它决定要不要把 cast 数据装进 style、要不要在缺数据时拦下。
        #   写成 `key == "cast"` 的话，新加的对唱/环列/追光三个样式就**永远拿不到**
        #   分配表（画面上一排空位、不报错）—— 这正是"装了但从没走到"那一类。
        "needs_cast": True, "cast_lay": "row",
        # ★ 这个样式不画"频谱主角"：它的主角是**人**。频谱只留底部一条很低的，
        #   高了就会跟头抢视觉中心（第一版给到 0.28 高度，头像全被淹了）。
        "desc": "一排角色头像，**谁在唱谁就亮起来**（放大、描边、发光），没在唱的"
                "去饱和压暗；下面滚动当前这一句歌词。多角色对唱 / 分段的成品用它，"
                "一看就知道这句是谁在唱。需要这条成品带分配表（多角色跑的才有）。",
    },
    {
        "key": "duet", "name": "对唱擂台", "tag": "两人歌最带感",
        "draw": draw_cast_duet, "palette": "neon", "nb": 56,
        "bg_top": (14, 9, 26), "bg_bot": (34, 10, 28), "scrim": 0.80, "vig": 0.60,
        "haze": (0.50, 0.70, 0.26, (120, 44, 110)),
        "dust": 0, "rays": 0,
        "needs_cast": True, "cast_lay": "duet",
        "desc": "两张脸各占左右半屏，中间一道**随当前演唱者变色**的能量线，谁唱就"
                "朝谁那边挑箭头，两人背后各有一团自己主色的光。对唱、双人 duet "
                "最有对峙感。人多也照画：前两位上擂台，其余缩成一排小头像。",
    },
    {
        "key": "ring", "name": "环列声场", "tag": "合唱团感",
        "draw": draw_cast_ring, "palette": "neon", "nb": 56,
        "bg_top": (8, 10, 26), "bg_bot": (16, 14, 38), "scrim": 0.78, "vig": 0.60,
        "haze": (0.50, 0.68, 0.28, (64, 56, 140)),
        "dust": 0, "rays": 0,
        "needs_cast": True, "cast_lay": "ring",
        "desc": "头像围成一圈坐在声场里，中心一颗随低音呼吸的球；轮到谁，**他那一段"
                "弧**就亮起来、头像放大。3~8 人的合唱最像「一圈人围着你唱」，"
                "人多时也不挤成一条。",
    },
    {
        "key": "stage", "name": "舞台追光", "tag": "现场感最强",
        "draw": draw_cast_stage, "palette": "sunset", "nb": 56,
        "bg_top": (10, 8, 18), "bg_bot": (26, 12, 22), "scrim": 0.82, "vig": 0.64,
        "haze": (0.50, 0.62, 0.30, (150, 70, 60)),
        "dust": 0, "rays": 0,
        "needs_cast": True, "cast_lay": "stage",
        "desc": "一排人站在台上，谁张嘴谁被一道**从顶上打下来的光柱**照住，其余人"
                "压成剪影；台沿一排脚灯跟着频带闪。人越多越好看（最多 6 人一列）。"
                "想要「现场 / Live」那种味道就用它。",
    },
]

FX_KEYS = [s["key"] for s in STYLES]

# ffmpeg 那套样式没有自己的配色（它们的链只画线条），给它们一张共用的背景：
# 深蓝紫渐变 + 一点雾光。有了它，滤镜画出来的线才不是浮在死黑里。
FFMPEG_BG = {
    "bg_top": (10, 12, 26), "bg_bot": (28, 10, 38),
    "scrim": 0.68, "haze": (0.50, 0.72, 0.30, (92, 52, 142)),
}


def make_bg_png(dst, w, h, style):
    """把渐变背景存成一张 PNG，给 ffmpeg 当 `-loop 1 -i` 的底。

    ffmpeg 的 `geq` 也能逐帧算渐变，但那是每帧都算一遍 ——
    一次算好、让它循环读，等于不要钱。
    """
    arr = _gradient_bg(w, h, style.get("bg_top", (13, 15, 32)),
                       style.get("bg_bot", (30, 12, 40)),
                       style.get("scrim", 0.72), haze=style.get("haze"))
    Image.fromarray(arr, "RGB").save(dst)
    return dst


def style_by_key(key):
    for s in STYLES:
        if s["key"] == key:
            return s
    return None


# ---------------------------------------------------------------- 渲染

def _load_cover(path, size=1024):
    if not path or not os.path.isfile(path):
        return None
    try:
        im = Image.open(path)
        im.load()
        return im.convert("RGB").resize((size, size), Image.LANCZOS)
    except Exception:                                        # noqa: BLE001
        return None


def ff_path(p):
    """filter 参数里的路径：先统一 `/`，再转义 `:`（顺序反了等于白转义）。"""
    return str(p).replace("\\", "/").replace(":", "\\:")


def _even(n):
    """yuv420p 要求宽高都是偶数，奇数直接编码失败。"""
    n = int(round(n))
    return max(2, n - (n % 2))


def render_fx(ffmpeg, encoder, audio, dst, style, size, fps, ass=None, log=None,
              duration=None, start=None, cover_path=None, should_stop=None,
              video_bitrate="6000k", timeout=7200, render_scale=0.5):
    """逐帧画 + 管道喂 ffmpeg。返回 (成功?, 说明)。

    `render_scale`：绘制分辨率相对成片画幅的比例。
      0.5 = 半边长（1/4 像素）⇒ 快 ~3.5 倍，代价是画面略软（见 Scene 的说明）。
    """
    def _lg(m):
        if log:
            log(m)

    w, h = int(size["w"]), int(size["h"])
    if w % 2 or h % 2:
        return False, "画幅宽高必须都是偶数（yuv420p 的要求）"
    scale = float(render_scale or 1.0)
    rw, rh = _even(max(2, w * scale)), _even(max(2, h * scale))
    if rw == w and rh == h:
        upscale = ""
    else:
        upscale = "scale=%d:%d:flags=lanczos," % (w, h)

    x = decode(ffmpeg, audio, start, duration)
    if x is None:
        # ★ 从第 40 秒去解一段 5 秒的歌，ffmpeg 不报错、老老实实给你 0 个采样。
        #   这时候说"解码失败"会让用户去查文件坏没坏 —— 其实是起始位置截过头了。
        if start:
            return False, ("起始秒数 %.1f 已经超出这段音频的长度，后面什么都没有。"
                           "把「起始」改小一点，或者干脆不勾「只做一段」做整首。" % start)
        return False, "音频解码失败（读不出采样）"
    real = len(x) / float(SR)
    # ★ 起始秒数超出音频长度时 ffmpeg 不会报错，只会给你一个空壳
    #   （实测 262 字节）。在动手画之前就挡住，别浪费用户几分钟。
    if start and real < 0.30:
        return False, "起始秒数 %.1f 已超出音频长度 %.1f 秒" % (start, real + start)
    if duration and real < duration - 0.30:
        # ★ 这两种情况必须分开报：文案混成一句的话，用户对着
        #   "起始 0.0 超长" 根本不知道其实是"要的时长太长"（我自己踩过）。
        return False, ("要做的时长 %.1f 秒比这段音频还长（从 %.1f 秒起只剩 %.1f 秒）。"
                       "把时长改小，或者不勾「只做一段」。"
                       % (duration, start or 0.0, real))

    t0 = time.time()
    an = analyze(x, fps, nb=int(style.get("nb", 72)), sr=SR)
    nframes = int(an["spec"].shape[0])
    _lg("  分析完成：%d 帧频谱（%.1f 秒素材），用时 %.1f 秒"
        % (nframes, real, time.time() - t0))
    if nframes < 2:
        return False, "音频太短（不到一帧）"

    sc = Scene(rw, rh, style, an["nb"], cover=_load_cover(cover_path))
    sc.fps = fps
    # 片头/片尾要知道整段多长（按**这段素材**算，不是按原曲）
    sc.dur = nframes / float(fps)
    draw = style["draw"]

    vf = "[0:v]" + upscale + "format=yuv420p"
    if ass:
        vf += ",subtitles='%s'" % ff_path(ass)
    vf += "[_v]"

    cmd = [ffmpeg, "-y", "-v", "error", "-nostdin",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (rw, rh),
           "-r", str(fps), "-i", "-"]
    if start:
        cmd += ["-ss", "%.3f" % start]
    if duration:
        cmd += ["-t", "%.3f" % duration]
    # ★ 音频要补静音补到**跟视频一样长**，而且必须给 `apad` 一个明确终点。
    #   裸写 `-af apad` 是"无限补"，音频流永远不结束 ⇒ ffmpeg 一直等
    #   ⇒ 整个渲染挂死（实测：卡满 15 分钟被我掐掉）。
    #   也不用 `-shortest`：音频先到尾时 ffmpeg 会关掉 stdin，
    #   我们写到最后一帧就吃 `BrokenPipeError`。
    vid_dur = nframes / float(fps)
    cmd += ["-i", audio, "-filter_complex", vf, "-map", "[_v]", "-map", "1:a",
            "-af", "apad=whole_dur=%.4f" % vid_dur, "-c:v", encoder]
    if encoder == "h264_mf":
        cmd += ["-rate_control", "quality"]
    cmd += ["-b:v", video_bitrate, "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-t", "%.4f" % vid_dur,
            "-movflags", "+faststart", dst]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    _lg("  开始逐帧绘制：%dx%d 绘制 → %dx%d 成片 @%dfps，共 %d 帧，样式「%s」"
        % (rw, rh, w, h, fps, nframes, style["name"]))

    state = {"peak": True}
    written = 0
    t0 = time.time()
    broken = False
    for i in range(nframes):
        if should_stop and should_stop():
            try:
                proc.kill()
                proc.wait(timeout=10)
            except Exception:                                # noqa: BLE001
                pass
            return False, "已取消（画到第 %d/%d 帧）" % (i, nframes)
        frame = draw(sc, an, i, state)
        try:
            proc.stdin.write(frame.tobytes())
        except (BrokenPipeError, OSError):
            # 真因是"我们还在写、ffmpeg 已经把管道关了"。只要帧数基本够了，
            # 产物就是完好的（实测最后一两帧会撞上）。
            broken = i >= nframes - 3
            break
        written += 1
        if i % max(1, int(fps * 5)) == 0 or i == nframes - 1:
            el = time.time() - t0
            eta = (el / max(1, i + 1)) * (nframes - i - 1)
            _lg("  绘制进度 %d/%d 帧（%.0f%%，已用 %.0fs，预计还要 %.0fs）"
                % (i + 1, nframes, 100.0 * (i + 1) / nframes, el, eta))
    try:
        proc.stdin.close()
    except Exception:                                        # noqa: BLE001
        pass

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return False, "编码超时"
    err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
    okfile = os.path.exists(dst) and os.path.getsize(dst) > 4096
    if proc.returncode != 0 or not okfile:
        if broken and okfile:
            pass                       # 帧数够、产物在，就认
        else:
            return False, "渲染失败：" + ("\n".join(err.splitlines()[-3:]) or "原因不明")
    mb = os.path.getsize(dst) / 1048576.0
    _lg("  渲染完成：%d 帧 / %.1f MB，总用时 %.1f 秒"
        % (written, mb, time.time() - t0))
    return True, "%.1f MB" % mb
