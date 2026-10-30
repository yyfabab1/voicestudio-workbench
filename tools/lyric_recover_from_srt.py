# -*- coding: utf-8 -*-
r"""把**认过词的歌**的歌词缓存从成品 .srt 里倒回来。

为什么需要它：verify_lyric_ui 以前会清空整个歌词缓存目录（2026-10-23 实测，
跑一趟门禁，4 首歌的识别结果当场没了）。那些词其实没有真正丢失 ——
每首歌的**成品 .srt** 里就存着逐句的时间戳和文本，而 srt 正是从那份缓存
生成的。所以能原地倒回去，不必让老板把歌再认一遍（每首一两分钟）。

用法（项目根目录）：
    python tools/lyric_recover_from_srt.py            # 只列，不动手
    python tools/lyric_recover_from_srt.py --apply

只处理"库里这首歌现在没有缓存"的情况 —— 有缓存的一律跳过（不许覆盖真结果）。
"""
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import studio  # noqa: E402

EXTS = (".mp3", ".m4a", ".wav", ".flac", ".ogg")


def parse_srt(path):
    """读成品 srt，返回 [(start, end, text), ...]。

    ★ 一条 srt 的字幕里可能有两行（顯示時换行），合起来才是完整的一句 ——
      这正是缓存里 `line` 用 `|` 分开的那些句子。
    """
    try:
        raw = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return []
    blocks = re.split(r"\n\s*\n", raw.strip())
    out = []
    for b in blocks:
        lines = [x for x in b.splitlines() if x.strip()]
        if len(lines) < 2:
            continue
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*"
                      r"(\d+):(\d+):(\d+)[,.](\d+)", lines[1])
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = [int(x) for x in m.groups()]
        a = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000.0
        b2 = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000.0
        txt = "".join(x.strip() for x in lines[2:])
        if not txt or b2 <= a:
            continue
        out.append((a, b2, txt))
    return out


def library():
    out = {}
    for d in ("data/music", "data/uploads"):
        p = os.path.join(HERE, d)
        if not os.path.isdir(p):
            continue
        for f in sorted(os.listdir(p)):
            if f.lower().endswith(EXTS) and not f.startswith("_"):
                fp = os.path.join(p, f)
                try:
                    du = studio._probe_dur_safe(fp)
                except Exception:
                    du = 0.0
                if du and du > 3:
                    out[fp] = du
    return out


def main():
    apply = "--apply" in sys.argv
    songs = library()
    # 成品 -> (时长, srt)
    done = {}
    for p in glob.glob(os.path.join(HERE, "data", "roles", "*", "final",
                                    "multi_multi_*.json")):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        secs = round(float(d.get("secs") or 0), 2)
        srt = p.replace("multi_multi_", "mix_multi_").replace(".json", ".srt")
        if secs > 3 and os.path.isfile(srt) and secs not in done:
            done[secs] = srt

    for secs in sorted(done):
        srt = done[secs]
        segs_raw = parse_srt(srt)
        if not segs_raw:
            continue
        hits = [s for s, d0 in songs.items() if abs(d0 - secs) < 0.3]
        cids = sorted({studio._song_content_id(s) for s in hits})
        if len(cids) != 1:
            print("  skip  %.2fs（库里有 %d 首歌同长度，分不清）" % (secs, len(cids)))
            continue
        target = hits[0]
        # 只补现在真的没有的（不许覆盖）
        if os.path.isfile(studio.lyric_cache_path(target, "zh", "medium")):
            print("  skip  %-52s 已有缓存" % os.path.basename(target))
            continue
        segs = [{"start": round(a, 3), "end": round(b, 3), "text": t}
                for a, b, t in segs_raw]
        print("  %-52s %6.2fs  倒回 %d 句（来自 %s）"
              % (os.path.basename(target), secs, len(segs),
                 os.path.basename(srt)))
        if apply:
            studio.save_lyric_segments(target, segs, lang="zh", size="medium")


if __name__ == "__main__":
    main()
