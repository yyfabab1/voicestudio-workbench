#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁：逐帧手绘画面必须是**抗锯齿**的（超采样），且几何位置不能跑偏。

为什么需要这一关
----------------
老板的原话是「现在看起来不光滑」。渲出来放大一看，根因有两层：

  ① PIL 的 `ImageDraw` 画多边形 / 椭圆 / 直线是**硬边**的 —— 一个像素要么算
     "在里面"、要么算"在外面"，斜边在像素级就是台阶；
  ② 渲染管线本来就是「半分辨率画 → ffmpeg lanczos 拉到成片」，台阶被再放大 2 倍。

实测（tools/probe_smooth.py 局部放大 3 倍）：orbit 的圆内示波环像电锯、
aurora 的同心环整圈是楼梯。

修法是超采样：按 ss 倍分辨率画，再用 **BOX（盒式平均）** 缩回去。
ss=2 时每个目标像素由 4 个样本平均，台阶被摊成 2~3 级过渡带。

这一关锁四件事：
  A. 结构：`_SSDraw` / `Scene.ss_canvas` / `Scene.ss_patch` 在，默认 ss=2
  B. 几何：超采样层里画的东西**位置和大小都对**（坐标放大但画布没放大 ⇒
     画面缩到左上角四分之一，这是这类改造最经典的翻车方式）
  C. 尺子有牙：关掉超采样，边缘陡度必须**回升**（负对照）
  D. 接线：五个画法真的走了超采样（不是"函数写了没人调"）

★ 尺子的教训：第一版用"相邻亮度跳变 > 阈值的**次数**"当判据，方向是反的 ——
  硬边是「0→100」一次大跳变，抗锯齿后变成「0→33→66→100」三次小跳变，
  计数反而变多，照着它判会把"修好了"当"更糟了"（实测 0.0356→0.0449）。
  正确的量是「边缘有多陡」：grad_p95（每次跳变的幅度）和 lap_l1（二阶差分能量），
  这两个超采样后都会**变小**。
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
SRC = os.path.join(HERE, "vizfx.py")

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


# ---------------------------------------------------------------- 尺子

def grad_p95(arr, floor=6.0):
    """边缘陡度：只统计够得上"边缘"的梯度，取 95 分位。越小越光滑。"""
    a = np.asarray(arr, dtype=np.float32)
    g = np.concatenate([np.abs(np.diff(a, axis=1)).ravel(),
                        np.abs(np.diff(a, axis=0)).ravel()])
    v = g[g > floor]
    return float(np.percentile(v, 95)) if v.size else 0.0


def lap_l1(arr):
    """二阶差分能量。台阶/锯齿都藏在它里面，越小越光滑。"""
    a = np.asarray(arr, dtype=np.float32)
    lap = (4.0 * a[1:-1, 1:-1] - a[:-2, 1:-1] - a[2:, 1:-1]
           - a[1:-1, :-2] - a[1:-1, 2:])
    return float(np.abs(lap).mean())


def fake_an(nframes=40, nb=72):
    """造一份"有起伏"的假分析数据：不碰音频、不碰显卡，纯确定性。"""
    rng = np.random.RandomState(20261023)
    t = np.arange(nframes, dtype=np.float32)[:, None]
    spec = 0.45 + 0.34 * np.sin(t * 0.31 + np.arange(nb)[None, :] * 0.19)
    spec += 0.08 * rng.rand(nframes, nb)
    spec = np.clip(spec, 0.0, 1.0).astype(np.float32)
    peak = np.clip(spec + 0.06, 0.0, 1.0).astype(np.float32)
    bass = np.clip(spec[:, :8].mean(axis=1) * 1.4, 0.0, 1.0).astype(np.float32)
    wave = (0.5 * np.sin(np.arange(nframes)[:, None] * 0.7
                         + np.arange(192)[None, :] * 0.33)).astype(np.float32)
    return {"spec": spec, "peak": peak, "bass": bass,
            "mid": bass.copy(), "high": bass * 0.7,
            "wave": wave, "nb": nb}


# ---------------------------------------------------------------- A 结构

