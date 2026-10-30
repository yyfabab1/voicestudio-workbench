#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""门禁：粘进来的「原文 + 中文」要落对格子（大字原文 / 小字中文）。

历史（为什么必须有这一关）
--------------------------
分侧函数原来长这样 ——

    if rb > ra + 0.10:
        return a, b
    return a, b          ← 两个分支一字不差

不报错、不崩、看着也有逻辑，但**从来没生效过**：不管谁在前，永远按"先来的是原文"。
同一个毛病项目里同时出了**两处**（`_pair_sides` 和 `_by_layout(alt)`）。

★ 修它的路上踩了第二个坑（本关第二次重写，如实记下来）
----------------------------------------------------
第一版的修法是"到处按汉字多寡猜谁是大字"，结果被 **verify_lyrics_align 当场抓真回归**：

    捍卫神族的荣誉 | 捍卫神族的荣誉zh
    ↑ 这是**中文歌**：原文自己就是汉字（左边纯汉字），译文右边才带两个拉丁字母。
    按"汉字多的是中文"猜 ⇒ 原词和译文整个调个儿，而且每句都有词、都不报错。

⇒ **契约是这样的**（界面白纸黑字写着「② 原词 | 中文（一行一句）」）：

    · **有顺序约定的排版**（inline 的左右、LRC 双行的上下）⇒ 尊重书写顺序，**不许猜**；
    · **没有顺序约定的排版**（自动认出来的隔行 alt）⇒ 才按汉字多寡定谁是大字；
    · **中文在左**这种常见贴法 ⇒ 给**显式入口**（`inline-rev` / `alt-rev`），
      并且"看着像写反了"时**只提示、不自动改** —— 猜错 = 整首词错位，比让用户点一下糟。

这一关盯的就是上面这份契约，外加一个**这类项目的老规矩**：
    新加的入口必须**连到 indispensable 的三个环节**（后端认得 / 接口放行 / 界面显示），
    少一环它就安静地不存在。
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STAGE = os.path.join(HERE, "stages", "lyric_bi.py")
STUDIO = os.path.join(HERE, "studio.py")
SERVER = os.path.join(HERE, "server.py")
APPJS = os.path.join(HERE, "web", "viz.js")
HTML = os.path.join(HERE, "web", "index.html")

N_PASS = 0
FAILS = []

# 够长才能过 `_by_layout` 里 `len(body) >= 4` 的门槛（短文本直接返回 None，
# 会把真正的分侧逻辑整个绕过去 —— 用例太短 = 尺子没牙）。
EN = ["Twinkle twinkle little star", "How I wonder what you are",
      "Up above the world so high", "Like a diamond in the sky"]
ZH = ["一闪一闪小星星", "我多想知道你是什么", "高高挂在世界之上", "像天空中的钻石"]
CN = ["热情倔强已经升级", "风暴渐渐的距离", "捍卫神族的荣誉", "再一次站在这里"]


def check(name, ok, detail=""):
    global N_PASS
    if ok:
        N_PASS += 1
        print("  [ok] %s" % name)
    else:
        FAILS.append(name)
        print("  [!!] %s\n        %s" % (name, detail or "无诊断信息"))


