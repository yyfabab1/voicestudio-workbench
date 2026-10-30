# -*- coding: utf-8 -*-
"""多角色唱一首歌：**分配表**（谁唱哪几句）+ **拼接**（把 N 条角色轨合成一条）。

分成两半，是有意的：
  · 上半 `plan_*` —— **纯标准库**，只算时间轴，不碰音频。
    门禁（跑在托管 python 上，没有 numpy）可以直接 import 它做单测。
  · 下半 CLI 拼接 —— 要 numpy / soundfile，函数内 import。

★ 分配表是**完整划分**：[0, 时长] 被切成首尾相接的若干段，段间不留缝。
  为什么：留缝就得拿点什么去填 —— 填原唱会混进别人的声音，填静音会把上一句的
  尾音切断。把句与句之间的空隙归给**前一句**的角色，两种毛病都没有。
  附带好处是所有权重相加恒等于 1，拼接时音量既不塌陷也不叠加。

★ 为什么每个角色跑**整首**而不是只跑分给它的那几句：
  RVC 的 hubert 特征要上下文，两秒的碎片音色会飘；而且每条轨都等于原曲长度，
  拼接时不用对轨。代价是 N 倍推理时间 —— 这个代价在界面上是明写的。
"""

import argparse
import json
import os
import sys

# 接缝交叉淡化窗口。20ms 与 nonvocal_guard 保持一致。
# 对唱的切换点落在句与句之间（那里本来就是换气/静音），20ms 足够听不出来。
XFADE_MS = 20.0

# 分配方式。manual = 前端按句指定（每句一个角色下标）。
MODES = ("rotate", "phrase", "section", "manual")

# ★ 合唱段：分配表里 role 取这个值 = 这一段时间**所有人一起唱**。
#   对唱是"一段时间只归一个人"，合唱是"同一段时间每个人都开口"。
#   用一个特殊下标表示，比给每段再挂一个"这是合唱"的字段省事：
#   时间轴划分、接缝统计、画面字幕全都不用改形状（见 ROLE_ALL 用到处的说明）。
ROLE_ALL = -1
# 合唱段里非主唱那几层的音量 —— 与 overlay（整首叠唱）的 other_gain 同一口径。
CHORUS_GAIN = 0.55

# --------------------------------------------------------------------------
# ★ 和声层「人味化」参数（2026-10-24 第六轮）
#
# 起因：老板给了 B 站参考（BV1PyYReMEFL），要求「多角色一起唱」达到那种质量，
#   **不是简单的拼接**。之前的做法 = 主唱原音量 + 其余各层乘 0.55 直接相加：
#   同一条旋律、同一时刻起音、同一频段、同一空间 ⇒ 心理学上会融合成
#   「一个人唱得很厚」，而不是「几个人一起唱」。这就是「简单拼接」的听感来源。
#
# 多轨人声混音的通行解法（Github 上与 B 站多角色翻唱工程口径一致）是四条：
#   ① 时间错位：整层挪 10~20ms，每层再错开一点（真人不可能齐到样本级）
#   ② 频率让位：high-pass 掉 ~140Hz 以下（低频地基留给主唱），
#               再把 2~4kHz 压一点（齿音/临场感让给主唱）⇒ 两层不在同一频段里抢
#   ③ 空间后退：和声层多一点混响（站在主唱后面）
#   ④ makeup gain：EQ 抽掉的能量补回来，other_gain 才还是纯粹的「垫多厚」
# --------------------------------------------------------------------------
CHO_DELAY_MS = 14.0      # 第一层往后挪多少毫秒（10~20ms 是常用区间）
CHO_DELAY_STEP_MS = 5.0  # 每多一层再挪多少（三层以上不要齐平）
CHO_HP_HZ = 140.0        # 和声层的高通截止（100~200Hz 区间取中）
CHO_PRES_DB = -2.5       # 2~4kHz 让位深度（负 dB）
CHO_REVERB = 0.16        # 湿声占比（和声层比主唱更"远"）
CHO_REVERB_SEC = 1.1     # 混响尾巴长度（秒）

# 乐句 / 段落的分组阈值：句间空档超过这么多秒就算断句。
GAP_PHRASE = 2.0      # 乐句：跟 lyric_plan.plan_chunks 同一个口径
GAP_SECTION = 6.0     # 段落：更大的空档（间奏、换段）才换人

# 响度增益的上下限。上限 4.0 = +12dB：真实数据里一个降了八度的角色输出能比
# 另一个低 8dB 左右，卡在 2.0（+6dB）就补不平 —— 实测段间比值 1.25，
# 听感还是一句大一句小。再大就不该补了：那说明这条轨本来就没声音，
# 放大只会把底噪抬起来。
GAIN_MIN = 0.4
GAIN_MAX = 4.0


# --------------------------------------------------------------------------
# 上半：分配表（纯标准库）
# --------------------------------------------------------------------------

def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def group_lines(segs, gap_sec=GAP_PHRASE):
    """按句间空档把句子分组。空档 > gap_sec 就断开。返回 [[句下标...], ...]。

    返回的组**按下标升序**，但组内保持原始顺序 —— 调用方要的是"哪几句是一组"。
    """
    if not segs:
        return []
    order = sorted(range(len(segs)), key=lambda i: _f(segs[i].get("start")))
    groups, cur = [], [order[0]]
    for i in order[1:]:
        prev_end = _f(segs[cur[-1]].get("end"), _f(segs[cur[-1]].get("start")))
        if _f(segs[i].get("start")) - prev_end > gap_sec:
            groups.append(cur)
            cur = [i]
        else:
            cur.append(i)
    groups.append(cur)
    return groups


