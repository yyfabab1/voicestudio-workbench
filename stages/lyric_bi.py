# -*- coding: utf-8 -*-
r"""双语歌词：**一次粘进来，自己对着歌贴到该在的位置**。

用户要的是这个动作：
    把整首歌词（原文 + 中文）一次性粘进一个框 → 点一下 →
    每一句自己落到歌里第几秒到第几秒。

所以这里只干三件事，且**全部只用标准库**（门禁几秒钟就能验一遍，
不用真起服务、不用真跑模型）：

  ① parse_bilingual(text)  认出用户粘的是哪种排版，拆成 [(原词, 中文)]
  ② place(asr, pairs)      把每一句挂到歌里的第几秒到第几秒
  ③ 报告挂不上的句子        —— ★ 绝不静默丢句子

★ 三条不许碰的红线
  1. 时间戳只能来自 ASR（歌自己）或用户给的 LRC，**不许按句数均分**。
     均分会把前奏/间奏算进去，词就整体飘了 —— 这正是用户抱怨"对不上"的来源。
  2. 挂不上的句子要**列出来说是挂不上**，不能塞一个看着合理的时间假装贴好了。
  3. 相似度低的句子照样给时间，但要标 `low_conf` —— 时间八成是对的（位置是按
     顺序排的），只是"这句词和听到的对不上"，两件事别混为一谈。

排版识别支持这几种（按优先级）：
  lrc     `[00:12.34]原词` （可两行同时间戳 = 原文 + 中文）；有时间戳就不用 ASR
  inline  一行里 `原词 | 中文`（也认 ｜ // ／ tab 等分隔符）
  alt     奇数行原文、偶数行中文
  block   空行（或 --- 分隔线）分成前后两坨，句数相同
  plain   全是原文，没有中文
"""

import difflib
import re

from lyric_plan import _align_chars, _SECTION, norm_map

# LRC 时间戳：`[00:12.34]` / `[00:12]` / `[1:02.5]`，冒号后允许小数点是
# 有些网站写成 `[00:12.345]`、也有写成 `[00:12:34]`（百分秒）的。
_LRC = re.compile(r"^\s*\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]\s*(.*)$")
_CJK = re.compile(r"[\u4e00-\u9fff]")

# 行内分隔符，从"最不像歌词内容"的往下试。
# ★ ` / ` 放最后且要求两侧都得有字：英文歌名/网址里带斜杠太常见了。
_IN_SEPS = ("｜", "|", "\t", "‖", " // ", "／", "／", " ┃ ", "  ", " — ", " —— ",
            " -- ", "  /  ", " / ")

# 分隔线：用户用一行 --- 或 === 把原文和中文分开
_RULE = re.compile(r"^\s*[-=—═]{3,}\s*$")


# ---------------------------------------------------------------------------
# ① 排版识别
# ---------------------------------------------------------------------------

def _cjk_ratio(s):
    """这串字里汉字的比例 —— 用来判断"哪一半是中文"。"""
    s = (s or "").strip()
    if not s:
        return 0.0
    return len(_CJK.findall(s)) / float(len(s))


def _which_is_chinese(a, b):
    """这两坨里哪一坨是中文？返回 "a" / "b" / None（None = 分不出来）。

    ★★ 全项目**只有这一个地方**定义"谁是大字"，别在别处再写一份：
      以前 `_pair_sides`（逐句的）和 `_by_layout(alt)`（隔行的）各写了一份，
      口径慢慢漂就漂成了两套，最后两边一起写成死分支也没人发现。
    """
    ra, rb = _cjk_ratio(a), _cjk_ratio(b)
    if rb > ra + 0.10:
        return "b"
    if ra > rb + 0.10:
        return "a"
    return None


