# -*- coding: utf-8 -*-
r"""歌词规划：两件**不需要模型**的活 —— ① 用正确歌词订正识别错字；② 按乐句切块。

本模块**刻意只用标准库**（difflib / re），不碰 torch、不碰 faster-whisper。
理由：这两件事是纯字符/纯时间轴运算，门禁要能在几秒内反复跑它们，
一旦挂上重依赖就只能"在真环境里偶尔跑一次" —— 那等于没有门禁。

--------------------------------------------------------------------------
① 用「已知的正确歌词」去修**识别结果**（ASR）的错字 —— 时间戳不动，只换文本。

为什么必须有这一段（实测，不是听说）：
  改词那条链上，`ref_text`（原词）是要送进模型去音素化的。官方
  `align_lrc_sentence_level()` 会把 `|` 换成空格、把所有标点归一成逗号，
  也就是说**句内的时序不由文本决定**，文本只提供"唱哪些音素"。
  ⇒ 原词里有一个错字，模型学到的就是错的音素，唱出来就是错的词。
  而 faster-whisper 在**歌唱**上的字错率是肉眼可见的（本机缓存里
  "热情劫强已经升级" 这种），靠调扩散步数是救不回来的。

所以正确做法是：歌词这种东西，**网上搜到的正确版本往往就摆在那儿**，
拿它跟识别结果对齐一下，把文本换成正确的那一份，时间戳继续用识别给的。

这个模块**刻意不依赖 torch / faster-whisper**（只用标准库 difflib），
因为它是纯字符活儿：门禁要能在几秒钟内把它跑完，不能拉模型。

对外就一个函数：`align_ref(asr_segments, ref_text) -> (segments, report)`

对齐分两段，先句后字：
  ① 句级：把「识别的句子序列」和「参考的句子序列」配一次，
     得出 equal / replace / delete / insert 四种块。
  ② 字级：只在 replace 块里做 —— 把块内的识别句拼成一串、参考句拼成一串，
     再配一次字符，建立 识别字符 → 参考字符 的映射，最后按每个识别句
     占的字符区间，从参考串里切回它该有的那一段。
     标点和空格不参与比较（只留汉字/字母/数字），但**切回来时是带标点的**
     （保留参考原文的标点，模型那头反正会把标点归一成逗号）。

为什么不用"句数相同就一一对应"这种省事做法：
  识别会把一句断成两句、也会把两句并成一句（换气、哼唱都会影响），
  而且参考歌词里常常有"副歌重复段"标记。句数对不上是常态，
  一一对应会整段错位 —— 那就比不修更糟。
"""
import difflib
import re

# 参与比较的字符：汉字 / 片假名平假名 / 字母 / 数字。
# 标点、空格、**不参与比较**，这样"识别漏了标点"不会被判成错字。
_KEEP = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")

# 参考歌词里这些行不是歌词，要剔掉：段落标记、空行、纯标点行
_SECTION = re.compile(r"^\s*[\[\(（【【]?\s*(?:verse|chorus|bridge|intro|outro|pre-?chorus"
                      r"|主歌|副歌|间奏|前奏|尾奏|桥段|过门|rap|hook)"
                      r"\s*\d*\s*[\]\)）】]?\s*[:：]?\s*$", re.I)


def norm_map(text):
    """归一化文本：返回 (归一化串, [归一化第 i 个字符在原文中的下标])。

    归一化只做一件事 —— 把不参与比较的字符删掉，并记下"删之前它在哪"，
    这样后面从参考原文里切片段时能切回带标点的原始样子。
    """
    chars, idx = [], []
    for i, ch in enumerate(text or ""):
        if not _KEEP.match(ch):
            chars.append(ch)
            idx.append(i)
    return "".join(chars), idx


