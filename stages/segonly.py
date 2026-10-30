# -*- coding: utf-8 -*-
"""分角色只转自己分到的那几句（seg-only）。

背景
----
多角色合唱贵的那一步是「每个角色各把**整首**唱一遍」—— N 个角色就是 N 倍时间。
但拼装的时候只取每个人分到的那几段，其余全丢。也就是说整首里大约 2/3 的推理
是白做的。

这条路把这些废推理砍掉：只把「这个人要唱的段」前后各留一点上下文拼成一条短轨
去转，转完再把这几段**放回原时间轴**，其余置零。

为什么能这么做而不砸音色
------------------------
1. 送给 RVC 的每一段仍是**原音频里连续的一截**（不是把碎句硬缝起来），
   hubert 看到的上下文是真的；
2. 每个窗口前后各留 `DEFAULT_CTX` 秒上下文，而**取用时只取原本那几句**，
   上下文是拿来给模型"垫"的，不进成品 ⇒ 边缘效应被留在窗口里丢掉；
3. 窗口之间插 `DEFAULT_GAP` 秒静音，让相邻窗口的特征不要在缝上互相污染；
4. 输出形状和整首转换**完全一致**（同样长度、同样采样率的一条轨），
   所以下游（拼接 / 拉平响度 / 混音 / 高频补偿）一行都不用改。

什么时候**不许**走这条路
------------------------
`should_use()` 会算一笔账，省不了多少就退回整首 —— 不为省时间把质量搞坏：
  · 一句都没分到（那就别跑了，直接出静音轨）；
  · 分到的超过全曲 55%（省不了多少，白白多担接缝风险）；
  · 段太碎（要转的长度超过实唱长度的 3 倍 —— 全是上下文在充数）；
  · 叠唱（每个人本来就唱整首，没得省）。

numpy / soundfile 都在函数内 import：studio 要能在没装 numpy 的环境里被 import。
"""

import os

DEFAULT_CTX = 1.5      # 每个窗口前后各留多少秒上下文
DEFAULT_GAP = 0.5      # 窗口之间插多少秒静音
MERGE_GAP = 2.0        # 相隔小于这个秒数的段合成一个窗口（省接缝）
MAX_BLOWUP = 3.0       # 要转的长度 / 实唱长度 超过这个倍数就不省
MAX_SHARE = 0.55       # 分到的超过全曲这个比例就不省

# ---- 接缝处理（2026-10-22 实测后加的）----
# 实测：RVC 输出比输入**早 10~20 毫秒**（切片时丢了开头的样本），而且是
# 固定偏移不是累积漂移（量了轨道 5%/25%/45%/65%/85% 五个位置，偏移 -10~-15ms）。
# 不修的话多条角色轨会一起"提前"，跟伴奏差 10ms —— 混在一起是轻微的双音感。
# 用**能量包络**对齐（包络对音色不敏感，这正是我们需要的：转换后音色变了，
# 波形相关不能用了）。只在 ±ALIGN_MAX 秒内找，相关性太低就不硬掰。
FADE_SEC = 0.005       # 每个连续有声段首尾各淡入淡出 5ms，杜绝爆音
ALIGN_MAX = 0.05       # 对齐搜索范围 ±50ms
ALIGN_HOP = 0.005      # 包络帧长 5ms
ALIGN_MIN_CORR = 0.35  # 包络相关低于这个值就当没对上，不动


def takes_of(takes, who, with_chorus=True):
    """取出角色 `who` 要唱的段 [[a, b], ...]，按时间排序。

    takes 的形状是 [start, end, role_index]（multising.plan_takes 的产物）。
    认不出的条目直接跳过 —— 宁可少转一段（那一段会静音），也不要因为一条脏
    数据把整次转换搞挂。

    ★★ `with_chorus=True`（默认）时，**合唱段（role < 0）也算给他**。
      为什么必须算：合唱段是"所有人都要唱"。以前这里只认 `role == who`，
      于是开着「只转自己分到的那几句」时，合唱段**没有任何角色去转换** ——
      而 `blend` 的合唱混音是 `主唱轨 + 其余×0.55`，
      那些轨在合唱段是静音 ⇒ 合唱段里只剩"恰好退回整首转换"的那个人有声音，
      听起来就是**"一起唱其实一个人在唱"**（老板原话：「一起唱全算在一个人身上」）。
      更坏的一种：所有人都 seg_only ⇒ 合唱段直接全静音，只剩伴奏。
      这条 bug 只在"seg_only 开 + 分配表里有合唱段"同时成立时出现 ——
      两个功能各自都对，合起来才错，所以两边单测都绿。
    """
    out = []
    who = int(who)
    for t in (takes or []):
        try:
            r = int(t[2])
            if r != who and not (with_chorus and r < 0):
                continue
            a, b = float(t[0]), float(t[1])
        except (IndexError, TypeError, ValueError):
            continue
        if b > a:
            out.append([a, b])
    out.sort(key=lambda x: x[0])
    return out