def _pair_sides(a, b, guess=False):
    """两句里谁放大字（原文）、谁放小字（中文）。

    ★★ `guess` 这个开关是拿一次真回归换来的教训，别再把它简化掉：

      `guess=False`（默认）—— **尊重书写顺序**。凡"界面写了顺序约定"的排版
        （inline 的 `原词 | 中文`、LRC 同时间戳的上下两行）一律走这条。
      `guess=True`      —— 按"汉字多的那一半是中文"猜。**只有顺序本来就没有
        约定的场合**（自动认出来的隔行 alt）才准用。

      为什么不能到处猜：**中文歌的原文自己就是汉字**。实测 counterexample：
        `捍卫神族的荣誉 | 捍卫神族的荣誉zh`
      左边是原词（纯汉字），右边是译文（汉字 + 两个拉丁字母）⇒ 按"汉字多的是
      中文"一猜就把原词和译文调了个个儿。中文歌在本项目里占一大半，这条猜等于
      系统性错位 —— 而且不报错、不崩，画面上每句都有词，只是全都反了。

    ⚠ 这里以前是两个分支都 `return a, b`（if 里和外一字不差），
      等于**这个函数从来没生效过**：不管谁在前，永远按"先来的是原文"。
      症状是老板说的「导入文字全部放在英语位置，中文也一起」——
      两分支写成一样是最阴的一种：不报错、不崩、看着也像有逻辑。
    """
    if not guess:
        return a, b
    # "a 才是中文" ⇒ 掉个个儿；b 是中文或判不出来 ⇒ 顺序本来就是对的
    return (b, a) if _which_is_chinese(a, b) == "a" else (a, b)


def _flip(pairs):
    """把每一对的原文/中文互换（`-rev` 排版用）。

    ★ 只对**两边都有字**的行生效：一边为空（比如只认出原文的行）翻过来会把
      空串变成"原文"，那一行就白白变成看不见的空句 —— 比不翻更糟。
    """
    out = []
    for p in pairs or []:
        o, z = p.get("orig") or "", p.get("zh") or ""
        if o and z:
            out.append({"orig": z, "zh": o, "t": p.get("t")})
        else:
            out.append(p)
    return out


def _looks_reversed(pairs):
    """这批 (原文, 中文) 看着像是写反了（中文在左）？

    只用来**给提示**，绝不用来悄悄调换 — 见 `_pair_sides` 那段：猜是错的，
    中文歌的原文自己就是汉字。这里只在"左边汉字明显多于右边"时报一次，
    让用户自己去勾「中文在左」。
    """
    ps = [p for p in (pairs or []) if (p.get("orig") or "") and (p.get("zh") or "")]
    if len(ps) < 2:
        return False
    ra = sum(_cjk_ratio(p["orig"]) for p in ps) / float(len(ps))
    rb = sum(_cjk_ratio(p["zh"]) for p in ps) / float(len(ps))
    return (ra - rb) > 0.25


def _split_inline(line):
    """把 `原词 | 中文` 拆成两句。拆不成（或拆出来有空的一半）返回 None。"""
    for sep in _IN_SEPS:
        if sep in line:
            i = line.find(sep)
            a = line[:i].strip()
            b = line[i + len(sep):].strip()
            if a and b and len(a) < 200 and len(b) < 200:
                return (a, b) if sep != "  " else (a, b)
    return None


def _lrc_sec(mm, ss, frac):
    mm = int(mm)
    ss = int(ss)
    if frac:
        # 位数不定：`.34` 是百分秒，`.345` 是毫秒
        return mm * 60 + ss + int(frac) / (10.0 ** len(frac))
    return mm * 60 + ss


def _blocks_of(lines):
    """按空行 / 分隔线把文本切成几坨（段落标记不算一坨）。"""
    blocks, cur = [], []
    for ln in lines:
        if ln and not _RULE.match(ln):
            if _SECTION.match(ln):
                continue
            cur.append(ln)
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    return blocks


def split_layout(layout):
    """把 `inline-rev` 拆成 ("inline", True)。用户显式说"中文在左"就走 `-rev`。

    ★ 为什么要这个而不是"自动猜出来"：见 `_pair_sides` 那段 —— 中文歌的原文
      自己就是汉字，猜 = 系统性错位。宁可让用户点一下，也别悄悄猜。
      界面上这两个是把同一件事的两个方向摆出来让用户挑，成本只有一次点击。
    """
    lay = (layout or "auto").strip()
    if lay.endswith("-rev"):
        return lay[:-4], True
    return lay, False


