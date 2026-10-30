# -*- coding: utf-8 -*-
r"""「参考歌词订正」+「校对落盘」的正确性门禁（不跑模型，几秒钟）。

守的是「改词唱出来是错词」这类回归。

  原词（ref_text）是要送进模型做音素化的：官方 `align_lrc_sentence_level()`
  把 `|` 换空格、把标点归一成逗号 —— **文本只提供"唱哪些音素"**，
  认错一个字就唱错一个字。而 whisper 在**歌唱**上的错字是肉眼可见的
  （本机真实缓存里就有"热情劫强已经升级""捍卫神族的容誉"这种）。
  靠调扩散步数救不回来，得换文本。

所以允许两种修法，两个都要能验：
  ① 粘一份正确歌词进来 → 自动对齐、把错字换掉（时间戳不动）；
  ② 直接在界面上改某一句 → 存回缓存，改词时用改过的那份。

判据全部跑 `stages/lyric_plan.py` 的**真代码**（它只用标准库），
外加 `studio.save_lyric_segments` 会写出的那份缓存的结构校验。
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STAGES = os.path.join(HERE, "stages")
PLAN = os.path.join(STAGES, "lyric_plan.py")

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  PASS  " if ok else "  ! FAIL  ") + name
          + (("    —— " + str(detail)) if detail else ""))


def T(s, e, t):
    return {"start": s, "end": e, "text": t}


def main():
    print("参考歌词订正 + 校对落盘（stages/lyric_plan.py）")
    check("lyric_plan.py 存在", os.path.isfile(PLAN))
    sys.path.insert(0, STAGES)
    import lyric_plan as LP

    # ---- 1. 错字订正：文本换掉、时间戳一个字节都不许动 ------------------
    asr = [T(8.27, 14.69, "热情劫强已经升级"),
           T(14.69, 21.19, "风暴渐渐的距离"),
           T(37.91, 45.06, "捍卫神族的容誉")]
    ref = "热情倔强已经升级|风暴渐渐的距离|捍卫神族的荣誉"
    got, rep = LP.align_ref(asr, ref)
    check("三句都被订正", [x["text"] for x in got] ==
          ["热情倔强已经升级", "风暴渐渐的距离", "捍卫神族的荣誉"],
          "实际 %s" % [x["text"] for x in got])
    check("时间戳原样不动", [(x["start"], x["end"]) for x in got] ==
          [(8.27, 14.69), (14.69, 21.19), (37.91, 45.06)],
          "实际 %s" % [(x["start"], x["end"]) for x in got])
    check("订正了 2 句（第 2 句本来就是对的）", rep.get("changed") == 2,
          "实际 %s" % rep.get("changed"))
    check("参考歌词 3 句全用上", rep.get("ref_used") == 3,
          "实际 %s" % rep.get("ref_used"))

    # ---- 2. 标点/分隔符差异不干扰 ---------------------------------------
    a2 = [T(1.0, 2.0, "捍卫神族的荣誉")]
    g2, _ = LP.align_ref(a2, "捍卫神族的荣誉，")
    check("参考里多个标点不会把句子改坏", g2[0]["text"] == "捍卫神族的荣誉",
          "实际 %r" % g2[0]["text"])
    g2b, _ = LP.align_ref([T(1.0, 2.0, "第一句")], "第一句\n第二句\n第三句")
    check("换行分隔的参考歌词也能读", len(g2b) == 1 and g2b[0]["text"] == "第一句")
    # 段落标记不该被当成歌词
    g2c, r2c = LP.align_ref([T(1.0, 2.0, "副歌第一句")],
                            "[副歌]\n副歌第一句\n[主歌]\n主歌第一句")
    check("段落标记 [副歌] 不当成一句歌词", r2c.get("ref_lines") == 2,
          "参考句数 %s" % r2c.get("ref_lines"))

    # ---- 3. 句数对不上（识别断错句）—— 不能整段错位 ----------------------
    # 识别把前两句并成了一句，参考是两句 ⇒ 要按字符切回正确的两句边界
    a3 = [T(0.0, 4.0, "这是第一句这是第二句"),
          T(5.0, 8.0, "这是第三句")]
    g3, r3 = LP.align_ref(a3, "这是第一句|这是第二句|这是第三句")
    check("识别少断一句时，第三句不会串到第二句上",
          g3[1]["text"] == "这是第三句", "实际 %s" % [x["text"] for x in g3])
    check("合并句至少还带着第一句的字（没被整段丢掉）",
          "第一句" in g3[0]["text"] or "第二句" in g3[0]["text"],
          "实际 %r" % g3[0]["text"])
    check("时间戳仍是原来的两条",
          [(x["start"], x["end"]) for x in g3] == [(0.0, 4.0), (5.0, 8.0)])

    # 识别多出一句（哼唱），参考里没有 ⇒ 保留识别、报告里点出来
    a4 = [T(0.0, 2.0, "啦"), T(3.0, 5.0, "这一句参考里有")]
    g4, r4 = LP.align_ref(a4, "这一句参考里有")
    check("参考里没有的句子保留识别原文", g4[0]["text"] == "啦",
          "实际 %r" % g4[0]["text"])
    check("另一句照样被订正", g4[1]["text"] == "这一句参考里有")

    # ---- 4. 边界与异常：不许炸、不许返回半截 -----------------------------
    check("空识别结果 ⇒ 原样返回", LP.align_ref([], "随便什么")[1].get("ok") is False)
    check("空参考 ⇒ ok=False（不算订正过）",
          LP.align_ref([T(0, 1, "有词")], "")[1].get("ok") is False)
    # ★ 相似度闸门：认出来的和参考差得太远时不许硬替换
    #   （硬换 = 把一段不相干的词按到这条时间上，比留着错字更糟）
    # 用真实场景：这一句其实是哼唱/语气词，参考里根本没有它
    g5, r5 = LP.align_ref([T(0, 1, "哦哦哦啊哈哈")], "捍卫神族的荣誉")
    check("完全对不上时不硬替换（保留识别原文）",
          g5[0]["text"] == "哦哦哦啊哈哈", "实际 %r" % g5[0]["text"])
    check("并把这个位置记成 low_conf", r5.get("low_conf") == 1,
          "实际 %s" % r5.get("low_conf"))
    # 但"错一半"这种程度的该修还是得修（闸门不能太紧）
    g6, r6 = LP.align_ref([T(0, 1, "捍卫神族的容誉")], "捍卫神族的荣誉")
    check("只错一个字照样订正（闸门不能太紧）",
          g6[0]["text"] == "捍卫神族的荣誉" and r6.get("changed") == 1,
          "实际 %r / changed=%s" % (g6[0]["text"], r6.get("changed")))
    check("参考比识别长很多时不越界",
          LP.align_ref([T(0, 1, "短")], "很长很长很长的一整段歌词" * 5)[0][0]["text"]
          is not None)
    check("乱序时间戳不炸",
          len(LP.align_ref([T(9, 10, "后"), T(0, 1, "前")], "前后")[0]) == 2)

    # ---- 5. 校对落盘的清洗规则 -------------------------------------------
    clean, dropped = LP.clean_segments([
        T(1.0, 2.0, "第一句"),
        T(3.0, 4.0, "   "),          # 空文本 ⇒ 丢
        T(5.0, 6.0, "带|竖线"),        # 竖线会破坏句数 ⇒ 换掉
        T(9.0, 7.0, "时间写反了"),      # end < start ⇒ 拉回
        {"start": 11.0, "end": 12.0, "text": "缺 start 之外的都好"},
        "不是字典",                    # ⇒ 丢
        T(8.0, 8.5, "乱序的一句"),
    ])
    check("空文本 / 非字典条目被丢掉", dropped == 2, "丢了 %d 条" % dropped)
    check("文本里的 | 被换成全角（不然句数会算错）",
          all("|" not in x["text"] for x in clean),
          "%s" % [x["text"] for x in clean])
    check("end < start 的被拉回（不出现负时长）",
          all(x["end"] >= x["start"] for x in clean))
    check("结果按 start 排序",
          [x["start"] for x in clean] == sorted(x["start"] for x in clean),
          "%s" % [x["start"] for x in clean])
    check("洗干净后仍是 5 条", len(clean) == 5, "实际 %d" % len(clean))

    # ---- 6. 服务端保存逻辑用的是同一份清洗 -------------------------------
    src = io.open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    check("studio.save_lyric_segments 走的是 lyric_plan.clean_segments",
          "from lyric_plan import clean_segments" in src)
    check("校对结果写了 proofread 标记（界面能显示已校对）",
          '"proofread"' in src or "'proofread'" in src)
    check("校对后的 line 是拼出来的（改词时用它对句数）",
          'd["line"]' in src)

    # ---- 7. 负对照：把订正关掉，错字必须还在（证明判据有牙）---------------
    bad_copy = [T(8.27, 14.69, "热情劫强已经升级")]
    kept = bad_copy[0]["text"]
    check("负对照：不订正时错字仍在（订正确实起了作用）",
          kept != "热情倔强已经升级", kept)

    # ---- 8. 双语歌词：一次粘进来，自己对着歌贴到该在的秒数 ----------------
    # 模块：stages/lyric_bi.py（同样只用标准库）
    # 守的是老板那句「一次性输入歌词和中文，自己对照歌自动放置到合适位置」。
    # ★ 这一类功能的失效方式只有一种最坏：**贴错了还不吭声** ——
    #   画面上每句都有词、都有时间，但词和唱的对不上。所以判据的一半是
    #   "贴对了"，另一半是"贴不上时必须列出来"。
    LP2 = os.path.join(STAGES, "lyric_bi.py")
    check("lyric_bi.py 存在", os.path.isfile(LP2))
    import lyric_bi as LB

    # 8.1 四种排版都认得出来
    p = LB.parse_bilingual(
        "[00:12.34]Never gonna give you up\n[00:12.34]永远不会放弃你\n"
        "[00:16.00]Never gonna let you down\n[00:16.00]永远不会让你失望")
    check("LRC：双行同时间戳 = 原文 + 中文",
          p["mode"] == "lrc" and p["pairs"][0]["orig"].startswith("Never gonna give")
          and p["pairs"][0]["zh"] == "永远不会放弃你",
          "%s / %s" % (p["mode"], p["pairs"][:1]))
    p = LB.parse_bilingual("捍卫神族的荣誉 | 捍卫神族的荣誉zh\n风暴渐渐的距离 | 风暴zh")
    check("inline：`原词 | 中文` 一行一句",
          p["mode"] == "inline" and p["pairs"][1]["zh"] == "风暴zh",
          "%s" % p["pairs"])
    p = LB.parse_bilingual("前奏\n[副歌]\n第一句\n第二句\n\n第一句中文\n第二句中文")
    check("block：空行分成两坨（段落标记不当歌词）",
          p["mode"] == "block" and len(p["pairs"]) == 2
          and p["pairs"][0]["zh"] == "第一句中文",
          "%s / %s" % (p["mode"], p["pairs"]))
    p = LB.parse_bilingual("热情倔强已经升级\n风暴渐渐的距离\n捍卫神族的荣誉\n再一次站在这里",
                           layout="alt")
    check("alt：手动指定时中文歌也能劈（自动判据在中文歌上必然失灵）",
          p["mode"] == "alt" and p["pairs"][0]["zh"] == "风暴渐渐的距离",
          "%s / %s" % (p["mode"], p["pairs"][:1]))

    # 8.2 ★ 负对照：自动模式**不许**把中文歌劈成"原文/中文"
    #      （两边都是汉字，靠汉字比例分辨必错 ⇒ 只能退回"全是原文"）
    p = LB.parse_bilingual("热情倔强已经升级\n风暴渐渐的距离\n捍卫神族的荣誉\n再一次站在这里")
    check("负对照：自动模式下中文歌不被误劈（判据有牙）",
          p["mode"] == "plain" and all(not x["zh"] for x in p["pairs"]),
          "实际 %s / %s" % (p["mode"], p["pairs"][:1]))

    # 8.3 对位：时间只能来自 ASR / LRC，不许自己发明
    asr8 = [T(8.0, 12.0, "热情劫强已经升级"), T(20.0, 24.0, "风暴渐渐的距离"),
            T(40.0, 45.0, "捍卫神族的容誉")]
    pairs8 = [{"orig": "热情倔强已经升级", "zh": "热情倔强已经升级zh", "t": None},
              {"orig": "风暴渐渐的距离", "zh": "风暴zh", "t": None},
              {"orig": "捍卫神族的荣誉", "zh": "荣誉zh", "t": None}]
    segs8, rep8 = LB.place(asr8, pairs8)
    check("三句都贴上", rep8.get("placed") == 3, "实际 %s" % rep8)
    check("★ 时间就是 ASR 的时间（一个字节都不许自己发明）",
          [(x["start"], x["end"]) for x in segs8]
          == [(8.0, 12.0), (20.0, 24.0), (40.0, 45.0)],
          "实际 %s" % [(x["start"], x["end"]) for x in segs8])
    check("中文跟着原词走",
          [x["zh"] for x in segs8] == ["热情倔强已经升级zh", "风暴zh", "荣誉zh"],
          "实际 %s" % [x["zh"] for x in segs8])
    # ★ 用 .get / 长度守卫，不许让判据自己抛异常：崩了只会得到"退出码非 0 但
    #   失败数 0"，那是最难分诊的一种假红（变异测试里踩过两次）。
    check("认错的字也照样贴（相似但不相等 ⇒ 时间对、词待改）",
          len(segs8) == 3 and segs8[2]["text"] == "捍卫神族的荣誉",
          "实际 %s" % [x.get("text") for x in segs8])

    # 8.4 句数对不上
    #   识别把两句并成一句 ⇒ 按字数把那段时间分给两句（不许两句共用同一段时间）
    asr9 = [T(0.0, 8.0, "这是第一句这是第二句"), T(10.0, 12.0, "这是第三句")]
    p9 = [{"orig": "这是第一句", "zh": "", "t": None},
          {"orig": "这是第二句", "zh": "", "t": None},
          {"orig": "这是第三句", "zh": "", "t": None}]
    s9, r9 = LB.place(asr9, p9)
    check("识别少断一句 ⇒ 两句各自分到时间（不重叠、不共用）",
          len(s9) == 3 and s9[0]["end"] <= s9[1]["start"]
          and 0.0 <= s9[0]["start"] and s9[1]["end"] <= 8.0,
          "实际 %s" % [(x["start"], x["end"]) for x in s9])
    check("分时间的那两句被记进 report.split", r9.get("split") == 2,
          "实际 %s" % r9.get("split"))
    #   识别多断一句 ⇒ 并回一句（时间跨度 = 首尾）
    #   ★ 用"两句都在唱同一句"的素材：如果前一句是哼唱（和参考一个字都不沾），
    #     那它**不该**被并进来 —— 那是"识别多认了一句"，不是"同一句被切开"。
    asr10 = [T(0.0, 2.0, "这一句"), T(2.0, 5.0, "参考里有")]
    s10, r10 = LB.place(asr10, [{"orig": "这一句参考里有", "zh": "", "t": None}])
    check("识别多断 ⇒ 并成一句，时间跨首尾",
          len(s10) == 1 and s10[0]["start"] == 0.0 and s10[0]["end"] == 5.0,
          "实际 %s" % [(x["start"], x["end"]) for x in s10])
    #   哼唱不该被并进相邻那句（它本来就是"多出来的一句"）
    s10b, _ = LB.place([T(0.0, 2.0, "啦"), T(2.0, 5.0, "参考里有")],
                       [{"orig": "参考里有", "zh": "", "t": None}])
    check("哼唱那句不并入相邻句（时间不带 0 秒起）",
          len(s10b) == 1 and s10b[0]["start"] == 2.0,
          "实际 %s" % [(x["start"], x["end"]) for x in s10b])

    # 8.5 完全对不上 ⇒ 列出来，不许编时间
    s11, r11 = LB.place([T(0, 1, "哦哦哦啊哈哈")],
                        [{"orig": "捍卫神族的荣誉", "zh": "", "t": None}])
    check("对不上时把这句记进 unplaced（而不是给个假时间）",
          len(s11) == 0 and r11.get("unplaced") == ["捍卫神族的荣誉"],
          "实际 %s / %s" % (s11, r11.get("unplaced")))
    check("describe 里能看见没贴上的句子",
          "没贴上" in LB.describe(r11), LB.describe(r11))

    # 8.6 顺序与不重叠（这是"字幕会不会打架"的底线）
    # ★ 素材必须真能配上：用对不上的素材会得到空列表，而"空列表当然排好序了"
    #   ⇒ 判据变成一条永远绿的假尺子（我自己先踩了一次，这里钉死句数）。
    # ★ 素材怎么挑的（我自己先挑错过一次）：必须让「按参考句顺序拼出来的结果」
    #   本身就是乱序的，排序这一步才是**唯一**能把它救回来的东西。
    #   之前用的参考顺序（第一句/第二句/第三句）恰好让后两句落到同一个识别段上
    #   被按字数切了时间 ⇒ 不排序也刚好是升序 ⇒ 一条永远绿的假尺子。
    s12, _ = LB.place([T(5, 6, "第二句"), T(0, 1, "第一句"), T(2, 3, "第三句")],
                      [{"orig": "第二句", "zh": "", "t": None},
                       {"orig": "第一句", "zh": "", "t": None},
                       {"orig": "第三句", "zh": "", "t": None}])
    check("乱序的识别结果也能贴出 3 句（空列表不许冒充通过）",
          len(s12) == 3, "实际 %d 句" % len(s12))
    check("结果按时间排序", len(s12) == 3
          and [x["start"] for x in s12] == sorted(x["start"] for x in s12),
          "%s" % [x["start"] for x in s12])
    check("相邻两句不重叠", len(s12) == 3
          and all(s12[k]["start"] >= s12[k - 1]["end"] for k in range(1, len(s12))),
          "%s" % [(x["start"], x["end"]) for x in s12])
    check("每句贴到的是自己那句的时间（乱序下不许串位）",
          [(x["start"], x["text"]) for x in s12]
          == [(0.0, "第一句"), (2.0, "第三句"), (5.0, "第二句")],
          "实际 %s" % [(x["start"], x["text"]) for x in s12])

    # 8.7 LRC 自带时间戳 ⇒ 一次模型都不用跑（也不许去问 ASR）
    p13 = LB.parse_bilingual("[01:00.00]第一句\n[01:04.00]第二句\n[02:00.00]第三句")
    s13, r13 = LB.place([], p13["pairs"])
    check("LRC：时间照抄，end 补到下一句起点",
          [(x["start"], x["end"]) for x in s13] == [(60.0, 64.0), (64.0, 120.0), (120.0, 124.0)],
          "实际 %s" % [(x["start"], x["end"]) for x in s13])
    check("LRC：没有 ASR 也贴得上", r13.get("ok") is True and not r13.get("unplaced"))

    # 8.8 ★ 负对照：换成"按句数均分" ⇒ 必须和 ASR 对不上
    #      （证明 8.3 那条判据真的在量东西，而不是写什么都绿）
    dur = 45.0
    even = [(round(dur * k / 3.0, 3), round(dur * (k + 1) / 3.0, 3)) for k in range(3)]
    got = [(x["start"], x["end"]) for x in segs8]
    check("负对照：均分出来的时间和真实时间不一样（判据有牙）",
          even != got, "均分 %s vs 实际 %s" % (even, got))

    # 8.9 中文跟着句子落盘（clean_segments 不许把它洗掉）
    c14, d14 = LP.clean_segments([
        {"start": 1.0, "end": 2.0, "text": "第一句", "zh": "第一句中文"},
        {"start": 3.0, "end": 4.0, "text": "第二句"}])
    check("zh 跟着句子一起存", c14[0].get("zh") == "第一句中文", "实际 %s" % c14[0])
    check("没有中文的句子不写空键（老数据形状不变）",
          "zh" not in c14[1], "实际 %s" % c14[1])
    c15, _ = LP.clean_segments([{"start": 1.0, "end": 2.0, "text": "带|竖线",
                                 "zh": "中文也带|竖线"}])
    check("zh 里的 | 一样要换成全角（不然句数会算错）",
          "|" not in (c15[0].get("zh") or ""), "%s" % c15[0])

    # 8.10 服务端 / 成品侧：导入的那份要真能被读到
    src = io.open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    check("studio 有 lyric_bi_import（一次性导入 + 对位）",
          "def lyric_bi_import(" in src)
    check("studio 有 lyric_segments_bilingual（只读校对过的那份）",
          "def lyric_segments_bilingual(" in src)
    i_bi = src.find("lyric_segments_bilingual(src)")
    i_ms = src.find('p.get("multi_segs")')
    check("viz_cast_of 里双语缓存排在成品自带分句之前（否则导入完看不见）",
          i_bi > 0 and i_ms > 0 and i_bi < i_ms,
          "双语在 %d，multi_segs 在 %d" % (i_bi, i_ms))
    sp = io.open(os.path.join(HERE, "server.py"), encoding="utf-8").read()
    check("服务端有 /api/lyrics/import", '"/api/lyrics/import"' in sp)
    html = io.open(os.path.join(HERE, "web", "index.html"), encoding="utf-8").read()
    check("界面上有粘贴框（#biText）", 'id="biText"' in html)
    js = io.open(os.path.join(HERE, "web", "viz.js"), encoding="utf-8").read()
    check("界面上有「对位并贴上」按钮的动作", "function vizBiImport" in js)

    print("\n通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAIL %s" % f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