# --------------------------------------------------------------------------
# ★ 原曲分句（2026-10-25 第九轮）
#
# 老板：「多角色指定句子是按照原曲的分句来」
#
# 起因：界面上那份"逐句指定"的表直接就是 ASR 认出来的句子 —— 它是按**呼吸和
#   标点**切的，一首 138 秒的歌能切出 58~86 句。老板要的是"原曲的分句"
#   （他数出来是 11~14 句），也就是**乐句**：唱完一句、停下来换气，才是一句。
#   两者粒度差 5 倍 ⇒ 就会出现"你指定了 14 句，这首歌现在切出 86 句"。
#
# 怎么做才叫"按原曲"：断句点必须来自**音频本身**，不能靠文本猜。
#   ASR 句的 end 常常提前（拖尾被算进下一句），光看句间时间间隔会切错位置。
#   所以直接量分离出来的人声轨：两个 ASR 句之间，人声真的静了 ≥ PHRASE_GAP_SEC
#   才断句。这才是"原曲在哪儿换气"。
#
# ⚠ 必须用**分离后的人声**，不能用原曲：人声停顿时伴奏还在响 ⇒ 原曲的 RMS 不低
#   ⇒ 一个 gap 都量不出来（实测过）。
# ⚠ 拿不到人声轨时退化成"按句间时间间隔"，阈值用 FALLBACK_GAP_SEC（更保守，
#   宁可少合并不许多合并 —— 合错了会把两个人的句子并成一句，那更难发现）。
# --------------------------------------------------------------------------
PHRASE_GAP_SEC = 0.45       # 人声静这么久 = 换气/断句
# 一个乐句最长多少秒。★ 这个数不是随手定的：有的歌人声**几乎不停**
#   （实测那首 138 秒的歌静音占比 44%，但全在间奏，唱段内部一句接一句），
#   纯按换气切只能切出 6 句、每句 23~28 秒 —— 那不叫"分句"。
#   老板数出来的原曲分句是 11 句（138 秒 ⇒ 平均 12.6 秒/句），
#   取 14 秒 ⇒ 长句一定会在**换气最大处**被拆开，短句不动。
PHRASE_MAX_SEC = 14.0
FALLBACK_GAP_SEC = 0.60     # 没有人声轨时的退化阈值
GAP_WIN = 0.60              # 断句判定时，看切点两侧各多少秒（见 _gap_between）
_FRAME_SEC = 0.020          # 静音检测帧长
_HOP_SEC = 0.010            # 帧移