def a_struct():
    print("\nA 结构（超采样的对象在不在、默认值对不对）")
    src = open(SRC, encoding="utf-8").read()
    check("_SSDraw 类存在", "class _SSDraw(object):" in src)
    check("Scene.ss_canvas 存在", "def ss_canvas(self" in src)
    check("Scene.ss_patch 存在（局部超采样，头像圆环用）", "def ss_patch(self" in src)
    check("Scene.set_supersample 存在（负对照靠它关掉）",
          "def set_supersample(self" in src)
    check("默认 ss=2", "self.ss = 2.0" in src)
    # ★ 下采样必须是 BOX。写成 LANCZOS 不会报错、画面也还行，但会慢 4 倍
    #   （实测 3 分钟的歌从 2.2 分钟变 8.2 分钟），而且边缘会带振铃亮边。
    n_lan = src.count("Image.LANCZOS)")
    check("超采样下采样用 BOX（不是 LANCZOS）", "big.resize(wh, Image.BOX)" in src,
          "LANCZOS 出现 %d 处（其余是头像/封面缩小，允许）" % n_lan)
    check("width 跟着 ss 放大（不放大线条会越描越淡）",
          "def _wd(self, w)" in src and "float(w) * self._ss" in src)
    check("字体跟着 ss 放大（不然字会糊）", "def font(self, fnt)" in src)

    import vizfx
    st = vizfx.style_by_key("orbit")
    sc = vizfx.Scene(80, 80, st, 8)
    check("默认实际生效 ss=2", abs(sc.ss - 2.0) < 1e-6, "ss=%.2f" % sc.ss)
    check("set_supersample(1) 能关掉", abs(sc.set_supersample(1) - 1.0) < 1e-6)
    check("set_supersample(0) 不会变成 0 倍", sc.set_supersample(0) >= 1.0,
          "ss=%.2f" % sc.ss)


# ---------------------------------------------------------------- B 几何

def b_geom():
    print("\nB 几何（超采样层里画的东西位置/大小对不对）")
    import vizfx
    st = vizfx.style_by_key("orbit")
    W = H = 100
    sc = vizfx.Scene(W, H, st, 8)
    sc.set_supersample(2)
    d, done = sc.ss_canvas()
    # 画一个**不对称**的矩形：位置和尺寸一起考，写反了立刻看得见
    d.rectangle([10, 20, 40, 60], fill=(255, 0, 0, 255))
    lay = done()
    arr = np.asarray(lay.convert("RGB"))
    alpha = np.asarray(lay.split()[3])

    check("返回尺寸是 1×（不是 2×）", lay.size == (W, H), str(lay.size))

    def _red(x, y):
        r, g, b = arr[y, x]
        return int(r) > 180 and int(g) < 80 and int(b) < 80

    check("矩形左上角落在 (12,22)", _red(12, 22),
          "px=%s" % (tuple(arr[22, 12]),))
    check("矩形右下角落在 (38,58)", _red(38, 58),
          "px=%s" % (tuple(arr[38, 58]),))
    check("矩形**外面**没被涂上（(5,5) 透明）", int(alpha[5, 5]) < 12,
          "a=%d" % int(alpha[5, 5]))
    check("矩形外面 (45,65) 也透明（没有向右下溢出）", int(alpha[65, 45]) < 12,
          "a=%d" % int(alpha[65, 45]))
    # 面积：40-10=30 宽、60-20=40 高 ⇒ 1200 像素，允许边缘 1 像素误差
    area = int((alpha > 128).sum())
    check("矩形面积约 1200 像素（位置和尺寸同时正确）", 1100 <= area <= 1300,
          "area=%d" % area)

    # 线宽：给 width=2，缩放后应该是 4 个超采样像素 ⇒ 回到 1× 约 2 像素宽
    d2, done2 = sc.ss_canvas()
    d2.line([(10, 50), (90, 50)], fill=(0, 255, 0, 255), width=2)
    lay2 = done2()
    rgb = np.asarray(lay2.convert("RGB"))
    # ★ 取**一行**（y=50），不是一列 —— 横线在行方向铺开，取列只会碰到两三个
    #   像素（第一版写反了，量出来 len=3，看着像"线没画出来"）。
    #   线可能落在 49/50/51 三行里（亚像素位置），取三行最大值。
    greens = max(int((((rgb[y, :, 1] > 140) & (rgb[y, :, 0] < 90))).sum())
                 for y in (49, 50, 51))
    # 端点含在 10..90 里 ⇒ 理论 81 像素。允许 ±3 的取整误差。
    check("width=2 的横线缩回后约 81 像素长", 78 <= greens <= 84, "len=%d" % greens)

    # ss_patch：小补丁的坐标要从补丁自己的左上角算
    patch = sc.ss_patch(50, 50, 20, 20,
                        lambda dd, ox, oy: dd.ellipse([2, 2, 17, 17],
                                                      fill=(0, 0, 255, 255)))
    pa = np.asarray(patch.split()[3])
    check("ss_patch 尺寸 = 请求的 20×20", patch.size == (20, 20), str(patch.size))
    check("ss_patch 的圆在补丁中心（不是跑到补丁外）",
          int(pa[10, 10]) > 200 and int(pa[1, 1]) < 20,
          "center=%d corner=%d" % (int(pa[10, 10]), int(pa[1, 1])))