def _by_layout(lines, body, layout, strict=True):
    """按指定排版拆成 pairs；这个排版不适用就返回 None（让调用方继续试下一个）。

    ★ plain 永远返回非空（兜底：整份都当原文）。
    ★ `layout` 可以是 `<排版>-rev`（中文在左），见 `split_layout`。
    """
    base, rev = split_layout(layout)

    if base == "plain":
        return [{"orig": x, "zh": "", "t": None} for x in body]

    if base == "inline":
        hits = [_split_inline(ln) for ln in body]
        if sum(1 for h in hits if h) < max(2, int(len(body) * 0.5)):
            return None
        pairs = []
        for ln, h in zip(body, hits):
            if h:
                # ★ 不猜：界面上写的是「原词 | 中文」，左右顺序就是契约。
                #   （猜的话，中文歌的原文自己就是汉字 ⇒ 整首词反着贴。）
                #   粘反了请用排版里的「一行：中文 | 原词」。
                a, b = _pair_sides(*h)
            else:
                a, b = ln, ""
            pairs.append({"orig": a, "zh": b, "t": None})
        return _flip(pairs) if rev else pairs

    if base == "block":
        blocks = _blocks_of(lines)
        if len(blocks) != 2 or len(blocks[0]) != len(blocks[1]) \
                or len(blocks[0]) < 2:
            return None
        pairs = [{"orig": a, "zh": b, "t": None}
                 for a, b in zip(blocks[0], blocks[1])]
        return _flip(pairs) if rev else pairs

    if base == "alt":
        # ★ 自动认 alt 的判据必须**严**：只有奇偶两侧汉字比例明显不同才敢认，
        #   否则中文歌（两边都是汉字）会被劈成"原文/中文"两半，整首词全错位。
        #   手动指定时（`strict=False`）不判 —— 用户自己说了是隔行，那就是隔行。
        if len(body) < 4 or len(body) % 2:
            return None
        ea, eb = body[0::2], body[1::2]
        ra = sum(_cjk_ratio(x) for x in ea) / float(len(ea))
        rb = sum(_cjk_ratio(x) for x in eb) / float(len(eb))
        if strict and abs(rb - ra) <= 0.25:
            return None
        # 顺序：汉字**少**的那一半是原文（大字）。判据走全项目唯一那把尺子
        #   `_which_is_chinese`，不再自己写一份（两边各写一份正是这里出死分支的原因）。
        side = _which_is_chinese("".join(ea), "".join(eb))
        orig, zh = (eb, ea) if side == "a" else (ea, eb)
        if rev:
            orig, zh = zh, orig
        return [{"orig": a, "zh": b, "t": None} for a, b in zip(orig, zh)]

    return None


def parse_bilingual(text, layout="auto"):
    """把用户粘的一整坨文本拆成 [{"orig","zh","t"}...]，并说清认成了哪种排版。

    `t` 只有 LRC 排版才有（秒）；其余排版是 None —— 时间要等 `place()` 去贴。

    `layout` 让用户自己指定排版（"auto" 之外还有 lrc/inline/alt/block/plain，
    每种都可以加 `-rev` 表示"中文在左"，例如 `inline-rev`）。
    ★ 为什么必须能手动指定：**中文歌的原文也是汉字**，自动判据（靠汉字比例
      分辨哪一半是中文）在中文歌上必然失灵，那时候只能由用户说一句
      「我是隔行写的」。自动判不出来还硬猜 = 整首词错位，是最坏的一种失效。
    ★ `out["note"]` 里会报"看着像写反了"，但**绝不自动调换**（理由同上）。
    """
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in raw.split("\n")]
    body = [ln for ln in lines if ln and not _SECTION.match(ln) and not _RULE.match(ln)]

    out = {"mode": "plain", "pairs": [], "lines": len(body),
           "lrc_n": 0, "note": ""}
    if not body:
        return out

    base, rev = split_layout(layout)

    # ---- 手动指定的排版：跳过识别，但**同样的清洗与配对规则** ----
    if base in ("alt", "block", "inline", "plain"):
        got = _by_layout(lines, body, layout, strict=False)
        out.update({"mode": layout, "pairs": got or [],
                    "note": "" if got else "这个排版没读出句子"})
        if got and base != "plain" and _looks_reversed(got):
            out["note"] = ("看着是「中文在左、原词在右」—— 已按你选的顺序排。"
                           "想反过来就在上面的「排版」里改选带「中文在左」的那一项")
        return out

    # ---- LRC：带时间戳 ----
    lrc = []
    for ln in body:
        m = _LRC.match(ln)
        if m:
            lrc.append((_lrc_sec(m.group(1), m.group(2), m.group(3)),
                        (m.group(4) or "").strip()))
    if lrc and len(lrc) >= max(2, int(len(body) * 0.5)):
        lrc.sort(key=lambda x: x[0])
        pairs = []
        # 同一个时间戳连着两行 = 原文 + 中文（LRC 双行是通行写法）
        i = 0
        while i < len(lrc):
            t, txt = lrc[i]
            if not txt:
                i += 1
                continue
            inline = _split_inline(txt)
            if inline:
                # ★ 不猜：带 `|` 的行按写的顺序（界面写的是「原词 | 中文」）
                a, b = _pair_sides(*inline)
                pairs.append({"orig": a, "zh": b, "t": t})
                i += 1
                continue
            if i + 1 < len(lrc) and abs(lrc[i + 1][0] - t) < 0.35 and lrc[i + 1][1]:
                # ★ 不猜：LRC 双行的通行写法就是"先原文、后翻译"
                a, b = _pair_sides(txt, lrc[i + 1][1])
                pairs.append({"orig": a, "zh": b, "t": t})
                i += 2
            else:
                pairs.append({"orig": txt, "zh": "", "t": t})
                i += 1
        if rev:
            pairs = _flip(pairs)
        out.update({"mode": "lrc-rev" if rev else "lrc", "pairs": pairs,
                    "lrc_n": len(lrc)})
        return out

    # ---- 其余排版按"最不像巧合"的顺序试：inline > block > alt ----
    for mode in ("inline", "block", "alt"):
        got = _by_layout(lines, body, mode)
        if got:
            note = ""
            if _looks_reversed(got):
                note = ("看着是「中文在左、原词在右」—— 已按粘的顺序排。"
                        "要反过来就在上面的「排版」里选带「中文在左」的那一项")
            # ★ 自动认出来的排版也要吃 `-rev`（用户提前知道自己是反着粘的）
            if rev:
                got = _flip(got)
            out.update({"mode": mode + ("-rev" if rev else ""), "pairs": got,
                        "note": note})
            return out
    out["pairs"] = _by_layout(lines, body, "plain") or []
    out["note"] = ("只认出原文，没认出中文 —— 想要双语请用「原词 | 中文」、"
                   "空行分成两坨，或在上面的「排版」里直接选")
    return out