def merge_segments(segs, merge_gap=MERGE_GAP):
    """相隔 < merge_gap 秒的段并成一个。

    为什么合：一句 3 秒、下一句隔 1.5 秒又来 —— 为这两句开两个窗口就要多一条
    接缝（多一段静音、多一点边缘效应）。合成一个窗口只多转 1.5 秒，划算。
    """
    if not segs:
        return []
    out = [list(segs[0])]
    for a, b in segs[1:]:
        if a - out[-1][1] < merge_gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def plan_windows(segs, duration, ctx=DEFAULT_CTX):
    """给每段前后各加 ctx 秒上下文，裁到 [0, duration]，重叠的并起来。

    返回 [{"win": [a, b], "parts": [[a, b], ...]}, ...]。
    `parts` 是这个窗口真正要交给下游的段 —— 窗口多出来的部分只是上下文。
    """
    if not segs or duration <= 0:
        return []
    ctx = max(0.0, float(ctx))
    raw = []
    for a, b in segs:
        a2 = max(0.0, min(float(a), float(duration)))
        b2 = max(0.0, min(float(b), float(duration)))
        if b2 <= a2:
            continue          # 整段都在曲子外面，不要了
        raw.append([[a2 - ctx, b2 + ctx], [[a2, b2]]])
    if not raw:
        return []
    # 裁到 [0, duration]，并把裁完还重叠的并起来（合并后 parts 跟着合）
    clipped = []
    for w, parts in raw:
        a2 = max(0.0, w[0])
        b2 = min(float(duration), w[1])
        if b2 <= a2:
            continue
        if clipped and a2 <= clipped[-1][0][1]:
            clipped[-1][0][1] = max(clipped[-1][0][1], b2)
            clipped[-1][1].extend(parts)
        else:
            clipped.append([[a2, b2], list(parts)])
    for w, parts in clipped:
        parts.sort(key=lambda x: x[0])
    return [{"win": w, "parts": p} for w, p in clipped]


def build_input(src_wav, windows, out_wav, gap=DEFAULT_GAP):
    """把窗口拼成一条连续音频（窗口之间插 gap 秒静音）。

    返回 {"sr", "dur", "wins": [{"out_a","out_b","win_a","win_b","parts"}, ...]}。
    `out_a/out_b` 是这段在**拼出来的轨**上的秒数 —— 转完之后靠它们把结果取回来。
    """
    import numpy as np
    import soundfile as sf

    x, sr = sf.read(src_wav, dtype="float32", always_2d=False)
    if x.ndim > 1:
        x = x.mean(axis=1)
    n = len(x)
    gap_n = int(round(max(0.0, float(gap)) * sr))
    chunks, wins, pos = [], [], 0
    for i, w in enumerate(windows or []):
        a = int(round(max(0.0, w["win"][0]) * sr))
        b = int(round(min(w["win"][1], n / float(sr)) * sr))
        if b <= a:
            continue
        seg = x[a:b]
        chunks.append(seg)
        out_a = pos / float(sr)
        pos += len(seg)
        wins.append({"out_a": out_a, "out_b": pos / float(sr),
                     "win_a": a / float(sr), "win_b": b / float(sr),
                     "parts": [list(p) for p in w["parts"]]})
        if gap_n > 0 and i != len(windows) - 1:
            chunks.append(np.zeros(gap_n, dtype="float32"))
            pos += gap_n
    if not chunks:
        return {"sr": sr, "dur": 0.0, "wins": []}
    whole = np.concatenate(chunks)
    sf.write(out_wav, whole, sr, subtype="PCM_16")
    return {"sr": sr, "dur": len(whole) / float(sr), "wins": wins,
            "src": src_wav}


def _env(x, hop):
    """能量包络（每 hop 个样本取一次 RMS）。

    为什么用它对齐：转换后音色/相位都变了，波形相关会掉到 0.4 以下（实测），
    但"什么时候有声音、什么时候没声音、哪一句重"这条曲线是稳的。
    """
    import numpy as np

    n = len(x) // hop
    if n < 4:
        return np.zeros(4, dtype="float32")
    return np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(axis=1))