# ---------------------------------------------------------------- C 尺子有牙

def _frame(style_key, ss, an, at=18, sc_size=(270, 480)):
    import vizfx
    st = vizfx.style_by_key(style_key)
    sc = vizfx.Scene(sc_size[0], sc_size[1], st, an["nb"])
    sc.fps = 30
    sc.dur = an["spec"].shape[0] / 30.0
    sc.set_supersample(ss)
    state = {"peak": True}
    img = None
    for i in range(at - 3, at + 1):
        img = st["draw"](sc, an, i, state)
    return np.asarray(img.convert("L"))


def c_ruler():
    print("\nC 尺子有牙（关掉超采样，边缘陡度必须回升）")
    an = fake_an()
    rows = []
    for key in ("orbit", "tunnel", "mirror", "aurora", "pulse", "grid"):
        off = _frame(key, 1, an)
        on = _frame(key, 2, an)
        g1, g2 = grad_p95(off), grad_p95(on)
        l1, l2 = lap_l1(off), lap_l1(on)
        rows.append((key, g1, g2, l1, l2))
        # ★ 逐个样式判，不给"平均下来变好了"留后门：某个样式没接到超采样，
        #   靠其它样式拉平均也能变绿 —— 那正是"假绿"。
        check("%-7s 陡度下降 %.1f → %.1f" % (key, g1, g2), g2 < g1 * 0.94)
        # ★★ 锯齿能量（lap_l1）**不参与判定**，只打印参考。
        #   实测它会对"实心块"给出反向读数：mirror 全是轴对齐的窄矩形，
        #   抗锯齿把"一处强跳变"摊成"两处半亮"，二阶差分总量反而升 12%
        #   （7.60 → 8.53），而同一张图的陡度是 194 → 104（砍掉 46%，确实变软了）。
        #   按铁律：负对照下不变红（这里是"变红"但方向不对）的判据不许当判据用 ——
        #   留着它判会把"实心块的抗锯齿"误判成回归。
        print("         · 锯齿能量（参考，不判定）%.3f → %.3f" % (l1, l2))
    mg1 = sum(r[1] for r in rows) / len(rows)
    mg2 = sum(r[2] for r in rows) / len(rows)
    check("整体陡度改善 ≥ 15%%（%.1f → %.1f，%.0f%%）"
          % (mg1, mg2, 100 * (1 - mg2 / mg1)), mg2 < mg1 * 0.85)
    # 负对照：把 ss 强行设成 1 再跑一遍，必须回到原来的数（证明 ss 真的在起作用）
    again = _frame("orbit", 1, an)
    check("★ 负对照：ss=1 复跑与第一次一致（差 < 2）",
          abs(grad_p95(again) - rows[0][1]) < 2.0,
          "%.1f vs %.1f" % (grad_p95(again), rows[0][1]))


# ---------------------------------------------------------------- D 接线

def d_wiring():
    print("\nD 接线（五个画法真的走了超采样，不是写了没人调）")
    src = open(SRC, encoding="utf-8").read()
    for fn in ("draw_orbit", "draw_mirror", "draw_aurora",
               "draw_pulse", "draw_tunnel", "draw_grid"):
        i = src.find("def %s(" % fn)
        j = src.find("\ndef ", i + 10)
        body = src[i:j if j > 0 else len(src)]
        check("%-12s 里调了 ss_canvas()" % fn, "sc.ss_canvas()" in body)
        # 老写法必须绝迹：直接建全尺寸图层再画 = 完全没有超采样
        check("%-12s 里没有遗留 lay = Image.new" % fn,
              "lay = Image.new(\"RGBA\", (w, h)" not in body)
        # _done() 只能调一次：每次调用都真跑一遍 BOX 下采样，调两次白扔一倍开销
        n_done = body.count("= _done()")
        check("%-12s 的 _done() 只取一次" % fn, n_done <= 1,
              "调了 %d 次" % n_done)

    # 合唱台的圆环走 ss_patch
    check("_cast_face 里圆环走 ss_patch", "sc.ss_patch(" in src)
    check("_cast_avatar 的圆形遮罩超采样过（4 倍画再缩）",
          "mask_big" in src and "resize((n, n), Image.BOX)" in src)


def main():
    print("门禁：逐帧手绘必须抗锯齿（超采样）+ 几何不许跑偏")
    a_struct()
    b_geom()
    c_ruler()
    d_wiring()
    print("\n通过 %d 项，失败 %d 项" % (N_PASS, len(FAILS)))
    for f in FAILS:
        print("  失败：%s" % f)
    print("EXIT=%d" % (1 if FAILS else 0))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