# ---------------------------------------------------------------------------
# ② 对位：把每一句贴到歌里的第几秒
# ---------------------------------------------------------------------------

def _spans(items):
    """把一组句子摊平成一条字符串，返回 (拼接串, [(起,止), ...])。"""
    buf, spans, off = [], [], 0
    for s in items:
        buf.append(s)
        spans.append((off, off + len(s)))
        off += len(s)
    return "".join(buf), spans


def _idx_of(spans, pos):
    """pos 落在第几个 span 里（找不到就取最近的那个）。"""
    for i, (a, b) in enumerate(spans):
        if a <= pos < b:
            return i
    if not spans:
        return -1
    best, bd = 0, abs(pos - spans[0][0])
    for i, (a, b) in enumerate(spans):
        d = min(abs(pos - a), abs(pos - b))
        if d < bd:
            best, bd = i, d
    return best


def place(asr, pairs, min_gap=0.12, default_tail=4.0):
    """把 (原文, 中文) 一对对贴到 ASR 的时间轴上。

    asr:   [{"start","end","text"}, ...] —— 歌自己认出来的句子（带秒）
    pairs: parse_bilingual 给的 [{"orig","zh","t"}]

    返回 (segments, report)
      segments: [{"start","end","text","zh","conf"}]
      report:   {"placed","unplaced":[原文...],"low_conf":n,"asr_n","ref_n",
                 "split":n（几句共用一个识别段 ⇒ 按字数把这段时间分了）,
                 "merged":n（几句识别并成了一句）}
    """
    segs = []
    rep = {"placed": 0, "unplaced": [], "low_conf": 0,
           "asr_n": len(asr or []), "ref_n": len(pairs or []),
           "split": 0, "merged": 0, "ok": False}

    # LRC：时间本来就有，不用对位（但 end 要补 —— LRC 只给起点）
    if pairs and pairs[0].get("t") is not None:
        ts = [(float(p["t"]), p) for p in pairs if p.get("t") is not None]
        ts.sort(key=lambda x: x[0])
        total = None
        if asr:
            try:
                total = max(float(x.get("end") or 0) for x in asr)
            except Exception:
                total = None
        for i, (t, p) in enumerate(ts):
            nxt = ts[i + 1][0] if i + 1 < len(ts) else None
            if nxt is None:
                en = (total if (total and total > t) else t + default_tail)
            else:
                en = max(t + min_gap, nxt)
            segs.append({"start": round(t, 3), "end": round(en, 3),
                         "text": p.get("orig") or "", "zh": p.get("zh") or "",
                         "conf": 1.0})
        rep["placed"] = len(segs)
        rep["ok"] = bool(segs)
        return segs, rep

    if not asr or not pairs:
        rep["unplaced"] = [(p.get("orig") or "") for p in (pairs or [])]
        return segs, rep

    a_norm = [norm_map(x.get("text") or "")[0] for x in asr]
    b_norm = [norm_map(p.get("orig") or "")[0] for p in pairs]
    a_join, a_spans = _spans(a_norm)
    b_join, b_spans = _spans(b_norm)
    if not a_join or not b_join:
        rep["unplaced"] = [p.get("orig") or "" for p in pairs]
        return segs, rep

    # 参考句的每一个字 → 它在识别串里的位置（复用 lyric_plan 里那条
    # 已经过 30 多条判据的对齐，别再写一套）
    m = _align_chars(b_join, a_join)

    # 每句参考 ⇒ 覆盖哪几句识别
    owners = []          # [(i_first, i_last) 或 None]
    for j in range(len(pairs)):
        s0, s1 = b_spans[j]
        if s1 <= s0:
            owners.append(None)
            continue
        pos = [m[k] for k in range(s0, s1) if m[k] >= 0]
        if not pos:
            owners.append(None)
            continue
        owners.append((_idx_of(a_spans, min(pos)), _idx_of(a_spans, max(pos))))

    # 落在同一个识别段上的多句 ⇒ 按字数把那段时间切开
    # （识别常常把两句并成一句，这时候不切就等于第二句没有时间）
    by_first = {}
    for j, ow in enumerate(owners):
        if ow:
            by_first.setdefault(ow[0], []).append(j)
    extra_split = {}
    for i0, js in by_first.items():
        if len(js) < 2:
            continue
        st = float(asr[i0].get("start") or 0)
        en = float(asr[i0].get("end") or st)
        weights = [max(1, len(b_norm[j])) for j in js]
        tot = float(sum(weights))
        cur = st
        for k, j in enumerate(js):
            piece = (en - st) * weights[k] / tot
            extra_split[j] = (round(cur, 3), round(cur + piece, 3))
            cur += piece
        rep["split"] += len(js)

    for j, p in enumerate(pairs):
        ow = owners[j]
        txt = p.get("orig") or ""
        if ow is None and j not in extra_split:
            rep["unplaced"].append(txt)
            continue
        if j in extra_split:
            st, en = extra_split[j]
            heard = "".join(a_norm[ow[0]:ow[1] + 1]) if ow else ""
        else:
            i0, i1 = ow
            st = float(asr[i0].get("start") or 0)
            en = float(asr[i1].get("end") or st)
            heard = "".join(a_norm[i0:i1 + 1])
            if i1 > i0:
                rep["merged"] += (i1 - i0)
        if en <= st:
            en = st + min_gap
        conf = difflib.SequenceMatcher(None, norm_map(txt)[0], heard).ratio()
        if conf < 0.34:
            rep["low_conf"] += 1
        segs.append({"start": round(st, 3), "end": round(en, 3),
                     "text": txt, "zh": p.get("zh") or "",
                     "conf": round(conf, 3)})

    # 顺序 + 不许重叠：相邻两句时间交叉时，把交界挪到中间
    segs.sort(key=lambda x: x["start"])
    for k in range(1, len(segs)):
        if segs[k]["start"] < segs[k - 1]["end"]:
            mid = (segs[k - 1]["end"] + segs[k]["start"]) / 2.0
            segs[k - 1]["end"] = round(mid, 3)
            segs[k]["start"] = round(mid, 3)
    rep["placed"] = len(segs)
    rep["ok"] = bool(segs)
    return segs, rep


def describe(report):
    """把报告翻成人话（日志 / 界面共用同一份口径）。"""
    if not report.get("ok"):
        return "一句都没贴上（%s）" % (
            "这首歌还没认过词" if not report.get("asr_n") else "歌词没解析出来")
    bits = ["贴上 %d 句" % report["placed"]]
    if report.get("split"):
        bits.append("%d 句共用一个识别段 ⇒ 按字数分了时间" % report["split"])
    if report.get("merged"):
        bits.append("有 %d 句识别被并进了邻句" % report["merged"])
    if report.get("low_conf"):
        bits.append("%d 句词和听到的对不上（时间多半是对的，词你自己看一眼）"
                    % report["low_conf"])
    if report.get("unplaced"):
        bits.append("★ %d 句没贴上：%s" % (len(report["unplaced"]),
                                       "、".join(report["unplaced"][:3])
                                       + ("…" if len(report["unplaced"]) > 3 else "")))
    return "；".join(bits)
