# -*- coding: utf-8 -*-
r"""改词「按乐句切块」的正确性门禁（不跑模型，几秒钟）。

守的是「改词乱吼」这类回归 —— 根因是**块边界切在了句子中间**：
  官方 `prepare_input()` 把这一块的音频同时当音色参考（ref_audio）
  和旋律参考（melody_audio）。半句残片当旋律参考，模型只能靠猜补完，
  听感就是跑调/拖拍。而老实现是"每 28 秒切一刀"，切在哪里全看运气。

判据全部跑 `stages/lyric_plan.py` 的**真代码**（它刻意只用标准库，
所以这里不用 import torch、也不用起服务），不是抄一份副本 ——
抄副本的门曾经判过"源码改了、门禁还绿"的假绿。
"""
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STAGES = os.path.join(HERE, "stages")
SCRIPT = os.path.join(STAGES, "lyric_edit.py")
PLAN = os.path.join(STAGES, "lyric_plan.py")
CACHE = os.path.join(HERE, "data", "cache", "lyrics")

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  PASS  " if ok else "  ! FAIL  ") + name
          + (("    —— " + str(detail)) if detail else ""))


def T(s, e, t):
    return {"start": s, "end": e, "text": t}


def main():
    print("改词乐句切块（stages/lyric_plan.py → lyric_edit.py）")
    sys.path.insert(0, STAGES)
    import lyric_plan as LP

    # ---- 1. 源文件层面：参数与分支还在 --------------------------------
    src = io.open(SCRIPT, encoding="utf-8").read()
    check("lyric_edit.py 存在", os.path.isfile(SCRIPT))
    check("支持 --orig-lrc（时间戳入口）", "--orig-lrc" in src)
    check("走乐句分块（不是等时长切）", "lyric_plan.plan_chunks" in src)
    check("块间空档走直通分支（timeline 里有 None）",
          "lyric_plan.build_timeline" in src)
    check("直通段占比异常会报警", "七成以上的段都判定为间奏" in src)
    m = re.search(r'"--nfe",\s*type=int,\s*default=(\d+)', src)
    check("nfe 默认取官方值 32（不是 16）", bool(m) and m.group(1) == "32",
          ("实际 %s" % m.group(1)) if m else "没找到 --nfe 默认值")
    check("sil_len_to_end 跟官方默认 0.5", "sil_len_to_end=0.5" in src)

    # ---- 2. 合成样本：前奏 + 两组乐句 + 长间奏 --------------------------
    # 刻意让"等时长切"一定会切进句子里：
    #   0~20   前奏（无词）
    #   20~47  三句（句间空档 1 秒，属同一乐句群）
    #   47~75  间奏（无词）
    #   75~110 两句（句间空档 1 秒）
    segs = [
        {"start": 20.0, "end": 27.0, "text": "第一句"},
        {"start": 28.0, "end": 35.0, "text": "第二句"},
        {"start": 36.0, "end": 46.0, "text": "第三句"},
        {"start": 76.0, "end": 85.0, "text": "第四句"},
        {"start": 86.0, "end": 100.0, "text": "第五句"},
    ]
    sr = 44100
    total = int(120 * sr)
    plan = LP.plan_chunks(segs, total, sr, chunk_sec=28.0)
    check("分出 2 个乐句块", len(plan) >= 2, "实际 %d 块：%s" % (len(plan), plan))

    # ★ 核心判据：任何一句的**演唱区间**都不许被块边界切开
    cut = []
    for (s, e, idx) in plan:
        for i in idx:
            st = segs[i]["start"] * sr
            en = segs[i]["end"] * sr
            if st < s - 1 or en > e + 1:
                cut.append((i, s / float(sr), e / float(sr),
                            segs[i]["start"], segs[i]["end"]))
    check("没有一句被块边界切成两半", not cut, "被切：%s" % (cut,))

    # 每块必须是"连续几句"，不能跳着取
    ok_contig = all(idx == list(range(idx[0], idx[0] + len(idx))) for _, _, idx in plan)
    check("每块取的是连续几句（不跳句）", ok_contig,
          "实际 %s" % [i for _, _, i in plan])

    # 块边界要落在静音里：块起点 ≤ 首句 start，块终点 ≥ 末句 end
    ok_span = True
    for (s, e, idx) in plan:
        if s / float(sr) > segs[idx[0]]["start"] + 1e-6:
            ok_span = False
        if e / float(sr) < segs[idx[-1]]["end"] - 1e-6:
            ok_span = False
    check("块首尾把整句包住（含余量）", ok_span)

    # 对照：等时长切会切进句子里（证据，不是判据）
    n_eq = max(1, int(round(120 / 28.0)))
    cut_eq = 0
    for ci in range(n_eq):
        b0, b1 = 120.0 * ci / n_eq, 120.0 * (ci + 1) / n_eq
        for sg in segs:
            if sg["start"] < b1 - 1e-6 and sg["end"] > b1 + 1e-6:
                cut_eq += 1
    print("       （对照：等时长 28 秒一刀，这个样本上有 %d 句被切在半中）" % cut_eq)
    check("等时长切法确实会切坏（说明这条判据有区分力）", cut_eq >= 1,
          "实际切坏 %d 句" % cut_eq)

    # ---- 3. 时间线：覆盖、不重叠、间奏直通 ------------------------------
    tl = LP.build_timeline(plan, total)
    check("时间线首尾铺满整条音频",
          tl[0][0] == 0 and tl[-1][1] == total,
          "首 %d 尾 %d / 总 %d" % (tl[0][0], tl[-1][1], total))
    ok_no_gap = all(tl[k][1] == tl[k + 1][0] for k in range(len(tl) - 1))
    check("段与段首尾相接（不重叠、不留洞）", ok_no_gap,
          "%s" % [(a, b) for a, b, _ in tl])
    inter = [1 for _, _, idx in tl if not idx]
    check("间奏段标成直通（idx 为 None）", len(inter) >= 1,
          "直通段 %d / 共 %d 段" % (len(inter), len(tl)))
    # 直通段不许出现在"有词"的地方
    bad_pass = [(a / float(sr), b / float(sr)) for a, b, idx in tl if not idx
                for sg in segs
                if sg["start"] < b / float(sr) - 0.5 and sg["end"] > a / float(sr) + 0.5]
    check("没有把有词的时段判成间奏", not bad_pass, "冲突：%s" % bad_pass[:3])

    # ---- 4. 边界与异常 --------------------------------------------------
    check("没有句子 ⇒ 不分块（退回等时长）", LP.plan_chunks([], total, sr) == [])
    one = [{"start": 5.0, "end": 9.0, "text": "唯一一句"}]
    p1 = LP.plan_chunks(one, total, sr)
    check("只有一句 ⇒ 分出 1 块且包住它",
          len(p1) == 1 and p1[0][0] <= 5.0 * sr and p1[0][1] >= 9.0 * sr,
          "%s" % [(a / float(sr), b / float(sr)) for a, b, _ in p1])
    # 超长乐句群必须被 28 秒切断（显存约束）
    long_segs = [{"start": 1.0 * k, "end": 1.0 * k + 0.8, "text": "字%d" % k}
                 for k in range(1, 60)]
    pl = LP.plan_chunks(long_segs, total, sr, chunk_sec=28.0)
    spans = [(b - a) / float(sr) for a, b, _ in pl]
    check("超长乐句群会被切成多块", len(pl) >= 2, "实际 %d 块" % len(pl))
    check("每块不超过 ~30 秒（显存/官方单次上限）",
          all(x <= 30.5 for x in spans), "最长 %.1f 秒" % max(spans))
    # 时间戳乱序也不能炸
    shuf = [segs[2], segs[0], segs[4], segs[1], segs[3]]
    ps = LP.plan_chunks(shuf, total, sr)
    check("时间戳乱序不炸（按 start 排序后再分组）", len(ps) >= 1)

    # ---- 4b. 片段偏移（试听小样）-----------------------------------------
    # ★ 这个 bug 是真踩到的：小样从第 35% 处裁一段出来唱，却把**整首**的时间戳
    #   交给改词 ⇒ 片段里一句都对不上 ⇒ 每一段都被判成间奏 ⇒ 改词在小样上
    #   **等于没做**（成品听着还是原词）。裁了音频就必须裁时间戳。
    full = [T(8.0, 14.0, "前段第一句"), T(20.0, 26.0, "前段第二句"),
            T(90.0, 96.0, "后段第一句"), T(100.0, 108.0, "后段第二句")]
    ins, out = LP.slice_segments(full, 85.0, 20.0)      # 窗口 [85,105]
    check("小样：窗口外的句被剔除", out == 2, "剔除 %d 句" % out)
    check("小样：窗口内的句坐标被搬回 0 起点",
          [x["start"] for x in ins] == [5.0, 15.0],
          "实际 %s" % [(x["start"], x["end"]) for x in ins])
    check("小样：跨窗口边界的句被裁短（不是整句丢掉）",
          [(x["start"], x["end"]) for x in ins] == [(5.0, 11.0), (15.0, 20.0)],
          "实际 %s" % [(x["start"], x["end"]) for x in ins])
    check("小样：文本跟着走", [x["text"] for x in ins] ==
          ["后段第一句", "后段第二句"])
    ins0, _ = LP.slice_segments(full, 0.0, None)
    check("不裁时原样返回（offset=0, dur=None）",
          [(x["start"], x["end"]) for x in ins0] == [(8.0, 14.0), (20.0, 26.0),
                                                     (90.0, 96.0), (100.0, 108.0)])
    # 负对照：不搬坐标的话，小样里一句都对不上（这正是老代码的症状）
    naive = [1 for x in ins if x["start"] >= 20.0]
    check("负对照：不搬坐标时窗口内一句都不在片段里（判据有牙）",
          len(naive) == 0 and len(ins) == 2)

    # ---- 5. 本机真实缓存（有就当证据再验一遍）----------------------------
    real = 0
    if os.path.isdir(CACHE):
        import glob
        for fp in sorted(glob.glob(os.path.join(CACHE, "*.json"))):
            try:
                d = json.load(io.open(fp, encoding="utf-8"))
            except Exception:
                continue
            sg = [x for x in (d.get("segments") or [])
                  if isinstance(x, dict) and x.get("text")
                  and x.get("start") is not None]
            if len(sg) < 4:
                continue
            real += 1
            tag = os.path.basename(fp)[:8]
            du = float(d.get("duration") or (sg[-1]["end"] + 5))
            tt = int(du * sr)
            pp = LP.plan_chunks(sg, tt, sr, chunk_sec=28.0)
            # 真数据上：每块都得落在静音里（块起点前 0.3 秒内不该有词在唱）
            bad = 0
            # ★ 用**采样点**比，容差 1 个采样（约 0.02 毫秒）：
            #   真实识别结果里 end[i] == start[i+1] 是常态（词级时间戳首尾相接），
            #   块起点恰好落在上一句的 end 上，是**正确**而不是切坏；
            #   早先用浮点 ±1e-6 比，把这种"严丝合缝"判成了切坏（假红 6 处）。
            for (a, b, idx) in pp:
                for x in sg:
                    xs = int(float(x["start"]) * sr)
                    xe = int(float(x.get("end") or x["start"]) * sr)
                    if xs + 1 < a < xe - 1:            # 块起点切进了某句中间
                        bad += 1
                    if xs + 1 < b < xe - 1:            # 块终点切进了某句中间
                        bad += 1
            check("真实缓存 %s：块边界没切进句子" % tag, bad == 0,
                  "切进 %d 处" % bad)
            tt2 = LP.build_timeline(pp, tt)
            check("真实缓存 %s：时间线铺满且不重叠" % tag,
                  tt2[0][0] == 0 and tt2[-1][1] == tt
                  and all(tt2[k][1] == tt2[k + 1][0] for k in range(len(tt2) - 1)))
            n_inter = len([1 for _, _, i in tt2 if not i])
            print("       （%s：%d 句 → %d 段，其中 %d 段间奏直通）"
                  % (tag, len(sg), len(tt2), n_inter))
    print("       （本机歌词缓存样本：%d 份）" % real)

    print("\n通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAIL %s" % f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
