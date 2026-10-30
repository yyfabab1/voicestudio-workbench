# -*- coding: utf-8 -*-
r"""给**已有的**成品补一份 .srt（历史数据一次性补齐）。

新成品在落盘时就自己带了（studio._write_srt），这一支只管老数据。

跑法（cwd = VoiceStudio）：
    <venv python> -s tools/backfill_srt.py            # 只看不改（默认）
    <venv python> -s tools/backfill_srt.py --apply    # 真的写

来源按优先级（跟 `_write_srt` 一致，别在这里开后门）：
  ① 歌自带字幕（源旁边有同名 .srt）⇒ 原样复制，不改一个字；
  ② 有 .lrc（带时间轴）⇒ 反解出来生成；
  ③ 多人合唱成品身上记的 `params.multi_segs` ⇒ 用分句生成；
  ④ 都没有 ⇒ **不生成**，绝不按行数均分时间轴。

★ 改 roles.json 前先备份；写入走 studio.mutate_roles（原子落盘）。
"""
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import studio                                            # noqa: E402
import viz                                               # noqa: E402
from stages import srt_cli                               # noqa: E402


def _own_srt(src):
    """源音频旁边有没有同名 .srt？"""
    if not src:
        return None
    try:
        stem = os.path.splitext(os.path.abspath(str(src)))[0]
    except Exception:                                    # noqa: BLE001
        return None
    for ext in (".srt", ".SRT"):
        p = stem + ext
        if os.path.isfile(p):
            return p
    return None


def _same(a, b):
    """两个文件内容一样吗？（读不了就当不一样，会重补一次）"""
    try:
        return bool(a) and bool(b) and os.path.isfile(a) and os.path.isfile(b) and \
            open(a, encoding="utf-8", errors="replace").read() == \
            open(b, encoding="utf-8", errors="replace").read()
    except OSError:
        return False


def _brief_to_segs(ms):
    """`[[秒, 词, 中文?], …]` → `[{"start","end","text","zh"}]`。

    ★ 合唱分句里**没有 end**（存的时候刻意只留秒 + 词），所以这里必须留 None ——
      交给 `from_segments` 用「下一句的 start」去补尾，而不是拍脑袋给个固定时长。
    """
    out = []
    for m in (ms or []):
        if not isinstance(m, (list, tuple)) or not m:
            continue
        t = str(m[1] if len(m) > 1 else "").strip()
        if not t:
            continue
        d = {"start": m[0], "end": None, "text": t}
        if len(m) > 2 and str(m[2] or "").strip():
            d["zh"] = str(m[2]).strip()
        out.append(d)
    return out


def plan(db):
    """扫一遍所有成品，返回待办 [(rid, index, 方式, 说明, segs或None)]。"""
    todo = []
    for r in db.get("roles") or []:
        rid = r.get("id")
        for i, o in enumerate(r.get("outputs") or []):
            wav = o.get("wav") or ""
            if not wav or not os.path.isfile(wav):
                continue
            exist = o.get("lyric_srt")
            own = _own_srt(((o.get("stems") or {}).get("src")))

            if own:
                # 自带字幕在场 ⇒ 一律以它为准（哪怕之前记过别的）
                if not _same(exist, own):
                    todo.append((rid, i, "自带字幕", os.path.basename(own), None))
                continue
            if exist and os.path.isfile(exist):
                continue                                  # 已经有字幕了，不动
            lrc = o.get("lyric_lrc")
            if lrc and os.path.isfile(lrc):
                todo.append((rid, i, "由 .lrc 生成",
                             os.path.basename(lrc), None))
                continue
            ms = (o.get("params") or {}).get("multi_segs") or []
            if ms:
                todo.append((rid, i, "由合唱分句生成", "%d 句" % len(ms),
                             _brief_to_segs(ms)))
    return todo


def apply_db(db, todo):
    """把待办写进 db（只改内存；落盘交给 studio.mutate_roles）。"""
    done = {}
    errs = []
    idx = {r.get("id"): r for r in (db.get("roles") or [])}
    for rid, i, kind, note, segs in todo:
        r = idx.get(rid)
        if not r:
            continue
        try:
            o = (r.get("outputs") or [])[i]
            dst = os.path.splitext(o["wav"])[0] + ".srt"
            if kind == "自带字幕":
                shutil.copyfile(_own_srt(((o.get("stems") or {}).get("src"))), dst)
            elif kind == "由 .lrc 生成":
                pairs = viz.parse_lrc(o["lyric_lrc"])
                rows = srt_cli.from_segments(
                    [{"start": a, "end": None, "text": b} for a, b in pairs],
                    o.get("seconds"))
                if not rows:
                    errs.append("%s：.lrc 里没有时间轴 ⇒ 不生成" % note)
                    continue
                with open(dst, "w", encoding="utf-8") as f:
                    f.write(srt_cli.dump(rows))
            else:
                rows = srt_cli.from_segments(segs, o.get("seconds"))
                if not rows:
                    errs.append("%s：分句里没有可用文本 ⇒ 不生成" % note)
                    continue
                with open(dst, "w", encoding="utf-8") as f:
                    f.write(srt_cli.dump(rows))
            o["lyric_srt"] = dst
            done[kind] = done.get(kind, 0) + 1
        except Exception as e:                            # noqa: BLE001
            errs.append("%s：%s" % (note, e))
    return done, errs


def main(argv):
    do_apply = "--apply" in argv
    db = studio.load_roles()
    todo = plan(db)
    outs = [o for r in (db.get("roles") or []) for o in (r.get("outputs") or [])]
    print("成品 %d 条；待补 %d 条" % (len(outs), len(todo)))
    for rid, i, kind, note, _segs in todo[:20]:
        print("   %s  #%d  %-14s %s" % (rid, i, kind, note))
    if len(todo) > 20:
        print("   ... 还有 %d 条" % (len(todo) - 20))
    if not todo:
        print("   （都有字幕了，不用补）")

    if not do_apply:
        print("\n（dry-run；加 --apply 才真的写）")
        return 0

    bak = studio.ROLES_JSON + ".bak-srt-" + time.strftime("%Y%m%d_%H%M%S")
    shutil.copyfile(studio.ROLES_JSON, bak)
    print("\n已备份 → %s" % os.path.basename(bak))

    box = {}

    def _go(d):
        done, errs = apply_db(d, todo)
        box["done"] = done
        box["errs"] = errs

    studio.mutate_roles(_go)
    done = box.get("done", {})
    errs = box.get("errs", [])
    print("补完：")
    for k in sorted(done):
        print("   %-14s %d 条" % (k, done[k]))
    for e in errs[:10]:
        print("   ! %s" % e)
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