def split_ref_lines(ref_text):
    """把一整坨参考歌词切成「句」。

    能断的就三种：换行、`|`、以及句末标点后跟着的空档。
    段落标记（[副歌] 之类）直接丢 —— 它不是词，留着会被当成一句唱进去。
    """
    raw = (ref_text or "").replace("\r\n", "\n").replace("\r", "\n")
    raw = raw.replace("|", "\n")
    out = []
    for ln in raw.split("\n"):
        s = ln.strip()
        if not s:
            continue
        if _SECTION.match(s):
            continue
        # 句末标点之后如果还拖着内容，就再断一刀（一行里写了两句的情况）
        parts = re.split(r"(?<=[。！？!?；;])", s)
        for p in parts:
            p = p.strip().strip("，,、：:")
            if p:
                out.append(p)
    return out


def _align_chars(a_norm, b_norm):
    """字符级对齐：返回长度 len(a_norm) 的数组，第 i 项是它在 b_norm 中的下标
    （找不到就填 -1）。

    匹配块内部是线性映射；块与块之间的"夹缝"用**就近插值**补齐 ——
    不补齐的话，识别错得比较厉害的那个字会拿到 -1，整句文本就断在中间。
    """
    m = [-1] * len(a_norm)
    sm = difflib.SequenceMatcher(None, a_norm, b_norm, autojunk=False)
    for ai, bi, n in sm.get_matching_blocks():
        for k in range(n):
            m[ai + k] = bi + k
    # 夹缝插值：用前一个已知映射线性外推；开头那段用后一个已知映射回推。
    last_known = None
    for i, v in enumerate(m):
        if v >= 0:
            if last_known is None and v > 0:
                # 开头夹缝：把 a[:i] 摊到 b[:v] 上（按比例，可能重复取值但不会断）
                for j in range(i):
                    m[j] = int(j * v / max(1, i))
            last_known = (i, v)
        elif last_known is not None:
            m[i] = min(len(b_norm) - 1, last_known[1] + (i - last_known[0]))
    # 结尾夹缝
    if m and m[-1] < 0 and last_known is not None:
        li, lv = last_known
        tail = len(b_norm) - 1 - lv
        span = max(1, len(a_norm) - 1 - li)
        for i in range(li + 1, len(a_norm)):
            m[i] = min(len(b_norm) - 1, lv + int(tail * (i - li) / span))
    return m