def load_mod():
    # ★ lyric_bi 顶层 `from lyric_plan import ...`，而 lyric_plan 就在 stages/ 下。
    #   用 importlib 直接加载时 sys.path 里没有 stages ⇒ ModuleNotFoundError。
    _stages = os.path.dirname(STAGE)
    if _stages not in sys.path:
        sys.path.insert(0, _stages)
    spec = importlib.util.spec_from_file_location("lyric_bi_gate", STAGE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _read(path):
    try:
        return open(path, encoding="utf-8").read()
    except OSError:
        return ""


def _interleaved(a_side, b_side):
    """交叉成隔行文本：a0, b0, a1, b1, ..."""
    out = []
    for a, b in zip(a_side, b_side):
        out += [a, b]
    return "\n".join(out)


def _inline(a_side, b_side):
    return "\n".join("%s | %s" % (a, b) for a, b in zip(a_side, b_side))


# ---------------------------------------------------------------------------
# A. 源码形状：交换逻辑必须真的存在 + 猜的地方必须有界
# ---------------------------------------------------------------------------

def _body_of(src, head):
    i = src.find(head)
    if i < 0:
        return ""
    # ★ 上界取"下一个 def"，不能取 `return None` —— alt 分支第一句守卫就是
    #   `return None`，切到那儿会把交换逻辑整段切掉，判据变成"永远查不到"（假红）。
    j = src.find("\ndef ", i + 1)
    return src[i:j]


def a_shape():
    print("\nA. 源码形状：交换逻辑真的存在，且'猜'只准出现在没有约定的地方")
    src = _read(STAGE)
    check("读得到 lyric_bi.py", bool(src))
    if not src:
        return

    # ① 全项目只有一把尺子
    check("有统一的判政策函数 _which_is_chinese",
          "def _which_is_chinese(" in src)
    w = _body_of(src, "def _which_is_chinese(")
    check("它的三个取向必须真的不同（防再写成'两分支一样'）",
          '"a"' in w and '"b"' in w and "return None" in w,
          "三个取向里缺一个" )

    # ② _pair_sides 必须有交换分支，且**默认不猜**
    p = _body_of(src, "def _pair_sides(")
    check("_pair_sides 里有真正的交换分支", "(b, a)" in p)
    check("_pair_sides 默认 guess=False（有约定处不许猜）",
          "guess=False" in p and "if not guess" in p)

    # ③ inline / LRC 不许猜；alt 才猜
    lay = _body_of(src, "def _by_layout(")
    check("alt 分支两个取向不同", "(eb, ea)" in lay and "(ea, eb)" in lay)
    il = lay.find('if base == "inline":')
    il_end = lay.find('if base == "block":', il)
    inline_b = lay[il:il_end] if il >= 0 else ""
    check("★ inline 不猜（_pair_sides 调用不带 guess=True）",
          "_pair_sides(" in inline_b and "guess=True" not in inline_b,
          "inline 里又猜上了 —— 中文歌会被整首翻转")
    lrc = _read(STAGE).find("# ---- LRC：带时间戳 ----")
    lrc_b = _read(STAGE)[lrc:lrc + 1500]
    check("★ LRC 双行不猜（同理：双行约定就是'先原文后译文'）",
          "guess=True" not in lrc_b)

    # ④ 显式反向入口
    check("支持 `-rev`（中文在左）", "def split_layout(" in src
          and 'endswith("-rev")' in src)
    check("有互换但不伤害空半边的 _flip", "def _flip(" in src)


# ---------------------------------------------------------------------------
# B. 行为：按契约落位
# ---------------------------------------------------------------------------

def cases():
    """(名字, 文本, 排版, 期望的(大字, 小字))"""
    return [
        # --- 隔行：没有顺序约定 ⇒ 可以猜，大字必须是外语 -----------------
        ("alt 英文先 ⇒ 大字是英文", _interleaved(EN, ZH), "alt", (EN[0], ZH[0])),
        ("alt 中文先 ⇒ 也认得出大字是英文", _interleaved(ZH, EN), "alt", (EN[0], ZH[0])),
        ("alt-rev ⇒ 反过来（用户说了中文在上）",
         _interleaved(ZH, EN), "alt-rev", (ZH[0], EN[0])),
        # 中文歌：两边都是汉字 ⇒ 分不出来 ⇒ 按"先来的是原文"，一句都不许动
        ("alt 中文歌（两边都汉字）⇒ 保持原序", "\n".join(CN), "alt", (CN[0], CN[1])),

        # --- inline：顺序就是契约 ⇒ 尊重书写顺序 ------------------------
        ("inline 英文|中文 ⇒ 照粘的顺序", _inline(EN, ZH), "inline", (EN[0], ZH[0])),
        ("inline 中文|英文 ⇒ 照粘的顺序（不代猜）",
         _inline(ZH, EN), "inline", (ZH[0], EN[0])),
        ("inline-rev 中文|英文 ⇒ 大字是英文",
         _inline(ZH, EN), "inline-rev", (EN[0], ZH[0])),
        # ★★ 真回归的原案：中文歌的原文自己就是汉字
        ("★ inline 中文歌（原文自带 zh 后缀）⇒ 不许被翻",
         "捍卫神族的荣誉 | 捍卫神族的荣誉zh\n风暴渐渐的距离 | 风暴zh",
         "inline", ("捍卫神族的荣誉", "捍卫神族的荣誉zh")),

        # --- LRC 双行：约定就是"先原文、后译文" -------------------------
        ("LRC 双行 ⇒ 先原文后译文",
         "[00:12.34]Never gonna give you up\n[00:12.34]永远不会放弃你\n"
         "[00:16.00]Never gonna let you down\n[00:16.00]永远不会让你失望",
         "lrc", ("Never gonna give you up", "永远不会放弃你")),
    ]


def b_behavior(mod):
    print("\nB. 行为：按契约落位")
    for name, text, layout, want in cases():
        r = mod.parse_bilingual(text, layout=layout)
        ps = r.get("pairs") or []
        if not ps:
            check(name, False, "一对都没读出来（mode=%s）" % r.get("mode"))
            continue
        got = (ps[0].get("orig"), ps[0].get("zh"))
        check(name, got == want,
              "实际 大字=%r 小字=%r；期望 大字=%r 小字=%r" % (got + want))

    # ★ 写反了要**说出来**，但不许偷偷改（中文歌一猜就错，所以只能提示）
    wrong = mod.parse_bilingual(_inline(ZH, EN), layout="inline")
    ps = wrong.get("pairs") or []
    check("中文在左时不代改（照粘的顺序）",
          bool(ps) and (ps[0]["orig"], ps[0]["zh"]) == (ZH[0], EN[0]))
    check("中文在左时**提醒**用户（看完换个选项就行）",
          "中文在左" in (wrong.get("note") or ""),
          "note=%r" % wrong.get("note"))
    check("排版记的还是 inline（不是偷偷认成了别的）",
          wrong.get("mode") == "inline", wrong.get("mode"))


# ---------------------------------------------------------------------------
# C. 尺子自检：三种坏版本，各自必须被 B 段抓到
# ---------------------------------------------------------------------------

def c_ruler_has_teeth(mod):
    r"""**这一整段是尺子自检**，不是产品判据。

    把好的逻辑换成三种坏版本，B 段对应的那几条必须**判得出错**。
    抓不到 = 那条判据没牙，它的绿是假的（0✅0❌ 的那种绿）。
    """
    print("\nC. 尺子自检：坏版本必须被 B 段判成错")

    def cjk(s):
        return (len([c for c in s if '\u4e00' <= c <= '\u9fff']) / float(len(s))
                if s else 0.0)

    # 坏版本 ①：inline 也猜（2026-10-23 真正踩过的那个坑）
    def bad_inline(lines):
        out = []
        for ln in lines:
            a, b = ln.split(" | ")
            ra, rb = cjk(a), cjk(b)
            out.append((b, a) if ra > rb + 0.10 else (a, b))
        return out

    cn_pair = ["捍卫神族的荣誉 | 捍卫神族的荣誉zh", "风暴渐渐的距离 | 风暴zh"]
    bad1 = bad_inline(cn_pair)
    check("坏版本①（inline 到处猜）在中文歌上确实会翻",
          bad1[0][0] == "捍卫神族的荣誉zh", bad1[0])
    good1 = mod.parse_bilingual("\n".join(cn_pair), layout="inline")["pairs"]
    check("好版本在同份数据上给的是相反（正确）的结果 ⇒ 判据有牙",
          good1[0]["orig"] == "捍卫神族的荣誉"
          and good1[0]["orig"] != bad1[0][0])

    # 坏版本 ②：alt 不猜（两个取向写成一样的老毛病）
    def bad_alt(body):
        ea, eb = body[0::2], body[1::2]
        return list(zip(ea, eb))          # 永远"先来的是原文"

    zh_first = _interleaved(ZH, EN).split("\n")
    bad2 = bad_alt(zh_first)
    check("坏版本②（alt 不猜）在'中文先'上确实把中文放大字格",
          bad2[0][0] == ZH[0] and bad2[0][1] == EN[0], bad2[0])
    good2 = mod.parse_bilingual(_interleaved(ZH, EN), layout="alt")["pairs"]
    check("好版本 alt 与坏版本结论相反 ⇒ 判据有牙",
          (good2[0]["orig"], good2[0]["zh"]) == (EN[0], ZH[0])
          and good2[0]["orig"] != bad2[0][0])

    # 坏版本 ③：`-rev` 偷懒实现成"整份交换"（会把"只认出原文的行"翻成空句）
    def bad_flip(pairs):
        return [{"orig": p["zh"], "zh": p["orig"], "t": p.get("t")} for p in pairs]

    mixed = [{"orig": EN[0], "zh": ZH[0], "t": None}, {"orig": EN[1], "zh": "", "t": None}]
    bad3 = bad_flip(mixed)
    check("坏版本③（无脑翻转）会把'只认出原文的行'翻成空句",
          bad3[1]["orig"] == "" and bad3[1]["zh"] == EN[1], bad3[1])
    good3 = mod._flip(mixed)
    check("★ 好版本翻转时保护空半边 ⇒ 判据有牙",
          good3[1]["orig"] == EN[1] and good3[1]["zh"] == "", good3[1])


# ---------------------------------------------------------------------------
# D. 接线：后端认得 ≠ 接口放行 ≠ 界面看得见
# ---------------------------------------------------------------------------

def d_wiring():
    print("\nD. 接线：`-rev` 这条链路要一路通到用户眼前")
    st = _read(STUDIO)
    check("studio 调 parse_bilingual", "parse_bilingual(" in st)
    check("studio 有歌词导入入口", "def lyric_bi_import(" in st)
    # ★ 判的是"没有 strong 这一档"，不是"档位表长这样" ——
    #   写死整个元组的话，以后每加一档这里就假红一次（判据里的字面量是技术债）。
    _i = st.find("HF_LEVELS = (")
    _lv = st[_i:st.find(")", _i)] if _i >= 0 else ""
    check("高频补偿档位里已无 strong", '"strong"' not in _lv and '"mid"' in _lv,
          _lv[:70] or "找不到 HF_LEVELS")

    sv = _read(SERVER)
    # ★ 白名单不认 `-rev` 的话，用户选了就吃一个 400 —— 界面上看着像"没反应"
    check("★ 接口白名单放行 -rev（否则 400，看着像功能没接）",
          'endswith("-rev")' in sv, "server.py 里找不到 -rev 的处理")
    check("接口会把读到的排版回话说给用户", '"mode": parsed.get("mode")' in sv)

    h = _read(HTML)
    check("界面上有「中文在左」的入口（inline-rev）", 'value="inline-rev"' in h)
    check("界面上有「中文在上」的入口（alt-rev）", 'value="alt-rev"' in h)

    j = _read(APPJS)
    # ★ 后端的提示走的是 d.note；前端不显示它，用户永远不知道自己粘反了
    check("★ 前端会把服务端的提示显示出来（否则'粘反了'无人告知）",
          "d.note" in j and "hint.textContent +=" in j)


def main():
    print("门禁：双语歌词导入落位（大字原文 / 小字中文）")
    mod = load_mod()
    a_shape()
    b_behavior(mod)
    c_ruler_has_teeth(mod)
    d_wiring()
    print("\n通过 %d 项，失败 %d 项" % (N_PASS, len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  FAIL %s" % f)
    print("EXIT=%d" % (1 if FAILS else 0))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
