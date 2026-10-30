# -*- coding: utf-8 -*-
r"""探针：合唱台各种布局渲一帧，看「谁在唱」是不是一眼认得出。

★★ 这个探针存在的理由
------------------------------------------------------------------
合唱台是**唯一**一种"画面主角是人"的样式，它的好坏没法用"台阶率"衡量 ——
必须看：头像够不够大、亮的那个人是不是真的跳出来、歌词有没有压在脸上。
所以这里渲三样东西：

  1. 全帧（看整体构图）
  2. 「唱的人 vs 不唱的人」两块局部放大并排（看高亮是否成立了）
  3. 每种布局在 4 人 / 6 人 两种人数下都渲（人数一变，几何全变）

用法
------------------------------------------------------------------
    python tools/probe_cast.py              # 全部布局
    python tools/probe_cast.py duet ring    # 只渲这两样
"""
import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FFMPEG = r"D:\BaiduNetdiskDownload\GPT-SoVITS(1)\v5最新版\GPT-SoVITS-v5-20261005nvidia50x0\GPT-SoVITS-v2pro-20261006nvidia50x0\runtime\ffmpeg.exe"
OUT = os.path.join(ROOT, "data", "_probe_cast")

NAMES = ["暮光闪闪", "云宝", "苹果嘉儿", "瑞瑞", "小蝶", "碧琪"]
COLORS = ["#a06bff", "#5ad1ff", "#ff9f43", "#ff6ba8", "#7ee787", "#ffe066"]
LINES = ["We are the ones who light the sky", "谁在唱这一句就亮起来",
         "Every voice a different color", "四道声音叠成一条河",
         "Hold the note until it shines", "最后一个字交给夜风"]


def avatars():
    """尽量用真头像；不够就留空（走"首字母圆"那条路，也要能看）。"""
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
    members = []
    for k in range(n):
        members.append({
            "name": NAMES[k % len(NAMES)],
            "avatar": av[k % len(av)] if av else "",
            "color": COLORS[k % len(COLORS)],
        })
    takes = [[k * step, (k + 1) * step, k] for k in range(n)]
    lines = []
    for k in range(n * 2):
        t0 = k * step * 0.5
        lines.append({"start": t0, "end": t0 + step * 0.5,
                      "text": LINES[k % len(LINES)],
                      "zh": "第 %d 句中文" % (k + 1)})
    return {"members": members, "takes": takes, "lines": lines,
            "title": "Signal Fire", "song": "Signal Fire"}


def main():
    import vizfx

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    keys = args or ["cast", "duet", "ring", "stage"]
    os.makedirs(OUT, exist_ok=True)

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
    x = vizfx.decode(FFMPEG, audio, None, 20.0)
    an = vizfx.analyze(x, 30, nb=72)
    print("  %d 帧素材" % an["spec"].shape[0])

    bad = []
    for key in keys:
        st = vizfx.style_by_key(key)
        if not st:
            print("  ✗ 没有这个样式：%s" % key)
            bad.append(key)
            continue
        for n in (4, 6):
            style = dict(st)
            style["cast"] = make_cast(n)
            # 停在"第 2 个人正在唱"的那一帧：一亮一暗才有对比可看
            i = int(round((3.0 + 1.2) * 30))
            sc = vizfx.Scene(540, 960, style, an["nb"])
            sc.fps = 30
            sc.dur = an["spec"].shape[0] / 30.0
            state = {"peak": True}
            for k in range(max(0, i - 2), i + 1):
                img = style["draw"](sc, an, k, state)
            name = "%s_%d" % (key, n)
            p = os.path.join(OUT, "cast_%s.png" % name)
            img.save(p)
            print("  ✓ %-16s → %s" % (name, p))
    print("\n  产物目录：%s" % OUT)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
