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


def find_font(size, bold=False):
    """找一个能显示中文的字体。找不到就返回 None（PIL 会用内置位图字体）。"""
    cands = ["msyhbd.ttc", "msyh.ttc", "simhei.ttf", "Dengb.ttf", "Deng.ttf",
             "arialbd.ttf", "arial.ttf"]
    root = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts")
    if bold:
        cands = ["msyhbd.ttc"] + cands
    for c in cands:
        p = os.path.join(root, c)
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:                                # noqa: BLE001
                continue
    return None


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
        self.bg = _gradient_bg(w, h, style.get("bg_top", (13, 15, 32)),
                               style.get("bg_bot", (30, 12, 40)),
                               style.get("scrim", 0.72),
                               haze=style.get("haze"))
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

    lay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lay)

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

    img.alpha_composite(sc._glow_layer(lay, 0.62))
    img.alpha_composite(lay)

    # 中心圆盘（封面或渐变球）
    rr = max(10, int(r0 * (1.0 + 0.055 * b)))
    disc, has_cover = sc._disc(rr, sc.pal[nb // 2])
    img.alpha_composite(disc, (int(cx - rr), int(cy - rr)))
    d = ImageDraw.Draw(img, "RGBA")
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

    lay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lay)
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

    img.alpha_composite(sc._glow_layer(lay, 0.55))
    img.alpha_composite(lay)

    # 轴线：中间那条细亮线，把上下两半分开
    d = ImageDraw.Draw(img, "RGBA")
    lw = max(1, int(sc.mind * 0.0022))
    d.line([(m * 0.5, axis), (w - m * 0.5, axis)], fill=(255, 255, 255, 90), width=lw)
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

    lay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lay)
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

    img.alpha_composite(sc._glow_layer(lay, 0.62))
    img.alpha_composite(lay)

    rr = max(10, int(base * 2.3 * (1.0 + 0.12 * b)))
    disc, _ = sc._disc(rr, sc.pal[int(nb * 0.35)])
    img.alpha_composite(disc, (int(cx - rr), int(cy - rr)))
    d = ImageDraw.Draw(img, "RGBA")
    d.ellipse([cx - rr * 1.10, cy - rr * 1.10, cx + rr * 1.10, cy + rr * 1.10],
              outline=tint(sc.pal[int(nb * 0.35)], 1.25) + (70,),
              width=max(1, int(rr * 0.045)))
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

    lay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lay)
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

    img.alpha_composite(sc._glow_layer(lay, 0.85))
    img.alpha_composite(lay)

    img.alpha_composite(sc._glow_layer(lay, 0.85))
    img.alpha_composite(lay)

    d = ImageDraw.Draw(img, "RGBA")
    lw2 = max(1, int(sc.mind * 0.0020))
    d.line([(m * 0.6, cy), (w - m * 0.6, cy)], fill=(255, 255, 255, 70), width=lw2)
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

    lay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lay)
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

    img.alpha_composite(sc._glow_layer(lay, 0.66))
    img.alpha_composite(lay)

    rr = max(10, int(r0 * (1.0 + 0.055 * b)))
    disc, _ = sc._disc(rr, sc.pal[nb // 2])
    img.alpha_composite(disc, (int(cx - rr), int(cy - rr)))
    d = ImageDraw.Draw(img, "RGBA")
    ring = sc.pal[nb // 2]
    d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
              outline=ring + (240,), width=max(2, int(rr * 0.045)))
    d.ellipse([cx - rr * 1.09, cy - rr * 1.09, cx + rr * 1.09, cy + rr * 1.09],
              outline=tint(ring, 1.3) + (80,), width=max(1, int(rr * 0.018)))
    return sc._finish(img)


# ---------------------------------------------------------------- 样式表

STYLES = [
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