def align_ref(asr_segments, ref_text):
    """把识别结果（带时间戳）的文本换成参考歌词里的文本。

    参数
      asr_segments: [{"start":float,"end":float,"text":str}, ...]
      ref_text:    正确歌词原文（随便什么分隔：换行 / `|` / 一行两句都行）

    返回 (segments, report)
      segments: 同结构，start/end **原样不动**，text 换成参考的那份；
                实在配不上的句子保留识别原文，并在 report 里点出来。
      report:   给日志/界面看的字典 —— 改了多少句、有多少句没配到、
                参考里有多少句没用上（通常是识别漏了，值得提示用户）。
    """
    segs = [dict(x) for x in (asr_segments or [])]
    ref_lines = split_ref_lines(ref_text)
    rep = {
        "ref_lines": len(ref_lines),
        "asr_lines": len(segs),
        "changed": 0,
        "kept": 0,
        "low_conf": 0,
        "unmatched_ref": [],
    }
    if not segs or not ref_lines:
        rep["ok"] = False
        return segs, rep

    a_norm = [norm_map(x.get("text", ""))[0] for x in segs]
    b_norm = [norm_map(x)[0] for x in ref_lines]
    # 全空的句子（哼唱、纯语气）不参与句级匹配，否则会把参考句吸过去
    sm = difflib.SequenceMatcher(None, a_norm, b_norm, autojunk=False)

    used_ref = set()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            # 完全一样：直接用参考原文（顺手把繁体/标点统一成参考那份）
            for k in range(i2 - i1):
                # "equal" 是**归一化后**相等：文本可能还差个标点/繁简，
                # 那也该算"订正过"，所以按原文比，别一律记成 changed。
                old = segs[i1 + k].get("text", "")
                segs[i1 + k]["text"] = ref_lines[j1 + k]
                used_ref.add(j1 + k)
                rep["changed" if ref_lines[j1 + k] != old else "kept"] += 1
        elif tag == "replace":
            a_join = "".join(a_norm[i1:i2])
            # 把这一段参考句拼成一整串，同时记下"归一化后的第 i 个字符落在原文的哪一格"
            # —— 后面按这个坐标从原文里切片段，切出来的才是带标点的、用户写的那份。
            b_full = "\n".join(ref_lines[j1:j2])
            b_chars, b_pos = [], []
            off = 0
            for j in range(j1, j2):
                n, mp = norm_map(ref_lines[j])
                for t, p in enumerate(mp):
                    b_chars.append(n[t])
                    b_pos.append(off + p)
                off += len(ref_lines[j]) + 1
            m = _align_chars(a_join, "".join(b_chars))
            # 每句在 a_join 里的字符区间
            start = 0
            spans = []
            for i in range(i1, i2):
                spans.append((start, start + len(a_norm[i])))
                start += len(a_norm[i])
            for k, (s0, s1) in enumerate(spans):
                if s1 <= s0:
                    continue
                b0 = m[s0]
                b1 = m[s1 - 1]
                if b0 < 0 or b1 < 0 or b1 < b0:
                    # 一个字都没配上 ⇒ 同样是"不敢替换"，也要记成 low_conf，
                    # 否则界面/日志上看不出"这位置没订正"和"原本就没错"的区别。
                    rep["low_conf"] += 1
                    rep["kept"] += 1
                    continue
                frag = b_full[b_pos[b0]:b_pos[b1] + 1]
                # 片段可能跨了参考的两句（中间夹着拼接用的换行）⇒ 摊平成空格。
                # 模型那头会把 `|` 换成空格、把标点归一成逗号，留个换行进去
                # 等于送了个它没见过的分隔符。
                frag = re.sub(r"\s+", " ", frag).strip().strip("，,、：:。！？!?；;")
                if not frag:
                    rep["kept"] += 1
                    continue
                # ★ 相似度闸门：认出来的和参考差得太远（<34% 的字对得上），
                #   说明这一个位置很可能是"识别在瞎编 / 参考顺序不对"，
                #   这时候**绝不硬替换** —— 把一段完全不相干的词按到这条时间上，
                #   比留着原本的错字更糟（错字只是那一个字错，硬换是整句错）。
                old_txt = segs[i1 + k].get("text", "")
                sim = difflib.SequenceMatcher(
                    None, norm_map(old_txt)[0], norm_map(frag)[0]).ratio()
                if sim < 0.40:
                    rep["low_conf"] += 1
                    rep["kept"] += 1
                    continue
                if frag != old_txt:
                    rep["changed"] += 1
                else:
                    rep["kept"] += 1
                segs[i1 + k]["text"] = frag
            for j in range(j1, j2):
                used_ref.add(j)
        elif tag == "delete":
            # 识别有、参考没有 —— 保持原样（多半是哼唱/语气词/重复段的即兴）
            for i in range(i1, i2):
                rep["kept"] += 1
        elif tag == "insert":
            # 参考有、识别没有 —— 这几句没能落到时间轴上，要点出来
            for j in range(j1, j2):
                rep["unmatched_ref"].append(ref_lines[j])

    rep["unmatched_ref_n"] = len(rep["unmatched_ref"])
    rep["ref_used"] = len(used_ref)
    rep["ok"] = True
    rep["line"] = "|".join(x.get("text", "") for x in segs)
    return segs, rep


