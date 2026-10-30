# -*- coding: utf-8 -*-
r"""第三十四关：段级纯净度 —— 不属于这个声音的段不许混进角色。

老板的原话：「不会把其他声音切进去当素材……会自检，把不属于这个声音的音频剔除」。

================================================================================
这一关在防什么
================================================================================

**错了不会让任何既有判据变红。** 簇级认人（`match_clusters`）用的是"这一堆每一段
到角色中心距离的**中位数**" —— 一堆里混进别人的**整段**时，中位数照样过阈值，
于是那些段被一并并进角色。任务照样 done、角色照样建档、卡片上段数还变多了，
只有训练集被静默污染，等训出来声音不对才发现，十几分钟机时已经烧掉了。

所以加了 `studio.refine_segments()`（有角色中心时逐段判）和
`studio.purge_outliers()`（没角色中心时拿这一堆自己的中心当尺子）。这一关钉住它们。

================================================================================
素材：完全自足，不新增任何数据文件
================================================================================

  正例（同人）`data/roles/ra500fb92/slices` —— 大帝，只读，绝不改动
  负例（异人）`data/exp_spkB/`             —— 同一段视频里分出来的第二个人

为什么是这两个（实测，不是看名字猜的）：
    大帝 到自己中心    p95 = 0.128
    exp_spkB 到大帝中心 p05 = 0.374，且 **56/56 段全部超过段级阈值 0.25**
两条分布中间空得很开 ⇒ 判据不会红在"素材分不开"这种没意义的地方。
★ A 段第一件事就是**再量一遍这个分离度**：量不出来就当场红，后面的全绿也不作数。

（曾试过用 ffmpeg 变调造异人，`_probe_pitchshift.py` 实测 +5 半音 p05 也只有 0.325、
分布散到 0.26~0.76 ⇒ 同一批段里只有一部分会被判异人，造不出确定的负例，已放弃。）

================================================================================
判据要能红
================================================================================

每条承重判据都配一条负对照：**把尺子调坏，这条必须当场变红**。
红不了的判据是死尺子，等于没写（这条规矩来自前面几关的血泪，本关照做）。

用法：<python> -s verify_seg_purity.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import studio  # noqa: E402

OK, BAD = [], []
FAKE_A = "fake_dadi"          # 临时角色：只存在于内存，不写 roles.json
A_DIR = os.path.join(HERE, "data", "roles", "ra500fb92", "slices")
B_DIR = os.path.join(HERE, "data", "exp_spkB")


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name
          + (("  | " + str(detail)) if detail else ""))


def wav_seconds(p):
    import wave
    try:
        with wave.open(p, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return 0.0


def pick(dirpath, n, min_dur=3.0):
    """挑出 n 段够长的 wav 的完整路径。"""
    fs = sorted(f for f in os.listdir(dirpath) if f.lower().endswith(".wav"))
    out = []
    for f in fs:
        if len(out) >= n:
            break
        p = os.path.join(dirpath, f)
        if wav_seconds(p) >= min_dur:
            out.append(p)
    return out


def flat_of(paths, tag):
    feats = studio._feat_subprocess(paths, tag)
    return [dict(x, file=os.path.basename(x["file"])) for x in feats]


def pct(v, p):
    if not v:
        return float("nan")
    s = sorted(v)
    return s[min(len(s) - 1, int(p * len(s)))]


def main():
    print("=" * 70)
    print("A 段 · 前提自检（素材能不能把「同人 / 异人」分开）")
    print("=" * 70)

    if not (os.path.isdir(A_DIR) and os.path.isdir(B_DIR)):
        check("素材目录都在", False, "缺 %s 或 %s" % (A_DIR, B_DIR))
        return done()
    # ★ 取 120 段而不是 30 段：C 段要验"没有绝对下限就会被砍"，样本太小的话
    #   这批素材干净得连尾巴都没有（实测 30 段时 med+K·MAD=0.133，而全部分段
    #   都落在里面），负对照就永远红不了 —— 那是样本选得不对，不是功能没问题。
    pa = pick(A_DIR, 120)
    pb = pick(B_DIR, 30)
    check("大帝切片够（>=30）", len(pa) >= 30, "%d 段" % len(pa))
    check("第二个人切片够（>=20）", len(pb) >= 20, "%d 段" % len(pb))
    if len(pa) < 30 or len(pb) < 20:
        return done()

    FA = flat_of(pa, "sp_a")
    FB = flat_of(pb, "sp_b")
    check("两批素材都提得到声纹", bool(FA) and bool(FB),
          "A=%d B=%d" % (len(FA), len(FB)))
    if not FA or not FB:
        return done()

    SV = studio.VOICE_KIND
    va = [r[SV] for r in FA if r.get(SV)]
    vb = [r[SV] for r in FB if r.get(SV)]
    if not va or not vb:
        check("声纹向量非空", False, "A=%d B=%d" % (len(va), len(vb)))
        return done()
    cen = studio._emb_center(va)
    da = [x for x in (studio._emb_dist(v, cen) for v in va) if x is not None]
    db = [x for x in (studio._emb_dist(v, cen) for v in vb) if x is not None]
    thr = studio.SEG_MATCH_DIST
    print("     大帝 到自己中心   p05=%.3f p50=%.3f p95=%.3f" %
          (pct(da, .05), pct(da, .5), pct(da, .95)))
    print("     异人 到大帝中心   p05=%.3f p50=%.3f p95=%.3f" %
          (pct(db, .05), pct(db, .5), pct(db, .95)))
    check("★ 分离度够（同人 p95 < 阈值 < 异人 p05）",
          pct(da, .95) < thr < pct(db, .05),
          "同人p95=%.3f < %.2f < 异人p05=%.3f" % (pct(da, .95), thr, pct(db, .05)))

    # ---- 构造"库里只有大帝一个角色"的候选集（不碰 roles.json） ----
    real_load = studio.load_roles
    fake_db = {"roles": [{
        "id": FAKE_A, "name": "大帝（临时）",
        "voice": {"center": cen, "dim": len(va[0]),
                  "kind": SV, "n": len(va)},
    }]}
    studio.load_roles = lambda: fake_db

    try:
        # ============================================================
        print()
        print("=" * 70)
        print("B 段 · refine_segments（一堆认成某人之后，逐段复核）")
        print("=" * 70)
        # 混合堆：30 段大帝 + 15 段异人，假装它被认成了大帝
        mix = FA[:30] + FB[:15]
        same_names = {r["file"] for r in FA[:30]}
        other_names = {r["file"] for r in FB[:15]}

        owner, rejected, notes = studio.refine_segments(None, {0: mix}, mix, {0: FAKE_A})
        for t in notes:
            print("     " + t)

        got_same = sum(1 for n in same_names if owner.get(n) == FAKE_A)
        check("★ 同人的段全部归位（没被误剔）", got_same == len(same_names),
              "%d/%d" % (got_same, len(same_names)))
        got_other = sum(1 for n in other_names if n in rejected)
        check("★ 异人的段全部被剔除（没混进角色）", got_other == len(other_names),
              "%d/%d 进 rejected" % (got_other, len(other_names)))
        check("异人的段没有一条被当成大帝",
              not any(owner.get(n) == FAKE_A for n in other_names))

        # 短段不许表态：直接改内存里的 dur，**不落任何临时文件**
        short = dict(mix[0], file="__short_probe__.wav", dur=1.0)
        o2, r2, _n2 = studio.refine_segments(
            None, {0: [short] + mix[:5]}, [short] + mix[:5], {0: FAKE_A})
        check("★ 太短的段段级不表态（既不归位也不剔除）",
              "__short_probe__.wav" not in o2 and "__short_probe__.wav" not in r2)

        # ---- 负对照 1：阈值放到 9999 ⇒ 异人也要被收（判据必须变红）----
        old = studio.SEG_MATCH_DIST
        studio.SEG_MATCH_DIST = 9999.0
        o3, r3, _n3 = studio.refine_segments(None, {0: mix}, mix, {0: FAKE_A})
        leaked = sum(1 for n in other_names if o3.get(n) == FAKE_A)
        studio.SEG_MATCH_DIST = old
        check("坏尺子（阈值 9999）下异人**确实**被收进来了 ⇒ 判据会红",
              leaked == len(other_names), "%d/%d 被误收" % (leaked, len(other_names)))

        # ---- 负对照 2：阈值收到 0 ⇒ 同人也要被剔（判据必须变红）----
        studio.SEG_MATCH_DIST = 0.0
        o4, r4, _n4 = studio.refine_segments(None, {0: mix}, mix, {0: FAKE_A})
        killed = sum(1 for n in same_names if n in r4)
        studio.SEG_MATCH_DIST = old
        check("坏尺子（阈值 0）下同人**确实**被剔掉了 ⇒ 判据会红",
              killed == len(same_names), "%d/%d 被误剔" % (killed, len(same_names)))
        # 还原后必须回到原样（没被负对照污染）
        o5, r5, _n5 = studio.refine_segments(None, {0: mix}, mix, {0: FAKE_A})
        check("还原阈值后结果回到原样（负对照没污染状态）",
              sum(1 for n in same_names if o5.get(n) == FAKE_A) == len(same_names)
              and sum(1 for n in other_names if n in r5) == len(other_names))

        # ============================================================
        print()
        print("=" * 70)
        print("C 段 · purge_outliers（没认出来的堆，拿自己的中心当尺子）")
        print("=" * 70)
        # 纯净堆：大帝全部 ⇒ 本来就不该剔
        keep, out, note = studio.purge_outliers(None, FA, FA)
        print("     " + note)
        check("★ 纯净堆不误杀（outliers 为空）", len(out) == 0,
              "剔了 %d 段" % len(out))
        check("纯净堆的段一条没少", len(keep) == len(FA),
              "%d/%d" % (len(keep), len(FA)))

        # ---- floor 到底有没有在兜底 ----
        # 判据落在**逻辑**上（门槛 = max(med+K·MAD, floor)），不靠"数据恰好长了个尾巴"。
        # 顺带把"没有 floor 会砍掉几段"打出来当佐证 —— 那是 `_probe_outlier.py`
        # 里量到 4.2% 的来源。
        import statistics
        cen_p = studio._emb_center([r[SV] for r in FA])
        ds_p = [x for x in (studio._emb_dist(r[SV], cen_p) for r in FA) if x is not None]
        m_p = statistics.median(ds_p)
        mad_p = statistics.median([abs(x - m_p) for x in ds_p])
        raw = m_p + studio.OUTLIER_K * mad_p
        tail = sum(1 for x in ds_p if x > raw)
        print("     没有 floor 时门槛是 %.3f ⇒ %d/%d 段会被砍；floor=%.2f 把它顶住 ⇒ 砍 0 段"
              % (raw, tail, len(ds_p), studio.OUTLIER_FLOOR))
        check("★ 绝对下限确实在兜底（med+K·MAD < floor，门槛实际取 floor）",
              raw < studio.OUTLIER_FLOOR,
              "raw=%.3f < floor=%.2f" % (raw, studio.OUTLIER_FLOOR))
        check("没有 floor 就会误杀 ⇒ floor 不是摆设", tail > 0,
              "%d 段会落在 raw 门槛外" % tail)

        # 脏堆：大帝 30 + 异人 15 ⇒ 要能抓出离群
        dirty = FA[:30] + FB[:15]
        keep2, out2, note2 = studio.purge_outliers(None, dirty, dirty)
        print("     " + note2)
        caught = sum(1 for f in out2 if f["file"] in other_names)
        wrong = sum(1 for f in out2 if f["file"] in same_names)
        check("★ 脏堆抓到了离群段", caught > 0, "抓到异人 %d/%d" % (caught, 15))
        check("★ 脏堆没有误杀主流段", wrong == 0, "误杀大帝 %d 段" % wrong)

        # ---- 负对照：把 floor 拿掉 ⇒ 纯净堆**真的**会被砍（证明上面那条不是自说自话）----
        old_floor = studio.OUTLIER_FLOOR
        studio.OUTLIER_FLOOR = 0.0
        _k3, o3p, n3p = studio.purge_outliers(None, FA, FA)
        studio.OUTLIER_FLOOR = old_floor
        print("     坏尺子（floor=0）: " + n3p)
        check("坏尺子（floor=0）下纯净堆**确实**被砍 ⇒ 判据会红",
              len(o3p) == tail, "被砍 %d 段（与复算的 %d 一致）" % (len(o3p), tail))
        check("还原 floor 后纯净堆又回到不误杀",
              len(studio.purge_outliers(None, FA, FA)[1]) == 0)

        # ---- 段太少时不做 ----
        k4, o4p, n4p = studio.purge_outliers(None, FA[:5], FA[:5])
        check("段数不足时跳过簇内剔除（MAD 不稳，宁可不动）",
              len(o4p) == 0 and len(k4) == 5, n4p)
    finally:
        studio.load_roles = real_load

    # ============================================================
    print()
    print("=" * 70)
    print("D 段 · 接线自检（剔出去的段不许凭空消失）")
    print("=" * 70)
    src = open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    check("剔除的段进了「待归类」堆（不是直接丢）",
          "groups[-1].extend(dropped_out)" in src)
    check("待归类堆不存在时会先建出来",
          "groups.setdefault(-1, [])" in src)
    check("空堆不会再建一个空角色出来",
          "if not segs:" in src and src.count("if not segs:") >= 1)
    return done()


def done():
    print()
    print("=" * 70)
    print("通过 %d，失败 %d" % (len(OK), len(BAD)))
    if BAD:
        for b in BAD:
            print("   FAIL: " + b)
    print("结论: " + ("判据有效" if not BAD and OK else "有失败项"))
    return 0 if not BAD and OK else 1


if __name__ == "__main__":
    raise SystemExit(main())
