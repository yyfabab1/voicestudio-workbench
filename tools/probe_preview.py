# -*- coding: utf-8 -*-
"""出一张「给老板看」的对比图：四款合唱构型 + 抗锯齿前后放大对比。

不判好坏，只负责把画面摆出来 —— 数字在门禁里，眼睛看的东西在这里。

用法：
    python tools/probe_preview.py [输出.png]
"""

import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FFMPEG = r"D:\BaiduNetdiskDownload\GPT-SoVITS(1)\v5最新版\GPT-SoVITS-v5-20261005nvidia50x0\GPT-SoVITS-v2pro-20261006nvidia50x0\runtime\ffmpeg.exe"
OUT = os.path.join(ROOT, "data", "_preview")
CACHE = os.path.join(OUT, "_an.npz")

NAMES = ["暮光闪闪", "云宝", "苹果嘉儿", "瑞瑞", "小蝶", "碧琪"]
COLORS = ["#a06bff", "#5ad1ff", "#ff9f43", "#ff6ba8", "#7ee787", "#ffe066"]
LINES = ["We are the ones who light the sky", "谁在唱这一句就亮起来",
         "Every voice a different color", "四道声音叠成一条河",
         "Hold the note until it shines", "最后一个字交给夜风"]


def _ui_font(size=16, bold=False):
    """预览图上的说明文字要能显示中文 —— PIL 的默认位图字体没有中文字形，
    不换的话标签全变成 □□□□（第一版就是这样）。"""
    for p in ((r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc"),
              r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc",
              r"C:\Windows\Fonts\simhei.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def avatars():
    d = os.path.join(ROOT, "data", "roles")
    out = []
    if os.path.isdir(d):
        for r in sorted(os.listdir(d)):
            p = os.path.join(d, r, "avatar.jpg")
            if os.path.isfile(p):
                out.append(p)
    return out


def make_cast(n, step=3.0):
    av = avatars()
    members = [{"name": NAMES[k % len(NAMES)],
                "avatar": av[k % len(av)] if av else "",
                "color": COLORS[k % len(COLORS)]} for k in range(n)]
    takes = [[k * step, (k + 1) * step, k] for k in range(n)]
    lines = [{"start": k * step * 0.5, "end": k * step * 0.5 + step * 0.5,
              "text": LINES[k % len(LINES)], "zh": "第 %d 句中文" % (k + 1)}
             for k in range(n * 2)]
    return {"members": members, "takes": takes, "lines": lines,
            "title": "Signal Fire", "song": "Signal Fire"}


def load_an():
    """分析一次就缓存 —— 解码 + 频谱要十几秒，不缓存每改一次图都要重等。

    ★ 缓存**必须把 analyze 返回的数组字段全存下来**。第一版只存了 spec + nb，
      于是 `draw_cast` 里 `an["bass"]` 直接 KeyError —— 而且症状极像"渲染坏了"。
      ⇒ 只存 numpy 数组字段，其余（nb 这种整数）单独存。
    """
    import numpy as np
    import vizfx
    if os.path.exists(CACHE):
        z = np.load(CACHE)
        out = {k: z[k] for k in z.files if k != "nb"}
        out["nb"] = int(z["nb"])
        return out
    mus = os.path.join(ROOT, "data", "music")
    audio = None
    for f in sorted(os.listdir(mus)):
        if f.lower().endswith((".mp3", ".wav", ".flac")):
            p = os.path.join(mus, f)
            if os.path.getsize(p) > 200 * 1024:
                audio = p
                break
    if not audio:
        raise SystemExit("没有测试素材：data/music 下找不到能用的音频")
    x = vizfx.decode(FFMPEG, audio, None, 20.0)
    an = vizfx.analyze(x, 30, nb=72)
    arrs = {k: v for k, v in an.items() if isinstance(v, np.ndarray)}
    np.savez(CACHE, nb=an["nb"], **arrs)
    return an


def render(key, an, n=4, w=360, h=640, cast=None, ss=None):
    import vizfx
    st = vizfx.style_by_key(key)
    style = dict(st)
    if cast is not None:
        style["cast"] = cast
    sc = vizfx.Scene(w, h, style, an["nb"])
    if ss is not None:
        sc.set_supersample(ss)
    sc.fps = 30
    sc.dur = an["spec"].shape[0] / 30.0
    i = int(round((3.0 + 1.2) * 30))       # 停在"第 2 个人正在唱"那一帧
    img = None
    for k in range(max(0, i - 2), i + 1):
        img = style["draw"](sc, an, k, {"peak": True})
    return img.convert("RGB")


def main():
    import vizfx
    os.makedirs(OUT, exist_ok=True)
    an = load_an()

    # ---------- 上半：四款合唱构型 + 网格矩阵 ----------
    cards = [("cast", "合唱台·横排（谁唱谁亮）"), ("duet", "对唱擂台（左右对垒）"),
             ("ring", "环列声场（围坐一圈）"), ("stage", "舞台追光"),
             ("grid", "网格矩阵（新增·极简）")]
    W, H = 300, 534
    pad, lab = 14, 34
    f14 = _ui_font(15)
    cols = len(cards)
    sheet = Image.new("RGB", (cols * W + (cols + 1) * pad, H + pad * 2 + lab), "#0b1020")
    d = ImageDraw.Draw(sheet)
    for j, (key, name) in enumerate(cards):
        # ★ 合唱族必须把分配表喂进去：没有 `cast` 表时 draw_cast 按设计只画背景
        #   （真实链路里 studio 会先把它拦下并报错），预览图会是一整片空。
        cs = make_cast(4) if vizfx.style_by_key(key).get("needs_cast") else None
        img = render(key, an, n=4, w=W * 2, h=H * 2, cast=cs).resize((W, H), Image.LANCZOS)
        x = pad + j * (W + pad)
        sheet.paste(img, (x, pad + lab))
        d.text((x + 2, pad + 8), name, fill="#e8ecf8", font=f14)
    sheet.save(os.path.join(OUT, "cast_layouts.png"))
    print("  ✓ 构型对比 →", os.path.join(OUT, "cast_layouts.png"))

    # ---------- 下半：抗锯齿前后（ss=1 vs ss=2），同一块局部放大 3 倍 ----------
    keys_aa = ["orbit", "tunnel", "aurora"]
    ZW, ZH = 110, 110            # 从帧上裁下来的那块（原图坐标）
    zoom = 6
    rows = len(keys_aa)
    cw, ch = ZW * zoom, ZH * zoom
    sheet2 = Image.new("RGB", (pad * 3 + cw * 2, rows * (ch + lab) + pad * (rows + 1)),
                       "#0b1020")
    d2 = ImageDraw.Draw(sheet2)
    f14 = _ui_font(15)
    for r, key in enumerate(keys_aa):
        a = render(key, an, w=540, h=960, ss=1)
        b = render(key, an, w=540, h=960, ss=2)
        # ★ 裁那块**边缘最强**的地方，别用写死的坐标：
        #   写死的坐标换一首歌/换一款样式就裁到一片纯色，放大 6 倍也看不出差别，
        #   看图的人会以为"抗锯齿根本没效果"。
        import numpy as np
        g = np.asarray(a.convert("L"), dtype="float64")
        gy, gx = np.gradient(g)
        mag = np.abs(gx) + np.abs(gy)
        # 只看画面中段（避开顶/底那两条本来就对齐的区域）
        mag[: int(a.height * 0.15)] = 0
        mag[int(a.height * 0.85):] = 0
        k = int(np.argmax(mag))
        cy, cx = k // a.width, k % a.width
        x0 = int(min(max(0, cx - ZW // 2), a.width - ZW))
        y0 = int(min(max(0, cy - ZH // 2), a.height - ZH))
        box = (x0, y0, x0 + ZW, y0 + ZH)
        ca = a.crop(box).resize((cw, ch), Image.NEAREST)
        cb = b.crop(box).resize((cw, ch), Image.NEAREST)
        y = pad + r * (ch + lab + pad)
        sheet2.paste(ca, (pad, y + lab))
        sheet2.paste(cb, (pad * 2 + cw, y + lab))
        d2.text((pad + 2, y + 8), "%s ｜ 关（ss=1）" % vizfx.style_by_key(key)["name"],
                fill="#ff9a9a", font=f14)
        d2.text((pad * 2 + cw + 2, y + 8), "%s ｜ 开（ss=2，现在默认）"
                % vizfx.style_by_key(key)["name"], fill="#9ae6b0", font=f14)
    sheet2.save(os.path.join(OUT, "aa_before_after.png"))
    print("  ✓ 抗锯齿对比 →", os.path.join(OUT, "aa_before_after.png"))

    # ---------- 拼成一张 ----------
    w = max(sheet.width, sheet2.width)
    final = Image.new("RGB", (w, sheet.height + sheet2.height + 46), "#070b16")
    final.paste(sheet, ((w - sheet.width) // 2, 8))
    final.paste(sheet2, ((w - sheet2.width) // 2, sheet.height + 38))
    p = sys.argv[1] if len(sys.argv) > 1 else os.path.join(OUT, "preview_all.png")
    final.save(p)
    print("\n  成品：%s（%dx%d）" % (p, final.width, final.height))
    return 0


if __name__ == "__main__":
    sys.exit(main())
