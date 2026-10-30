"""One-off: register content-fingerprint aliases for lyric caches written
before the alias existed.

Why it is needed: the alias file is only written at the moment a lyric is
recognized. The five caches sitting in data/cache/lyrics were written before
that code existed, so they have no alias and each of them is invisible to any
copy of the same song (different path => different mtime => different key).
They carry no record of which song they came from, so we recover it by
duration ("this 241.02 s cache must be the 241.02 s song").

Usage: python tools/lyric_alias_migrate.py [--apply]
Without --apply it only prints what it would do.
"""
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import studio  # noqa: E402

EXTS = (".mp3", ".m4a", ".wav", ".flac", ".ogg", ".mp4")


def song_files():
    out = []
    for d in ("data/music", "data/uploads"):
        p = os.path.join(HERE, d)
        if not os.path.isdir(p):
            continue
        for f in sorted(os.listdir(p)):
            if f.lower().endswith(EXTS) and not f.startswith("_"):
                out.append(os.path.join(p, f))
    return out


def main():
    apply = "--apply" in sys.argv
    caches = sorted(glob.glob(os.path.join(studio.LYRIC_CACHE, "*.json")))
    songs = song_files()
    print("caches=%d  songs=%d" % (len(caches), len(songs)))

    durs = {}
    for s in songs:
        try:
            d = studio._probe_dur_safe(s)
        except Exception:
            d = 0.0
        if d and d > 3:
            durs[s] = d

    for c in caches:
        try:
            cj = json.load(open(c, encoding="utf-8"))
        except Exception:
            print("  skip (unreadable) %s" % os.path.basename(c))
            continue
        cd = float(cj.get("duration") or 0)
        if not cd:
            print("  skip (no duration) %s" % os.path.basename(c))
            continue
        sz = cj.get("asr_size") or None
        # ★ 容差 0.3 秒，不是 1 秒：ffprobe 量的时长和 whisper 量的差不到
        #   0.1 秒，而不同歌之间差 0.4 秒以上（实测 241.02 的缓存对上
        #   241.06 的 Rather Be，同时库里还有 241.42 的 Tori Kelly、
        #   240.14 的 Showtime —— 用 1 秒容差这三首会一起入围，分不出来）。
        hits = [s for s, d in durs.items() if abs(d - cd) < 0.3]
        # 入围的可能是同一首歌的几个副本（这是常态，库里 Tori Kelly 有 8 份）
        # ⇒ 按内容指纹归并：归出来不止一首就是真分不清，宁可不登记。
        cids = sorted({studio._song_content_id(s) for s in hits})
        if len(cids) != 1:
            print("  skip (%d distinct songs for %.2fs: %s) %s"
                  % (len(cids), cd,
                     [os.path.basename(h) for h in hits][:4],
                     os.path.basename(c)))
            continue
        s = hits[0]
        cid = cids[0]
        reg = studio._lyric_alias_path(cid, "zh", sz)
        exists = os.path.isfile(reg)
        print("  %-22s %.2fs  <- %s  (cid=%s sz=%s) %s"
              % (os.path.basename(c), cd, os.path.basename(s), cid, sz,
                 "already" if exists else "NEW"))
        if apply and not exists:
            studio._lyric_alias_write(s, c, "zh", sz)

    # after registering, report which songs now resolve
    print("\n-- resolution check --")
    for s in songs:
        segs, dur = None, 0.0
        try:
            segs, dur = studio.lyric_segments_any(s, "zh")
        except Exception:
            pass
        if segs:
            print("  OK   %-52s %d ju" % (os.path.basename(s), len(segs)))
        else:
            print("  --   %s" % os.path.basename(s))


if __name__ == "__main__":
    main()
