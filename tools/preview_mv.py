#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""拿**老板自己的成品**渲一段 MV 风样片（改完排版要能立刻看到效果）。

为什么不用假数据：假数据只能证明"画得出来"。真歌词长度分布、真角色数量、
真时长下角色格挤不挤 —— 只有真成品能暴露，而这几样正是这款样式的全部观感。

用法（项目根目录）：
    python tools/preview_mv.py 1021_193449            # 6 角色那段
    python tools/preview_mv.py 1023_182614 --sec 24

产物：data/videos/<音频名>_mv.mp4（覆盖同名旧片）
"""
import argparse
import glob
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
os.chdir(HERE)

import studio    # noqa: E402
import vizfx     # noqa: E402
import viz       # noqa: E402

ROLESDIR = os.path.join("data", "roles")


def parse_srt(path):
    """srt → [{start,end,text}]。只取英文（成品 srt 里没有中文行）。"""
    txt = io.open(path, encoding="utf-8-sig", errors="replace").read()
    txt = txt.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for blk in re.split(r"\n\s*\n", txt.strip()):
        ls = [x for x in blk.split("\n") if x.strip()]
        if not ls:
            continue
        ti = next((k for k, l in enumerate(ls) if "-->" in l), None)
        if ti is None:
            continue
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*"
                      r"(\d+):(\d+):(\d+)[,.](\d+)", ls[ti])
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        st = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
        en = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0
        text = " ".join(x.strip() for x in ls[ti + 1:]).strip()
        if text and en > st:
            out.append({"start": round(st, 3), "end": round(en, 3),
                        "text": text, "zh": ""})
    return out


def role_at(takes, t):
    """这一句归谁。-1 = 没归到任何人（合唱 / 间奏留在分配表外）。"""
    for a, b, r in takes:
        if a - 0.40 <= t < b:
            return int(r)
    return -1


def members_of(names):
    """按 plan 里的 rid/name 取真头像与角色色（画面里要放真头像）。"""
    db = studio.load_roles()
    by_id, by_name = {}, {}
    for r in (db.get("roles") or []):
        by_id[r.get("id")] = r
        by_name[r.get("name") or ""] = r
    out = []
    for n in names:
        r = by_id.get(n.get("rid")) or by_name.get(n.get("name")) or {}
        av = r.get("avatar") or ""
        if not av:
            p = os.path.join(ROLESDIR, r.get("id") or "", "avatar.jpg")
            av = p if os.path.isfile(p) else ""
        out.append({"name": n.get("name") or r.get("name") or "",
                    "avatar": av if av and os.path.isfile(av) else "",
                    "color": r.get("color") or "#7aa2ff"})
    return out


def find_final(ts):
    """在 data/roles/*/final 里找这一批产物的 plan / wav / srt。"""
    hits = glob.glob(os.path.join(ROLESDIR, "*", "final",
                                  "plan_multi_%s.json" % ts))
    if not hits:
        raise SystemExit("没找到 plan_multi_%s.json" % ts)
    d = os.path.dirname(hits[0])
    plan = json.load(io.open(hits[0], encoding="utf-8"))
    return (d, plan,
            os.path.join(d, "mix_multi_%s.wav" % ts),
            os.path.join(d, "mix_multi_%s.srt" % ts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ts", help="成品时间戳，如 1021_193449")
    ap.add_argument("--sec", type=float, default=26.0, help="渲多少秒（默认 26）")
    ap.add_argument("--from", dest="start", type=float, default=0.0,
                    help="从第几秒开始（默认 0）")
    ap.add_argument("--style", default="mv",
                    help="样式 key（mv / mvclean …），默认 mv")
    a = ap.parse_args()

    d, plan, wav, srt = find_final(a.ts)
    if not os.path.isfile(wav):
        raise SystemExit("音频不在：%s" % wav)
    takes = [[float(x[0]), float(x[1]), int(x[2])] for x in plan["takes"]]
    lines = parse_srt(srt) if os.path.isfile(srt) else []
    for ln in lines:
        ln["role"] = role_at(takes, ln["start"])
    mem = members_of(plan.get("roles") or [])

    cast = {"members": mem, "takes": takes, "lines": lines,
            "title": os.path.basename(wav), "song": ""}
    print("角色 %d 个：%s" % (len(mem), "、".join(m["name"] for m in mem)))
    print("头像 %d 个，歌词 %d 句，takes %d 段"
          % (sum(1 for m in mem if m["avatar"]), len(lines), len(takes)))

    st = dict(vizfx.style_by_key(a.style))
    st["cast"] = cast
    st["cast_opt"] = {"singer": True}
    enc, tested = viz.probe_encoder(studio.FFMPEG, os.path.join("data", "cache"))
    print("编码器：%s（实测可用 %s）" % (enc, tested))

    tag = "_mv" if a.style == "mv" else "_" + a.style
    dst = os.path.join("data", "videos", os.path.basename(wav) + tag + ".mp4")
    ok, msg = vizfx.render_fx(studio.FFMPEG, enc, wav, dst, st,
                              {"w": 960, "h": 540}, 30,
                              duration=a.sec, start=a.start or None,
                              render_scale=1.0, log=print)
    print("%s → %s" % ("OK" if ok else "失败", msg))
    if not ok:
        return 1
    print("产物：%s（%.1f MB）" % (dst, os.path.getsize(dst) / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