def preview_diff(asr_segments, ref_text, limit=12):
    """给日志/界面用的一句话对照（前 limit 句），一眼能看出改对了什么。"""
    segs, rep = align_ref(asr_segments, ref_text)
    out = []
    for k, (a, b) in enumerate(zip(asr_segments or [], segs)):
        if k >= limit:
            out.append("  …共 %d 句" % len(segs))
            break
        ta = (a or {}).get("text", "")
        tb = b.get("text", "")
        flag = "  " if ta == tb else "→ "
        out.append("  %s%6.1f~%6.1f  %s%s" % (flag, b.get("start", 0), b.get("end", 0),
                                              ta, ("  =>  " + tb) if ta != tb else ""))
    return out, rep


# ---------------------------------------------------------------------------
# ② 按**乐句**切块（不是按等时长）
#
# 为什么这比"每 28 秒切一刀"重要（源码 + 实测两方面的理由）：
#   · 源码层面：官方 `prepare_input()` 把这一块的音频**同时**当音色参考
#     （ref_audio）和旋律参考（melody_audio）。从半句中间起刀，模型拿到的
#     旋律参考就是"半句残片 + 静音"，它只能靠猜把这句补完 —— 听感就是
#     跑调/拖拍，也就是用户说的"乱吼"。
#   · 而且 `align_lrc_sentence_level()` 里 `|` 是**被换成空格**的：文本并不
#     携带句内时序，时序全靠音频本身给。音频切坏了，文本再准也没用。
# ---------------------------------------------------------------------------

def plan_chunks(segs, total, sr, chunk_sec=28.0, gap_sec=2.0,
                pre_pad=0.25, post_pad=0.45):
    """按乐句切块。返回 [(起始采样, 结束采样, [句下标...]), ...]。

    分组规则：
      · 句与句的空档 > gap_sec ⇒ 一定是换气/间奏，在这里断最安全；
      · 一段攒到 chunk_sec ⇒ 也断（显存 + "单次别太长"，官方示例也是短片段）；
      · 块首尾各留一点余量（pre/post pad），别把字头字尾切掉。
    """
    if not segs:
        return []
    ss = sorted(segs, key=lambda x: float(x.get("start", 0.0)))
    groups = []
    cur = [0]
    for i in range(1, len(ss)):
        prev_end = float(ss[cur[-1]].get("end", ss[cur[-1]].get("start", 0.0)))
        st = float(ss[i].get("start", 0.0))
        g_start = float(ss[cur[0]].get("start", 0.0))
        g_end = float(ss[i].get("end", st))
        if st - prev_end > gap_sec or g_end - g_start > chunk_sec:
            groups.append(cur)
            cur = [i]
        else:
            cur.append(i)
    groups.append(cur)

    out = []
    for gi, g in enumerate(groups):
        f_start = float(ss[g[0]].get("start", 0.0))
        l_end = float(ss[g[-1]].get("end", ss[g[-1]].get("start", 0.0)))
        # ★ 余量必须**让着邻居**：连续演唱时句与句之间常常只隔 0.1~0.3 秒，
        #   死板地往前 0.25 秒、往后 0.45 秒，就会啃进上一句/下一句 ——
        #   那等于"边界切在句子里"，正是这个算法要消灭的东西。
        #   （真数据上抓到过：25 句的歌里 6 处被啃。）
        prev_i = g[0] - 1
        if prev_i >= 0:
            prev_end = float(ss[prev_i].get("end", ss[prev_i].get("start", 0.0)))
            pad_pre = min(pre_pad, max(0.0, f_start - prev_end) * 0.5)
        else:
            pad_pre = pre_pad
        next_i = g[-1] + 1
        if next_i < len(ss):
            n_start = float(ss[next_i].get("start", 0.0))
            pad_post = min(post_pad, max(0.0, n_start - l_end) * 0.5)
        else:
            pad_post = post_pad
        pad_pre = min(pad_pre, max(0.0, f_start))
        s = int(max(0.0, f_start - pad_pre) * sr)
        e = int(min(total, (l_end + pad_post) * sr))
        if e <= s:
            e = min(total, s + int(sr * 0.5))
        if e - s < int(sr * 0.6):
            continue
        out.append((s, e, g))
    return out


