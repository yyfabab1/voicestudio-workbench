#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""出一眼能判定的 MV 风样片：黑底 + 顶部角色格 + 粗斜体色差歌词 + 故障。

为什么单开一个出图脚本：色差、倾斜、故障、扫描线这四样**判据说不清好不好看**，
只能人眼判。老板一句话就能说"就要这个"或"太花"，比读像素计数快。

用法（在本项目根目录下）：
    python tools/shot_mv.py
产物：data/_preview/mv_<独唱|合唱|间奏>.png
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
os.chdir(HERE)
import studio  # noqa: E402
import vizfx  # noqa: E402

OUT = os.path.join("data", "_preview")
SIZE = (960, 540)          # 横屏，和参考片同比例（16:9）


def fake_an(nframes=150, nb=64):
    rng = np.random.RandomState(4321)
    t = np.arange(nframes, dtype=np.float32)[:, None]
    spec = 0.40 + 0.34 * np.sin(t * 0.19 + np.arange(nb)[None, :] * 0.21)
    spec += 0.08 * rng.rand(nframes, nb)
    spec = np.clip(spec, 0.0, 1.0).astype(np.float32)
    peak = np.clip(spec + 0.05, 0.0, 1.0).astype(np.float32)
    bass = np.clip(spec[:, :6].mean(axis=1) * 1.6, 0.0, 1.0).astype(np.float32)
    wave = (0.5 * np.sin(np.arange(nframes)[:, None] * 0.5
                         + np.arange(192)[None, :] * 0.3)).astype(np.float32)
    return {"spec": spec, "peak": peak, "bass": bass, "mid": bass.copy(),
            "high": bass * 0.6, "wave": wave, "nb": nb}


def real_members(n=6):
    """仓库里真实角色的名字/头像/角色色 —— 画面里放的都是真头像。"""
    db = studio.load_roles()
    out = []
    for r in (db.get("roles") or []):
        av = r.get("avatar") or ""
        out.append({"name": r.get("name") or r.get("id") or "",
                    "avatar": av, "color": r.get("color") or "#7aa2ff"})
    with_av = [m for m in out if m["avatar"] and os.path.isfile(m["avatar"])]
    pick = (with_av or out)[:n]
    return pick


def build(members, roles, text, zh):
    """roles: 每一句归谁（-1 = 合唱）。"""
    lines, takes, t = [], [], 0.0
    for k, who in enumerate(roles):
        if k == 0:
            s, e = 0.0, 3.2
        elif k == 1:
            s, e = 3.2, 6.4
        else:
            s, e = 6.4, 9.6
        lines.append({"start": s, "end": e,
                      "text": text if k == 0 else "%s 2" % text,
                      "zh": zh if k == 0 else "", "role": int(who)})
        takes.append([s, e, int(who)])
    return {"members": members, "takes": takes, "lines": lines,
            "title": "Something Ain't Right", "song": "demo"}


def shot(cast, an, out, at=50, size=SIZE, key="mv"):
    st = dict(vizfx.style_by_key(key))
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


def main():
    os.makedirs(OUT, exist_ok=True)
    an = fake_an()
    mem = real_members(6)
    print("角色：", [m["name"] for m in mem],
          "（有头像 %d 个）" % sum(1 for m in mem if m["avatar"]))
    n = len(mem)
    # 独唱：第 3 个人在唱（t≈1.67s 落在第 1 句）
    cast_a = build(mem, [2] + [0] * (n - 1), "Something ain't right", "有点不对劲")
    shot(cast_a, an, os.path.join(OUT, "mv_solo.png"))
    # 合唱：第 1 句所有人一起
    cast_b = build(mem, [-1] + [0] * (n - 1), "Everybody sing it now", "现在大家一起唱")
    shot(cast_b, an, os.path.join(OUT, "mv_chorus.png"))
    # 竖屏（手机端）：布局全按比例算，横竖屏共用一套 draw
    shot(cast_a, an, os.path.join(OUT, "mv_portrait.png"),
         size=(540, 960))
    # 干净版（老板嫌花哨）—— 同一份排版，装饰全关
    shot(cast_a, an, os.path.join(OUT, "clean_solo.png"), key="mvclean")
    shot(cast_b, an, os.path.join(OUT, "clean_chorus.png"), key="mvclean")
    shot(cast_a, an, os.path.join(OUT, "clean_portrait.png"),
         size=(540, 960), key="mvclean")


if __name__ == "__main__":
    main()