def best_shift(src, conv, sr, max_sec=ALIGN_MAX, hop_sec=ALIGN_HOP,
               min_corr=ALIGN_MIN_CORR):
    """返回 (偏移秒, 相关系数)：conv 相对 src 差多少（正数=conv 晚）。

    只比包络、只在 ±max_sec 内找、相关太低就返回 0 并说明（调用方据此决定
    要不要信这个偏移）。两条轨长度不一样时按较短的一边比，不做拉伸。
    """
    import numpy as np

    hop = max(1, int(round(hop_sec * sr)))
    e1, e2 = _env(src, hop), _env(conv, hop)
    n = min(len(e1), len(e2))
    if n < 8:
        return 0.0, 0.0
    e1, e2 = e1[:n], e2[:n]
    e1 = e1 - e1.mean()
    d1 = float(np.sqrt((e1 * e1).mean()))
    if d1 < 1e-9:
        return 0.0, 0.0          # 这一段几乎是静音，对不上也不该对
    maxlag = int(round(max_sec / hop_sec))
    best_c, best_l = -2.0, 0
    for L in range(-maxlag, maxlag + 1):
        a0, a1 = max(0, -L), n - max(0, L)
        if a1 - a0 < 8:
            continue
        x = e1[a0:a1]
        y = e2[a0 + L:a1 + L]
        dy = float(np.sqrt((y * y).mean()))
        if dy < 1e-9:
            continue
        c = float((x * y).mean() / (float(np.sqrt((x * x).mean())) * dy))
        if c > best_c:
            best_c, best_l = c, L
    if best_c < min_corr:
        return 0.0, best_c
    return best_l * hop_sec, best_c


