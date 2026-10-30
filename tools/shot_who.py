#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""出一眼能看懂的对照图：同一句歌词在「甲独唱 / 合唱 / 乙独唱」下长什么样。

为什么留着它：「句前加名字」和「合唱句标成合唱」都是**画面上的**改动，
光看判据的像素计数说不清它到底好看不好看。老板一句话就能判定，比读数字快。

用法（在本项目根目录下）：
    python tools/shot_who.py
产物落在 data/_preview/who_<构型>_<归属>.png
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
os.chdir(HERE)
import vizfx  # noqa: E402

STYLES = ("cast", "duet", "ring", "stage")     # vizfx.style_by_key 认的名字
NAMES = ["甲", "乙"]
COLORS = ["#a06bff", "#5ad1ff"]
OUT = os.path.join("data", "_preview")


def fake_an(nframes=140, nb=56):
    rng = np.random.RandomState(4321)
    t = np.arange(nframes, dtype=np.float32)[:, None]
    spec = 0.40 + 0.34 * np.sin(t * 0.21 + np.arange(nb)[None, :] * 0.23)
    spec += 0.07 * rng.rand(nframes, nb)
    spec = np.clip(spec, 0.0, 1.0).astype(np.float32)
    peak = np.clip(spec + 0.05, 0.0, 1.0).astype(np.float32)
    bass = np.clip(spec[:, :6].mean(axis=1) * 1.5, 0.0, 1.0).astype(np.float32)
    wave = (0.5 * np.sin(np.arange(nframes)[:, None] * 0.5
                         + np.arange(192)[None, :] * 0.3)).astype(np.float32)
    return {"spec": spec, "peak": peak, "bass": bass, "mid": bass.copy(),
            "high": bass * 0.6, "wave": wave, "nb": nb}


def build(who, text, zh, span=20.0):
    """who: 0/1 = 那个人独唱；-1（ROLE_ALL）= 合唱。"""
    members = [{"name": NAMES[k], "avatar": "", "color": COLORS[k]}
               for k in range(len(NAMES))]
    # ★ 每一句的 `role` 必须跟上：画面取名字看的是 **line 的 role**，
    #   不是 takes。只写 takes 的话画面会一律画成第 0 个人（本脚本初版就栽在这，
    #   合唱那句显示成「甲：」，看着像产品 bug，其实是喂的假数据不全）。
    return {"members": members,
            "takes": [[0.0, span, int(who)]],
            "lines": [{"start": 0.0, "end": span, "text": text, "zh": zh,
                       "role": int(who)}],
            "title": "Solo / Chorus", "song": "demo"}


def shot(style, cast, an, out, at=90, size=(300, 520)):
    st = dict(vizfx.style_by_key(style))
    st["cast"] = cast
    st["cast_opt"] = {"singer": True}
    sc = vizfx.Scene(size[0], size[1], st, an["nb"])
    sc.fps = 30
    sc.dur = an["spec"].shape[0] / 30.0
    sc.set_supersample(2)
    img = None
    for i in range(max(0, at - 2), at + 1):
        img = st["draw"](sc, an, i, {"peak": True})
    img.convert("RGB").save(out)
    print("saved %s (%dx%d)" % (out, img.size[0], img.size[1]))
    return img


def main():
    an = fake_an()
    os.makedirs(OUT, exist_ok=True)
    cases = [(0, "hold that note", "守住这个音", "a"),
             (-1, "sing it all together", "一起唱这一句", "chorus"),
             (1, "let it ring", "让它响下去", "b")]
    for style in STYLES:
        for who, en, zh, tag in cases:
            shot(style, build(who, en, zh), an,
                 os.path.join(OUT, "who_%s_%s.png" % (style, tag)))


if __name__ == "__main__":
    main()