def slice_segments(segs, t_start, dur):
    """把**整首**的时间戳切成"片段内"的坐标 —— 试听小样必须用它。

    为什么：小样是从整首的第 35% 处裁一段出来的，而歌词时间戳是**整首的**绝对
    时间。以前直接把整首的时间戳交给改词，片段内一条都对不上 ⇒ 每一段都被判成
    间奏 ⇒ 改词在小样上**等于没做**（成品听着就是原词，用户还会以为是自己写的
    新词没生效）。裁了音频就必须裁时间戳，两件事一直是一件事。

    返回 (片段内的句, 落在片段外的句数)。
    """
    if not segs:
        return [], 0
    # dur 不给 = 截到结尾（不给就把整段判成"在窗外"，那是静默失效，最坏的一种）
    t_end = None if dur is None else (t_start or 0.0) + dur
    inside, outside = [], 0
    for x in segs:
        try:
            st = float(x.get("start"))
            en = float(x.get("end") if x.get("end") is not None else st)
        except Exception:
            outside += 1
            continue
        if en <= (t_start or 0.0) or (t_end is not None and st >= t_end):
            outside += 1
            continue
        inside.append({
            "start": round(max(0.0, st - (t_start or 0.0)), 3),
            "end": round((en if t_end is None else min(t_end, en))
                         - (t_start or 0.0), 3),
            "text": x.get("text") or "",
        })
    return inside, outside


def clean_segments(segments, min_len=0.05):
    """把用户校对过的分句**洗干净**再落盘 —— 宁可拒，不可存坏数据。

    洗什么：
      · 去掉没有文本 / 时间非法的条目；
      · `|` 是句子分隔符，混进文本会让句数算错 ⇒ 换成全角斜杠；
      · end 早于 start 的（手滑填反）拉回 start+min_len；
      · 按 start 排序。

    ★ `zh`（中文翻译）跟着句子一起存 —— 双语字幕就靠它。
      它是**可选**的：没有就整条不写这个键（老数据、单语歌词都还是老样子），
      有就洗一遍（同样不许带 `|`）。空串也照样不写，免得每条都挂个空键。

    返回 (干净的列表, 被丢弃的条数)。调用方拿"丢弃数"去提示用户。
    """
    clean, dropped = [], 0
    for x in (segments or []):
        if not isinstance(x, dict):
            dropped += 1
            continue
        txt = str(x.get("text") or "").strip().replace("|", "／")
        try:
            st = float(x.get("start"))
            en = float(x.get("end"))
        except Exception:
            dropped += 1
            continue
        if st < 0 or en < 0 or not txt:
            dropped += 1
            continue
        if en < st:
            en = st + min_len
        item = {"start": round(st, 3), "end": round(max(en, st + min_len), 3),
                "text": txt}
        zh = str(x.get("zh") or "").strip().replace("|", "／")
        if zh:
            item["zh"] = zh
        clean.append(item)
    clean.sort(key=lambda x: x["start"])
    return clean, dropped


def build_timeline(plan, total):
    """把"要重唱的块"拼成覆盖**整条时间轴**的时间线，空档标成 None（= 间奏直通）。

    直通而不是静音：间奏里常常有和声/哼唱（只是没被识别出词），全填 0 会把
    这些也抹掉，听起来像被剪了一刀；直通最多是"这段没改词"，而硬唱是"毁了"。
    """
    timeline = []
    cursor = 0
    for (s, e, idx) in sorted(plan, key=lambda x: x[0]):
        if s > cursor:
            timeline.append((cursor, s, None))
        timeline.append((max(s, cursor), e, idx))
        cursor = max(cursor, e)
    if cursor < total:
        timeline.append((cursor, total, None))
    return timeline