def _fade_runs(out, sr, spans, fade_sec=FADE_SEC):
    """给输出里每一段连续有声区做首尾淡入淡出。

    ★ 区间**由放置时记下的 [pa,pb] 算**，不能靠"样本非零"去扫：音频本身到处
      过零，锯齿波 12 万样本能被切成 576 "段"（实测第一批就是这么错的）。
    ★ 只在整段有声区的边界做，不在每个 part 的接缝做：同一个人相邻两句如果
      从同一个窗口取回来，它们本来就是连着的，逐句淡入淡出等于在中间挖一个
      5ms 的坑（听起来像卡了一下）。
    """
    import numpy as np

    if not spans:
        return 0
    nf = max(1, int(round(fade_sec * sr)))
    sp = sorted(spans)
    merged = [list(sp[0])]
    for a, b in sp[1:]:
        if a <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    n_run = 0
    for a, b in merged:
        a = max(0, int(a))
        b = min(len(out), int(b))
        m = min(nf, (b - a) // 2)
        if m < 2:
            continue
        out[a:a + m] *= np.linspace(0.0, 1.0, m, dtype="float32")
        out[b - m:b] *= np.linspace(1.0, 0.0, m, dtype="float32")
        n_run += 1
    return n_run


def scatter(conv_wav, mapping, duration, out_wav, align=True):
    """把转换结果按 mapping 放回**原时间轴**，其余置零。

    ★ 只取 `parts`（这个人真正要唱的几句）；窗口多出来的上下文丢掉 ——
      它们只是给模型垫的，进成品只会带来边缘杂音。

    conv 的长度可能和拼出来的输入差几个样本（RVC 切片会丢边界），所以按
    比例 `scale` 校正一圈，不让误差累积成"越往后越偏"。

    `align=True` 时再按**包络**逐窗口把 conv 对回源轨（修 RVC 输出固定早
    10~20 毫秒那个偏差）。这一层是"多条角色轨能不能叠在一起"的关键：
    差 10ms 听不出是错位，只听到"有点糊"。
    """
    import numpy as np
    import soundfile as sf

    y, sr = sf.read(conv_wav, dtype="float32", always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)
    n_out = int(round(max(0.0, float(duration)) * sr))
    out = np.zeros(n_out, dtype="float32")
    conv_dur = len(y) / float(sr)
    src_dur = float(mapping.get("dur") or 0.0)
    # 输入被切成好几截拼起来，conv 应该是同一条时间轴；RVC 切片会丢掉边界几个
    # 样本，所以差一点点就按比例拉回去，不让误差累积成"越往后越偏"。
    # ★ 这里是**比例**而不是"差 2% 就不校正"：拼接轨才 20 多秒，2% 就是半秒，
    #   那个阈值反而会把偏的当没偏。偏差多大都如实报给调用方（drift），
    #   由它决定要不要提醒 —— 这里只管把时间轴对齐。
    scale = (conv_dur / src_dur) if src_dur > 0.01 else 1.0

    # 源轨只读一次（对齐要用；不传就当不对齐，绝不让它成为必需条件）
    src = None
    if align and mapping.get("src"):
        try:
            src, s_sr = sf.read(mapping["src"], dtype="float32", always_2d=False)
            if src.ndim > 1:
                src = src.mean(axis=1)
            if int(s_sr) != int(sr):      # 采样率不同就当没有，别错上加错
                src = None
        except Exception:
            src = None

    placed = 0
    shifts = []
    spans = []
    for w in (mapping.get("wins") or []):
        win_a = float(w["win_a"])
        # ---- 这个窗口整体该往哪挪 ----
        shift, corr = 0.0, 0.0
        if src is not None:
            i0 = max(0, int(round(float(w["out_a"]) * sr)))
            i1 = min(len(y), int(round(float(w["out_b"]) * sr)))
            j0 = max(0, int(round(float(w["win_a"]) * sr)))
            j1 = min(len(src), int(round(float(w["win_b"]) * sr)))
            if i1 - i0 > int(0.2 * sr) and j1 - j0 > int(0.2 * sr):
                m = min(i1 - i0, j1 - j0)
                shift, corr = best_shift(src[j0:j0 + m], y[i0:i0 + m], sr)
        # ★ 对齐可用时**不再乘 scale**：两者修的是同一件事（转换前后长度/内容对不上），
        #   一起用就是修两遍（实测：人为晚 10ms 会被 scale 和对齐各修一次，落回 -10ms）。
        #   对齐量的是"这一段实际差多少"，比全局比例更准；没有源轨或对不上时才退回比例。
        here_aligned = corr >= ALIGN_MIN_CORR
        mul = 1.0 if here_aligned else scale
        base = float(w["out_a"]) * mul
        shifts.append(round(shift, 4))
        for a, b in (w.get("parts") or []):
            a, b = float(a), float(b)
            # 这段在窗口内的偏移（窗口是含上下文的，所以要从 win_a 起算）
            off = a - win_a
            s = int(round((base + off * mul + shift) * sr))
            e = int(round((base + (off + (b - a)) * mul + shift) * sr))
            s = max(0, min(s, len(y)))
            e = max(0, min(e, len(y)))
            if e <= s:
                continue
            pa = int(round(a * sr))
            pb = int(round(b * sr))
            pa = max(0, min(pa, n_out))
            pb = max(0, min(pb, n_out))
            if pb <= pa:
                continue
            piece = y[s:e]
            if len(piece) < (pb - pa):
                piece = np.concatenate(
                    [piece, np.zeros((pb - pa) - len(piece), dtype="float32")])
            out[pa:pb] = piece[:pb - pa]
            spans.append((pa, pb))
            placed += (pb - pa)
    runs = _fade_runs(out, sr, spans)
    sf.write(out_wav, out, sr, subtype="PCM_16")
    return {"sr": sr, "dur": n_out / float(sr), "samples": placed,
            "scale": scale, "drift": conv_dur - src_dur,
            "shifts": shifts, "runs": runs,
            "aligned": bool(src is not None)}


def silent_track(duration, out_wav, sr=44100):
    """一句都没分到 ⇒ 给一条同长的静音轨（下游拼接照常能跑）。"""
    import numpy as np
    import soundfile as sf

    n = int(round(max(0.0, float(duration)) * sr))
    sf.write(out_wav, np.zeros(n, dtype="float32"), sr, subtype="PCM_16")
    return {"sr": sr, "dur": n / float(sr), "samples": 0}


def should_use(takes, who, duration, ctx=DEFAULT_CTX):
    """(用不用, 理由, 明细) —— 省不了就明说为什么，别闷声退回。

    明细给的是"实唱多久 / 要转多久"，写进日志，用户能自己判断值不值。
    """
    segs = takes_of(takes, who)
    if not segs:
        return False, "这个角色一句都没分到", {"sing": 0.0, "conv": 0.0, "wins": 0}
    sing = sum(b - a for a, b in segs)
    if duration > 0 and sing > duration * MAX_SHARE:
        return False, ("分到 %.0f 秒（全曲 %.0f 秒的 %.0f%%）超过 %.0f%%，"
                       "省不了多少 —— 按整首转"
                       % (sing, duration, sing / duration * 100, MAX_SHARE * 100)), \
            {"sing": sing, "conv": 0.0, "wins": 0}
    wins = plan_windows(merge_segments(segs), duration, ctx)
    conv = sum(w["win"][1] - w["win"][0] for w in wins)
    d = {"sing": sing, "conv": conv, "wins": len(wins)}
    if sing > 0.01 and conv > sing * MAX_BLOWUP:
        return False, ("段太碎：实唱 %.0f 秒，却要转 %.0f 秒（%.1f 倍，全是上下文"
                       "在充数）—— 按整首转" % (sing, conv, conv / sing)), d
    return True, ("实唱 %.0f 秒 ⇒ 只转 %.0f 秒（省 %.0f%%）"
                  % (sing, conv, (1 - conv / max(duration, 0.01)) * 100)), d