def silence_mask(audio_path, thr_mult=4.0):
    """从音频量出"哪里没声音"。返回 (hop_sec, mask)，mask[i]=True 表示第 i 帧静音。

    阈值自适应（不写死 dB）：取能量分位数当底噪，底噪的 `thr_mult` 倍算响。
    分离轨的底噪水平千差万别（有的模型漏一点伴奏、有的干干净净），
    写死一个 dB 会在某些歌上把整首都判成静音、某些歌上一句都切不出来。

    ★ np / sf 必须在这里自己 import：本模块是延迟加载的，顶部没有 numpy。
    """
    import numpy as np
    import soundfile as sf

    y, sr = sf.read(audio_path, dtype="float32", always_2d=False)
    if getattr(y, "ndim", 1) > 1:
        y = y.mean(axis=1)
    y = np.asarray(y, dtype=np.float64)
    if len(y) < sr * 0.5:
        return _HOP_SEC, np.zeros(0, dtype=bool)
    w = max(1, int(_FRAME_SEC * sr))
    hop = max(1, int(_HOP_SEC * sr))
    n = max(0, (len(y) - w) // hop + 1)
    if n <= 0:
        return (float(hop) / float(sr)), np.zeros(0, dtype=bool)
    # 逐帧 RMS：用 stride 展开，别在 Python 里循环（一首 4 分钟的歌 24000 帧）
    idx = np.arange(n) * hop
    fr = np.sqrt(np.mean(
        np.square(np.lib.stride_tricks.sliding_window_view(y, w)[idx]), axis=1))
    pk = float(fr.max())
    if pk <= 1e-9:
        return (float(hop) / float(sr)), np.ones(n, dtype=bool)
    # ★★ 阈值**不能**只靠一个分位数（第一版用 p25 当底噪，被实测打脸）：
    #    静音只占 15% 时 p25 落在**响段** ⇒ thr 成了峰值的 4 倍 ⇒ 整首都判成
    #    静音（实测静音占比 100%，一句都切不出来，反而 6 句全不合并）。
    #    改成"绝对 + 相对"双轨，再卡一个上限：
    #      · pk*0.03  （-30dB）：换气处通常低于它
    #      · p05*6    ：分离不干净时底噪不为 0，靠它抬起来（p05 才更像底噪）
    #      · 上限 pk*0.25：不许把阈值抬到连中位数都判成静音（那等于整段静音）
    lo = float(np.percentile(fr, 5))
    thr = max(pk * 0.03, lo * thr_mult * 1.5)
    thr = min(thr, pk * 0.25)
    return (float(hop) / float(sr)), (fr < thr)


def silent_sec_between(hop, mask, t0, t1):
    """[t0, t1] 这段时间里累计静音了几秒。"""
    import numpy as np

    if t1 <= t0 or not len(mask):
        return 0.0
    a = max(0, int(t0 / hop))
    b = min(len(mask), int(t1 / hop) + 1)
    if b <= a:
        return 0.0
    return float(np.count_nonzero(mask[a:b])) * hop


def _split_long(g, segs, gap_fn, max_sec, depth=0):
    """把一组（句下标）按"组内换气最大处"二分，直到不超 `max_sec`。

    ★ 为什么不在合并时就限制长度：那样下刀位置是"刚好超过 14 秒的那句"，
      可能正落在一句歌词的中间。这里改成先在换气点分完、再把过长的一组
      从**它内部静音最长的那个缝**切开 —— 切点仍然在换气上。
    """
    if len(g) <= 1 or depth > 12:
        return [g]
    s0 = _f(segs[g[0]].get("start"))
    s1 = max(_f(segs[i].get("end"), _f(segs[i].get("start"))) for i in g)
    if s1 - s0 <= max_sec:
        return [g]
    best_k, best_gap = -1, -1.0
    for k in range(1, len(g)):
        pe = _f(segs[g[k - 1]].get("end"), _f(segs[g[k - 1]].get("start")))
        cs = _f(segs[g[k]].get("start"))
        gp = gap_fn(pe, cs)
        if gp > best_gap:
            best_gap, best_k = gp, k
    # 组内一点静音都没有（人声连成一片）⇒ 只能均分，这是诚实的退让
    if best_k < 0 or best_gap <= 0.0:
        best_k = len(g) // 2
    return (_split_long(g[:best_k], segs, gap_fn, max_sec, depth + 1)
            + _split_long(g[best_k:], segs, gap_fn, max_sec, depth + 1))


def phrase_segs(segs, audio_path=None, min_gap=PHRASE_GAP_SEC,
                max_sec=PHRASE_MAX_SEC, fallback_gap=FALLBACK_GAP_SEC):
    """把 ASR 的碎句按**原曲的换气**合并成乐句。

    返回新的句表（start/end/text/zh），粒度 = 原曲的乐句。
    `segs` 为空或只有一句 ⇒ 原样返回（没什么可合的）。

    ★ 为什么要合并后再分角色，而不是"分角色时再按组"：
      界面那张表（lines）和后端分配（line_roles）必须是**同一份句**，
      否则用户点的是 14 句、内部算的是 86 句，"指定了却对不上"就又回来了。
    """
    if not segs:
        return []
    order = sorted(range(len(segs)), key=lambda i: _f(segs[i].get("start")))
    if len(order) <= 1:
        return [dict(segs[i]) for i in order]

    hop, mask = (0.0, None)
    if audio_path:
        try:
            hop, mask = silence_mask(audio_path)
        except Exception:
            hop, mask = (0.0, None)

    def _gap_between(prev_end, cur_start):
        """这两句之间"停了多久"：有人声轨就量静音，没有就用间隔本身。

        ★★ 不能只量 (prev_end, cur_start) 这一段 —— ASR 的句子常常**首尾相接**
            （上一句 end 正好等于下一句 start），那段长度为 0，永远量不到静音
            ⇒ 整首并成一大块。实测：6 句首尾相接的合成素材，老写法返回 1 句。
            改成看**切点两侧各 GAP_WIN 秒的窗口**：换气通常发生在句尾拖尾之后、
            下一句起音之前，落在窗口里就够。
        """
        if mask is not None and len(mask):
            return silent_sec_between(hop, mask,
                                      cur_start - GAP_WIN, cur_start + GAP_WIN)
        return max(0.0, cur_start - prev_end)

    gap_need = min_gap if mask is not None and len(mask) else fallback_gap

    groups, cur = [], [order[0]]
    for i in order[1:]:
        prev_end = _f(segs[cur[-1]].get("end"), _f(segs[cur[-1]].get("start")))
        st = _f(segs[i].get("start"))
        too_long = (st - _f(segs[cur[0]].get("start"))) > max_sec
        if too_long or _gap_between(prev_end, st) >= gap_need:
            groups.append(cur)
            cur = [i]
        else:
            cur.append(i)
    groups.append(cur)

    # ★ 长句再拆：只在"组内换气最大处"下刀，不硬切。
    #   不拆的话，人声连续的歌会整首并成 6 大块（每块 26 秒），
    #   "逐句指定"就名存实亡 —— 那跟"整首都给 TA"没区别。
    split = []
    for g in groups:
        split.extend(_split_long(g, segs, _gap_between, max_sec))

    out = []
    for g in split:
        parts = [(s.get("text") or "").strip() for s in (segs[i] for i in g)]
        zhs = [str(s.get("zh") or "").strip() for s in (segs[i] for i in g)]
        out.append({
            "start": _f(segs[g[0]].get("start")),
            "end": max(_f(segs[i].get("end"), _f(segs[i].get("start")))
                       for i in g),
            "text": " ".join([p for p in parts if p]).strip(),
            "zh": " ".join([z for z in zhs if z]).strip(),
            # ★ 记下来：这一句原是 ASR 的哪几句。界面要能说清"这句是哪几句合的"，
            #   不然用户数着 14 句、看到 12 句会以为丢了。
            "n_sub": len(g),
        })
    return out


def split_groups(groups, want):
    """组数不够就拆：反复把**句子最多**的那一组对半拆，直到够 `want` 组。

    ★ 为什么必须有这一步（实测产品缺陷）：
      phrase / section 是"一组一个人"轮流分。一组连续的句子在短样本里会被
      `group_lines` 合成**唯一一组**（16 秒片段 4 句、句间空档都 < 2 秒）
      ⇒ 只有第 0 组 ⇒ **只有第一个人开口，第二个人一句都分不到**。
      用户明明选了对唱，听到的却是一个人的独唱 —— 这类"选了没生效"是最难
      自查的（声音是好的，就是没有第二个人）。

      拆法选"句子最多的那组对半"，不是"按时间等分"：切点仍然落在句与句之间，
      不会把一句话劈成两半（那会听起来像卡带）。

    拆不动（每组都只剩 1 句，即句数 < 人数）就原样返回 —— 物理上没法让三个人
    唱两句。这种情况由调用方在日志里点名说明，不许静默。
    """
    out = [list(g) for g in (groups or [])]
    want = max(1, int(want or 1))
    while len(out) < want:
        i = max(range(len(out)), key=lambda k: (len(out[k]), -k))
        g = out[i]
        if len(g) < 2:
            break
        mid = len(g) // 2
        out[i:i + 1] = [g[:mid], g[mid:]]
    return out


def groups_for_roles(segs, n_roles, gap_sec=GAP_PHRASE):
    """分组 + 补足：返回 (groups, 拆分前的组数)。第二项只用于日志说人话。"""
    gs = group_lines(segs, gap_sec)
    return split_groups(gs, n_roles), len(gs)


def _manual_by_time(segs, manual):
    """把「按行点的 manual」按**时间**铺回现在的句上。返回每句该取 manual 的第几份。

    ★ 为什么必须按时间而不是按序号：manual 是用户在界面上点出来的，
      点的是"**那一段时间**归谁"（界面上要么显示歌词、要么显示"第 N 段"）。
      认一次词之后行数会变（14 → 86），但**时间没变** —— 序号一变就全错，
      时间才是那个不变的量。

    铺法：manual 的 N 份把"句子覆盖的时间跨度"均分；每句按**中点**落进哪一份
    就取哪一份（用中点而不是起点，是因为句有长有短，起点会把长句整体前移）。
    """
    n = len(segs or [])
    m = len(manual or [])
    if n == 0 or m == 0:
        return [0] * max(n, 0)
    st, en = None, None
    for s in segs:
        a, b = _f(s.get("start")), _f(s.get("end"))
        if b < a:
            a, b = b, a
        st = a if st is None else min(st, a)
        en = b if en is None else max(en, b)
    span = max((en - st) if (st is not None and en is not None) else 0.0, 1e-9)
    out = []
    for s in segs:
        a, b = _f(s.get("start")), _f(s.get("end"))
        mid = (a + b) / 2.0
        j = int((mid - st) / span * m)
        out.append(max(0, min(m - 1, j)))
    return out


def assign_lines(segs, n_roles, mode="rotate", manual=None, gap_sec=GAP_PHRASE,
                 info=None):
    """每一句归哪个角色。返回 list[int]（长度 = 句数），值是角色序号 0..n_roles-1。

    rotate  逐句轮流 —— 最像"对唱"，但一句话内部不会换人（一句就是一个单位）。
    phrase  按乐句轮流（空档 > 2s 断组）—— 一组连续的句子同一个人唱完。
    section 按段落轮流（空档 > 6s 断组）—— 主歌一人、副歌一人那种。
    manual  前端给的逐句角色下标。★ 允许 `ROLE_ALL`(-1) = 这一句**合唱**。

    ★ phrase / section 存在的理由：rotate 会在换气处频繁换人，听感是"两个人
      抢着唱"。按组轮流才像真的分了段落。
    ★ 但"一组一个人"有个退化：组数 < 人数时后面的人一句都分不到。所以分组之后
      还要 `split_groups` 把组数补到人数（切点仍在句间），见那里的说明。
    """
    n = len(segs)
    if n == 0 or n_roles <= 0:
        return []
    if mode == "manual":
        # ★★ 2026-10-24 第五轮：manual 为空时**不许**返回 [0]*n。
        #   `[0]*n` = 整首给第一个人、其余人 **0 秒**，而且不报错、不提示 ——
        #   实测老板看到的预览就是「苹果嘉儿 174.46 秒 / 1 段，云宝 0 秒 / 0 段」，
        #   而界面上一句都还没点（manual=None）。这是"分工"功能最坏的失效形态：
        #   产出一份**看着正常**的独唱表（铁律 22）。
        #   ⇒ 没指定过就退回**逐句轮流**（每个人都分得到），并把"还没指定"
        #   记进 info，由调用方说出来；用户点第一行的瞬间就换成他的那份。
        if not manual:
            if info is not None:
                info["manual_empty"] = True
            return [i % max(1, n_roles) for i in range(n)]
        # ★★ 份数对不上 ⇒ **按时间对齐**，不是拒绝，也不是静默补齐。
        #   为什么会不对：预览（`multi_plan_preview`）只读**缓存里的**歌词
        #   （没认过词 ⇒ 按时间切 N 段），开跑（`sing_multi`）会自动认一次词
        #   ⇒ 变成 M 句真句子。老板这次：按 14 段点好，认完词是 86 句。
        #   三种处理法，只有第三种站得住：
        #     ① 老的 `manual[i] if i < len else manual[-1]` —— 后 72 句全部沿用
        #        第 14 个值 ⇒ 后半首变独唱，还不报错（"选了但没按选的来"）。
        #     ② 上一条：直接 ValueError 拒绝 —— 用户的 14 次点击全废，还要在
        #        86 行里重点一遍。**这不是"不静默"，是"把他赶走"。**
        #     ③ 按时间对齐：manual 第 j 份本来就是"那一**段时间**归谁"，
        #        句数变了，时间没变 ⇒ 照时间落回去，用户点的"第 3 段合唱"
        #        仍然落在 00:24–00:37。意图完整保留。
        if len(manual) != n:
            idx = _manual_by_time(segs, manual)
            if info is not None:
                info["remap"] = {"from": len(manual), "to": n}
            out = []
            for i in range(n):
                v = manual[idx[i]] if idx[i] < len(manual) else 0
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    v = 0
                out.append(ROLE_ALL if v == ROLE_ALL
                           else max(0, min(n_roles - 1, v)))
            return out
        out = []
        for i in range(n):
            v = manual[i] if i < len(manual) else manual[-1]
            try:
                v = int(v)
            except (TypeError, ValueError):
                v = 0
            # ★ 合唱（-1）是"所有人"，不是"第 -1 个人" ⇒ 不许被夹到 0。
            #   以前这里无脑 `max(0, ...)`，用户点的合唱会被静默改成第 1 个人独唱
            #   —— 界面上句句都有名字，只是合唱永远不出来。
            out.append(ROLE_ALL if v == ROLE_ALL else max(0, min(n_roles - 1, v)))
        return out
    if mode == "rotate":
        return [i % n_roles for i in range(n)]
    # phrase / section
    out = [0] * n
    gs, _nb = groups_for_roles(segs, n_roles, gap_sec)
    for gi, g in enumerate(gs):
        who = gi % n_roles
        for i in g:
            out[i] = who
    return out


def takes_from_lines(segs, line_roles, duration, n_roles=1):
    """每句的角色 → **完整时间轴划分** [(start, end, role_index), ...]。

    三条规则（见文件头的"完整划分"）：
      · 第 0 句之前（前奏）归第 0 句那个人；
      · 句与句的空隙归**前一句**那个人（接住尾音）；
      · 最后一句之后（尾奏）归最后一句那个人。

    相邻且同角色的段会合并 —— 不合并的话一首 25 句的歌会切出 50 个接缝，
    每个接缝都要交叉淡化，白白多出 50 次音量渐变。

    ★ `ROLE_ALL`(-1) 段一样是"一个人"在合并规则眼里：相邻的两句都是合唱就并入
      同一段（否则每句合唱之间都要缝一次）。它跟任何独唱段都**不**合并 ——
      那正是一次"进/出合唱"的切换，必须留接缝。
    """
    duration = _f(duration)
    if duration <= 0:
        return []
    if not segs:
        # 没有歌词时间戳：退化成"整首给第一个人"，不假装分过
        return [(0.0, duration, 0)]
    order = sorted(range(len(segs)), key=lambda i: _f(segs[i].get("start")))
    takes = []
    cursor = 0.0

    def _push(s, e, who):
        """追加一段；跟前一段同角色且首尾相接就直接并进去。"""
        nonlocal takes
        if e - s <= 1e-6:
            return
        if takes and takes[-1][2] == who and abs(takes[-1][1] - s) < 1e-6:
            takes[-1] = (takes[-1][0], e, who)
        else:
            takes.append((s, e, who))

    for i in order:
        st = max(0.0, min(duration, _f(segs[i].get("start"))))
        en = max(st, min(duration, _f(segs[i].get("end"), st)))
        who = line_roles[i] if i < len(line_roles) else 0
        # ★ 不许 `max(0, ...)`：-1 是"合唱"，夹成 0 就把合唱变成了第一个人独唱
        who = ROLE_ALL if int(who) < 0 else max(0, int(who))
        if st > cursor:
            # 空隙（或前奏）归前一句那个人；还没有前一句就归这一句
            _push(cursor, st, takes[-1][2] if takes else who)
        elif not takes and st > 0:
            _push(0.0, st, who)
        if en > cursor:
            _push(cursor if st <= cursor else st, en, who)
            cursor = max(cursor, en)
    if cursor < duration:
        _push(cursor, duration, takes[-1][2] if takes else 0)
    return takes


def takes_even(duration, n_roles, pieces=None):
    """没有歌词时间戳时的退化方案：按角色轮流切成等长段。

    这是"能用"不是"好听" —— 切点会落在句子中间。studio 那边必须在日志里
    说明歌词没认过，别让用户以为是真的分了句。
    """
    duration = _f(duration)
    if duration <= 0 or n_roles <= 0:
        return []
    n = int(pieces) if pieces else max(n_roles, int(duration // 12.0) or n_roles)
    n = max(n_roles, n)
    step = duration / float(n)
    out = []
    for k in range(n):
        s = k * step
        e = duration if k == n - 1 else (k + 1) * step
        out.append((s, e, k % n_roles))
    return out


def plan_takes(segs, duration, n_roles, mode="rotate", manual=None, info=None):
    """一步到位：歌词时间戳 + 分配方式 → 完整时间轴划分。

    返回 (takes, line_roles, 是否退化)。

    info（可选 dict）：把"分组到底怎么分的"顺手写进去，供调用方在日志里说人话。
    以前这些只能靠猜 —— 用户听到"怎么还是一个人在唱"，日志里却只有一句
    "分句：4 句 → 1 段"。现在写明"只有 1 组（少于 2 人），已按句拆成 2 组"。
      info["groups"]  分组后的组数（补足之后）
      info["groups_before"] 补足之前
      info["split"]   是否为了让每个人都能唱而拆过组
      info["short"]   句数 < 人数，拆也拆不动（有人注定唱不到）
    """
    gap = GAP_SECTION if mode == "section" else GAP_PHRASE
    if not segs:
        return takes_even(duration, n_roles), [], True
    if info is not None:
        gs, nb = groups_for_roles(segs, n_roles, gap) if mode in ("phrase", "section") \
            else ([], 0)
        info["groups"] = len(gs)
        info["groups_before"] = nb
        info["split"] = bool(gs) and len(gs) > nb
        info["short"] = bool(gs) and len(gs) < max(1, int(n_roles or 1))
    lr = assign_lines(segs, n_roles, mode=mode, manual=manual, gap_sec=gap,
                      info=info)
    if not lr:
        return takes_even(duration, n_roles), [], True
    return takes_from_lines(segs, lr, duration, n_roles), lr, False


def role_spans(takes, k, join_gap=0.02):
    """角色 k 在时间轴上的**连续演唱区间**（相邻段合并成一段）。

    用途有两个，都必须用"合并后的区间"而不是"分配表的原始段"：
      · 逐人体检取样 —— 分成 20 个 1 秒碎段的角色，单看某一段太短、抖得没法量；
      · 排除区间 —— 质检要跳过"这个人没在唱"的时间，边界必须整块。
    """
    spans = []
    for t in sorted(takes, key=lambda x: _f(x[0])):
        # ★ 合唱段（ROLE_ALL）算**每个人**都在唱 —— 逐人体检取样/排除区间要
        #   按"他有没有出声"算，那段他确实在唱（只是旁边还有别人）。
        if int(t[2]) != int(k) and int(t[2]) >= 0:
            continue
        s, e = max(0.0, _f(t[0])), _f(t[1])
        if e - s <= 1e-6:
            continue
        if spans and s - spans[-1][1] <= join_gap:
            spans[-1] = (spans[-1][0], max(spans[-1][1], e))
        else:
            spans.append((s, e))
    return spans


def role_skips(takes, k, duration, min_gap=0.05):
    """角色 k **没在唱**的时间段 —— 逐人体检时要排除的帧。

    ★ 为什么要有它：多角色混成一条以后，"原唱 f0 vs 成品 f0"没有意义
      （A 负责的段会被按 B 的音高去比）。正确做法是拿**这个角色自己的轨**，
      只在它负责的时间窗里比；其余时间必须显式排除，否则那些段（那条轨上
      是 RVC 对别人句子的输出）会把吻合率拖成一个纯噪声的数。
    ★ 返回的是"补集"，不是"它唱的那几段"：qc_cli 的接口就是 skip。
    """
    duration = _f(duration)
    out, cursor = [], 0.0
    if duration <= 0:
        return []
    for s, e in role_spans(takes, k):
        s, e = min(duration, max(0.0, s)), min(duration, e)
        if s - cursor >= min_gap:
            out.append((round(cursor, 3), round(s, 3)))
        cursor = max(cursor, e)
    if duration - cursor >= min_gap:
        out.append((round(cursor, 3), round(duration, 3)))
    return out


def longest_span(takes, k, cap=30.0):
    """角色 k 最长的一段演唱区间 → (起点, 取样秒数)。没唱返回 (0, 0)。

    取样而不是整曲：整曲里大部分时间它没在唱，把那些段算进吻合率等于抽签。
    长度封顶（默认 30 秒）—— 再长也不会让结论更可信，只会让体检更慢。
    """
    sp = role_spans(takes, k)
    if not sp:
        return (0.0, 0.0)
    s, e = max(sp, key=lambda x: x[1] - x[0])
    return (round(s, 3), round(min(_f(cap), e - s), 3))


def takes_stats(takes, n_roles):
    """按角色统计演唱秒数与段数 —— 用来在日志里告诉用户"谁唱了多少"。

    ★ 合唱段（`ROLE_ALL`）单独记，不掺进任何人的 `sec`：
      `sec` 是"这个人**独唱**了多久"，所有人的 `sec` 加起来仍等于全曲 ——
      这是界面上"谁唱了多少"那一行的可读前提（掺进去的话总和会超过曲长，
      看着像分多了）。合唱多久是**所有人共同**的，所以放在 `chorus_sec` 里
      （每个人的值相同），由调用方单独说一句"其中 N 秒是合唱"。
    """
    takes = list(takes or [])
    cho = [t for t in takes if int(t[2]) < 0]
    chorus_sec = round(sum(max(0.0, _f(t[1]) - _f(t[0])) for t in cho), 2)
    out = []
    for k in range(max(1, n_roles)):
        seg = [t for t in takes if int(t[2]) == k]
        out.append({
            "role_index": k,
            "sec": round(sum(max(0.0, _f(t[1]) - _f(t[0])) for t in seg), 2),
            "parts": len(seg),
            "chorus_sec": chorus_sec,
            "chorus_parts": len(cho),
            # 开口总时长（独唱 + 合唱）—— 逐人体检要排的是"他有没有出声"
            "sec_all": round(sum(max(0.0, _f(t[1]) - _f(t[0])) for t in seg)
                             + chorus_sec, 2),
        })
    return out


# --------------------------------------------------------------------------
# 下半：拼接（要 numpy）
# --------------------------------------------------------------------------

def _load_mono(p):
    import numpy as np
    import soundfile as sf
    x, sr = sf.read(p, dtype="float32", always_2d=True)
    # ★ 必须无条件压成一维：单声道时 shape 是 (n, 1)，后面 x * mask 会被
    #   广播成 (n, n) —— 实测一条 5 秒的轨直接去申请 181 GB，当场炸。
    #   （踩过一次，别再用 `if x.shape[1] > 1` 这种"看着对"的写法。）
    if x.ndim > 1:
        x = x.mean(axis=1)
    return np.asarray(x, dtype=np.float32), int(sr)


def _rms(x):
    import numpy as np
    if x is None or len(x) == 0:
        return 0.0
    return float(np.sqrt(float(np.mean(x * x))))


def _mask_of(takes, n, sr, idx, chorus=True):
    """角色 idx **在出声**的时间（1 = 有他）。

    `chorus=True`（默认）时合唱段也算 —— 响度对齐、逐人体检都要按"有没有出声"
    算，那段他确实在唱。拼接时只想要"独唱窗口"就用 `chorus=False`。
    """
    import numpy as np
    m = np.zeros(n, dtype=np.float32)
    for t in takes:
        r = int(t[2])
        if r != idx and not (chorus and r < 0):
            continue
        a = max(0, int(round(_f(t[0]) * sr)))
        b = min(n, int(round(_f(t[1]) * sr)))
        if b > a:
            m[a:b] = 1.0
    return m


def _mask_chorus_of(takes, n, sr):
    """合唱段的时间窗（1 = 所有人一起唱）。"""
    import numpy as np
    m = np.zeros(n, dtype=np.float32)
    for t in takes:
        if int(t[2]) >= 0:
            continue
        a = max(0, int(round(_f(t[0]) * sr)))
        b = min(n, int(round(_f(t[1]) * sr)))
        if b > a:
            m[a:b] = 1.0
    return m


def _smooth(m, sr, xfade_ms):
    import numpy as np
    w = max(1, int(round(xfade_ms / 1000.0 * sr)))
    kern = np.hanning(w * 2 + 1)
    kern = kern / kern.sum()
    return np.convolve(m, kern, mode="same").astype(np.float32)


def _rms_masked(x, m):
    """只在 mask 覆盖到的样本上算 RMS。"""
    import numpy as np
    sel = m > 0.5
    if not np.any(sel):
        return 0.0
    return float(np.sqrt(float(np.mean(np.asarray(x[sel], dtype=np.float32) ** 2))))


def _gains(xs, masks, sr, min_sec=0.5):
    """每个角色该乘多少增益，才能跟别人一样响。

    ★ 按**各自真正被分到的那几段**算，不是整条轨（这条踩过）：
      整条轨的 RMS 由它能量最高的地方决定，而用户在成品里听到的只是
      分给它的那几段。实测同一模型两个音高：
        整条轨 RMS 只差 7.9% —— 增益一补就"看起来对齐了"
        各自负责那半段却差 46%（3.3 dB）—— 听感就是一句大一句小
      分配的区间短于 min_sec 时退回整条轨（几十毫秒的样本抖得没法用）。
    """
    import numpy as np
    vals = []
    for x, m in zip(xs, masks):
        secs = float(np.count_nonzero(m > 0.5)) / float(sr)
        vals.append(_rms_masked(x, m) if secs >= min_sec else _rms(x))
    live = sorted([v for v in vals if v > 1e-9])
    target = live[len(live) // 2] if live else 0.0
    out = []
    for v in vals:
        if v > 1e-9 and target > 1e-9:
            out.append(round(max(GAIN_MIN, min(GAIN_MAX, target / v)), 4))
        else:
            out.append(1.0)
    return out, [round(v, 6) for v in vals]


def _peak_guard(out):
    """整体缩放，别让叠加削顶。返回 (信号, 是否缩放, 原峰值)。"""
    import numpy as np
    pk = float(np.abs(out).max()) if len(out) else 0.0
    if pk > 0.99:
        return out * (0.99 / pk), True, round(pk, 3)
    return out, False, round(pk, 3)


def _hp(x, sr, hz=CHO_HP_HZ):
    """二阶 Butterworth 高通（零相位，FFT 实现 —— 离线处理不在乎延迟）。

    为什么要高通掉和声层的低频：40~150Hz 是"胸腔/地基"，两个人的低频完全同相
    叠在一起只会互相抵消或糊成一团，而主唱必须站在那儿。让开之后两层各占一段，
    听起来才是"两个人"。
    """
    import numpy as np
    n = len(x)
    if n <= 0 or hz <= 0:
        return x
    nfft = 1 << int(np.ceil(np.log2(max(n, 2))))
    X = np.fft.rfft(np.asarray(x, dtype=np.float64), nfft)
    f = np.fft.rfftfreq(nfft, 1.0 / float(sr))
    w = f / float(hz)
    H = (w ** 2) / np.sqrt(1.0 + w ** 4)
    y = np.fft.irfft(X * H, nfft)[:n]
    return y.astype(np.float32)


def _shelf_24k(x, sr, db=CHO_PRES_DB, lo=2000.0, hi=4000.0):
    """2~4kHz 让位：主唱的齿音/临场感在这一段。

    和声层同频段全音量叠上去 ⇒ 两个人的"s / t / 气"挤在一起，糊成一片 ——
    这是"简单拼接"最典型的一个听感。压 2~3dB 就够，再多和声层会发闷。
    """
    import numpy as np
    n = len(x)
    if n <= 0 or db >= 0:
        return x
    nfft = 1 << int(np.ceil(np.log2(max(n, 2))))
    X = np.fft.rfft(np.asarray(x, dtype=np.float64), nfft)
    f = np.fft.rfftfreq(nfft, 1.0 / float(sr))
    g = np.ones_like(f)
    band = (f >= lo) & (f <= hi)
    if band.any():
        t = (f[band] - lo) / max(hi - lo, 1e-9)
        # 余弦凹陷：边缘 0dB，正中 db（不要硬边，硬边会出金属声）
        g[band] = 10.0 ** ((db * 0.5 * (1.0 - np.cos(2.0 * np.pi * t))) / 20.0)
    y = np.fft.irfft(X * g, nfft)[:n]
    return y.astype(np.float32)


def _reverb(x, sr, amount=CHO_REVERB, tail_sec=CHO_REVERB_SEC, seed=7):
    """一点点混响（衰减噪声当脉冲响应），让和声层退到主唱后面。

    湿声先拉到跟干声等响再按 amount 混 —— 否则 amount 的语义会被素材响度带偏。
    """
    import numpy as np
    n = len(x)
    if n <= 0 or amount <= 0:
        return x
    L = int(float(sr) * tail_sec)
    if L < 8:
        return x
    rng = np.random.default_rng(seed)
    ir = rng.standard_normal(L) * np.exp(-np.linspace(0.0, 6.0, L))
    ir[0] = 0.0                      # 只要尾巴，不要把直达声也算进去
    ir /= (float(np.sqrt((ir ** 2).sum())) + 1e-12)
    wet = np.convolve(np.asarray(x, dtype=np.float64), ir, mode="full")[:n]
    w = float(np.sqrt((wet ** 2).mean())) + 1e-12
    d = float(np.sqrt((np.asarray(x, dtype=np.float64) ** 2).mean())) + 1e-12
    wet = wet * (d / w)
    y = np.asarray(x, dtype=np.float64) * (1.0 - amount) + wet * amount
    return y.astype(np.float32)


def chorus_layer(x, sr, layer_idx=1, humanise=True):
    """把一条"和声层"做成**真的另一把声音**，而不是主唱的复制品。

    四条手段依次是：时间错位 → 频率让位（高通 + 2~4k 让位）→ 空间后退（混响）
    → makeup gain（把 EQ 抽掉的能量补回来）。

    ★ 顺序有讲究：makeup gain 必须在 EQ 之后。先补后 EQ 的话，`other_gain`
      的语义就不再是"垫多厚"了 —— 门禁量"叠上去的倍数"会跟着 EQ 跑。

    ★ `humanise=False` ⇒ 与改动前**逐样本等价**（直接原样返回），
      给门禁/单测留一条对照通道。
    """
    import numpy as np
    y = np.asarray(x, dtype=np.float32)
    if not humanise or len(y) == 0:
        return y
    # ① 时间错位：往后挪（和声比主唱迟一点进来，真人就是跟不上）
    delay = int(round(float(sr) * (CHO_DELAY_MS + CHO_DELAY_STEP_MS
                                   * max(0, int(layer_idx) - 1)) / 1000.0))
    if delay > 0 and delay < len(y):
        y = np.concatenate([np.zeros(delay, dtype=np.float32), y])[:len(y)]
    r0 = float(np.sqrt((y ** 2).mean())) or 1.0
    # ② 频率让位
    y = _hp(y, sr, CHO_HP_HZ)
    y = _shelf_24k(y, sr, CHO_PRES_DB, 2000.0, 4000.0)
    # ③ 空间后退
    if CHO_REVERB > 0:
        y = _reverb(y, sr, CHO_REVERB, CHO_REVERB_SEC, seed=7 + int(layer_idx))
    # ④ makeup gain：other_gain 必须还是纯粹的"垫多厚"
    r1 = float(np.sqrt((y ** 2).mean()))
    if r1 > 1e-9:
        y = y * (r0 / r1)
    return y.astype(np.float32)


def blend(tracks, takes, sr, xfade_ms=XFADE_MS, other_gain=CHORUS_GAIN,
          humanise=True):
    """把 N 条轨按分配表拼成一条（**对唱**：轮流唱；**合唱段**：一起唱）。

    tracks: [np.ndarray, ...]，顺序 = 分配表里的角色下标
    takes:  [(start, end, role), ...]，role 为 `ROLE_ALL`(-1) 的那一段是**合唱**
    返回 (拼接结果, 统计 dict)

    ★ 归一化那一步不能省：平滑之后权重和不再是严格 1（卷积的边界效应），
      不归一化就会在接缝处掉零点几个 dB。虽然听不出来，但它会让"两条轨
      是不是等响"这件事变得不可验证 —— 门禁要量的就是它。

    ★★ 合唱段怎么混（口径跟 overlay 完全一致，别各写一套）：
      主唱（第 0 条）原音量 + 其余各层按 other_gain 垫在下面，**不做归一化**。
      为什么归一化不行：那会把"叠了一个人"变成"两个人各占一半"，主唱被稀释、
      整体还变小 —— 那就不是和声了。所以只做峰值保护（超了整体缩）。
      ★ 和声层先过 `chorus_layer`（错位 / 让频段 / 加混响），不再是主唱的复制品。
      ★ 但**叠加量按"整堆"对齐**：所有层之和的 RMS 归一化到主唱的 `other_gain` 倍，
        人多人少都是这个比例（以前每层各乘 0.55，8 个人就是 4.85 倍 → 只能整体
        往下压 → 主唱被压扁，听感「一群人喊、发哑」）。
      ⚠ 没有合唱段时走的还是独唱分支（权重恒定 1，不做归一化），但**峰值保护
        现在两条分支都过** —— 独唱分支以前不过，而增益最高可到 4 倍，实测出现过
        峰值 1.33 直接写盘削顶的情况。
    """
    import numpy as np
    n = min(len(x) for x in tracks) if tracks else 0
    if n <= 0:
        raise ValueError("没有可拼接的轨")
    xs = [np.asarray(x[:n], dtype=np.float32) for x in tracks]

    # 响度对齐：按"有没有出声"算（**含**合唱段，那段他确实在唱）
    masks_full = [_mask_of(takes, n, sr, i) for i in range(len(xs))]
    gains, rms_used = _gains(xs, masks_full, sr)
    xs = [x * g for x, g in zip(xs, gains)]

    # ---- 独唱部分：只认"这段归我一个人"的窗口 -------------------------
    solo_raw = [_mask_of(takes, n, sr, i, chorus=False) for i in range(len(xs))]
    masks = [_smooth(m, sr, xfade_ms) for m in solo_raw]

    total = np.zeros(n, dtype=np.float32)
    for m in masks:
        total += m
    # 分配表是完整划分 ⇒ 独唱窗口内理论恒为 1；这里只是兜住边界效应
    lo = total < 1e-6
    total = np.where(lo, 1.0, total)

    out = np.zeros(n, dtype=np.float32)
    for x, m in zip(xs, masks):
        out += x * (m / total)

    # ---- 合唱部分（有才加，没有就一个样本都不动）----------------------
    cho_raw = _mask_chorus_of(takes, n, sr)
    has_cho = bool(n) and float(cho_raw.max()) > 0.5
    scaled, peak = False, 0.0
    eff_gain = float(other_gain)
    if has_cho:
        c = _smooth(cho_raw, sr, xfade_ms)
        # ★★ 和声层改成「整堆能量对齐」，不再每层各乘 other_gain：
        #   以前 N 个人 = 1 + (N-1)×0.55 倍叠加（8 个人 4.85 倍），峰值保护只能
        #   整体往下压 ⇒ 主唱被压扁、听感就是「很多人一起喊、发哑」。
        #   现在把**所有层的和**归一化到「主唱在合唱段的 55%」，人多人少都是 55%：
        #   多一层是加厚，不是加响。单层时与旧行为数值一致（RMS 比 = 1）。
        layers = np.zeros_like(xs[0])
        for j in range(1, len(xs)):
            layers += chorus_layer(xs[j], sr, j, humanise=humanise)
        sel = c > 0.5
        if sel.any():
            r_lead = float(np.sqrt(np.mean(xs[0][sel].astype(np.float64) ** 2)))
            r_lay = float(np.sqrt(np.mean(layers[sel].astype(np.float64) ** 2)))
            if r_lay > 1e-9 and r_lead > 1e-9:
                eff_gain = float(other_gain) * r_lead / r_lay
        mix = xs[0] + layers * eff_gain
        out = out + mix * c
    # ★ 峰值保护**两条分支都要过**：独唱分支以前不过，而分轨增益最高可到 4.0 倍
    #   （实测一次对唱：r0 增益 1.50 × 峰值 0.88 = 1.33）⇒ 写 PCM16 时直接削顶，
    #   听感就是「哑、破、白」。这不是"理论风险"，是 2026-10-27 用真实产物量到的。
    out, scaled, peak = _peak_guard(out)

    # 接缝数 = 分配表里"谁在唱"发生变化的次数（进出合唱也算一次）
    seams = 0
    prev = None
    for t in takes:
        k = int(t[2])
        if prev is not None and k != prev:
            seams += 1
        prev = k

    stats = {
        "mode": "take",
        "sr": int(sr),
        "secs": round(n / float(sr), 2),
        "samples": int(n),
        "tracks": len(xs),
        "takes": len(takes),
        "seams": int(seams),
        "gains": gains,
        "rms_before": rms_used,
        "xfade_ms": xfade_ms,
        "chorus": bool(has_cho),
        "other_gain": float(other_gain),
        "other_gain_eff": round(float(eff_gain), 3),
        "chorus_sec": round(sum(max(0.0, _f(t[1]) - _f(t[0]))
                                for t in takes if int(t[2]) < 0), 2),
        "peak_guard": bool(scaled),
        "peak": peak,
        "per_role": takes_stats(takes, len(xs)),
    }
    return out, stats


def overlay(tracks, sr, other_gain=0.55):
    """所有人**同时**唱（主唱 + 和声）。

    第 1 条当主唱（原音量），第 2..N 条按 other_gain 叠上去。

    ★ 这里故意**不归一化**：归一化会把"叠了一个人"变成"两个人各占一半" ——
      那已经不是和声了，主唱会被稀释掉、整体还变小。叠唱要的是"底下垫一层"，
      所以只做峰值保护（超了就整体缩）。

    ★ 响度对齐照做：不对齐的话"和声"其实是"另一个人在吼"。
      叠唱没有"分配区间"可言，所以按**整轨** RMS 对齐（跟 blend 不同）。

    ★ 第 2..N 条先过 `chorus_layer`（时间错位 / 让开主唱频段 / 多一点混响），
      否则两层完全同相 = 主唱复制一遍 = 老板说的「简单的拼接」。
      `other_gain` 的语义保持"垫多厚"（chorus_layer 内部做了 makeup gain）。
    """
    import numpy as np
    n = min(len(x) for x in tracks) if tracks else 0
    if n <= 0:
        raise ValueError("没有可叠加的轨")
    xs = [np.asarray(x[:n], dtype=np.float32) for x in tracks]

    rs = [_rms(x) for x in xs]
    live = sorted([r for r in rs if r > 1e-9])
    target = live[len(live) // 2] if live else 0.0
    gains = [round(max(GAIN_MIN, min(GAIN_MAX, target / r)), 4)
             if r > 1e-9 and target > 1e-9 else 1.0 for r in rs]
    xs = [x * g for x, g in zip(xs, gains)]

    out = xs[0].copy()
    for j, x in enumerate(xs[1:], start=1):
        out += chorus_layer(x, sr, j) * float(other_gain)
    out, scaled, peak = _peak_guard(out)

    stats = {
        "mode": "chorus",
        "sr": int(sr),
        "secs": round(n / float(sr), 2),
        "samples": int(n),
        "tracks": len(xs),
        "takes": 1,
        "seams": 0,
        "gains": gains,
        "rms_before": [round(r, 6) for r in rs],
        "other_gain": float(other_gain),
        "peak_guard": scaled,
        "peak": peak,
        "per_role": [],
    }
    return out, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="", help="分配表 json（对唱必填；叠唱不用）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--track", action="append", default=[],
                    help="角色轨，按分配表里的角色下标顺序给，可重复")
    ap.add_argument("--report", default="")
    ap.add_argument("--xfade", type=float, default=XFADE_MS)
    ap.add_argument("--layer", type=float, default=0.0,
                    help=">0 就走叠唱（主唱 + 和声），值是和声音量 0~1；给了就不看 --plan")
    ap.add_argument("--cgain", type=float, default=0.0,
                    help="分配表里的**合唱段**（role=-1）中非主唱层的音量；"
                         "0 = 用默认 %.2f" % CHORUS_GAIN)
    a = ap.parse_args()

    if not a.track:
        print("[err] 至少要给一条 --track")
        return 2
    for p in a.track:
        if not os.path.isfile(p):
            print("[err] 轨不存在：%s" % p)
            return 2

    takes = []
    if a.layer <= 0:
        if not a.plan:
            print("[err] 对唱必须给 --plan（或者用 --layer 走叠唱）")
            return 2
        try:
            plan = json.load(open(a.plan, encoding="utf-8"))
        except Exception as e:
            print("[err] 分配表读不出来：%s" % e)
            return 2
        takes = plan.get("takes") if isinstance(plan, dict) else plan
        if not takes:
            print("[err] 分配表是空的")
            return 2
        takes = [(_f(t[0]), _f(t[1]), int(t[2])) for t in takes if len(t) >= 3]

    # np / sf 在**函数开头** import，不能在某个分支里 import：
    # 函数体内出现过 `import numpy as np`，np 就是局部变量，
    # 走到没执行那条分支的路径上会 UnboundLocalError（踩过）。
    import numpy as np
    import soundfile as sf

    xs, sr0 = [], None
    for p in a.track:
        x, sr = _load_mono(p)
        if sr0 is None:
            sr0 = sr
        elif sr != sr0:
            # 采样率不一致就重采样（不同 RVC 模型输出可能不同）
            import numpy as np
            try:
                import librosa
                x = librosa.resample(x, orig_sr=sr, target_sr=sr0)
            except Exception:
                ratio = sr0 / float(sr)
                idx = (np.arange(int(len(x) * ratio)) / ratio).astype(int)
                idx = np.clip(idx, 0, len(x) - 1)
                x = x[idx]
            x = np.asarray(x, dtype=np.float32)
        xs.append(x)

    try:
        if a.layer > 0:
            out, stats = overlay(xs, sr0, a.layer)
        else:
            out, stats = blend(xs, takes, sr0, a.xfade,
                               a.cgain if a.cgain > 0 else CHORUS_GAIN)
    except Exception as e:
        print("[err] %s失败：%s" % ("叠唱" if a.layer > 0 else "拼接", e))
        return 1

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    sf.write(a.out, out.astype(np.float32), sr0)

    print("MULTI 轨数 %d  采样率 %d  时长 %.2f 秒（%s）"
          % (stats["tracks"], sr0, stats["secs"],
             "叠唱" if a.layer > 0 else "对唱"))
    if a.layer > 0:
        print("  和声音量 %.2f  峰值 %.3f%s"
              % (stats.get("other_gain", 0), stats.get("peak", 0),
                 "（已整体缩放防削顶）" if stats.get("peak_guard") else ""))
    else:
        print("  分配段 %d 段，角色切换 %d 次，交叉淡化 %.0f ms"
              % (stats["takes"], stats["seams"], a.xfade))
        if stats.get("chorus"):
            print("  其中**合唱** %.1f 秒（主唱原音量 + 其余各层 %.0f%% 垫底%s）"
                  % (stats.get("chorus_sec") or 0.0,
                     (stats.get("other_gain") or 0) * 100,
                     "，已整体缩放防削顶" if stats.get("peak_guard") else ""))
    for i, g in enumerate(stats["gains"]):
        print("    轨 %d：响度增益 %.3f（原 RMS %.5f）" % (i, g, stats["rms_before"][i]))
    for r in stats["per_role"]:
        # ★ 独唱秒数与合唱秒数分开报：混在一起会让"谁唱了多少"读不出真相
        #   （合唱是所有人共同的时长，不是某一个人的）。
        extra = ""
        if r.get("chorus_sec"):
            extra = "，另含合唱 %.1f 秒" % r["chorus_sec"]
        print("    角色 %d：独唱 %.1f 秒 / %d 段%s"
              % (r["role_index"], r["sec"], r["parts"], extra))
    if a.report:
        json.dump(stats, open(a.report, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    print("MULTI_DONE %.2f" % stats["secs"])
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
